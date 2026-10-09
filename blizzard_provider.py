"""Resolve deck card IDs through the official Battle.net Hearthstone API.

This adapter needs an application's Client ID and Client Secret, and performs
no full-database download. OAuth tokens remain in memory; credentials and tokens
are never put in a URL. The renderer receives a small HearthstoneJSON-compatible
record, while the complete official response is retained in ``blizzard``.
"""
from __future__ import annotations

import copy
import math
import os
import re
import threading
import time
from typing import Iterable
from urllib.parse import urlsplit

import requests

TOKEN_URL = "https://oauth.battle.net/token"
LOCALES = {"ruRU": "ru_RU", "enUS": "en_US",
           "ru_RU": "ru_RU", "en_US": "en_US"}
CLASS_IDS = {1: "DEATHKNIGHT", 2: "DRUID", 3: "HUNTER", 4: "MAGE",
             5: "PALADIN", 6: "PRIEST", 7: "ROGUE", 8: "SHAMAN",
             9: "WARLOCK", 10: "WARRIOR", 11: "DREAM", 12: "NEUTRAL",
             13: "WHIZBANG", 14: "DEMONHUNTER"}
# The official API's first two rarity IDs differ from Hearthstone's game enums.
# Upstream Battle.net crafting costs are 1:0 and 2:40 (FREE and COMMON).
# Verified against the live official gallery metadata and Shadow Dance/Fireball.
RARITY_IDS = {1: "COMMON", 2: "FREE", 3: "RARE", 4: "EPIC", 5: "LEGENDARY"}
TYPE_IDS = {1: "GAME", 2: "PLAYER", 3: "HERO", 4: "MINION", 5: "SPELL",
            6: "ENCHANTMENT", 7: "WEAPON", 8: "ITEM", 9: "TOKEN",
            10: "HERO_POWER", 11: "BLANK", 12: "GAME_MODE_BUTTON",
            22: "MOVE_MINION_HOVER_TARGET", 23: "LETTUCE_ABILITY",
            24: "BATTLEGROUND_HERO_BUDDY", 39: "LOCATION",
            40: "BATTLEGROUND_QUEST_REWARD", 42: "BATTLEGROUND_SPELL",
            43: "BATTLEGROUND_ANOMALY", 44: "BATTLEGROUND_TRINKET", 45: "PET"}
_SYMBOLIC_ID = re.compile(r"[A-Za-z0-9_]+\Z")
_TIMEOUT = (8, 30)
_ATTEMPTS = 3


class BlizzardProviderError(ValueError):
    """An actionable, credential-free message for the local app's user."""


def normalize_card(record: dict, expected_id: int) -> dict:
    """Adapt one official record without inventing another card's identity."""
    if not isinstance(record, dict):
        raise BlizzardProviderError("Blizzard вернул неверные данные карты.")
    identifier = record.get("id")
    if type(identifier) is int and identifier == expected_id:
        symbolic_id = f"BLIZZARD_{identifier}"
    elif (isinstance(identifier, str) and _SYMBOLIC_ID.fullmatch(identifier)
          and type(record.get("dbfId")) is int and record["dbfId"] == expected_id):
        symbolic_id = identifier
    else:
        raise BlizzardProviderError(
            f"Blizzard вернул другую карту вместо карты с ID {expected_id}.")
    card = {"dbfId": expected_id, "id": symbolic_id,
            "blizzard": copy.deepcopy(record)}
    for field in ("name", "text", "attack", "health", "durability", "armor",
                  "runeCost", "hideCost", "isZilliaxFunctionalModule"):
        if field in record:
            card[field] = copy.deepcopy(record[field])
    card["cost"] = record.get("manaCost", record.get("cost", 0))
    card["collectible"] = record.get("collectible") in (True, 1)
    for input_key, output_key, mapping in (
        ("classId", "cardClass", CLASS_IDS),
        ("rarityId", "rarity", RARITY_IDS),
        ("cardTypeId", "type", TYPE_IDS),
    ):
        value = record.get(input_key)
        if type(value) is int and value in mapping:
            card[output_key] = mapping[value]
    if isinstance(record.get("set"), str):
        card["set"] = record["set"]
    elif record.get("cardSetId") in (1637, 1810):
        card["set"] = "CORE"
    if record.get("isZilliaxCosmeticModule"):
        card["hideCost"] = True
    image_url = record.get("image") or record.get("imageUrl")
    if image_url:
        if not isinstance(image_url, str):
            raise BlizzardProviderError("Blizzard вернул неверный адрес изображения карты.")
        try:
            parsed = urlsplit(image_url)
        except ValueError:
            raise BlizzardProviderError("Blizzard вернул неверный адрес изображения карты.") from None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise BlizzardProviderError("Blizzard вернул неверный адрес изображения карты.")
        card["imageUrl"] = image_url
    return card


