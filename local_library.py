"""Compact local card catalog with automatic, credential-free updates.

The full HSJSON snapshot and exact supplemental records from Blizzard's public
card gallery are saved together as gzip JSON. No SQL database or API key is
needed. Missing cards are queried by their exact DBF IDs, never by guessed names.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

import requests

from blizzard_provider import BlizzardProviderError, normalize_card
from card_library import CardLibraryError, index_json

LOCALES = {"ruRU": ("ru-ru", "ru_RU"), "enUS": ("en-us", "en_US")}
CHECK_INTERVAL = 86400
RETRY_INTERVAL = 600
MAX_JSON_BYTES = 40 * 1024 * 1024


class LocalLibraryError(ValueError):
    """A message suitable for the standalone app."""


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def pack_cards(cards: dict[int, dict]) -> bytes:
    raw = json.dumps(list(cards.values()), ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return gzip.compress(raw, compresslevel=9, mtime=0)


def unpack_cards(data: bytes) -> dict[int, dict]:
    # Bound decompression independently of the downloaded compressed size.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Card snapshot is too large")
    return index_json(raw)


def gallery_card(record: dict) -> dict:
    dbf_id = record.get("id")
    if type(dbf_id) is not int or not 0 < dbf_id < 10_000_000:
        raise LocalLibraryError("Галерея Blizzard вернула неверный ID карты.")
    try:
        card = normalize_card(record, dbf_id)
    except BlizzardProviderError as error:
        raise LocalLibraryError(str(error)) from error
    card["_source"] = "blizzard-gallery"
    card["sourceUrl"] = "https://hearthstone.blizzard.com/en-us/cards/" + str(dbf_id)
    return card


def image_revision(card: dict) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()[:16]


class LocalLibrary:
    def __init__(self, cache_dir: Path, bundled_dir: Path, *, session=None, clock=None):
        self.cache_dir = Path(cache_dir)
        self.bundled_dir = Path(bundled_dir)
        self.session = session if session is not None else requests.Session()
        self.clock = clock if clock is not None else time.time
        self._locks = {locale: threading.RLock() for locale in LOCALES}
        self._cards = {}
        self._state = {}
        self._attempts = {}
        self._last_checks = {}

    def _paths(self, locale):
        return self.cache_dir / f"cards.{locale}.json.gz", self.cache_dir / f"cards.{locale}.info.json"

    @staticmethod
    def _progress(progress, message):
        if progress:
            progress(message)

    def _load(self, locale):
        if locale in self._cards:
            return
        cached, info_path = self._paths(locale)
        for path in (cached, self.bundled_dir / cached.name):
            try:
                payload = path.read_bytes()
                cards = unpack_cards(payload)
            except (OSError, ValueError, EOFError, CardLibraryError):
                continue
            state = {}
            state_path = info_path if path == cached else self.bundled_dir / info_path.name
            try:
                candidate = json.loads(state_path.read_bytes())
                # A stale info file after an interrupted write cannot validate
                # a different snapshot's ETag or freshness.
                if (isinstance(candidate, dict) and candidate.get("sha256") ==
                        hashlib.sha256(payload).hexdigest()):
                    state = {key: value for key, value in candidate.items()
                        if key not in ("checkedAt", "galleryCheckedAt", "etag", "lastModified")}
                    for key in ("checkedAt", "galleryCheckedAt"):
                        value = candidate.get(key)
                        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= self.clock():
                            state[key] = value
                    for key in ("etag", "lastModified"):
                        value = candidate.get(key)
                        if isinstance(value, str) and "\r" not in value and "\n" not in value:
                            state[key] = value
            except (OSError, ValueError):
                pass
            self._cards[locale], self._state[locale] = cards, state
            return
        # Import the previous app's raw cache once, if no bundled catalog exists.
        legacy = self.cache_dir.parent / f"cards.{locale}.json"
        try:
            cards = index_json(legacy.read_bytes())
        except (OSError, ValueError, CardLibraryError):
            cards = {}
        self._cards[locale], self._state[locale] = cards, {}
        if cards:
            self._store(locale, cards, {"checkedAt": legacy.stat().st_mtime})

    def _store(self, locale, cards, state):
        path, info_path = self._paths(locale)
        payload = pack_cards(cards)
        next_state = dict(state, locale=locale, cardCount=len(cards),
            galleryCount=sum(c.get("_source") == "blizzard-gallery" for c in cards.values()),
            bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        # Publish validated content first. On restart a stale manifest forces a
        # fresh check rather than treating different content as already current.
        atomic_write(path, payload)
        atomic_write(info_path, json.dumps(next_state, ensure_ascii=False).encode("utf-8"))
        self._cards[locale], self._state[locale] = cards, next_state

    def sync(self, locale="ruRU", *, refresh=False, progress=None):
        if locale not in LOCALES:
            raise LocalLibraryError("Выбери русский или английский язык карт.")
        with self._locks[locale]:
            self._load(locale)
            cards, state = self._cards[locale], self._state[locale]
            now = self.clock()
            if not refresh and cards and now - state.get("checkedAt", 0) < CHECK_INTERVAL:
                return cards
            if now - self._last_checks.get(locale, 0) < RETRY_INTERVAL:
                return cards
            self._last_checks[locale] = now
            self._progress(progress, "Проверяю обновление локальной библиотеки…")
            headers = {"User-Agent": "DeckViewLocal/1.0"}
            if state.get("etag") and cards:
                headers["If-None-Match"] = state["etag"]
            if state.get("lastModified") and cards:
                headers["If-Modified-Since"] = state["lastModified"]
            try:
                response = self.session.get(
                    f"https://api.hearthstonejson.com/v1/latest/{locale}/cards.json",
                    headers=headers, timeout=(4, 15))
                if response.status_code == 304 and cards:
                    next_state = dict(state, checkedAt=now)
                    # The data is unchanged; do not rewrite the large snapshot.
                    _, info_path = self._paths(locale)
                    if not self._paths(locale)[0].is_file():
                        self._store(locale, cards, next_state)
                    else:
                        atomic_write(info_path, json.dumps(next_state).encode("utf-8"))
                        self._state[locale] = next_state
                else:
                    response.raise_for_status()
                    if len(response.content) > MAX_JSON_BYTES:
                        raise ValueError("Card snapshot is too large")
                    updated = index_json(response.content)
                    # Retain revealed cards until they become part of HSJSON.
                    updated.update({cid: item for cid, item in cards.items()
                        if item.get("_source") == "blizzard-gallery" and cid not in updated})
                    self._store(locale, updated, dict(state, checkedAt=now,
                        etag=response.headers.get("ETag"), lastModified=response.headers.get("Last-Modified"),
                        hsjsonUrl=response.url))
                    cards = updated
            except (requests.RequestException, OSError, ValueError, CardLibraryError):
                if not cards:
                    raise LocalLibraryError("Не удалось загрузить библиотеку карт. Проверь интернет и повтори запуск.") from None
                self._progress(progress, "Использую сохранённую локальную библиотеку.")
            self._refresh_gallery(locale, progress=progress)
            return self._cards[locale]

    def _gallery_lookup(self, ids, locale):
        url_locale, api_locale = LOCALES[locale]
        response = self.session.get(
            f"https://hearthstone.blizzard.com/{url_locale}/api/cards",
            params={"locale": api_locale, "ids": ",".join(map(str, sorted(ids))), "pageSize": 450},
            headers={"Accept": "application/json", "User-Agent": "DeckViewLocal/1.0"},
            timeout=(4, 15))
        response.raise_for_status()
        if len(response.content) > MAX_JSON_BYTES:
            raise LocalLibraryError("Галерея Blizzard вернула слишком большой ответ.")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
            raise LocalLibraryError("Галерея Blizzard вернула неверные данные карт.")
        result = {}
        for record in data["cards"]:
            if not isinstance(record, dict) or record.get("id") not in ids:
                raise LocalLibraryError("Галерея Blizzard вернула другие карты вместо запрошенных.")
            card = gallery_card(record)
            if card["dbfId"] in result:
                raise LocalLibraryError("Галерея Blizzard вернула повторяющиеся карты.")
            result[card["dbfId"]] = card
        return result

    def _refresh_gallery(self, locale, *, progress=None):
        state = self._state[locale]
        if self.clock() - state.get("galleryCheckedAt", 0) < CHECK_INTERVAL:
            return
        ids = {cid for cid, c in self._cards[locale].items() if c.get("_source") == "blizzard-gallery"}
        if not ids:
            return
        try:
            refreshed = {}
            ordered = sorted(ids)
            for start in range(0, len(ordered), 450):
                refreshed.update(self._gallery_lookup(set(ordered[start:start+450]), locale))
            updated = dict(self._cards[locale])
            updated.update(refreshed)
            self._store(locale, updated, dict(state, galleryCheckedAt=self.clock()))
        except (requests.RequestException, OSError, ValueError):
            self._progress(progress, "Сохранённые карты галереи доступны локально.")

    def get_cards(self, dbf_ids, locale="ruRU", *, progress=None):
        ids = set(dbf_ids)
        if any(type(cid) is not int or not 0 < cid < 10_000_000 for cid in ids):
            raise LocalLibraryError("Нужны числовые ID карт из кода колоды.")
        if locale not in LOCALES:
            raise LocalLibraryError("Выбери русский или английский язык карт.")
        with self._locks[locale]:
            cards = self.sync(locale, progress=progress)
            missing = ids - cards.keys()
            if missing:
                cards = self.sync(locale, refresh=True, progress=progress)
                missing = ids - cards.keys()
            eligible = {cid for cid in missing if self.clock() - self._attempts.get((locale, cid), 0) >= RETRY_INTERVAL}
            if eligible:
                self._progress(progress, "Догружаю недостающие карты из галереи Blizzard…")
                for cid in eligible:
                    self._attempts[locale, cid] = self.clock()
                try:
                    found = self._gallery_lookup(eligible, locale)
                    if found:
                        cards = dict(cards)
                        cards.update(found)
                        self._store(locale, cards, self._state[locale])
                except (requests.RequestException, OSError, ValueError):
                    pass
            result = {}
            for cid in ids & cards.keys():
                item = dict(cards[cid])
                # Changes to cost/text/stats invalidate only that card's image.
                item["imageRevision"] = image_revision(cards[cid])
                result[cid] = item
            return result

    def info(self, locale="ruRU"):
        if locale not in LOCALES:
            raise LocalLibraryError("Выбери русский или английский язык карт.")
        with self._locks[locale]:
            self._load(locale)
            return dict(self._state[locale], locale=locale, cardCount=len(self._cards[locale]))
