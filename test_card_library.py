"""Offline checks for complete, atomic, reusable card metadata snapshots."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from card_library import CardLibrary, CardLibraryError, index_json


def payload(records):
    return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")


ENGLISH = [
    {"dbfId": 31, "id": "HERO_05", "type": "HERO", "cardClass": "HUNTER", "name": "Rexxar"},
    {"dbfId": 100, "id": "TEST_100", "name": "Hunter card", "collectible": True,
     "cost": 3, "mechanics": ["DEATHRATTLE"], "runeCost": {"blood": 2}},
    {"dbfId": 101, "id": "TEST_100t", "name": "Token", "collectible": False,
     "customFutureField": {"nested": [1, {"text": "kept exactly"}]}},
]
RUSSIAN = [dict(record, name=name) for record, name in zip(
    ENGLISH, ["Рексар", "Карта охотника", "Токен"])]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[2] / "work" / "library-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.library = CardLibrary(Path(self.temp.name) / "library")

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_metadata_and_locales_survive_reopening(self):
        self.library.import_json(payload(ENGLISH), "enUS", build="12345", source="fixture")
        self.library.import_json(payload(RUSSIAN), "ruRU", build="12345")
        reopened = CardLibrary(self.library.root)
        self.assertEqual(reopened.all_cards("enUS"), {record["dbfId"]: record for record in ENGLISH})
        self.assertEqual(reopened.get_cards([31, 100, 101], "ruRU"),
                         {record["dbfId"]: record for record in RUSSIAN})
        self.assertEqual(reopened.get_cards([101], "enUS")[101]["customFutureField"],
                         ENGLISH[2]["customFutureField"])
        info = reopened.info()
        self.assertEqual(info["locales"]["enUS"]["cardCount"], 3)
        self.assertEqual(info["locales"]["enUS"]["collectibleCount"], 1)
        self.assertEqual(info["locales"]["enUS"]["build"], "12345")
        self.assertEqual(info["locales"]["enUS"]["source"], "fixture")

    def test_exact_source_json_is_committed_with_card_records(self):
        raw = payload(RUSSIAN) + b"\n"
        self.library.import_json(raw, "ruRU")
        self.assertEqual(self.library.export_json("ruRU"), raw)
        self.assertEqual(index_json(raw), self.library.all_cards("ruRU"))
        with self.assertRaises(KeyError):
            self.library.export_json("deDE")

    def test_reimport_replaces_locale_but_retains_other_locales(self):
        self.library.import_json(payload(ENGLISH), "enUS", build="old")
        self.library.import_json(payload(RUSSIAN), "ruRU", build="old")
        replacement = [dict(ENGLISH[1], name="Updated name")]
        self.library.import_json(payload(replacement), "enUS", build="new")
        self.assertEqual(self.library.all_cards("enUS"), {100: replacement[0]})
        self.assertEqual(len(self.library.all_cards("ruRU")), 3)
        self.assertEqual(self.library.info()["locales"]["enUS"]["build"], "new")

    def test_malformed_import_keeps_previous_snapshot(self):
        original = payload(ENGLISH)
        self.library.import_json(original, "enUS", build="old")
        invalid = [b"not-json", b"[]", b"{}", b"[null]",
                   payload([ENGLISH[0], ENGLISH[0]]),
                   payload([dict(ENGLISH[0], dbfId=True)]),
                   payload([dict(ENGLISH[0], id="../unsafe")]),
                   b'[{"dbfId":1,"id":"CARD", "cost":NaN}]']
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(CardLibraryError):
                self.library.import_json(candidate, "enUS", build="invalid")
            self.assertEqual(self.library.export_json("enUS"), original)
            self.assertEqual(len(self.library.all_cards("enUS")), 3)
            self.assertEqual(self.library.info()["locales"]["enUS"]["build"], "old")

    def test_sql_write_failure_rolls_back_card_index_and_source(self):
        original = payload(ENGLISH)
        self.library.import_json(original, "enUS", build="old")
        with closing(sqlite3.connect(self.library.database_path)) as connection, connection:
            connection.execute("""
                CREATE TRIGGER reject_test_card BEFORE INSERT ON cards
                WHEN NEW.dbf_id = 100
                BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.import_json(payload([dict(ENGLISH[1], name="New")]), "enUS", build="new")
        self.assertEqual(self.library.export_json("enUS"), original)
        self.assertEqual(self.library.all_cards("enUS"), index_json(original))
        self.assertEqual(self.library.info()["locales"]["enUS"]["build"], "old")

    def test_missing_ids_empty_input_and_parameter_validation(self):
        self.library.import_json(payload(ENGLISH), "enUS")
        self.assertEqual(self.library.get_cards([100, 999, 100], "enUS"), {100: ENGLISH[1]})
        self.assertEqual(self.library.get_cards([], "enUS"), {})
        self.assertEqual(self.library.get_cards([100], "deDE"), {})
        self.assertEqual(self.library.all_cards("deDE"), {})
        for ids in (["100"], [True], [-1], [2**63]):
            with self.subTest(ids=ids), self.assertRaises(CardLibraryError):
                self.library.get_cards(ids, "enUS")
        with self.assertRaises(CardLibraryError):
            self.library.import_json(payload(ENGLISH), "../ruRU")

    def test_large_id_lookup_chunks_and_preserves_every_record(self):
        records = [{"dbfId": number, "id": f"CARD_{number}"} for number in range(1, 1251)]
        self.library.import_json(payload(records), "enUS")
        found = self.library.get_cards(iter(range(1, 1252)), "enUS")
        self.assertEqual(len(found), 1250)
        self.assertEqual(found[1250], records[-1])

    def test_independent_connections_support_request_threads(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.library.import_json, payload(records), locale)
                       for records, locale in ((ENGLISH, "enUS"), (RUSSIAN, "ruRU"))]
            for future in futures:
                self.assertEqual(future.result()["cardCount"], 3)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.library.get_cards, [31, 100, 101], locale)
                       for locale in ("ruRU", "enUS", "ruRU", "enUS")]
            self.assertEqual([len(future.result()) for future in futures], [3, 3, 3, 3])


if __name__ == "__main__":
    unittest.main()
