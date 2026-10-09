"""An offline, reusable index of complete HearthstoneJSON card snapshots.

There is deliberately no HTTP client here. A caller explicitly imports a JSON
snapshot; resolving a deck then uses only this local SQLite database. The full
records, including tokens and non-collectible cards, are retained unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 1
_LOCALE_PATTERN = re.compile(r"[a-z]{2}[A-Z]{2}\Z")
_CARD_ID_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")


class CardLibraryError(ValueError):
    """Invalid metadata or an incompatible local library."""


def _locale(value: str) -> str:
    if not isinstance(value, str) or not _LOCALE_PATTERN.fullmatch(value):
        raise CardLibraryError("Expected a Hearthstone locale such as ruRU or enUS.")
    return value


def _reject_constant(value: str) -> None:
    raise CardLibraryError(f"Non-JSON number in card metadata: {value}.")


def _parse_json(payload: bytes | str) -> tuple[bytes, list[dict]]:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise CardLibraryError("The card snapshot must be JSON bytes or text.")
    try:
        records = json.loads(raw, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CardLibraryError("The card snapshot is not valid JSON.") from error
    if not isinstance(records, list) or not records:
        raise CardLibraryError("The card snapshot must be a non-empty JSON array.")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise CardLibraryError("Every card record must be a JSON object.")
        dbf_id, card_id = record.get("dbfId"), record.get("id")
        if (type(dbf_id) is not int or not 0 < dbf_id < 2**63
                or not isinstance(card_id, str)
                or not _CARD_ID_PATTERN.fullmatch(card_id)):
            raise CardLibraryError("Every card needs a positive dbfId and a valid id.")
        if dbf_id in seen:
            raise CardLibraryError(f"Duplicate dbfId in card snapshot: {dbf_id}.")
        seen.add(dbf_id)
    return raw, records


def index_json(payload: bytes | str) -> dict[int, dict]:
    """Build an in-memory index for a tracker which already supplies its JSON."""
    _, records = _parse_json(payload)
    return {record["dbfId"]: record for record in records}


class CardLibrary:
    """Persistent card lookup, with one atomically replaced snapshot per locale.

    ``root`` is a library directory, not a file. Each operation opens its own
    SQLite connection, so one instance can safely serve several request threads.
    The exact imported JSON is stored alongside the index in the same database
    transaction and is available through :meth:`export_json`.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.database_path = self.root / "cards.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise CardLibraryError(f"Unsupported card library schema: {version}.")
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        locale TEXT PRIMARY KEY,
                        build TEXT,
                        source TEXT,
                        imported_at TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        card_count INTEGER NOT NULL,
                        collectible_count INTEGER NOT NULL,
                        source_json BLOB NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS cards (
                        locale TEXT NOT NULL,
                        dbf_id INTEGER NOT NULL,
                        card_id TEXT NOT NULL,
                        record_json TEXT NOT NULL,
                        PRIMARY KEY (locale, dbf_id),
                        FOREIGN KEY (locale) REFERENCES snapshots(locale)
                            ON DELETE CASCADE
                    ) WITHOUT ROWID
                """)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def import_json(self, payload: bytes | str, locale: str,
                    build: str | None = None, source: str | None = None) -> dict:
        """Validate and replace all cards in one locale, keeping other locales.

        Validation completes before touching the database. Failed writes roll
        back both the lookup index and the exact source JSON. ``build`` and
        ``source`` describe the caller's snapshot; neither triggers a download.
        """
        locale = _locale(locale)
        for label, value in (("build", build), ("source", source)):
            if value is not None and not isinstance(value, str):
                raise CardLibraryError(f"The {label} must be text or None.")
        raw, records = _parse_json(payload)
        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        digest = hashlib.sha256(raw).hexdigest()
        collectible_count = sum(record.get("collectible") is True for record in records)
        rows = [
            (locale, record["dbfId"], record["id"],
             json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            for record in records
        ]
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM snapshots WHERE locale=?", (locale,))
            connection.execute("""
                INSERT INTO snapshots
                    (locale, build, source, imported_at, sha256, card_count,
                     collectible_count, source_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (locale, build, source, imported_at, digest, len(records),
                  collectible_count, raw))
            connection.executemany("""
                INSERT INTO cards (locale, dbf_id, card_id, record_json)
                VALUES (?, ?, ?, ?)
            """, rows)
        return {
            "locale": locale,
            "build": build,
            "source": source,
            "importedAt": imported_at,
            "sha256": digest,
            "cardCount": len(records),
            "collectibleCount": collectible_count,
            "bytes": len(raw),
        }

    def get_cards(self, dbf_ids: Iterable[int], locale: str) -> dict[int, dict]:
        """Look up deck IDs; absent cards and absent locales are omitted."""
        locale = _locale(locale)
        ids = list(dict.fromkeys(dbf_ids))
        if any(type(value) is not int or not 0 < value < 2**63 for value in ids):
            raise CardLibraryError("Card lookup expects positive integer dbfIds.")
        result = {}
        with self._connection() as connection:
            # Stay below the variable limit on older embedded SQLite runtimes.
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT dbf_id, record_json FROM cards "
                    f"WHERE locale=? AND dbf_id IN ({placeholders})",
                    (locale, *batch),
                )
                result.update((dbf_id, json.loads(record)) for dbf_id, record in rows)
        return result

    def all_cards(self, locale: str) -> dict[int, dict]:
        """Return a complete local metadata index, including non-collectibles."""
        locale = _locale(locale)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT dbf_id, record_json FROM cards WHERE locale=?", (locale,))
            return {dbf_id: json.loads(record) for dbf_id, record in rows}

    def export_json(self, locale: str) -> bytes:
        """Return the exact imported JSON; raise KeyError for a missing locale."""
        locale = _locale(locale)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT source_json FROM snapshots WHERE locale=?", (locale,)).fetchone()
        if row is None:
            raise KeyError(locale)
        return bytes(row[0])

    def info(self) -> dict:
        """Describe installed snapshots without reading all card records."""
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT locale, build, source, imported_at, sha256, card_count,
                       collectible_count, length(source_json)
                FROM snapshots ORDER BY locale
            """)
            snapshots = {
                locale: {
                    "build": build,
                    "source": source,
                    "importedAt": imported_at,
                    "sha256": digest,
                    "cardCount": card_count,
                    "collectibleCount": collectible_count,
                    "bytes": size,
                }
                for (locale, build, source, imported_at, digest, card_count,
                     collectible_count, size) in rows
            }
        return {"schemaVersion": SCHEMA_VERSION,
                "database": str(self.database_path), "locales": snapshots}
