"""Meaningful offline regression checks; run: python -m unittest test_engine."""
import base64
import json
import os
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from hearthstone.deckstrings import Deck
from hearthstone.enums import CardClass, FormatType
from PIL import Image

import engine
import local_library
import requests

PRIEST_ETC = "AAEBAZCaBgjlsASotgSX7wTvkQXipAX9xAXPxgXGxwUQvp8EobYElrcE+dsEuNwEutwE9vAEhoMFopkF4KQFlMQFu8QFu8cFuJ4Gz54G0Z4GAAED8J8E/cQFuNkE/cQF/+EE/cQFAAA="
DK_ZILLIAX = "AAECAfHhBAiy9wTt/wWLkgaYlQb/lwbHpAbT5QbW5QYLh/YE88gF8OgFl5UGkZcGkqAGurEGvLEG9rEGi7cG1+UGAAED87MGx6QG9rMGx6QG6N4Gx6QGAAA="
HAROLD_ROGUE = "AAECAaIHBIrUB4jZB4XrB5n3Bw2chwe+hwfBlwfXlwfOmwedxQfVxQeN2geE6Ae88gfA+AfJ/QeAgwgAAA=="


def code(cards, hero=31, sideboards=None):
    deck = Deck()
    deck.cards, deck.heroes, deck.format = cards, [hero], FormatType.FT_WILD
    deck.sideboards = sideboards or []
    return deck.as_deckstring


def card(dbf, identifier, **extra):
    return dict(dbfId=dbf, id=identifier, name=identifier, cost=2, rarity="COMMON", collectible=True, **extra)


def png():
    stream = BytesIO()
    Image.new("RGBA", (180, 240), (100, 120, 200, 255)).save(stream, format="PNG")
    return stream.getvalue()


class DecoderTests(unittest.TestCase):
    def test_full_export_and_wrapped_code(self):
        value = engine.EXAMPLE_CODE
        normalized, deck = engine.decode_deck("### Моя колода\n# Класс: Охотник\n" + value + "\n# Импортируйте колоду")
        self.assertEqual(normalized, value)
        self.assertEqual(sum(count for _, count in deck.cards), 30)
        self.assertEqual(deck.heroes, [31])
        _, wrapped = engine.decode_deck(value[:35] + "\n" + value[35:])
        self.assertEqual(wrapped.cards, deck.cards)

    def test_reject_truncation_and_multiple_decks(self):
        for invalid in ("", "AAgarbage123", engine.EXAMPLE_CODE[:35], base64.b64encode(b"\x00\x01\x01\x01\x1f\xff").decode()):
            with self.subTest(invalid=invalid), self.assertRaises(engine.DeckViewError):
                engine.decode_deck(invalid)
        with self.assertRaisesRegex(engine.DeckViewError, "несколько"):
            engine.decode_deck(engine.EXAMPLE_CODE + "\n" + PRIEST_ETC)

    def test_harold_rogue_full_export_keeps_every_dbf_id(self):
        text = "### Harold Rogue\n" + HAROLD_ROGUE + "\n### You can view this deck at https://www.hsguru.com/deck/42404127"
        normalized, deck = engine.decode_deck(text)
        self.assertEqual(normalized, HAROLD_ROGUE)
        self.assertEqual(deck.heroes, [930])
        self.assertEqual(len(deck.cards), 17)
        self.assertEqual(sum(count for _, count in deck.cards), 30)
        self.assertEqual(deck.sideboards, [])
        self.assertTrue({128389, 129340, 130112, 130761, 131456}.issubset({cid for cid, _ in deck.cards}))

    def test_sideboard_decoder_real_regressions(self):
        _, priest = engine.decode_deck(PRIEST_ETC)
        self.assertEqual(sum(count for _, count in priest.cards), 40)
        self.assertEqual(priest.heroes, [101648])
        self.assertEqual(len(priest.sideboards), 3)
        self.assertEqual({owner for _, _, owner in priest.sideboards}, {90749})
        _, dk = engine.decode_deck(DK_ZILLIAX)
        self.assertEqual(sum(count for _, count in dk.cards), 30)
        self.assertEqual({owner for _, _, owner in dk.sideboards}, {102983})

    def test_reject_oversized_count_or_orphan_sideboard(self):
        for invalid in (code([(100, 101)]), code([(100, 1)], sideboards=[(200, 1, 999)])):
            with self.assertRaises(engine.DeckViewError):
                engine.decode_deck(invalid)


