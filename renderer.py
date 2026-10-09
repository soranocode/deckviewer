"""Deck montage adapted from hextract/HearthstoneDeckView (MIT)."""
from __future__ import annotations

import math
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent


def _tint_sideboard(image):
    red, green, blue, alpha = image.split()
    red = ImageChops.add(red, Image.new("L", image.size, 100))
    green = ImageChops.add(green, Image.new("L", image.size, 50))
    blue = ImageChops.add(blue, Image.new("L", image.size, 50))
    return Image.merge("RGBA", (red, green, blue, alpha))


def _layout(count):
    # Original deckview grid sizes, reduced only when extra sideboard cards
    # would overlap the class footer or leave the canvas.
    for size in (500, 428, 375, 300, 260, 220, 180, 150, 125, 100):
        columns = 3000 // size
        rows = math.ceil(count / columns)
        # Reserve a complete count badge below the card and space before the next row.
        cell_height = round(size * 1.41) + round(size * .21)
        if rows * cell_height <= 2090:
            return size, columns, cell_height
    raise ValueError("Too many cards to render")


def _card_area_height(size):
    return round(size * 1.41) - 14


def _body_font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf", str(BASE_DIR / "belwe.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _configure_zilliax(image, card):
    # Static upstream composites use old costs and English text. Compose the
    # current, localized modules directly on the blank Zilliax card instead.
    overlay = Image.new("RGBA", (512, 776))
    draw = ImageDraw.Draw(overlay)
    stats = ImageFont.truetype(str(BASE_DIR / "belwe.ttf"), 86)
    for value, position in ((card["cost"], (77, 136)),
                            (card["attack"], (69, 650)),
                            (card["health"], (444, 650))):
        draw.text(position, str(value), fill="white", font=stats, anchor="mm",
                  stroke_fill="black", stroke_width=5)
    descriptions = [re.sub(r"<[^>]+>|\[x\]", "", item.get("text", "")).replace("\xa0", " ")
                    for item in card["sideboardCards"]]
    for size in range(28, 18, -1):
        font = _body_font(size)
        lines = []
        for description in descriptions:
            line = ""
            for word in description.split():
                candidate = (line + " " + word).strip()
                if line and font.getlength(candidate) > 333:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            if line:
                lines.append(line)
        line_height = size + 4
        if len(lines) * line_height <= 151:
            break
    draw.rounded_rectangle((87, 478, 428, 637), radius=8, fill=(188, 175, 145, 255))
    top = 482 + (151 - len(lines) * line_height) // 2
    for line in lines:
        draw.text((257, top), line, fill=(27, 26, 24), font=font, anchor="mt")
        top += line_height
    if overlay.size != image.size:
        overlay = overlay.resize(image.size, Image.Resampling.LANCZOS)
    image.alpha_composite(overlay)


def _alpha_components(alpha):
    """Find connected opaque regions using row runs instead of pixel loops."""
    width, height = alpha.size
    pixels = alpha.tobytes()
    parents, runs, previous = [], [], []

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for row in range(height):
        current = []
        for match in re.finditer(rb"[^\x00]+", pixels[row * width:(row + 1) * width]):
            left, right = match.span()
            index = len(parents)
            parents.append(index)
            runs.append((row, left, right))
            # Eight-connected regions include diagonally attached ornaments.
            for prior_left, prior_right, prior_index in previous:
                if prior_right < left:
                    continue
                if prior_left > right:
                    break
                parents[root(index)] = root(prior_index)
            current.append((left, right, index))
        previous = current
    components = {}
    for index, (row, left, right) in enumerate(runs):
        key = root(index)
        if key not in components:
            components[key] = {"area": 0, "bbox": [left, row, right, row + 1], "runs": []}
        component = components[key]
        component["area"] += right - left
        component["runs"].append((row, left, right))
        bounds = component["bbox"]
        bounds[0], bounds[1] = min(bounds[0], left), min(bounds[1], row)
        bounds[2], bounds[3] = max(bounds[2], right), max(bounds[3], row + 1)
    return list(components.values())


def _remove_black_background(image):
    corners = [(0, 0), (image.width - 1, 0), (0, image.height - 1), (image.width - 1, image.height - 1)]
    # Some HearthstoneJSON renders have an opaque black outer background.
    # Flood only the outside region, preserving internal black art and text.
    for point in corners:
        if image.getpixel(point) == (0, 0, 0, 255):
            ImageDraw.floodfill(image, point, (0, 0, 0, 0), thresh=2)
    alpha = image.getchannel("A")
    bounds = alpha.getbbox()
    # The debug footer also occurs on otherwise transparent renders. It is a
    # detached, small text box at a bottom corner, below the main card region.
    # Normal transparent cards need no connected-region scan.
    if not bounds or bounds[3] != image.height or not (bounds[0] == 0 or bounds[2] == image.width):
        return image
    components = _alpha_components(alpha)
    if not components:
        return image
    main = max(components, key=lambda item: item["area"])
    draw = ImageDraw.Draw(alpha)
    for component in components:
        left, top, right, bottom = component["bbox"]
        is_small_corner_footer = (
            component is not main
            and top > main["bbox"][3]
            and bottom == image.height
            and (left == 0 or right == image.width)
            and component["area"] <= main["area"] * .05
            and (right - left >= 3 * (bottom - top) or right - left <= 2 or component["area"] <= 8)
        )
        if is_small_corner_footer:
            for row, run_left, run_right in component["runs"]:
                draw.line((run_left, row, run_right - 1, row), fill=0)
    image.putalpha(alpha)
    return image


