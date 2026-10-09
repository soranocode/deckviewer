"""Decode deck codes, resolve cards from a local library, and render PNG.

The compact library updates from HSJSON and Blizzard's public card gallery.
Trackers can inject their existing metadata, images, or an explicit provider.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from hearthstone.deckstrings import Deck
from PIL import Image

from renderer import render_deck
from local_library import LocalLibrary, LocalLibraryError

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
GENERATED_DIR = BASE_DIR / "generated"
LIBRARY_DIR = BASE_DIR / "library"
IMAGE_CACHE_LIMIT = 100 * 1024 * 1024
LOCALES = {"ruRU", "enUS"}
EXAMPLE_CODE = "AAEBAR8GhwS7BfixAqHCAtnCAubCAgyoArUD6wftCf4Mzq4CubQC6rsC7LsCicMCjsMCk8MCAAA="
CLASS_INFO = {
    # Background filenames use Blizzard API class IDs, not deckstring enums.
    "DEATHKNIGHT": (1, "Рыцарь смерти", "Death Knight"),
    "DEMONHUNTER": (14, "Охотник на демонов", "Demon Hunter"),
    "DRUID": (2, "Друид", "Druid"),
    "HUNTER": (3, "Охотник", "Hunter"),
    "MAGE": (4, "Маг", "Mage"),
    "PALADIN": (5, "Паладин", "Paladin"),
    "PRIEST": (6, "Жрец", "Priest"),
    "ROGUE": (7, "Разбойник", "Rogue"),
    "SHAMAN": (8, "Шаман", "Shaman"),
    "WARLOCK": (9, "Чернокнижник", "Warlock"),
    "WARRIOR": (10, "Воин", "Warrior"),
}
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_METADATA_MEMORY: dict[Path, tuple[int, dict[int, dict]]] = {}
_ID_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")
_BLIZZARD_PROVIDERS = {}
_LOCAL_LIBRARIES = {}
_IMAGE_JOBS = 0
# Exact game identities keep configured Zilliax working with either API schema.
_ZILLIAX_FUNCTIONAL_IDS = frozenset(range(104944, 104952))
_ZILLIAX_COSMETIC_IDS = frozenset(range(110440, 110447)) | {112530}


class DeckViewError(ValueError):
    """A safe, actionable message intended for the local app's user."""


def _lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback:
        callback(message)


def _decode_candidate(code: str) -> tuple[str, Deck]:
    # The library is the format authority. Strict base64 and small limits keep
    # invalid pastes from being silently accepted or consuming unbounded work.
    code = code.rstrip("=")
    if len(code) > 8192 or not re.fullmatch(r"[A-Za-z0-9+/]+", code):
        raise ValueError("Invalid base64")
    code += "=" * (-len(code) % 4)
    raw = base64.b64decode(code, validate=True)
    deck = Deck.from_deckstring(code)
    if len(deck.heroes) != 1 or not deck.cards:
        raise ValueError("One hero and at least one card are required")
    if len(deck.cards) > 100 or len(deck.sideboards) > 100:
        raise ValueError("Too many entries")
    ids = [card_id for card_id, _ in deck.cards]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate card entries")
    if any(not 0 < card_id < 10_000_000 or not 0 < count <= 100
           for card_id, count in deck.cards):
        raise ValueError("Invalid card id or count")
    if not 1 <= sum(count for _, count in deck.cards) <= 100:
        raise ValueError("Invalid deck size")
    if not 0 <= sum(count for _, count, _ in deck.sideboards) <= 100:
        raise ValueError("Invalid sideboard size")
    keys = [(owner, card_id) for card_id, _, owner in deck.sideboards]
    if len(set(keys)) != len(keys) or any(
        owner not in ids or not 0 < card_id < 10_000_000 or not 0 < count <= 100
        for card_id, count, owner in deck.sideboards
    ):
        raise ValueError("Invalid sideboard")
    # Hearthstone library accepts old codes without the final zero. Reject
    # unexplained trailing data, while keeping those historical codes valid.
    canonical = base64.b64decode(deck.as_deckstring)
    if len(raw) != len(canonical) and not (
        not deck.sideboards and len(raw) == len(canonical) - 1
    ):
        raise ValueError("Unexpected data in deck code")
    return code, deck