class SpecialDeckTests(unittest.TestCase):
    def test_every_class_uses_its_background_asset(self):
        for class_name, (background_id, _, _) in engine.CLASS_INFO.items():
            with self.subTest(class_name=class_name):
                self.assertEqual(background_id, int(getattr(CardClass, class_name)))
                self.assertTrue((engine.BASE_DIR / "backs" / f"{background_id}.png").is_file())

    def test_modules_counts_runes_and_crafting_cost(self):
        value = code([(102983, 1), (500, 2), (501, 1)], hero=78065,
                     sideboards=[(104947, 1, 102983), (104950, 1, 102983), (110440, 1, 102983)])
        _, deck = engine.decode_deck(value)
        metadata = {
            78065: {"id": "HERO_11", "dbfId": 78065, "type": "HERO", "cardClass": "DEATHKNIGHT"},
            102983: {"id": "TOY_330", "dbfId": 102983, "cost": 0, "name": "Zilliax", "rarity": "LEGENDARY", "collectible": True},
            500: card(500, "SPELL_500", runeCost={"blood": 1}),
            501: card(501, "SPELL_501", runeCost={"blood": 2, "unholy": 1}),
            104947: {"id": "TOY_330t95", "dbfId": 104947, "cost": 3, "collectible": False},
            104950: {"id": "TOY_330t98", "dbfId": 104950, "cost": 4, "collectible": False},
            110440: {"id": "TOY_330t5", "dbfId": 110440, "cost": 0, "hideCost": True},
        }
        result = engine.prepare_deck(deck, metadata)
        zilliax = next(item for item in result["cards"] if item["dbfId"] == 102983)
        self.assertEqual(zilliax["cost"], 7)
        self.assertEqual(zilliax["zilliax"], "104947-104950")
        self.assertEqual(result["sideboardCount"], 2)
        self.assertEqual(result["runeSlots"], {"blood": 2, "frost": 0, "unholy": 1})
        self.assertEqual(result["dust"], 1720)
        self.assertEqual(result["cardCount"], 4)

    def test_core_card_is_free_and_alternate_hero_determines_class(self):
        _, deck = engine.decode_deck(code([(100, 2)], hero=101648))
        metadata = {101648: {"type": "HERO", "cardClass": "PRIEST"},
                    100: card(100, "CORE_100", set="CORE")}
        result = engine.prepare_deck(deck, metadata)
        self.assertEqual(result["classId"], 6)
        self.assertEqual(result["dust"], 0)

    def test_blizzard_zilliax_uses_exact_module_ids_without_optional_flags(self):
        from blizzard_provider import normalize_card
        _, deck = engine.decode_deck(code([(102983, 1)], hero=78065,
            sideboards=[(104947, 1, 102983), (104950, 1, 102983), (110440, 1, 102983)]))
        raw = [
            {"id": 78065, "classId": 1, "cardTypeId": 3},
            {"id": 102983, "manaCost": 0, "attack": 0, "health": 0, "rarityId": 5, "collectible": 1},
            {"id": 104947, "manaCost": 3, "attack": 2, "health": 2},
            {"id": 104950, "manaCost": 4, "attack": 1, "health": 3},
            {"id": 110440, "manaCost": 0},
        ]
        result = engine.prepare_deck(deck, {item["id"]: normalize_card(item, item["id"]) for item in raw})
        item = result["cards"][0]
        self.assertEqual((item["cost"], item["attack"], item["health"]), (7, 3, 5))
        self.assertEqual(item["zilliax"], "104947-104950")
        self.assertEqual((result["sideboardCount"], result["dust"]), (2, 1600))


