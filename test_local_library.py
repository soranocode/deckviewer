"""Local catalog freshness, exact gallery lookups and offline regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from local_library import LocalLibrary, LocalLibraryError, gallery_card, image_revision, pack_cards


def card(cid=100, **fields):
    return dict(dbfId=cid, id=f"CARD_{cid}", name=f"Card {cid}", cost=2, **fields)


def response(cards=None, status=200, headers=None):
    data = cards if cards is not None else []
    result = Mock(status_code=status, headers=headers or {}, url="https://api.hearthstonejson.com/v1/latest/ruRU/cards.json")
    result.content = json.dumps(data).encode()
    result.json.return_value = data
    return result


class LocalCatalogTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent.parent.parent / "work" / "local-library-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        root = Path(self.temp.name)
        self.bundle, self.cache = root / "bundle", root / "cache"
        self.bundle.mkdir()
        self.clock = Mock(return_value=1_000_000)
        self.session = Mock()
        self.session.get.side_effect = requests.ConnectionError("Offline")
        self.library = LocalLibrary(self.cache, self.bundle, session=self.session, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, cards, state=None):
        bundler = LocalLibrary(self.bundle, self.bundle, session=self.session, clock=self.clock)
        bundler._store("ruRU", cards, state or {"checkedAt": self.clock(), "galleryCheckedAt": self.clock()})

    def test_all_record_fields_and_noncollectibles_work_offline(self):
        records = {100: card(extra={"future": [1, 2]}, collectible=False),
                   101: card(101, type="HERO", cardClass="ROGUE")}
        self.install(records)
        fetched = self.library.get_cards([100, 101], "ruRU")
        self.assertEqual(fetched[100]["extra"], {"future": [1, 2]})
        self.assertFalse(fetched[100]["collectible"])
        self.assertEqual(fetched[101]["type"], "HERO")
        self.session.get.assert_not_called()

    def test_304_sends_etag_and_preserves_snapshot(self):
        self.library._store("ruRU", {100: card()}, {"checkedAt": 1, "etag": '"version-one"'})
        path = self.cache / "cards.ruRU.json.gz"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.session.get.side_effect = None
        self.session.get.return_value = response(status=304)
        self.library.sync("ruRU")
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["If-None-Match"], '"version-one"')
        self.assertEqual(self.library.info()["checkedAt"], self.clock())

    def test_balance_update_adds_cards_and_changes_only_affected_revisions(self):
        self.install({100: card(), 101: card(101)}, {"checkedAt": 1})
        old = self.library.get_cards([100, 101])
        # Previous call failed offline and was throttled; advance retry interval.
        self.clock.return_value += 601
        self.session.get.side_effect = None
        self.session.get.return_value = response([dict(card(), cost=3), card(101), card(102)])
        new = self.library.get_cards([100, 101, 102])
        self.assertNotEqual(old[100]["imageRevision"], new[100]["imageRevision"])
        self.assertEqual(old[101]["imageRevision"], new[101]["imageRevision"])
        self.assertIn(102, new)
        self.assertEqual(new[100]["cost"], 3)

    def test_exact_missing_id_is_saved_and_available_after_offline_restart(self):
        self.install({100: card()})
        official = {"id": 129340, "name": "Shadow Dance", "manaCost": 0,
                    "rarityId": 1, "collectible": 1, "cardTypeId": 5,
                    "image": "https://images.example/shadow-dance.png"}
        self.session.get.side_effect = [response([card()]), response({"cards": [official]})]
        result = self.library.get_cards([100, 129340])
        self.assertEqual(result[129340]["rarity"], "COMMON")
        self.assertEqual(result[129340]["blizzard"], official)
        lookup = self.session.get.call_args
        self.assertEqual(lookup.args[0], "https://hearthstone.blizzard.com/ru-ru/api/cards")
        self.assertEqual(lookup.kwargs["params"]["ids"], "129340")
        self.assertNotIn("Authorization", lookup.kwargs["headers"])
        offline = Mock()
        offline.get.side_effect = AssertionError("No network needed")
        restarted = LocalLibrary(self.cache, self.bundle, session=offline, clock=self.clock)
        # The supplemental lookup itself is fresh even if its set was not synced.
        restarted._state = {}
        self.assertIn(129340, restarted.get_cards([129340]))

    def test_wrong_gallery_id_never_replaces_requested_card(self):
        self.install({100: card()})
        self.session.get.side_effect = [response([card()]), response({"cards": [{"id": 999, "name": "Wrong"}]})]
        self.assertNotIn(101, self.library.get_cards([101]))
        self.assertNotIn(999, self.library.info())
        self.assertEqual(self.library.info()["cardCount"], 1)

    def test_corrupt_or_failed_update_keeps_last_good_catalog(self):
        self.library._store("ruRU", {100: card()}, {"checkedAt": 1})
        before = (self.cache / "cards.ruRU.json.gz").read_bytes()
        self.session.get.side_effect = None
        self.session.get.return_value = response({"error": "not card data"})
        self.assertIn(100, self.library.sync())
        self.assertEqual((self.cache / "cards.ruRU.json.gz").read_bytes(), before)
        # A damaged active snapshot falls back to the bundled copy on restart.
        self.install({101: card(101)})
        (self.cache / "cards.ruRU.json.gz").write_bytes(b"corrupt")
        restored = LocalLibrary(self.cache, self.bundle, session=self.session, clock=self.clock)
        self.assertIn(101, restored.get_cards([101]))

    def test_supplement_refresh_changes_official_image_and_retains_cards_not_in_hsjson(self):
        old = gallery_card({"id": 101, "name": "Old", "manaCost": 2,
                            "image": "https://images.example/old.png"})
        self.install({100: card(), 101: old}, {"checkedAt": 1, "galleryCheckedAt": 1})
        new = {"id": 101, "name": "Updated", "manaCost": 3,
               "image": "https://images.example/new.png"}
        self.session.get.side_effect = [response([card()]), response({"cards": [new]})]
        result = self.library.get_cards([101])
        self.assertEqual(result[101]["cost"], 3)
        self.assertEqual(result[101]["imageUrl"], new["image"])
        self.assertNotEqual(result[101]["imageRevision"], image_revision(old))

    def test_hsjson_takes_over_a_previously_revealed_card(self):
        extra = gallery_card({"id": 101, "name": "Revealed"})
        self.install({100: card(), 101: extra}, {"checkedAt": 1})
        self.session.get.side_effect = None
        self.session.get.return_value = response([card(), card(101)])
        result = self.library.get_cards([101])
        self.assertEqual(result[101]["id"], "CARD_101")
        self.assertNotIn("_source", result[101])
        self.assertEqual(self.library.info()["galleryCount"], 0)


if __name__ == "__main__":
    unittest.main()