def decode_deck(text: str) -> tuple[str, Deck]:
    """Accept a bare code or the complete text exported by Hearthstone."""
    if not isinstance(text, str) or not text.strip():
        raise DeckViewError("Вставь код колоды или полный текст экспорта из Hearthstone.")
    if len(text) > 32_768:
        raise DeckViewError("Текст слишком длинный. Вставь один код колоды.")
    candidates = re.findall(r"(?<![A-Za-z0-9+/])AA[A-Za-z0-9+/]{8,}={0,2}", text)
    # Handles a code broken across lines when copied by a text editor.
    compact = "".join(text.split())
    if compact.startswith("AA") and re.fullmatch(r"[A-Za-z0-9+/]+=*", compact):
        candidates.append(compact)
    decoded = {}
    for candidate in dict.fromkeys(candidates):
        try:
            code, deck = _decode_candidate(candidate)
            decoded[deck.as_deckstring] = (code, deck)
        except (ValueError, TypeError, OverflowError, EOFError):
            continue
    if len(decoded) > 1:
        raise DeckViewError("Найдено несколько колод. Оставь код одной колоды.")
    if not decoded:
        raise DeckViewError("Не удалось прочитать код колоды. Скопируй его целиком из Hearthstone.")
    return next(iter(decoded.values()))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _download(url: str, validator: Callable[[bytes], object], attempts: int = 3) -> bytes:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=(8, 30), headers={"User-Agent": "DeckViewLocal/1.0"})
            response.raise_for_status()
            data = response.content
            if not data or len(data) > 40 * 1024 * 1024:
                raise ValueError("Empty or oversized response")
            validator(data)
            return data
        except (requests.RequestException, ValueError, OSError) as error:
            last_error = error
            if isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code == 404:
                break
            if attempt + 1 < attempts:
                time.sleep(0.4 * (2 ** attempt))
    raise DeckViewError("Не удалось скачать данные карт. Проверь подключение к интернету и попробуй снова.") from last_error


def _metadata_index(content: bytes) -> dict[int, dict]:
    data = json.loads(content)
    if not isinstance(data, list) or not data:
        raise ValueError("Invalid card database")
    index = {}
    for card in data:
        if not isinstance(card, dict):
            raise ValueError("Invalid card record")
        dbf_id, card_id = card.get("dbfId"), card.get("id")
        if isinstance(dbf_id, int) and dbf_id > 0 and isinstance(card_id, str) and _ID_PATTERN.fullmatch(card_id):
            index[dbf_id] = card
    if not index:
        raise ValueError("Empty card index")
    return index


def load_metadata(locale: str, *, refresh: bool = False,
                  progress: Callable[[str], None] | None = None) -> dict[int, dict]:
    try:
        return _local_library().sync(locale, refresh=refresh, progress=progress)
    except LocalLibraryError as error:
        raise DeckViewError(str(error)) from error


def _local_library():
    key = (CACHE_DIR.resolve(), LIBRARY_DIR.resolve())
    with _LOCKS_GUARD:
        if key not in _LOCAL_LIBRARIES:
            _LOCAL_LIBRARIES[key] = LocalLibrary(CACHE_DIR / "library", LIBRARY_DIR)
        return _LOCAL_LIBRARIES[key]


def _matches_revision(path: Path, revision: str | None) -> bool:
    if not _image_is_valid(path):
        return False
    if revision:
        try:
            return path.with_suffix(".revision").read_text() == revision
        except OSError:
            return False
    return True


def _mark_image(path: Path, revision: str | None):
    if revision:
        _atomic_write(path.with_suffix(".revision"), revision.encode("ascii"))
    try:
        os.utime(path, None)
    except OSError:
        pass


def _trim_image_cache():
    root = (CACHE_DIR / "images").resolve()
    entries = []
    for path in root.rglob("*.png"):
        # Only prune generated cache images within this app's image directory.
        if not path.resolve().is_relative_to(root):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        entries.append((info.st_mtime_ns, info.st_size, path))
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= IMAGE_CACHE_LIMIT:
            break
        try:
            path.unlink(missing_ok=True)
            path.with_suffix(".revision").unlink(missing_ok=True)
            path.with_suffix(".english-fallback").unlink(missing_ok=True)
            total -= size
        except OSError:
            pass