class CacheTests(unittest.TestCase):
    def setUp(self):
        scratch = engine.BASE_DIR.parent.parent / "work" / "engine-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.cache = Path(self.temp.name) / "cache"
        self.generated = Path(self.temp.name) / "generated"
        self.cache_patch = patch.object(engine, "CACHE_DIR", self.cache)
        self.generated_patch = patch.object(engine, "GENERATED_DIR", self.generated)
        self.library_patch = patch.object(engine, "LIBRARY_DIR", Path(self.temp.name) / "bundle")
        self.network_patch = patch.object(local_library.requests.Session, "get", side_effect=requests.ConnectionError("Offline test"))
        self.cache_patch.start()
        self.generated_patch.start()
        self.library_patch.start()
        self.network_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.generated_patch.stop()
        self.library_patch.stop()
        self.network_patch.stop()
        self.temp.cleanup()

    def test_valid_cache_is_reused_and_corrupt_image_is_replaced(self):
        path = self.cache / "images" / "ruRU" / "CARD_100.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"broken")
        with patch.object(engine, "_download", return_value=png()) as download:
            returned, fallback = engine._cache_image(card(100, "CARD_100"), "ruRU")
            self.assertEqual(returned, path)
            self.assertFalse(fallback)
            self.assertTrue(engine._image_is_valid(path))
            self.assertEqual(download.call_count, 1)
        with patch.object(engine, "_download", side_effect=AssertionError("Offline")):
            self.assertEqual(engine._cache_image(card(100, "CARD_100"), "ruRU"), (path, False))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failed_download_never_publishes_target(self):
        with patch.object(engine, "_download", side_effect=engine.DeckViewError("Offline")):
            with self.assertRaises(engine.DeckViewError):
                engine._cache_image(card(100, "CARD_100"), "enUS")
        self.assertFalse((self.cache / "images" / "enUS" / "CARD_100.png").exists())

    def test_download_validates_payload_and_retries_html_response(self):
        invalid = Mock(content=b"<html>temporary proxy error</html>")
        valid = Mock(content=png())
        with patch.object(engine.requests, "get", side_effect=[invalid, valid]) as get, patch.object(engine.time, "sleep"):
            data = engine._download("https://example.invalid/card.png", engine._validate_image)
        self.assertEqual(data, valid.content)
        self.assertEqual(get.call_count, 2)

    def test_english_cache_does_not_preempt_russian_download(self):
        english = self.cache / "images" / "enUS" / "CARD_100.png"
        engine._atomic_write(english, png())
        with patch.object(engine, "_download", return_value=png()) as download:
            returned, fallback = engine._cache_image(card(100, "CARD_100"), "ruRU")
        self.assertEqual(returned.parent.name, "ruRU")
        self.assertFalse(fallback)
        self.assertIn("/ruRU/", download.call_args.args[0])

    def test_known_english_fallback_can_be_reused_offline(self):
        english = self.cache / "images" / "enUS" / "CARD_100.png"
        engine._atomic_write(english, png())
        with patch.object(engine, "_download", side_effect=engine.DeckViewError("Missing Russian render")):
            returned, fallback = engine._cache_image(card(100, "CARD_100"), "ruRU")
        self.assertEqual((returned, fallback), (english, True))
        with patch.object(engine, "_download", side_effect=AssertionError("No network allowed")):
            self.assertEqual(engine._cache_image(card(100, "CARD_100"), "ruRU"), (english, True))

    def test_old_metadata_survives_offline_refresh(self):
        path = self.cache / "cards.ruRU.json"
        content = b'[{"dbfId":31,"id":"HERO_05","type":"HERO","cardClass":"HUNTER"}]'
        engine._atomic_write(path, content)
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        with patch.object(engine, "_download", side_effect=engine.DeckViewError("Offline")):
            self.assertEqual(engine.load_metadata("ruRU")[31]["cardClass"], "HUNTER")
        self.assertEqual(path.read_bytes(), content)

    def test_generate_cached_deck_offline(self):
        import json
        metadata = [{"dbfId": 31, "id": "HERO_05", "type": "HERO", "cardClass": "HUNTER"},
                    card(100, "CARD_100"), card(200, "CARD_200")]
        engine._atomic_write(self.cache / "cards.ruRU.json", json.dumps(metadata).encode())
        for item in metadata[1:]:
            path = self.cache / "images" / "ruRU" / (item["id"] + ".png")
            engine._atomic_write(path, png())
            engine._mark_image(path, local_library.image_revision(item))
        with patch.object(engine, "_download", side_effect=AssertionError("No network allowed")):
            result = engine.generate(code([(100, 2), (200, 1)]))
        self.assertEqual(result["cardCount"], 3)
        self.assertEqual(result["dust"], 120)
        self.assertEqual(result["className"], "Охотник")
        with Image.open(result["path"]) as image:
            self.assertEqual(image.size, (3000, 2344))

    def test_official_image_cache_tracks_url_version(self):
        item = card(100, "BLIZZARD_100", imageUrl="https://images.example/version-one.png")
        with patch.object(engine, "_download", return_value=png()) as download:
            first, fallback = engine._cache_image(item, "ruRU")
            self.assertFalse(fallback)
            self.assertEqual(download.call_args.args[0], item["imageUrl"])
            self.assertEqual(engine._cache_image(item, "ruRU"), (first, False))
            self.assertEqual(download.call_count, 1)
            item["imageUrl"] = "https://images.example/version-two.png"
            second, _ = engine._cache_image(item, "ruRU")
            self.assertNotEqual(second, first)
            self.assertEqual(download.call_count, 2)
            self.assertTrue(engine._image_is_valid(first))
            self.assertTrue(engine._image_is_valid(second))

    def test_official_card_without_image_cannot_fall_through_to_hsjson(self):
        item = card(100, "BLIZZARD_100", blizzard={"id": 100})
        with patch.object(engine, "_download", side_effect=AssertionError("No synthetic HSJSON URL")):
            with self.assertRaisesRegex(engine.DeckViewError, "не вернул изображение"):
                engine._cache_image(item, "ruRU")

    def test_default_source_uses_local_library_without_reading_api_keys(self):
        metadata = {31: {"id": "HERO_05", "type": "HERO", "cardClass": "HUNTER"},
                    100: card(100, "CARD_100")}
        library = Mock()
        library.get_cards.return_value = metadata
        with patch.object(engine, "_local_library", return_value=library), patch.object(
                engine, "_configured_provider", side_effect=AssertionError("No API keys needed")):
            result = engine.resolve_deck(code([(100, 2)]))
        library.get_cards.assert_called_once_with({31, 100}, "ruRU", progress=None)
        self.assertEqual((result["source"], result["cardCount"]), ("local", 2))

    def test_balance_revision_downloads_changed_art_once(self):
        item = card(100, "CARD_100", imageRevision="before-patch")
        with patch.object(engine, "_download", return_value=png()) as download:
            first, _ = engine._cache_image(item, "ruRU")
            engine._cache_image(item, "ruRU")
            self.assertEqual(download.call_count, 1)
            item["imageRevision"] = "after-patch"
            changed, _ = engine._cache_image(item, "ruRU")
            engine._cache_image(item, "ruRU")
            self.assertEqual(download.call_count, 2)
        self.assertEqual(first, changed)
        self.assertEqual(changed.with_suffix(".revision").read_text(), "after-patch")

    def test_cache_budget_prunes_oldest_after_concurrent_renders_finish(self):
        paths = [self.cache / "images" / "ruRU" / name for name in ("OLD.png", "RECENT.png")]
        for index, path in enumerate(paths):
            engine._atomic_write(path, png())
            engine._mark_image(path, "version")
            os.utime(path, (20 + index, 20 + index))
        with patch.object(engine, "IMAGE_CACHE_LIMIT", len(png())):
            with engine._image_cache_session():
                with engine._image_cache_session():
                    self.assertTrue(all(path.exists() for path in paths))
                self.assertTrue(all(path.exists() for path in paths))
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].with_suffix(".revision").exists())
        self.assertTrue(engine._image_is_valid(paths[1]))

    def test_provider_resolves_harold_without_loading_full_json(self):
        _, deck = engine.decode_deck(HAROLD_ROGUE)
        metadata = {cid: card(cid, f"BLIZZARD_{cid}") for cid, _ in deck.cards}
        metadata[930] = {"id": "BLIZZARD_930", "dbfId": 930, "type": "HERO", "cardClass": "ROGUE"}
        provider = Mock()
        provider.get_cards.return_value = metadata
        with patch.object(engine, "load_metadata", side_effect=AssertionError("Full JSON not needed")):
            result = engine.resolve_deck(HAROLD_ROGUE, provider=provider)
        provider.get_cards.assert_called_once_with(set(metadata), "ruRU")
        self.assertEqual(result["source"], "blizzard")
        self.assertEqual(result["cardCount"], 30)
        self.assertEqual({item["dbfId"]: item["count"] for item in result["cards"]}, dict(deck.cards))

    def test_missing_provider_cards_cannot_produce_a_partial_png(self):
        _, deck = engine.decode_deck(HAROLD_ROGUE)
        missing = {128389, 129340, 130112, 130761, 131456}
        metadata = {cid: card(cid, f"BLIZZARD_{cid}") for cid, _ in deck.cards if cid not in missing}
        metadata[930] = {"id": "BLIZZARD_930", "type": "HERO", "cardClass": "ROGUE"}
        provider = Mock()
        provider.get_cards.return_value = metadata
        with patch.object(engine, "_cache_image", side_effect=AssertionError("No partial images")):
            with self.assertRaises(engine.DeckViewError) as error:
                engine.generate(HAROLD_ROGUE, provider=provider)
        self.assertTrue(all(str(cid) in str(error.exception) for cid in missing))
        self.assertEqual(list(self.generated.glob("*.png")), [])

    def test_tracker_metadata_and_images_render_without_network(self):
        metadata = {31: {"id": "HERO_05", "dbfId": 31, "type": "HERO", "cardClass": "HUNTER"},
                    100: card(100, "TRACKER_100"), 200: card(200, "TRACKER_200")}
        local = self.cache / "tracker.png"
        engine._atomic_write(local, png())
        target = self.generated / "tracker-export"
        with patch.object(engine, "load_metadata", side_effect=AssertionError("Tracker owns data")), \
             patch.object(engine, "_download", side_effect=AssertionError("Tracker owns images")):
            result = engine.generate(code([(100, 2), (200, 1)]), metadata=metadata,
                                     image_paths={"TRACKER_100": local, "TRACKER_200": local}, output_dir=target)
        self.assertEqual(result["source"], "tracker")
        self.assertEqual(Path(result["path"]).parent, target)
        self.assertEqual((result["cardCount"], result["dust"]), (3, 120))
        with Image.open(result["path"]) as picture:
            self.assertEqual(picture.size, (3000, 2344))

    def test_official_api_png_uses_returned_image_urls(self):
        from blizzard_provider import normalize_card
        metadata = {cid: normalize_card({"id": cid, "name": f"Card {cid}", "classId": 3,
                        "cardTypeId": 4, "manaCost": 2, "collectible": 1, "rarityId": 3,
                        "image": f"https://images.example/{cid}.png"}, cid) for cid in (100, 200)}
        metadata[31] = normalize_card({"id": 31, "classId": 3, "cardTypeId": 3}, 31)
        provider = Mock()
        provider.get_cards.return_value = metadata
        with patch.object(engine, "load_metadata", side_effect=AssertionError("Full JSON not needed")), \
             patch.object(engine, "_download", return_value=png()) as download:
            result = engine.generate(code([(100, 2), (200, 1)]), provider=provider)
        self.assertEqual({call.args[0] for call in download.call_args_list},
                         {"https://images.example/100.png", "https://images.example/200.png"})
        self.assertEqual((result["source"], result["cardCount"], result["dust"]), ("blizzard", 3, 300))
        self.assertTrue(Path(result["path"]).is_file())


class BlizzardConfigurationTests(unittest.TestCase):
    def test_config_does_not_require_restart_and_rejects_malformed_fields(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(engine, "BASE_DIR", Path(directory)), \
             patch.dict(os.environ, {}, clear=True), patch.object(engine, "_BLIZZARD_PROVIDERS", {}), \
             patch("blizzard_provider.BlizzardProvider") as provider:
            self.assertIsNone(engine._configured_provider())
            settings = Path(directory) / "blizzard.local.json"
            settings.write_text(json.dumps({"clientId": "client", "clientSecret": "secret", "region": "eu"}))
            self.assertIs(engine._configured_provider(), provider.return_value)
            engine._configured_provider()
            provider.assert_called_once_with("client", "secret", region="eu")
            for invalid in ({"clientId": ["bad"], "clientSecret": "secret"},
                            {"clientId": 0, "clientSecret": False}):
                settings.write_text(json.dumps(invalid))
                with self.subTest(invalid=invalid), self.assertRaisesRegex(engine.DeckViewError, "Неверные настройки"):
                    engine._configured_provider()


if __name__ == "__main__":
    unittest.main()