def _card_image(card, images):
    with Image.open(images[card["id"]]) as source:
        image = source.convert("RGBA")
    image = _remove_black_background(image)
    if card.get("zilliax"):
        _configure_zilliax(image, card)
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    return _tint_sideboard(image) if card.get("sideboard") else image


def _fit_card(image, width, height):
    # Both sources include different transparent margins and resolutions.
    # _card_image trims those margins; fill the same cell even for smaller art.
    scale = min(width / image.width, height / image.height)
    dimensions = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(dimensions, Image.Resampling.LANCZOS)


def _count_badge_top(card_image, badge_image):
    # Attack/health circles can extend below the frame. Attach the badge to
    # the central frame under the type plaque rather than to those side circles.
    # Account for the badge's own transparent top margin as well. Match the
    # two visible contours so their edges touch. Ignore faint resampling fringes
    # which would otherwise leave a visible gap between the actual borders.
    left = (card_image.width - badge_image.width) // 2
    card_alpha = card_image.getchannel("A").tobytes()
    badge_alpha = badge_image.getchannel("A").tobytes()
    positions = []
    for badge_x in range(badge_image.width):
        card_x = left + badge_x
        if not 0 <= card_x < card_image.width:
            continue
        top = next((row for row in range(badge_image.height)
                    if badge_alpha[row * badge_image.width + badge_x] >= 128), None)
        bottom = next((row for row in range(card_image.height - 1, -1, -1)
                       if card_alpha[row * card_image.width + card_x] >= 128), None)
        if top is not None and bottom is not None:
            positions.append(bottom + 1 - top)
    # Join the antialiased outer borders by two pixels; the type plaque and
    # all text remain above this join.
    return max(0, max(positions) - 2) if positions else card_image.height


def _blank_footer_corner(image):
    # Upstream backgrounds contain a baked-in title and URL. Continue their
    # smooth colour gradient from clean top/left edges to leave writing space.
    left, top = 2080, 2150
    box = (left, top, image.width, image.height)
    size = (image.width - left, image.height - top)
    horizontal = image.crop((left, top - 1, image.width, top)).resize(size)
    vertical = image.crop((left - 1, top, left, image.height)).resize(size)
    anchor = image.getpixel((left - 1, top - 1))
    channels = [ImageChops.add(horizontal.getchannel(channel), vertical.getchannel(channel),
                              offset=-anchor[index])
                for index, channel in enumerate("RGB")]
    clean = Image.merge("RGBA", (*channels, Image.new("L", size, 255)))
    mask = Image.new("L", size, 255)
    blend = ImageDraw.Draw(mask)
    for offset in range(16):
        blend.line((offset, 0, offset, size[1] - 1), fill=round(offset * 255 / 16))
    for offset in range(8):
        blend.line((0, offset, size[0] - 1, offset), fill=round(offset * 255 / 8))
    image.paste(clean, box, mask)


def render_deck(response, images):
    ordered = []
    for card in sorted(response["cards"], key=lambda item: (item.get("cost", 0), item.get("name", ""))):
        ordered.append(card)
        ordered.extend(sorted(card.get("sideboardCards", []), key=lambda item: (item.get("cost", 0), item.get("name", ""))))
    size, columns, cell_height = _layout(len(ordered))
    with Image.open(BASE_DIR / "backs" / f"{response['classId']}.png") as background:
        image = background.convert("RGBA")
    # The upstream Demon Hunter background is six pixels taller than the rest.
    if image.size != (3000, 2344):
        image = image.resize((3000, 2344), Image.Resampling.LANCZOS)
    _blank_footer_corner(image)
    for index, card in enumerate(ordered):
        x, y = (index % columns) * size, (index // columns) * cell_height
        rendered = _card_image(card, images)
        rendered = _fit_card(rendered, size, _card_area_height(size))
        left = x + (size - rendered.width) // 2
        image.paste(rendered, (left, y), rendered)
        count = card["count"]
        if count >= 2:
            with Image.open(BASE_DIR / "labels" / f"x{min(count, 9)}.png") as source:
                label = source.convert("RGBA")
                label.thumbnail((round(size * .38), round(size * .21)), Image.Resampling.LANCZOS)
            label_y = y + _count_badge_top(rendered, label)
            image.paste(label, (x + (size - label.width) // 2, label_y), label)
            if count > 9:
                draw = ImageDraw.Draw(image)
                font = ImageFont.truetype(str(BASE_DIR / "belwe.ttf"), max(18, size // 12))
                draw.text((x + size // 2, label_y), f"x{count}", fill="white", font=font,
                          anchor="mt", stroke_fill="black", stroke_width=2)
    font = ImageFont.truetype(str(BASE_DIR / "belwe.ttf"), 140)
    draw = ImageDraw.Draw(image)
    draw.text((170, 2150), str(response["dust"]), fill="white", font=font,
              stroke_fill="black", stroke_width=5)
    if response["classId"] == 1:
        offset = 0
        for rune_name, count in response.get("runeSlots", {}).items():
            for _ in range(count):
                with Image.open(BASE_DIR / "death_knight" / f"{rune_name}.png") as source:
                    rune = source.convert("RGBA").resize((200, 200), Image.Resampling.LANCZOS)
                image.paste(rune, (1200 + offset, 2120), rune)
                offset += 200
    return image