@contextmanager
def _image_cache_session():
    global _IMAGE_JOBS
    with _LOCKS_GUARD:
        _IMAGE_JOBS += 1
    try:
        yield
    finally:
        # Keep every image until all concurrent renders have finished reading.
        with _LOCKS_GUARD:
            _IMAGE_JOBS -= 1
            if _IMAGE_JOBS == 0:
                try:
                    _trim_image_cache()
                except OSError:
                    pass  # Cache maintenance cannot discard a completed PNG.


def _validate_image(content: bytes) -> None:
    with Image.open(BytesIO(content)) as image:
        if image.format != "PNG" or not 100 <= image.width <= 2048 or not 100 <= image.height <= 3072:
            raise ValueError("Invalid card render")
        image.verify()


def _image_is_valid(path: Path) -> bool:
    try:
        _validate_image(path.read_bytes())
        return True
    except (ValueError, OSError):
        return False


def _cache_image(card: dict, locale: str) -> tuple[Path, bool]:
    card_id = card["id"]
    revision = card.get("imageRevision")
    if not _ID_PATTERN.fullmatch(card_id):
        raise DeckViewError("В базе карт найден неверный идентификатор.")
    image_url = card.get("imageUrl")
    if image_url:
        if not isinstance(image_url, str):
            raise DeckViewError("Некорректный адрес изображения карты.")
        parsed = urlparse(image_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise DeckViewError("Blizzard вернул некорректный адрес изображения карты.")
        # Blizzard changes the image URL when artwork or card stats change.
        # Keep caches of different versions separate instead of serving old art.
        version = hashlib.sha256(image_url.encode()).hexdigest()[:12]
        path = CACHE_DIR / "images" / locale / f"{card_id}-{version}.png"
        with _lock(path):
            if not _image_is_valid(path):
                try:
                    _atomic_write(path, _download(image_url, _validate_image))
                except DeckViewError as error:
                    raise DeckViewError(f"Не удалось загрузить изображение Blizzard «{card.get('name', card_id)}».") from error
            _mark_image(path, None)
            return path, False
    if "blizzard" in card:
        raise DeckViewError(f"Blizzard не вернул изображение карты «{card.get('name', card_id)}».")
    path = CACHE_DIR / "images" / locale / f"{card_id}.png"
    with _lock(path):
        if _matches_revision(path, revision):
            _mark_image(path, revision)
            return path, False
        # Only reuse a known localized fallback. An English image cached by
        # an English request must never preempt downloading the Russian card.
        english = CACHE_DIR / "images" / "enUS" / f"{card_id}.png"
        fallback_marker = path.with_suffix(".english-fallback")
        if locale != "enUS" and fallback_marker.is_file() and _matches_revision(english, revision):
            _mark_image(english, revision)
            return english, True
        url = f"https://art.hearthstonejson.com/v1/render/latest/{locale}/512x/{card_id}.png"
        fallback = False
        try:
            content = _download(url, _validate_image)
        except DeckViewError as error:
            if locale == "enUS":
                raise DeckViewError(f"Не удалось загрузить изображение «{card.get('name', card_id)}». Попробуй снова.") from error
            if _matches_revision(english, revision):
                _atomic_write(fallback_marker, b"English image used for this locale")
                _mark_image(english, revision)
                return english, True
            try:
                content = _download(url.replace(f"/{locale}/", "/enUS/"), _validate_image)
                _atomic_write(english, content)
                _mark_image(english, revision)
                _atomic_write(fallback_marker, b"English image used for this locale")
                return english, True
            except DeckViewError as fallback_error:
                raise DeckViewError(f"Не удалось загрузить изображение «{card.get('name', card_id)}». Попробуй снова.") from fallback_error
        _atomic_write(path, content)
        _mark_image(path, revision)
        fallback_marker.unlink(missing_ok=True)
        return path, fallback


def _dust(card: dict) -> int:
    # Noncollectible modules are included in the cost of Zilliax itself. Core
    # cards are supplied free even when they have a normal rarity marker.
    if not card.get("collectible") or card.get("set") in {"CORE", "CORE_HIDDEN"}:
        return 0
    return {"COMMON": 40, "RARE": 100, "EPIC": 400, "LEGENDARY": 1600}.get(card.get("rarity"), 0)


def prepare_deck(deck: Deck, metadata: dict[int, dict]) -> dict:
    wanted = set(deck.heroes) | {card_id for card_id, _ in deck.cards} | {card_id for card_id, _, _ in deck.sideboards}
    missing = sorted(wanted - metadata.keys())
    if missing:
        raise DeckViewError("Не удалось получить данные карт с ID: " + ", ".join(map(str, missing[:8])) + ". Проверь подключение к интернету и код колоды.")
    hero = metadata[deck.heroes[0]]
    class_key = hero.get("cardClass")
    if class_key not in CLASS_INFO or hero.get("type") != "HERO":
        raise DeckViewError("Не удалось определить класс колоды по её герою.")
    groups = defaultdict(list)
    for card_id, count, owner in deck.sideboards:
        card = dict(metadata[card_id], count=count, sideboard=True)
        card["cosmetic"] = owner == 102983 and (
            bool(card.get("hideCost")) or card_id in _ZILLIAX_COSMETIC_IDS)
        groups[owner].append(card)
    cards, runes, dust = [], {"blood": 0, "frost": 0, "unholy": 0}, 0
    for card_id, count in deck.cards:
        card = dict(metadata[card_id], count=count, sideboard=False)
        sideboard = groups.get(card_id, [])
        card["sideboardCards"] = [item for item in sideboard if not item["cosmetic"]]
        if card_id == 102983:
            functional = sorted(item["dbfId"] for item in sideboard
                                if item["dbfId"] in _ZILLIAX_FUNCTIONAL_IDS
                                or item.get("isZilliaxFunctionalModule")
                                or re.fullmatch(r"TOY_330t9[2-9]", item["id"]))
            if functional:
                card["cost"] = sum(metadata[module].get("cost", 0) for module in functional)
                card["zilliax"] = "-".join(map(str, functional))
                card["attack"] = sum(metadata[module].get("attack", 0) for module in functional)
                card["health"] = sum(metadata[module].get("health", 0) for module in functional)
        cards.append(card)
        dust += count * _dust(card)
        for rune, value in card.get("runeCost", {}).items():
            if rune in runes:
                runes[rune] = max(runes[rune], int(value))
    # Sideboard copies count separately; identical cards under separate owners
    # must keep their own labels and grouping in the visual output.
    sidecards = [item for items in groups.values() for item in items if not item["cosmetic"]]
    dust += sum(item["count"] * _dust(item) for item in sidecards)
    return {"cards": cards, "sideboard": sidecards, "runeSlots": runes,
            "classKey": class_key, "classId": CLASS_INFO[class_key][0],
            "dust": dust, "cardCount": sum(count for _, count in deck.cards),
            "sideboardCount": sum(item["count"] for item in sidecards)}


def _configured_provider():
    from blizzard_provider import BlizzardProvider, BlizzardProviderError
    settings = {}
    settings_path = BASE_DIR / "blizzard.local.json"
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError
        except (OSError, ValueError):
            raise DeckViewError("Не удалось прочитать настройки Blizzard. Повтори настройку через ConfigureBlizzard.cmd.")
    client_id = os.environ.get("BLIZZARD_CLIENT_ID") or os.environ.get("CLIENT_ID")
    client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET") or os.environ.get("CLIENT_SECRET")
    client_id = client_id or settings.get("clientId")
    client_secret = client_secret or settings.get("clientSecret")
    region = os.environ.get("BLIZZARD_REGION") or settings.get("region", "eu")
    if any(value is not None and not isinstance(value, str)
           for value in (client_id, client_secret, region)):
        raise DeckViewError("Неверные настройки Blizzard. Повтори настройку через ConfigureBlizzard.cmd.")
    if not client_id and not client_secret:
        return None
    key = (client_id, client_secret, region)
    with _LOCKS_GUARD:
        if key not in _BLIZZARD_PROVIDERS:
            try:
                _BLIZZARD_PROVIDERS[key] = BlizzardProvider(client_id, client_secret, region=region)
            except BlizzardProviderError as error:
                raise DeckViewError(str(error)) from error
        return _BLIZZARD_PROVIDERS[key]


def resolve_deck(deck_code: str, locale: str = "ruRU", *, metadata=None,
                 provider=None, progress=None) -> dict:
    """Code -> DBF IDs/counts -> card records; no UI or rendering required.

    A tracker can supply its own metadata index or provider with get_cards().
    The standalone app always uses the local library; keys are optional only for
    a provider which the caller explicitly supplies.
    """
    if locale not in LOCALES:
        raise DeckViewError("Выбери русский или английский язык карт.")
    code, deck = decode_deck(deck_code)
    wanted = set(deck.heroes) | {cid for cid, _ in deck.cards} | {cid for cid, _, _ in deck.sideboards}
    source = "tracker" if metadata is not None else "local"
    if metadata is None:
        if provider is not None:
            from blizzard_provider import BlizzardProviderError
            _progress(progress, "Получаю данные карт из Blizzard API…")
            try:
                metadata = provider.get_cards(wanted, locale)
            except BlizzardProviderError as error:
                raise DeckViewError(str(error)) from error
            source = "blizzard"
            # Some cosmetic hero IDs are not exposed by the public card API.
            # Reuse an already present hero definition without refreshing HSJSON.
            missing_heroes = set(deck.heroes) - metadata.keys()
            saved = CACHE_DIR / f"cards.{locale}.json"
            if missing_heroes and saved.is_file():
                try:
                    cached = _metadata_index(saved.read_bytes())
                except (OSError, ValueError):
                    cached = {}
                metadata = dict(metadata)
                metadata.update({cid: cached[cid] for cid in missing_heroes if cid in cached})
        else:
            try:
                metadata = _local_library().get_cards(wanted, locale, progress=progress)
            except LocalLibraryError as error:
                raise DeckViewError(str(error)) from error
    response = prepare_deck(deck, metadata)
    response.update(deckCode=code, heroId=deck.heroes[0], format=int(deck.format), source=source)
    return response


def generate(deck_code: str, locale: str = "ruRU",
             progress: Callable[[str], None] | None = None, *, metadata=None,
             provider=None, image_paths=None, output_dir=None) -> dict:
    with _image_cache_session():
        return _generate(deck_code, locale, progress, metadata=metadata,
            provider=provider, image_paths=image_paths, output_dir=output_dir)


def _generate(deck_code: str, locale: str = "ruRU",
             progress: Callable[[str], None] | None = None, *, metadata=None,
             provider=None, image_paths=None, output_dir=None) -> dict:
    """Generate a 3000px PNG; return its local path and displayable statistics."""
    if locale not in LOCALES:
        raise DeckViewError("Выбери русский или английский язык карт.")
    _progress(progress, "Читаю код колоды…")
    response = resolve_deck(deck_code, locale, metadata=metadata, provider=provider, progress=progress)
    code = response["deckCode"]
    cards = {card["id"]: card for card in response["cards"] + response["sideboard"]}
    images, fallback_cards = {}, []
    _progress(progress, f"Подготавливаю изображения: 0 из {len(cards)}…")
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="card-image") as executor:
        futures = {}
        for card in cards.values():
            if image_paths is not None and card["id"] in image_paths:
                local_image = Path(image_paths[card["id"]])
                if not _image_is_valid(local_image):
                    raise DeckViewError(f"Некорректное изображение из трекера: {card.get('name', card['id'])}.")
                images[card["id"]] = local_image
            else:
                futures[executor.submit(_cache_image, card, locale)] = card
        for future in as_completed(futures):
            card = futures[future]
            path, fallback = future.result()
            images[card["id"]] = path
            if fallback:
                fallback_cards.append(card.get("name", card["id"]))
            _progress(progress, f"Подготавливаю изображения: {len(images)} из {len(cards)}…")
    _progress(progress, "Собираю изображение колоды…")
    picture = render_deck(response, images)
    _, canonical_deck = decode_deck(code)
    digest = hashlib.sha256((canonical_deck.as_deckstring + locale).encode()).hexdigest()[:16]
    filename = f"deck-{locale}-{digest}.png"
    target = (Path(output_dir) if output_dir is not None else GENERATED_DIR) / filename
    with BytesIO() as output:
        picture.save(output, format="PNG")
        _atomic_write(target, output.getvalue())
    warnings = []
    if fallback_cards:
        warnings.append("Для некоторых карт доступно только английское изображение: " + ", ".join(fallback_cards))
    _progress(progress, "Готово.")
    return {"path": str(target), "filename": filename, "className": CLASS_INFO[response["classKey"]][1 if locale == "ruRU" else 2],
            "cardCount": response["cardCount"], "sideboardCount": response["sideboardCount"],
            "dust": response["dust"], "locale": locale, "deckCode": code,
            "width": picture.width, "height": picture.height, "warnings": warnings,
            "source": response["source"]}
