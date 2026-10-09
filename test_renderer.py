"""Artifact cleanup regressions, including real cached Hearthstone renders."""
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

import renderer


def fixture(opaque=False):
    image = Image.new("RGBA", (120, 160), (0, 0, 0, 255 if opaque else 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 10, 99, 130), fill=(190, 160, 120, 255))
    draw.rectangle((45, 55, 69, 70), fill=(0, 0, 0, 255))
    # Debug text box at the corner, plus a detached card ornament. The latter
    # must survive even though it appears below the principal card frame.
    draw.rectangle((0, 150, 40, 159), fill=(25, 25, 25, 255))
    draw.rectangle((3, 152, 35, 154), fill=(255, 255, 255, 255))
    draw.line((119, 145, 119, 159), fill=(30, 30, 30, 255))
    draw.rectangle((63, 147, 67, 151), fill=(230, 200, 20, 255))
    return image


class ArtifactCleanupTests(unittest.TestCase):
    def test_count_badge_is_below_art_without_covering_text_or_stats(self):
        card = {"id": "CARD", "cost": 1, "name": "Card", "count": 1}
        source = Image.new("RGBA", (180, 240), (210, 110, 80, 255))
        with patch.object(renderer, "_card_image", return_value=source):
            single = renderer.render_deck({"cards": [card], "classId": 7, "dust": 0}, {})
            doubled = renderer.render_deck({"cards": [dict(card, count=2)], "classId": 7, "dust": 0}, {})
        changes = ImageChops.difference(single.convert("RGB"), doubled.convert("RGB")).getbbox()
        self.assertIsNotNone(changes)
        self.assertGreaterEqual(changes[1], 665)
        self.assertLessEqual(changes[3], renderer._layout(1)[2])
        self.assertEqual(single.crop((0, 0, 500, 665)).tobytes(), doubled.crop((0, 0, 500, 665)).tobytes())

    def test_badge_touches_type_frame_instead_of_lower_stat_circles(self):
        source = Image.new("RGBA", (180, 240))
        draw = ImageDraw.Draw(source)
        draw.rectangle((0, 0, 179, 209), fill=(210, 110, 80, 255))
        draw.rectangle((0, 210, 35, 239), fill=(250, 230, 80, 255))
        draw.rectangle((144, 210, 179, 239), fill=(250, 80, 80, 255))
        draw.rectangle((57, 190, 122, 205), fill=(240, 240, 220, 255))  # type plaque
        badge = Image.new("RGBA", (68, 20), (150, 100, 80, 255))
        self.assertGreater(renderer._count_badge_top(source, badge), 205)
        self.assertLessEqual(renderer._count_badge_top(source, badge), 210)
        padded_badge = Image.new("RGBA", (68, 30))
        padded_badge.paste(badge, (0, 10))
        self.assertEqual(renderer._count_badge_top(source, padded_badge) + 10,
                         renderer._count_badge_top(source, badge))
        self.assertEqual(source.getchannel("A").getbbox()[3], 240)

    def test_count_badges_and_cards_fit_above_footer_on_dense_decks(self):
        import math
        for count in (1, 17, 30, 40, 50, 100):
            with self.subTest(count=count):
                width, columns, height = renderer._layout(count)
                self.assertLessEqual(math.ceil(count / columns) * height, 2090)
                self.assertLessEqual(renderer._card_area_height(width) + 6 + round(width * .21), height)

    def test_mixed_source_resolutions_fill_equal_card_cells(self):
        cards = [{"id": "SMALL", "cost": 1, "name": "A", "count": 1},
                 {"id": "LARGE", "cost": 1, "name": "B", "count": 1}]
        sources = [Image.new("RGBA", (180, 240), (210, 110, 80, 255)),
                   Image.new("RGBA", (360, 480), (210, 110, 80, 255))]
        with patch.object(renderer, "_card_image", side_effect=sources):
            result = renderer.render_deck({"cards": cards, "classId": 7, "dust": 0}, {})
        # The same card at half resolution must occupy the same frame in the
        # montage, including the bottom of its text/stats area.
        first = result.crop((0, 0, 500, 667))
        second = result.crop((500, 0, 1000, 667))
        self.assertEqual(first.getpixel((250, 660)), (210, 110, 80, 255))
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_zilliax_stats_fit_images_at_half_resolution(self):
        image = Image.new("RGBA", (256, 388), (190, 160, 120, 255))
        original = image.copy()
        card = {"cost": 7, "attack": 3, "health": 5, "sideboardCards": [{"text": "Module effect"}]}
        renderer._configure_zilliax(image, card)
        # Cost, attack and health all stay in their frame positions.
        for box in ((20, 45, 55, 90), (15, 303, 55, 348), (203, 303, 243, 348)):
            with self.subTest(box=box):
                self.assertIsNotNone(ImageChops.difference(image.crop(box).convert("RGB"),
                    original.crop(box).convert("RGB")).getbbox())
        self.assertEqual(image.getpixel((0, 0)), original.getpixel((0, 0)))

    def test_transparent_corner_debug_footer_removed_without_cutting_ornament(self):
        original = fixture()
        clean = renderer._remove_black_background(original.copy())
        self.assertEqual(clean.size, original.size)
        self.assertEqual(clean.getpixel((20, 159))[3], 0)
        self.assertEqual(clean.getpixel((119, 159))[3], 0)
        self.assertEqual(clean.getpixel((65, 149)), original.getpixel((65, 149)))
        self.assertEqual(clean.getpixel((50, 60)), (0, 0, 0, 255))
        self.assertEqual(original.crop((20, 10, 100, 131)).tobytes(), clean.crop((20, 10, 100, 131)).tobytes())

    def test_opaque_outer_black_removed_and_internal_black_preserved(self):
        original = fixture(opaque=True)
        clean = renderer._remove_black_background(original.copy())
        self.assertEqual(clean.getpixel((0, 0))[3], 0)
        self.assertEqual(clean.getpixel((110, 140))[3], 0)
        self.assertEqual(clean.getpixel((20, 159))[3], 0)
        self.assertEqual(clean.getpixel((50, 60)), (0, 0, 0, 255))
        self.assertEqual(clean.getpixel((65, 149)), original.getpixel((65, 149)))

    def test_transparent_frame_without_footer_is_unchanged(self):
        image = Image.new("RGBA", (120, 160))
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 20, 105, 155), fill=(190, 160, 120, 255))
        draw.rectangle((65, 158, 70, 159), fill=(230, 200, 20, 255))
        clean = renderer._remove_black_background(image.copy())
        self.assertEqual(image.tobytes(), clean.tobytes())

    def test_diagonal_card_component_stays_connected(self):
        image = Image.new("L", (4, 4))
        for point in ((0, 0), (1, 1), (2, 2)):
            image.putpixel(point, 255)
        components = renderer._alpha_components(image)
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["area"], 3)


