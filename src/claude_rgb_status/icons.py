"""Tray icon images: one colored badge per status, drawn with Pillow."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

from .rgb.base import parse_hex

SIZE = 64
OUTLINE = (40, 40, 40, 255)


@lru_cache(maxsize=64)
def badge(color: str, style: str = "solid", paused: bool = False) -> Image.Image:
    """style: "solid" (working/finished/error), "ring" (waiting for you), "hollow" (idle/unknown)."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rgb = parse_hex(color) + (255,)
    if paused:
        draw.ellipse((4, 4, SIZE - 5, SIZE - 5), fill=(150, 150, 150, 255), outline=OUTLINE, width=3)
        for x in (22, 36):
            draw.rectangle((x, 18, x + 6, SIZE - 19), fill=(255, 255, 255, 255))
        return img
    if style == "ring":
        draw.ellipse((2, 2, SIZE - 3, SIZE - 3), fill=rgb, outline=OUTLINE, width=2)
        draw.ellipse((14, 14, SIZE - 15, SIZE - 15), fill=(255, 255, 255, 255), outline=OUTLINE, width=2)
        draw.ellipse((22, 22, SIZE - 23, SIZE - 23), fill=rgb)
    elif style == "hollow":
        draw.ellipse((5, 5, SIZE - 6, SIZE - 6), fill=(60, 60, 60, 160), outline=rgb, width=9)
    else:
        draw.ellipse((4, 4, SIZE - 5, SIZE - 5), fill=rgb, outline=OUTLINE, width=3)
    return img


def for_state(state: str, color: str, pulse: bool, paused: bool) -> Image.Image:
    if paused:
        return badge(color, paused=True)
    if state in ("idle", "unknown"):
        return badge(color, "hollow")
    return badge(color, "ring" if pulse else "solid")
