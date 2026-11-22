#!/usr/bin/env python3
"""Write syria_governorates.geojson from real geoBoundaries ADM1 polygons.

A fresh download is preferred. The existing 189254-byte geoBoundaries file is
the fallback. Rectangles are never written over real boundaries.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "syria_governorates.geojson"
SOURCE_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/main/"
    "releaseData/gbOpen/SYR/ADM1/geoBoundaries-SYR-ADM1_simplified.geojson"
)
KNOWN_GEOBOUNDARIES_BYTES = 189254

# geoBoundaries shapeName -> English name used on the map.
NAME_MAP = {
    "Damascus": "Damascus",
    "Rural Damascus": "Rif Dimashq",
    "Aleppo": "Aleppo",
    "Idleb": "Idlib",
    "Idlib": "Idlib",
    "Lattakia": "Latakia",
    "Latakia": "Latakia",
    "Tartous": "Tartus",
    "Tartus": "Tartus",
    "Hama": "Hama",
    "Homs": "Homs",
    "Ar-Raqqa": "Raqqa",
    "Raqqa": "Raqqa",
    "Deir-ez-Zor": "Deir ez-Zor",
    "Deir ez-Zor": "Deir ez-Zor",
    "Al-Hasakeh": "Al-Hasakah",
    "Al-Hasakah": "Al-Hasakah",
    "Dar'a": "Daraa",
    "Daraa": "Daraa",
    "Quneitra": "Quneitra",
    "As-Sweida": "As-Suwayda",
    "As-Suwayda": "As-Suwayda",
}

ORDER = [
    "Damascus",
    "Rif Dimashq",
    "Aleppo",
    "Idlib",
    "Latakia",
    "Tartus",
    "Hama",
    "Homs",
    "Raqqa",
    "Deir ez-Zor",
    "Al-Hasakah",
    "Daraa",
    "Quneitra",
    "As-Suwayda",
]


def ring_lengths(geometry: dict) -> list[int]:
    coords = geometry.get("coordinates")
    kind = geometry.get("type")
    lengths: list[int] = []
    if kind == "Polygon" and isinstance(coords, list):
        lengths.extend(len(ring) for ring in coords if isinstance(ring, list))
    elif kind == "MultiPolygon" and isinstance(coords, list):
        for polygon in coords:
            if isinstance(polygon, list):
                lengths.extend(len(ring) for ring in polygon if isinstance(ring, list))
    return lengths


def is_real_boundary(geometry: dict) -> bool:
    """Reject missing geometry and simple rectangles (a closed box has 5 positions)."""
    lengths = ring_lengths(geometry)
    return bool(lengths) and max(lengths) >= 20


def canonical_name(properties: dict) -> str | None:
    raw = properties.get("shapeName") or properties.get("name") or ""
    return NAME_MAP.get(str(raw))


def normalize(payload: dict) -> dict | None:
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        return None
    by_name: dict[str, dict] = {}
    for feature in payload["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            continue
        geometry = feature.get("geometry") or {}
        if not is_real_boundary(geometry):
            return None
        properties = dict(feature.get("properties") or {})
        name = canonical_name(properties)
        if not name or name in by_name:
            return None
        properties["shapeName"] = name
        properties["name"] = name
        by_name[name] = {
            "type": "Feature",
            "properties": properties,
            "geometry": geometry,
        }
    if set(by_name) != set(ORDER):
        return None
    return {"type": "FeatureCollection", "features": [by_name[name] for name in ORDER]}


def parse_collection(raw: bytes) -> dict | None:
    if not raw or raw.lstrip().startswith(b"version https://git-lfs"):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return normalize(payload)


def download() -> bytes | None:
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "osint-map"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception as exc:
        print(f"Download failed: {exc}")
        return None


def existing_bytes() -> bytes | None:
    if not OUTPUT.exists():
        return None
    raw = OUTPUT.read_bytes()
    if len(raw) == KNOWN_GEOBOUNDARIES_BYTES:
        print(f"Using the existing {KNOWN_GEOBOUNDARIES_BYTES}-byte geoBoundaries file")
    return raw


def main() -> None:
    downloaded = download()
    collection = parse_collection(downloaded) if downloaded else None
    source = "fresh download"
    if collection is None:
        current = existing_bytes()
        collection = parse_collection(current) if current else None
        source = "existing real boundaries"
    if collection is None:
        print("No real governorate polygons were available. syria_governorates.geojson was not replaced.")
        sys.exit(1)
    OUTPUT.write_text(json.dumps(collection, ensure_ascii=False) + "\n", encoding="utf-8")
    names = [feature["properties"]["shapeName"] for feature in collection["features"]]
    print(f"Wrote {OUTPUT.name} from {source}: {', '.join(names)}")


if __name__ == "__main__":
    main()
