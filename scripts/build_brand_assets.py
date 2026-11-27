#!/usr/bin/env python3
"""Build GeoRiskGlobal header, PDF, and favicon assets from Dawn's lockup.

The source is a 1200×630 JPEG: a blue-and-gold globe, a gold divider, and the
white/gold wordmark on pure black. This script trims the black border, knocks
the background out so the navbar has no visible rectangle, and crops the globe
for the favicon and home-screen icons.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).resolve().parent / "brand" / "georiskglobal-source.jpg"
STATIC = ROOT / "static"
ICONS = STATIC / "icons"
NAVY = (6, 10, 19, 255)
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def _peak(r: int, g: int, b: int) -> int:
    return r if r >= g and r >= b else g if g >= b else b


def _floor(r: int, g: int, b: int) -> int:
    return r if r <= g and r <= b else g if g <= b else b


def _content_bbox(image: Image.Image, limit_x: int | None = None) -> tuple[int, int, int, int]:
    rgb = image.convert("RGB")
    pixels = rgb.load()
    width, height = rgb.size
    right = width if limit_x is None else min(limit_x, width)
    min_x, min_y, max_x, max_y = right, height, 0, 0
    for y in range(height):
        for x in range(right):
            r, g, b = pixels[x, y]
            if _peak(r, g, b) > 16:
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
    if max_x < min_x or max_y < min_y:
        raise SystemExit("brand source has no visible pixels")
    return min_x, min_y, max_x + 1, max_y + 1


def knockout_black(image: Image.Image) -> Image.Image:
    """Turn the pure-black field transparent and lift grey anti-aliasing.

    Coloured pixels (gold rim, blue ocean, gold wordmark) stay opaque so the
    globe is not punched full of holes. Near-grey fringe from the JPEG is
    un-premultiplied back towards white so it does not leave a dark box.
    """
    source = image.convert("RGBA")
    pixels = source.load()
    width, height = source.size
    for y in range(height):
        for x in range(width):
            r, g, b, _alpha = pixels[x, y]
            peak = _peak(r, g, b)
            chroma = peak - _floor(r, g, b)
            if chroma > 16 and peak > 18:
                pixels[x, y] = (r, g, b, 255)
                continue
            if peak <= 12:
                pixels[x, y] = (0, 0, 0, 0)
                continue
            if peak < 52:
                alpha = int((peak - 12) * 255 / 40)
                scale = 255 / peak
                pixels[x, y] = (
                    min(255, int(r * scale)),
                    min(255, int(g * scale)),
                    min(255, int(b * scale)),
                    max(0, min(255, alpha)),
                )
                continue
            pixels[x, y] = (r, g, b, 255)
    return source


def _square_crop(
    image: Image.Image,
    box: tuple[int, int, int, int],
    pad: int,
    max_right: int | None = None,
) -> Image.Image:
    left, top, right, bottom = box
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    side = max(right - left, bottom - top) + pad * 2
    half = side / 2
    if max_right is not None and cx + half > max_right:
        half = max_right - cx
    crop = (
        int(round(cx - half)),
        int(round(cy - half)),
        int(round(cx + half)),
        int(round(cy + half)),
    )
    return image.crop(crop)


def _fit(image: Image.Image, size: int, background: tuple[int, int, int, int] | None) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    scale = min(size / image.width, size / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), RESAMPLE)
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
    return canvas


def _save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    print(f"{path.relative_to(ROOT)} {image.size} {image.mode}")


def _within_width(image: Image.Image, width: int) -> Image.Image:
    if image.width <= width:
        return image
    height = max(1, int(round(image.height * (width / image.width))))
    return image.resize((width, height), RESAMPLE)


def build(source: Image.Image) -> None:
    # The gold divider sits in the empty gap just after the globe (x ≈ 356).
    globe_box = _content_bbox(source, limit_x=348)
    lockup_box = _content_bbox(source)
    pad = 18
    lockup = source.crop((
        max(0, lockup_box[0] - pad),
        max(0, lockup_box[1] - pad),
        min(source.width, lockup_box[2] + pad),
        min(source.height, lockup_box[3] + pad),
    ))
    transparent = knockout_black(lockup)
    _save_png(_within_width(transparent, 980), STATIC / "logo.png")
    _save_png(transparent, STATIC / "logo-horizontal.png")

    banner_pad_x, banner_pad_y = 36, 22
    banner = Image.new(
        "RGBA",
        (transparent.width + banner_pad_x * 2, transparent.height + banner_pad_y * 2),
        NAVY,
    )
    banner.paste(transparent, (banner_pad_x, banner_pad_y), transparent)
    _save_png(_within_width(banner, 1100).convert("RGB"), STATIC / "logo-pdf.png")

    # Stop the square before the gold divider (it starts at about x=356).
    globe = knockout_black(_square_crop(source, globe_box, pad=28, max_right=346))
    # Favicon stays transparent so a browser tab does not show a black tile.
    favicon = _fit(globe, 64, None)
    _save_png(favicon, STATIC / "favicon.png")
    ico_base = _fit(globe, 64, (0, 0, 0, 255)).convert("RGBA")
    ico_base.save(
        STATIC / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )
    print(f"static/favicon.ico {ico_base.size}")

    # Home-screen icons are opaque. Extra inset keeps the rim inside the maskable safe area.
    opaque = _fit(globe, 512, (0, 0, 0, 255))
    _save_png(opaque.resize((192, 192), RESAMPLE).convert("RGB"), ICONS / "icon-192.png")
    _save_png(opaque.convert("RGB"), ICONS / "icon-512.png")
    _save_png(opaque.resize((180, 180), RESAMPLE).convert("RGB"), ICONS / "apple-touch-icon.png")
    _save_png(opaque.resize((180, 180), RESAMPLE).convert("RGB"), STATIC / "apple-touch-icon.png")
    _save_png(opaque.resize((192, 192), RESAMPLE).convert("RGB"), STATIC / "icon-192.png")
    _save_png(opaque.resize((512, 512), RESAMPLE).convert("RGB"), STATIC / "icon-512.png")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"missing brand source: {SOURCE}")
    build(Image.open(SOURCE))


if __name__ == "__main__":
    main()