class BlizzardProvider:
    """Official card lookup with bounded retries and cached client credentials.

    Missing cards (HTTP 404) are omitted from ``get_cards``. Other failures raise
    ``BlizzardProviderError``. Callers can therefore handle missing hero skins or
    unavailable cards explicitly rather than silently substituting an alias.
    """

    def __init__(self, client_id: str | None = None,
                 client_secret: str | None = None, region: str = "eu",
                 session: requests.Session | None = None):
        configured_id = (client_id if client_id is not None
                         else os.environ.get("BLIZZARD_CLIENT_ID", ""))
        configured_secret = (client_secret if client_secret is not None
                             else os.environ.get("BLIZZARD_CLIENT_SECRET", ""))
        if not isinstance(configured_id, str) or not isinstance(configured_secret, str):
            raise BlizzardProviderError("Client ID и Client Secret должны быть строками.")
        self._client_id = configured_id.strip()
        self._client_secret = configured_secret.strip()
        if any(character in value for value in (self._client_id, self._client_secret)
               for character in ("\r", "\n", "\x00")):
            raise BlizzardProviderError("Client ID и Client Secret содержат недопустимые символы.")
        if not self._client_id or not self._client_secret:
            raise BlizzardProviderError(
                "Для подключения к Blizzard укажи Client ID и Client Secret "
                "в BLIZZARD_CLIENT_ID и BLIZZARD_CLIENT_SECRET.")
        if not isinstance(region, str) or region not in {"eu", "us"}:
            raise BlizzardProviderError("Выбери регион Blizzard eu или us.")
        self.region = region
        self.base_url = f"https://{region}.api.blizzard.com/hearthstone"
        self.session = session if session is not None else requests.Session()
        self._token_lock = threading.Lock()
        self._token = None
        self._token_expires = 0.0

    @staticmethod
    def _delay(response, attempt: int) -> None:
        delay = 0.4 * 2**attempt
        if response is not None and response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
                if math.isfinite(retry_after):
                    delay = max(0.0, min(2.0, retry_after))
            except (TypeError, ValueError):
                pass
        time.sleep(delay)

    def _request_token(self) -> tuple[str, float]:
        for attempt in range(_ATTEMPTS):
            try:
                response = self.session.post(
                    TOKEN_URL, data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                    timeout=_TIMEOUT, allow_redirects=False,
                )
            except requests.RequestException:
                if attempt + 1 == _ATTEMPTS:
                    raise BlizzardProviderError(
                        "Не удалось подключиться к Blizzard. Проверь интернет и попробуй снова.") from None
                self._delay(None, attempt)
                continue
            if response.status_code in (400, 401, 403):
                raise BlizzardProviderError(
                    "Blizzard не принял Client ID или Client Secret. Проверь ключи приложения.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == _ATTEMPTS:
                    raise BlizzardProviderError(
                        "Сервис авторизации Blizzard временно недоступен. Попробуй позже.")
                self._delay(response, attempt)
                continue
            if response.status_code != 200:
                raise BlizzardProviderError("Не удалось получить доступ к API Blizzard.")
            try:
                data = response.json()
                token = data["access_token"]
                expiry = float(data.get("expires_in", 3600))
                if (not isinstance(token, str) or not token or "\r" in token or "\n" in token
                        or not math.isfinite(expiry) or expiry <= 0):
                    raise ValueError("Invalid token")
            except (ValueError, TypeError, KeyError):
                raise BlizzardProviderError("Blizzard вернул неверный ответ авторизации.") from None
            return token, max(0, expiry - min(60, expiry / 10))
        raise BlizzardProviderError("Не удалось получить доступ к API Blizzard.")

    def _access_token(self, rejected_token: str | None = None) -> str:
        with self._token_lock:
            if rejected_token is not None and self._token == rejected_token:
                self._token = None
                self._token_expires = 0.0
            if self._token and self._token_expires > time.monotonic():
                return self._token
            token, duration = self._request_token()
            self._token = token
            self._token_expires = time.monotonic() + duration
            return token

    def _card(self, dbf_id: int, locale: str) -> dict | None:
        token = self._access_token()
        transient_attempt = 0
        renewed = False
        while transient_attempt < _ATTEMPTS:
            try:
                response = self.session.get(
                    f"{self.base_url}/cards/{dbf_id}",
                    params={"locale": locale},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=_TIMEOUT, allow_redirects=False,
                )
            except requests.RequestException:
                transient_attempt += 1
                if transient_attempt == _ATTEMPTS:
                    raise BlizzardProviderError(
                        "Не удалось загрузить карты Blizzard. Проверь интернет и попробуй снова.") from None
                self._delay(None, transient_attempt - 1)
                continue
            if response.status_code == 401:
                if renewed:
                    raise BlizzardProviderError(
                        "Blizzard отклонил доступ к картам. Проверь ключи приложения.")
                token = self._access_token(rejected_token=token)
                renewed = True
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                transient_attempt += 1
                if transient_attempt == _ATTEMPTS:
                    raise BlizzardProviderError(
                        "API карт Blizzard временно недоступен. Попробуй позже.")
                self._delay(response, transient_attempt - 1)
                continue
            if response.status_code != 200:
                raise BlizzardProviderError(
                    f"Blizzard не разрешил получить карту с ID {dbf_id}.")
            try:
                data = response.json()
            except ValueError:
                raise BlizzardProviderError("Blizzard вернул неверные данные карты.") from None
            return normalize_card(data, dbf_id)
        raise BlizzardProviderError("API карт Blizzard временно недоступен. Попробуй позже.")

    def get_cards(self, dbf_ids: Iterable[int], locale: str = "ruRU") -> dict[int, dict]:
        """Fetch exactly the unique IDs requested by a decoded deck."""
        if not isinstance(locale, str) or locale not in LOCALES:
            raise BlizzardProviderError("Выбери русский или английский язык карт.")
        ids = list(dbf_ids)
        if any(type(value) is not int or not 0 < value < 10_000_000 for value in ids):
            raise BlizzardProviderError("Для поиска карт нужны числовые ID из кода колоды.")
        cards = {}
        for dbf_id in dict.fromkeys(ids):
            record = self._card(dbf_id, LOCALES[locale])
            if record is not None:
                cards[dbf_id] = record
        return cards
