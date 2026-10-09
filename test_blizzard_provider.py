"""Official API adapter checks without real credentials or remote requests."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import requests

from blizzard_provider import BlizzardProvider, BlizzardProviderError, TOKEN_URL, normalize_card


def response(status=200, data=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data
    return result


def token(value="token-one", expiry=3600):
    return response(data={"access_token": value, "expires_in": expiry})


def card(dbf_id=39046):
    return {"id": dbf_id, "collectible": 1, "slug": f"{dbf_id}-example",
            "classId": 12, "multiClassIds": [], "cardTypeId": 4,
            "cardSetId": 21, "rarityId": 5, "artistName": "Artist",
            "health": 4, "attack": 2, "manaCost": 2, "name": "Example card",
            "text": "Card text", "image": "https://images.example/card.png",
            "futureField": {"nested": [1, 2]}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.post.return_value = token()
        self.session.get.return_value = response(data=card())
        self.provider = BlizzardProvider("test-client", "test-secret", session=self.session)
        self.sleep = patch("blizzard_provider.time.sleep")
        self.sleep_mock = self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_oauth_uses_basic_auth_and_bearer_never_url(self):
        result = self.provider.get_cards([39046, 39046], "ruRU")
        self.assertEqual(result[39046]["dbfId"], 39046)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.get.call_count, 1)
        args, kwargs = self.session.post.call_args
        self.assertEqual(args, (TOKEN_URL,))
        self.assertEqual(kwargs["auth"], ("test-client", "test-secret"))
        self.assertEqual(kwargs["data"], {"grant_type": "client_credentials"})
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, ("https://eu.api.blizzard.com/hearthstone/cards/39046",))
        self.assertEqual(kwargs["params"], {"locale": "ru_RU"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-one")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("access_token", kwargs["params"])

    def test_normalization_keeps_identity_complete_source_and_game_fields(self):
        original = card()
        original.update(runeCost={"blood": 2}, isZilliaxCosmeticModule=True)
        result = normalize_card(original, 39046)
        self.assertEqual(result["id"], "BLIZZARD_39046")
        self.assertEqual((result["cost"], result["cardClass"], result["rarity"], result["type"]),
                         (2, "NEUTRAL", "LEGENDARY", "MINION"))
        self.assertTrue(result["collectible"])
        self.assertTrue(result["hideCost"])
        self.assertEqual(result["runeCost"], {"blood": 2})
        self.assertEqual(result["imageUrl"], original["image"])
        self.assertEqual(result["blizzard"], original)
        original["futureField"]["nested"].append(3)
        self.assertEqual(result["blizzard"]["futureField"]["nested"], [1, 2])
        location = dict(card(100), cardTypeId=39, classId=14, rarityId=3, cardSetId=1637)
        result = normalize_card(location, 100)
        self.assertEqual((result["type"], result["cardClass"], result["rarity"], result["set"]),
                         ("LOCATION", "DEMONHUNTER", "RARE", "CORE"))

    def test_symbolic_id_only_when_dbf_identity_matches(self):
        source = dict(card(), id="EX1_001", dbfId=39046)
        self.assertEqual(normalize_card(source, 39046)["id"], "EX1_001")
        for incorrect in (dict(card(), id=39047), dict(source, dbfId=999),
                          dict(source, id="../unsafe"), {"error": {"status": 200}}):
            with self.subTest(incorrect=incorrect), self.assertRaises(BlizzardProviderError):
                normalize_card(incorrect, 39046)

    def test_official_free_and_common_rarities_remain_distinct(self):
        # Live gallery: Fireball (315) is FREE/2, Shadow Dance (129340) COMMON/1.
        basic = normalize_card(dict(card(315), rarityId=2), 315)
        common = normalize_card(dict(card(129340), rarityId=1), 129340)
        self.assertEqual(basic["rarity"], "FREE")
        self.assertEqual(common["rarity"], "COMMON")
        crafting_costs = {"FREE": 0, "COMMON": 40, "RARE": 100,
                          "EPIC": 400, "LEGENDARY": 1600}
        self.assertEqual(crafting_costs[basic["rarity"]], 0)
        self.assertEqual(crafting_costs[common["rarity"]], 40)

    def test_primary_official_examples_keep_class_and_card_type(self):
        # Official Blizzard forum examples: Dr. Boom, Mad Genius has classId 10
        # and cardTypeId 3; Nat, the Darkfisher has classId 12/cardTypeId 4.
        boom = normalize_card(dict(card(48146), classId=10, cardTypeId=3), 48146)
        nat = normalize_card(card(), 39046)
        self.assertEqual((boom["cardClass"], boom["type"]), ("WARRIOR", "HERO"))
        self.assertEqual((nat["cardClass"], nat["type"]), ("NEUTRAL", "MINION"))

    def test_token_cached_until_expiry(self):
        with patch("blizzard_provider.time.monotonic", return_value=100):
            self.provider.get_cards([39046], "enUS")
            self.provider.get_cards([39046], "en_US")
        self.assertEqual(self.session.post.call_count, 1)
        with patch("blizzard_provider.time.monotonic", return_value=4000):
            self.provider.get_cards([39046], "enUS")
        self.assertEqual(self.session.post.call_count, 2)

    def test_token_lock_prevents_parallel_duplicate_authorization(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.provider._access_token(), range(8)))
        self.assertEqual(values, ["token-one"] * 8)
        self.assertEqual(self.session.post.call_count, 1)

    def test_401_renews_once_and_uses_new_header(self):
        self.session.post.side_effect = [token(), token("token-two")]
        self.session.get.side_effect = [response(401), response(data=card())]
        self.assertIn(39046, self.provider.get_cards([39046]))
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["Authorization"], "Bearer token-two")
        self.session.get.side_effect = [response(401), response(401)]
        self.session.post.side_effect = None
        with self.assertRaisesRegex(BlizzardProviderError, "отклонил"):
            self.provider.get_cards([39046])
        self.assertEqual(self.session.get.call_count, 4)

    def test_429_and_server_failures_have_bounded_retries(self):
        self.session.get.side_effect = [response(429, headers={"Retry-After": "1000"}),
                                        response(503), response(data=card())]
        self.assertIn(39046, self.provider.get_cards([39046]))
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.sleep_mock.call_args_list[0].args, (2.0,))
        self.session.get.side_effect = [response(503)] * 3
        with self.assertRaisesRegex(BlizzardProviderError, "временно недоступен"):
            self.provider.get_cards([39046])
        self.assertEqual(self.session.get.call_count, 6)

    def test_404_is_missing_without_substitution_or_retries(self):
        self.session.get.side_effect = [response(404), response(data=card())]
        self.assertEqual(set(self.provider.get_cards([111, 39046])), {39046})
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args_list[0].args[0].split("/")[-1], "111")
        self.assertFalse(self.sleep_mock.called)

    def test_authentication_and_network_errors_do_not_expose_credentials(self):
        self.session.post.return_value = response(401, data={"error": "test-secret"})
        with self.assertRaisesRegex(BlizzardProviderError, "не принял") as failure:
            self.provider.get_cards([39046])
        self.assertNotIn("test-secret", str(failure.exception))
        self.assertEqual(self.session.post.call_count, 1)
        self.session.post.side_effect = requests.ConnectionError("test-secret token-one")
        with self.assertRaises(BlizzardProviderError) as failure:
            self.provider.get_cards([39046])
        self.assertNotIn("test-secret", str(failure.exception))
        self.assertEqual(self.session.post.call_count, 4)

    def test_env_credentials_and_us_region(self):
        with patch.dict(os.environ, {"BLIZZARD_CLIENT_ID": " env-id ",
                                     "BLIZZARD_CLIENT_SECRET": " env-secret "}):
            provider = BlizzardProvider(region="us", session=self.session)
            provider.get_cards([39046], "enUS")
        self.assertEqual(self.session.post.call_args.kwargs["auth"], ("env-id", "env-secret"))
        self.assertTrue(self.session.get.call_args.args[0].startswith("https://us.api.blizzard.com/"))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(BlizzardProviderError, "BLIZZARD_CLIENT_ID"):
            BlizzardProvider()

    def test_malformed_constructor_inputs_have_safe_errors(self):
        for client_id, secret in ((123, "secret"), ("client", []),
                                  ({"value": "client"}, "secret"), (True, "secret")):
            with self.subTest(client_id=client_id, secret=secret), self.assertRaisesRegex(
                    BlizzardProviderError, "должны быть строками"):
                BlizzardProvider(client_id, secret, session=self.session)
        for region in ([], {}, 1, True, None, "bad"):
            with self.subTest(region=region), self.assertRaisesRegex(BlizzardProviderError, "регион"):
                BlizzardProvider("client", "secret", region=region, session=self.session)
        for secret in ("a\nb", "a\rb", "a\x00b"):
            with self.subTest(secret=secret), self.assertRaisesRegex(BlizzardProviderError, "символы"):
                BlizzardProvider("client", secret, session=self.session)
        self.assertFalse(self.session.post.called)

    def test_invalid_inputs_and_responses_are_reported_without_network_guesses(self):
        for ids, locale in (([True], "ruRU"), (["39046"], "ruRU"), ([0], "ruRU"), ([1], "bad")):
            with self.subTest(ids=ids, locale=locale), self.assertRaises(BlizzardProviderError):
                self.provider.get_cards(ids, locale)
        self.assertFalse(self.session.post.called)
        self.assertEqual(self.provider.get_cards([]), {})
        self.session.post.return_value = response(data={"access_token": "token", "expires_in": "NaN"})
        with self.assertRaisesRegex(BlizzardProviderError, "ответ авторизации"):
            self.provider.get_cards([39046])
        self.session.post.return_value = token()
        self.session.get.return_value = response(data={"id": 39047})
        with self.assertRaisesRegex(BlizzardProviderError, "другую карту"):
            self.provider.get_cards([39046])


if __name__ == "__main__":
    unittest.main()