class CachedRenderTests(unittest.TestCase):
    def test_footer_title_removed_for_all_classes_without_changing_cards_area(self):
        for class_id in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14):
            with self.subTest(class_id=class_id), Image.open(renderer.BASE_DIR / "backs" / f"{class_id}.png") as source:
                image = source.convert("RGBA").resize((3000, 2344))
                before = image.crop((0, 0, 3000, 2090)).tobytes()
                renderer._blank_footer_corner(image)
                self.assertEqual(image.crop((0, 0, 3000, 2090)).tobytes(), before)
                corner = image.crop((2120, 2170, 3000, 2344)).convert("RGB")
                self.assertLess(max(high for low, high in corner.getextrema()), 230)

    def test_demon_hunter_background_has_standard_output_dimensions(self):
        image = renderer.render_deck({"cards": [], "classId": 14, "dust": 0}, {})
        self.assertEqual(image.size, (3000, 2344))

    def test_cached_problem_cards_preserve_frame_text_and_stats(self):
        directory = renderer.BASE_DIR / "cache" / "images" / "ruRU"
        fixtures = {"TLC_365": (31, 76, 459, 682),
                    "EDR_856": (20, 37, 478, 693),
                    "WW_373": (19, 37, 478, 693)}
        found = 0
        for card_id, expected in fixtures.items():
            path = directory / (card_id + ".png")
            if not path.exists():
                continue
            found += 1
            with self.subTest(card=card_id), Image.open(path) as source:
                original = source.convert("RGBA")
                clean = renderer._remove_black_background(original.copy())
                bbox = clean.getchannel("A").getbbox()
                self.assertEqual(clean.size, original.size)
                self.assertEqual(bbox, expected)
                # Interior text and artwork must remain byte-for-byte equal.
                before, after = original.crop((100, 180, 410, 620)), clean.crop((100, 180, 410, 620))
                self.assertEqual(before.tobytes(), after.tobytes())
        if not found:
            self.skipTest("No real card renders cached yet")

    def test_cached_clean_card_types_do_not_change(self):
        directory = renderer.BASE_DIR / "cache" / "images" / "ruRU"
        found = 0
        for card_id in ("DS1_178", "CORE_CS2_028", "EX1_536", "REV_290", "TTN_737", "AV_200", "TOY_330", "ETC_080"):
            path = directory / (card_id + ".png")
            if not path.exists():
                continue
            found += 1
            with self.subTest(card=card_id), Image.open(path) as source:
                image = source.convert("RGBA")
                clean = renderer._remove_black_background(image.copy())
                self.assertEqual(clean.size, image.size)
                self.assertEqual(image.tobytes(), clean.tobytes())
        if not found:
            self.skipTest("No representative card renders cached yet")


if __name__ == "__main__":
    unittest.main()
