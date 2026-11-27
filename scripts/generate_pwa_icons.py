#!/usr/bin/env python3
"""Draw the GeoRiskIntel home-screen icons: a navy radar globe in emerald."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static" / "icons"
NAVY = (11, 17, 32, 255)
GLOBE = (14, 36, 48, 255)
EMERALD = (16, 185, 129, 255)
MINT = (110, 231, 183, 255)
PALE = (236, 253, 245, 255)


def _ring(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, color: tuple, width: int) -> None:
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(box, outline=color, width=width)


def _mask_circle(layer: Image.Image, cx: float, cy: float, radius: float) -> Image.Image:
    mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(mask).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
    red, green, blue, alpha = layer.split()
    layer.putalpha(ImageChops.multiply(alpha, mask))
    return layer


def _globe(size: int, cx: float, cy: float, radius: float) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=GLOBE)
    stroke = max(2, size // 180)
    for fraction in (0.28, 0.55, 0.82):
        rx = radius * fraction
        draw.ellipse((cx - rx, cy - radius, cx + rx, cy + radius), outline=MINT, width=stroke)
    for fraction in (-0.62, -0.32, 0.0, 0.32, 0.62):
        half = math.sqrt(max(0.0, 1 - fraction * fraction)) * radius
        ry = max(radius * 0.075, stroke * 2)
        y = cy + radius * fraction
        draw.ellipse((cx - half, y - ry, cx + half, y + ry), outline=(*MINT[:3], 210), width=stroke)
    return _mask_circle(layer, cx, cy, radius - stroke)


def _sweep(size: int, cx: float, cy: float, radius: float) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    # Pillow measures pieslice degrees clockwise from 3 o'clock.
    for step in range(16):
        alpha = 12 + step * 8
        start = 250 + step * 4
        draw.pieslice(box, start=start, end=start + 6, fill=(16, 185, 129, alpha))
    angle = math.radians(250 + 16 * 4)
    # Pillow's y axis grows downward, and its pieslice angles run clockwise.
    tip_x = cx + math.cos(angle) * radius
    tip_y = cy + math.sin(angle) * radius
    draw.line((cx, cy, tip_x, tip_y), fill=PALE, width=max(2, size // 160))
    return _mask_circle(layer, cx, cy, radius)


def draw_icon(size: int) -> Image.Image:
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), NAVY)
    draw = ImageDraw.Draw(image)
    cx = cy = canvas / 2
    outer = canvas * 0.36
    globe = canvas * 0.27
    stroke = max(3, canvas // 140)
    for index, fraction in enumerate((1.0, 0.78, 0.56)):
        alpha = 230 if index == 0 else 150
        _ring(draw, cx, cy, outer * fraction, (*EMERALD[:3], alpha), stroke if index == 0 else max(2, stroke - 1))
    tick_inner = outer * 1.06
    tick_outer = outer * 1.16
    for degrees in (0, 90, 180, 270):
        theta = math.radians(degrees)
        draw.line(
            (
                cx + math.cos(theta) * tick_inner,
                cy + math.sin(theta) * tick_inner,
                cx + math.cos(theta) * tick_outer,
                cy + math.sin(theta) * tick_outer,
            ),
            fill=EMERALD,
            width=stroke,
        )
    image = Image.alpha_composite(image, _globe(canvas, cx, cy, globe))
    image = Image.alpha_composite(image, _sweep(canvas, cx, cy, globe * 0.98))
    draw = ImageDraw.Draw(image)
    _ring(draw, cx, cy, globe, EMERALD, stroke + 1)
    dot = canvas * 0.018
    halo = dot * 2.4
    draw.ellipse((cx - halo, cy - halo, cx + halo, cy + halo), fill=(16, 185, 129, 90))
    draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=PALE)
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize((size, size), resample).convert("RGB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = {
        192: OUT / "icon-192.png",
        512: OUT / "icon-512.png",
        180: OUT / "apple-touch-icon.png",
    }
    for size, path in targets.items():
        draw_icon(size).save(path, format="PNG", optimize=True)
        print(path)


if __name__ == "__main__":
    main()
