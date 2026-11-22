#!/usr/bin/env python3
# Live X search follows the xAI docs read on 2026-11-21:
# https://docs.x.ai/developers/tools/x-search
# https://docs.x.ai/developers/model-capabilities/text/structured-outputs
# Client: OpenAI-compatible Responses API, base URL https://api.x.ai/v1,
# model grok-4-fast-reasoning. max_output_tokens is 1000.
# Tool type x_search. Handles go out as included_x_handles.
# Current X Search docs still filter with allowed_x_handles, so that field
# is set to the same list: https://docs.x.ai/developers/tools/x-search
# from_date/to_date as YYYY-MM-DD. to_date is exclusive: posts from 00:00 UTC
# on from_date up to, but not including, to_date. Full datetimes are not
# applied, so the last-24-hours window is stated in the prompt and enforced
# again on each timestamp. from_date is yesterday and to_date is tomorrow,
# both YYYY-MM-DD, because to_date is exclusive at 00:00 UTC.
# search_parameters is not used.
# Structured output uses text.format json_schema on responses.create.
# max_turns is sent via extra_body, as documented on the X Search page.
# Responses API maps max_tokens to max_output_tokens:
# https://docs.x.ai/developers/model-capabilities/text/comparison

"""Fetch recent physical-security posts from vetted X accounts into hotspots.json."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
HOTSPOTS = ROOT / "hotspots.json"
ARCHIVE = ROOT / "archive.json"
SOURCES = ROOT / "sources.json"

# First model, then the retry if the API rejects it.
MODEL = "grok-4-fast-reasoning"
BASE_URL = "https://api.x.ai/v1"
# Plain HTTP (the OpenAI client's transport) stops at 10s.
# The xAI Responses call itself stops at 25s, with no retries.
HTTP_TIMEOUT = 10
API_TIMEOUT = 25
# Responses API name for max_tokens. Caps the visible answer.
MAX_OUTPUT_TOKENS = 1000
HANDLE_LIMIT = 20
ACTIVE_DAYS = 7
LEAD_MATCH_KM = 100
LEAD_MATCH_HOURS = 6

REGIONS = (
    "Gulf",
    "Saudi & Yemen",
    "Levant",
    "Iran & Iraq",
    "Maritime",
    "Other / International",
)
CATEGORIES = (
    "Security Incident",
    "Maritime Advisory",
    "Travel / Embassy Advisory",
)
SEVERITIES = ("high", "medium", "low")
TIERS = (
    "Official / Primary",
    "Attributed",
    "State-Affiliated / Single-Source",
    "Unverified Lead",
)
OFFICIAL_BATCHES = {
    "travel_advisories_and_embassies",
    "diplomatic_and_international",
}
AGGREGATOR_BATCH = "aggregators_leads_only"

# Government, military, UKMTO, CENTCOM, and official state agencies.
OFFICIAL_HANDLES = {
    "moi_bahrain",
    "bdf_bahrain",
    "kuwaitarmyghq",
    "moinformation",
    "mofaqatar_en",
    "uk_mto",
    "centcom",
    "centcomfarsi",
    "cjfcspox",
    "saudidcd",
    "ksagaca",
    "spa_eng",
    "sparegions",
    "yemen_pm",
    "idf",
    "idfspokesperson",
    "avichayadraee",
    "nnaleb",
    "lebarmyofficial",
    "sanaenofficial",
    "syrianmofaex",
    "irimfa_spox",
    "araghchi",
    "iraqipmo",
}
WIRES = {"reuters", "arareuters", "reutersiran", "ap", "afp", "afpar"}
REGIONAL_OUTLETS = {"bbcarabic", "ajenglish", "alhadath", "alarabytv"}
# State broadcasters. Tagged "Attributed", not the Ansarallah accounts below.
STATE_MEDIA = {
    "presstv",
    "tasnimarabic1",
    "iribnews_irib",
}
# Named state media and Ansarallah accounts. IRIB is the state broadcaster
# that operates Press TV.
STATE_AFFILIATED = {
    "presstv",
    "tasnimarabic1",
    "yah_saree",
    "mmy1444",
    "iribnews_irib",
}
# Always searched and kept, even if they also appear in excluded_handles.
BAHRAIN_HANDLES = {"moi_bahrain", "bdf_bahrain"}
AGGREGATOR_HANDLES = {
    "sentdefender",
    "insidergeo",
    "faytuksnetwork",
    "defencegeek",
    "menchosint",
    "me_observer_",
    "mata_osint",
    "barakravid",
    "kaisos1987",
    "lorientlejour",
}
OUTLET_LABELS = {
    "presstv": "Press TV",
    "tasnimarabic1": "Tasnim",
    "yah_saree": "Ansarallah spokesman Yahya Saree",
    "mmy1444": "Ansarallah media",
    "iribnews_irib": "IRIB",
    "bbcarabic": "BBC Arabic",
    "ajenglish": "Al Jazeera English",
    "alhadath": "Al Hadath",
    "alarabytv": "Al Araby TV",
}

REGION_ALIASES = {
    "gulf": "Gulf",
    "saudi & yemen": "Saudi & Yemen",
    "saudi and yemen": "Saudi & Yemen",
    "levant": "Levant",
    "iran & iraq": "Iran & Iraq",
    "iran and iraq": "Iran & Iraq",
    "maritime": "Maritime",
    "other / international": "Other / International",
    "other": "Other / International",
}
CATEGORY_ALIASES = {
    "security incident": "Security Incident",
    "maritime advisory": "Maritime Advisory",
    "travel / embassy advisory": "Travel / Embassy Advisory",
    "travel advisory": "Travel / Embassy Advisory",
    "embassy advisory": "Travel / Embassy Advisory",
}

STATUS_URL = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
NOTE_URL = re.compile(r"https?://[^\s)]+", re.IGNORECASE)

INCIDENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "longitude": {"type": "number"},
                    "latitude": {"type": "number"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "summary": {"type": "string"},
                    "timestamp": {"type": "string"},
                    "source": {"type": "string"},
                    "account": {"type": "string"},
                    "region": {"type": "string", "enum": list(REGIONS)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "cites_primary_official": {"type": "boolean"},
                    "cites_named_official_or_wire": {"type": "boolean"},
                    "cited_handle": {"type": "string"},
                    "claimant": {"type": "string"},
                },
                "required": [
                    "longitude",
                    "latitude",
                    "severity",
                    "summary",
                    "timestamp",
                    "source",
                    "account",
                    "region",
                    "category",
                    "cites_primary_official",
                    "cites_named_official_or_wire",
                    "cited_handle",
                    "claimant",
                ],
            },
        }
    },
    "required": ["incidents"],
}


def read_api_key() -> str:
    load_dotenv(ROOT / ".env")
    return (os.environ.get("XAI_API_KEY") or "").strip()


def exit_if_no_key() -> str:
    key = read_api_key()
    if not key:
        print(
            "XAI_API_KEY is missing. Add it to the environment or a .env file. "
            "hotspots.json was not changed."
        )
        sys.exit(0)
    return key


def public_error(exc: BaseException, secret: str) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    if secret:
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"(?:sk|xai)-[A-Za-z0-9_\-]+", "[redacted]", message)
    return message[:600]


def load_sources() -> dict:
    try:
        data = json.loads(SOURCES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read sources.json ({exc.__class__.__name__}). hotspots.json was not changed.")
        sys.exit(1)
    batches = data.get("batches")
    if not isinstance(batches, dict) or not batches:
        print("sources.json has no batches. hotspots.json was not changed.")
        sys.exit(1)
    excluded = data.get("excluded_handles") or []
    if not isinstance(excluded, list):
        print("sources.json excluded_handles is invalid. hotspots.json was not changed.")
        sys.exit(1)
    return data


def clean_handle(value: object) -> str:
    return str(value or "").strip().lstrip("@")


def excluded_set(handles: list) -> set[str]:
    return {clean_handle(handle).lower() for handle in handles if clean_handle(handle)}


def canonical_handle(value: object, allowed: list[str]) -> str | None:
    key = clean_handle(value).lower()
    if not key:
        return None
    for handle in allowed:
        if handle.lower() == key:
            return handle
    return None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Maritime chokepoints and regional capitals: (longitude, latitude, region).
# Vague water names stay on these points instead of a geocoder centroid.
PLACES = {
    "strait of hormuz": (56.2667, 26.5667, "Maritime"),
    "hormuz": (56.2667, 26.5667, "Maritime"),
    "bab el mandeb": (43.3333, 12.5833, "Maritime"),
    "bab al mandab": (43.3333, 12.5833, "Maritime"),
    "red sea": (38.5000, 20.0000, "Maritime"),
    "gulf of aden": (48.0000, 12.0000, "Maritime"),
    "gulf of oman": (58.0000, 25.0000, "Maritime"),
    "persian gulf": (51.5000, 26.5000, "Maritime"),
    "arabian gulf": (51.5000, 26.5000, "Maritime"),
    "arabian sea": (64.0000, 16.0000, "Maritime"),
    "suez canal": (32.3500, 30.4500, "Maritime"),
    "eastern mediterranean": (34.0000, 33.5000, "Maritime"),
    "manama": (50.5860, 26.2285, "Gulf"),
    "bahrain": (50.5577, 26.0667, "Gulf"),
    "doha": (51.5310, 25.2854, "Gulf"),
    "abu dhabi": (54.3773, 24.4539, "Gulf"),
    "dubai": (55.2708, 25.2048, "Gulf"),
    "muscat": (58.4059, 23.5880, "Gulf"),
    "kuwait city": (47.9774, 29.3759, "Gulf"),
    "riyadh": (46.6753, 24.7136, "Saudi & Yemen"),
    "jeddah": (39.1925, 21.4858, "Saudi & Yemen"),
    "sanaa": (44.1910, 15.3694, "Saudi & Yemen"),
    "aden": (45.0187, 12.7855, "Saudi & Yemen"),
    "hodeidah": (42.9511, 14.7978, "Saudi & Yemen"),
    "yemen": (44.1910, 15.3694, "Saudi & Yemen"),
    "damascus": (36.2765, 33.5138, "Levant"),
    "aleppo": (37.1612, 36.2021, "Levant"),
    "idlib": (36.6317, 35.9306, "Levant"),
    "idleb": (36.6317, 35.9306, "Levant"),
    "latakia": (35.7953, 35.5317, "Levant"),
    "tartus": (35.8866, 34.8890, "Levant"),
    "hama": (36.7552, 35.1318, "Levant"),
    "homs": (36.7137, 34.7324, "Levant"),
    "raqqa": (39.0094, 35.9594, "Levant"),
    "deir ez zor": (40.1408, 35.3359, "Levant"),
    "deir ezzor": (40.1408, 35.3359, "Levant"),
    "hasakah": (40.7477, 36.5024, "Levant"),
    "al hasakah": (40.7477, 36.5024, "Levant"),
    "daraa": (36.1021, 32.6189, "Levant"),
    "quneitra": (35.8244, 33.1259, "Levant"),
    "suwayda": (36.5667, 32.7094, "Levant"),
    "as suwayda": (36.5667, 32.7094, "Levant"),
    "beirut": (35.5018, 33.8938, "Levant"),
    "nabatieh": (35.4830, 33.3770, "Levant"),
    "amman": (35.9304, 31.9632, "Levant"),
    "jerusalem": (35.2137, 31.7683, "Levant"),
    "gaza": (34.4668, 31.5017, "Levant"),
    "tehran": (51.3890, 35.6892, "Iran & Iraq"),
    "baghdad": (44.3661, 33.3152, "Iran & Iraq"),
    "bandar abbas": (56.2808, 27.1832, "Iran & Iraq"),
    "cairo": (31.2357, 30.0444, "Other / International"),
}
VAGUE_PLACES = {
    "strait of hormuz",
    "hormuz",
    "bab el mandeb",
    "bab al mandab",
    "red sea",
    "gulf of aden",
    "gulf of oman",
    "persian gulf",
    "arabian gulf",
    "arabian sea",
    "suez canal",
    "eastern mediterranean",
}
REGION_CAPITALS = {
    "Gulf": PLACES["manama"],
    "Saudi & Yemen": PLACES["riyadh"],
    "Levant": PLACES["damascus"],
    "Iran & Iraq": PLACES["tehran"],
    "Maritime": PLACES["strait of hormuz"],
    "Other / International": PLACES["cairo"],
}
GEO_MODEL = "grok-3-mini"
_missing_key_noted = False
SYRIA_GOVERNORATES = (
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
)
GOVERNORATE_ALIASES = {
    "damascus": "Damascus",
    "rif dimashq": "Rif Dimashq",
    "rural damascus": "Rif Dimashq",
    "damascus countryside": "Rif Dimashq",
    "aleppo": "Aleppo",
    "idlib": "Idlib",
    "idleb": "Idlib",
    "latakia": "Latakia",
    "lattakia": "Latakia",
    "tartus": "Tartus",
    "tartous": "Tartus",
    "hama": "Hama",
    "homs": "Homs",
    "raqqa": "Raqqa",
    "deir ez zor": "Deir ez-Zor",
    "deir ezzor": "Deir ez-Zor",
    "deir al zor": "Deir ez-Zor",
    "al hasakah": "Al-Hasakah",
    "hasakah": "Al-Hasakah",
    "hasaka": "Al-Hasakah",
    "daraa": "Daraa",
    "quneitra": "Quneitra",
    "as suwayda": "As-Suwayda",
    "suwayda": "As-Suwayda",
    "sweida": "As-Suwayda",
}


def place_key(value: object) -> str:
    text = str(value or "").lower().replace("'", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def places_in(text: object) -> list[str]:
    remaining = place_key(text)
    found = []
    for name in sorted(PLACES, key=len, reverse=True):
        if name and name in remaining:
            found.append(name)
            remaining = remaining.replace(name, " ")
    return found


def is_vague_location(text: object) -> bool:
    found = places_in(text)
    return bool(found) and all(name in VAGUE_PLACES for name in found)


def lookup_place(text: object) -> tuple[float, float, str] | None:
    found = places_in(text)
    specific = [name for name in found if name not in VAGUE_PLACES]
    chosen = specific[0] if specific else (found[0] if found else "")
    if not chosen:
        return None
    return PLACES[chosen]


def valid_coord(lon: object, lat: object) -> tuple[float, float] | None:
    try:
        longitude = float(lon)
        latitude = float(lat)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    if abs(longitude) < 1e-9 and abs(latitude) < 1e-9:
        return None
    return longitude, latitude


def one_sentence(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_region(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    if text in REGIONS:
        return text
    return REGION_ALIASES.get(text.lower())


LIVE_SEVERITIES = ("High", "Medium", "Low")
LIVE_CATEGORIES = (
    "Kinetic/Military",
    "Maritime",
    "Security/Clashes",
    "Political/Other",
)


def live_severity(value: object) -> str:
    text = one_sentence(value).lower()
    if text == "high":
        return "High"
    if text == "low":
        return "Low"
    return "Medium"


def live_category(value: object) -> str:
    text = one_sentence(value)
    if text in LIVE_CATEGORIES:
        return text
    aliases = {
        "kinetic": "Kinetic/Military",
        "kinetic/military": "Kinetic/Military",
        "military": "Kinetic/Military",
        "maritime": "Maritime",
        "maritime advisory": "Maritime",
        "security": "Security/Clashes",
        "security/clashes": "Security/Clashes",
        "clashes": "Security/Clashes",
        "security incident": "Security/Clashes",
        "political": "Political/Other",
        "political/other": "Political/Other",
        "other": "Political/Other",
        "travel / embassy advisory": "Political/Other",
    }
    return aliases.get(text.lower(), "Political/Other")


def match_governorate(text: object) -> str:
    padded = f" {place_key(text)} "
    best = ""
    best_len = 0
    for alias, name in GOVERNORATE_ALIASES.items():
        if f" {alias} " in padded and len(alias) > best_len:
            best = name
            best_len = len(alias)
    return best


def extract_location(headline: str) -> dict | None:
    """One grok-3-mini call. No live search. Does not write hotspot or brief files."""
    global _missing_key_noted
    key = read_api_key()
    if not key:
        if not _missing_key_noted:
            print("XAI_API_KEY is missing. Location extract skipped. hotspots.json was not changed.")
            _missing_key_noted = True
        return None
    prompt = (
        "Extract the place from this headline. Do not use live search and do not invent a place "
        "that the text does not support. region must be one of: "
        + ", ".join(REGIONS)
        + ". location_name is the most specific place named. "
        "governorate is one of "
        + ", ".join(SYRIA_GOVERNORATES)
        + ", or an empty string when the event is not in a Syrian governorate. "
        "title is a concise English headline. Translate Arabic into English and do not add facts. "
        "severity is exactly High, Medium, or Low. "
        "category is exactly one of: Kinetic/Military, Maritime, Security/Clashes, Political/Other. "
        "lat and lng are decimal degrees, or null when the text does not locate the event.\n\n"
        + headline
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "region": {"type": "string", "enum": list(REGIONS)},
            "location_name": {"type": "string"},
            "governorate": {"type": "string"},
            "lat": {"type": ["number", "null"]},
            "lng": {"type": ["number", "null"]},
            "severity": {"type": "string", "enum": list(LIVE_SEVERITIES)},
            "category": {"type": "string", "enum": list(LIVE_CATEGORIES)},
        },
        "required": ["title", "region", "location_name", "governorate", "lat", "lng", "severity", "category"],
    }
    try:
        response = make_client(key).responses.create(
            model=GEO_MODEL,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "incident_location",
                    "schema": schema,
                    "strict": True,
                }
            },
            max_output_tokens=400,
            timeout=API_TIMEOUT,
        )
        text = response_text(response)
        if not text:
            raise RuntimeError("xAI response did not include output text")
        payload = parse_json_object(text)
    except Exception as exc:
        print(f"Location extract failed: {public_error(exc, key)}.")
        return None
    if not isinstance(payload, dict):
        print("Location extract failed: response was not an object.")
        return None
    return payload


def geocode_name(location_name: str) -> tuple[float, float] | None:
    name = one_sentence(location_name)
    if not name:
        return None
    try:
        from geopy.geocoders import Nominatim
    except ImportError:
        print(f"Geocode failed for {name}: geopy is not installed.")
        return None
    try:
        geolocator = Nominatim(user_agent="osint_tracker", timeout=10)
        found = geolocator.geocode(name)
    except Exception as exc:
        print(f"Geocode failed for {name}: {exc.__class__.__name__}.")
        return None
    if found is None:
        print(f"Geocode failed for {name}: no result.")
        return None
    return valid_coord(found.longitude, found.latitude)


def locate_event(
    headline: str,
    *,
    location_name: str = "",
    lat: object = None,
    lng: object = None,
    region: object = None,
) -> dict:
    """Place one incident. Missing key or a geocoder failure does not write files."""
    text = one_sentence(location_name) or one_sentence(headline)
    known = valid_coord(lng, lat)
    known_region = normalize_region(region)
    governorate = match_governorate(text) or match_governorate(headline)
    english_title = ""
    vague = is_vague_location(text) or is_vague_location(headline)
    severity = "Medium"
    category = "Political/Other"
    if known and known_region and not vague:
        return {
            "region": known_region,
            "governorate": governorate,
            "location_name": text,
            "title": english_title,
            "lat": known[1],
            "lng": known[0],
            "severity": severity,
            "category": category,
        }
    extracted = extract_location(one_sentence(headline) or text)
    if extracted:
        english_title = one_sentence(extracted.get("title"))
        known_region = normalize_region(extracted.get("region")) or known_region
        named = one_sentence(extracted.get("location_name"))
        if named:
            text = named
        named_governorate = match_governorate(extracted.get("governorate")) or match_governorate(text)
        if named_governorate:
            governorate = named_governorate
        extracted_coord = valid_coord(extracted.get("lng"), extracted.get("lat"))
        if extracted_coord:
            known = extracted_coord
        severity = live_severity(extracted.get("severity"))
        category = live_category(extracted.get("category"))
        vague = is_vague_location(text) or is_vague_location(headline)
    if (known is None or vague) and text and not vague:
        coded = geocode_name(text)
        if coded:
            known = coded
    if vague or known is None:
        place = lookup_place(text) or lookup_place(headline)
        if place:
            known = (place[0], place[1])
            known_region = known_region or place[2]
    if known_region is None and any(token in f"{headline} {text}".lower() for token in ("syria", "syrian")):
        known_region = "Levant"
    if known_region is None and any(token in f"{headline} {text}" for token in ("سوريا", "سورية")):
        known_region = "Levant"
    if known is None and known_region in REGION_CAPITALS:
        capital = REGION_CAPITALS[known_region]
        known = (capital[0], capital[1])
    if not governorate:
        governorate = match_governorate(text) or match_governorate(headline)
    return {
        "region": known_region or "Other / International",
        "governorate": governorate,
        "location_name": text,
        "title": english_title,
        "lat": known[1] if known else None,
        "lng": known[0] if known else None,
        "severity": severity,
        "category": category,
    }


def normalize_category(value: object, batch: str) -> str:
    text = " ".join(str(value or "").split())
    if text in CATEGORIES:
        return text
    alias = CATEGORY_ALIASES.get(text.lower())
    if alias:
        return alias
    if batch == "travel_advisories_and_embassies":
        return "Travel / Embassy Advisory"
    return "Security Incident"


def handle_batches(batches: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, handles in batches.items():
        if not isinstance(handles, list):
            continue
        for handle in handles:
            cleaned = clean_handle(handle)
            if cleaned:
                found[cleaned.lower()] = str(name)
    return found


def is_primary_handle(handle: str, batches_by_handle: dict[str, str]) -> bool:
    key = handle.lower()
    if key in WIRES or key in OFFICIAL_HANDLES:
        return True
    return batches_by_handle.get(key) in OFFICIAL_BATCHES


def assign_tier(handle: str, batch: str, cites_official: bool) -> str:
    key = handle.lower()
    if batch == AGGREGATOR_BATCH or key in AGGREGATOR_HANDLES:
        return "Unverified Lead"
    if key in STATE_MEDIA:
        return "Attributed"
    if key in REGIONAL_OUTLETS:
        if cites_official:
            return "Official / Primary"
        return "State-Affiliated / Single-Source"
    if key in STATE_AFFILIATED:
        return "State-Affiliated / Single-Source"
    if key in WIRES or key in OFFICIAL_HANDLES or batch in OFFICIAL_BATCHES:
        return "Official / Primary"
    return "State-Affiliated / Single-Source"


def ensure_named(summary: str, handle: str, claimant: str) -> str:
    label = one_sentence(claimant) or OUTLET_LABELS.get(handle.lower()) or handle
    haystack = summary.lower()
    if label.lower() in haystack or handle.lower() in haystack:
        return summary
    return f"{label} said: {summary}"


def blocked_hosts(notes: object) -> set[str]:
    hosts = {"mofa.go.jp", "www.mofa.go.jp"}
    if not isinstance(notes, list):
        return hosts
    for note in notes:
        for match in NOTE_URL.findall(str(note)):
            host = (urlparse(match).hostname or "").lower()
            if host:
                hosts.add(host)
    return hosts


def url_host_blocked(url: str, hosts: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    return any(host == blocked or host.endswith("." + blocked) for blocked in hosts)


def status_parts(url: object) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    match = STATUS_URL.match(url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def extract_warning_number(text: str) -> str | None:
    match = re.search(
        r"(?i)\b(UKMTO(?:\s*(?:warning|advisory))?\s*#?\s*[A-Z]{0,6}\d[\w\-]*)",
        text or "",
    )
    if not match:
        return None
    return " ".join(match.group(1).split())


def build_feature(
    *,
    lon: float,
    lat: float,
    severity: str,
    summary: str,
    timestamp: str,
    source: str,
    handle: str,
    batch: str,
    tier: str,
    region: str,
    category: str,
    cites_primary: bool,
    title: str = "",
    warning_number: str | None = None,
    location_name: str = "",
) -> dict:
    warning = one_sentence(warning_number) or extract_warning_number(summary)
    properties = {
        "severity": severity,
        "title": one_sentence(title) or summary,
        "summary": summary,
        "timestamp": timestamp,
        "source": source,
        "account": handle,
        "handle": handle,
        "source_handle": handle,
        "batch": batch,
        "verification_tier": tier,
        "region": region,
        "category": category,
        "warning_number": warning or None,
        "_cites_primary": cites_primary,
    }
    named = one_sentence(location_name)
    if named:
        properties["location_name"] = named
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": properties,
    }


def prepare_incidents(
    raw_items: list,
    *,
    batch: str,
    allowed: list[str],
    excluded: set[str],
    window_start: datetime,
    window_end: datetime,
    batches_by_handle: dict[str, str],
    require_recent: bool,
) -> list[dict]:
    features = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        parsed_url = status_parts(item.get("source"))
        if not parsed_url:
            continue
        handle = canonical_handle(parsed_url[0], allowed)
        if not handle or (handle.lower() in excluded and handle.lower() not in BAHRAIN_HANDLES):
            continue
        account = canonical_handle(item.get("account"), allowed)
        if account and account.lower() != handle.lower():
            continue
        source = f"https://x.com/{handle}/status/{parsed_url[1]}"
        if source in seen:
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in SEVERITIES:
            continue
        summary = one_sentence(item.get("summary"))
        if not summary:
            continue
        title = one_sentence(item.get("title") or "")
        hint = item.get("location_name") if isinstance(item.get("location_name"), str) else ""
        incoming = valid_coord(item.get("longitude"), item.get("latitude"))
        placed = locate_event(
            title or summary,
            location_name=hint,
            lat=incoming[1] if incoming else None,
            lng=incoming[0] if incoming else None,
            region=item.get("region"),
        )
        coords = valid_coord(placed.get("lng"), placed.get("lat"))
        if coords is None:
            continue
        moment = parse_timestamp(item.get("timestamp"))
        if moment is None:
            continue
        if require_recent and (moment < window_start or moment > window_end + timedelta(minutes=15)):
            continue
        region = normalize_region(placed.get("region")) or normalize_region(item.get("region"))
        if region is None:
            continue
        category = normalize_category(item.get("category"), batch)
        cited = clean_handle(item.get("cited_handle"))
        if cited.lower() in excluded:
            continue
        cites_named = bool(item.get("cites_named_official_or_wire"))
        cites_primary = bool(item.get("cites_primary_official"))
        if cited and cited.lower() != handle.lower() and is_primary_handle(cited, batches_by_handle):
            cites_named = True
            cites_primary = True
        tier = assign_tier(handle, batch, cites_named or cites_primary)
        if tier in ("State-Affiliated / Single-Source", "Attributed"):
            summary = ensure_named(summary, handle, str(item.get("claimant") or ""))
        seen.add(source)
        features.append(
            build_feature(
                lon=coords[0],
                lat=coords[1],
                severity=severity,
                summary=summary,
                timestamp=format_timestamp(moment),
                source=source,
                handle=handle,
                batch=batch,
                tier=tier,
                region=region,
                category=category,
                cites_primary=cites_primary or cites_named,
                title=title,
                warning_number=item.get("warning_number") if isinstance(item.get("warning_number"), str) else None,
                location_name=one_sentence(placed.get("location_name")),
            )
        )
    return features


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def feature_time(feature: dict) -> datetime | None:
    return parse_timestamp((feature.get("properties") or {}).get("timestamp"))


def same_incident(left: dict, right: dict) -> bool:
    left_time = feature_time(left)
    right_time = feature_time(right)
    if left_time is None or right_time is None:
        return False
    if abs((left_time - right_time).total_seconds()) > LEAD_MATCH_HOURS * 3600:
        return False
    lon1, lat1 = left["geometry"]["coordinates"][:2]
    lon2, lat2 = right["geometry"]["coordinates"][:2]
    return haversine_km(float(lon1), float(lat1), float(lon2), float(lat2)) <= LEAD_MATCH_KM


def apply_lead_rule(features: list[dict]) -> list[dict]:
    others = []
    leads = []
    for feature in features:
        props = feature.setdefault("properties", {})
        if props.get("batch") == AGGREGATOR_BATCH:
            leads.append(feature)
        else:
            props.pop("_cites_primary", None)
            others.append(feature)
    kept = list(others)
    for lead in leads:
        cites = bool(lead["properties"].pop("_cites_primary", False))
        if cites or any(same_incident(lead, other) for other in others):
            kept.append(lead)
    return kept


def source_of(feature: dict) -> str:
    return str((feature.get("properties") or {}).get("source") or "").strip()


def feature_handle(feature: dict) -> str:
    props = feature.get("properties") or {}
    return clean_handle(props.get("handle") or props.get("account")).lower()


def feature_excluded(feature: dict, excluded: set[str]) -> bool:
    handle = feature_handle(feature)
    parsed = status_parts(source_of(feature))
    source_handle = parsed[0].lower() if parsed else ""
    if handle in BAHRAIN_HANDLES or source_handle in BAHRAIN_HANDLES:
        return False
    if handle in excluded:
        return True
    return bool(source_handle and source_handle in excluded)


def read_collection(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path.name} ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise RuntimeError(f"{path.name} is not a GeoJSON FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list):
        raise RuntimeError(f"{path.name} has no features array")
    return features


def write_collection(path: Path, features: list, generated_at: str | None = None) -> None:
    payload = {"type": "FeatureCollection", "features": features}
    if generated_at:
        payload = {"type": "FeatureCollection", "generated_at": generated_at, "features": features}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_active_and_archive(
    new_features: list[dict],
    *,
    now: datetime | None = None,
    hotspots_path: Path = HOTSPOTS,
    archive_path: Path = ARCHIVE,
    excluded: set[str] | None = None,
    generated_at: str | None = None,
) -> dict[str, int]:
    """Keep incidents from the last 7 days and append older ones to archive.json.

    Archive entries are never deleted. Duplicate source links are kept once.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=ACTIVE_DAYS)
    blocked = excluded or set()
    existing = read_collection(hotspots_path)
    combined: list[dict] = []
    seen: set[str] = set()
    for feature in list(new_features) + existing:
        if not isinstance(feature, dict):
            continue
        if feature_excluded(feature, blocked):
            continue
        key = source_of(feature)
        if not key or key in seen:
            continue
        props = feature.setdefault("properties", {})
        props.pop("_cites_primary", None)
        timestamp = parse_timestamp(props.get("timestamp"))
        if timestamp is None:
            continue
        props["timestamp"] = format_timestamp(timestamp)
        seen.add(key)
        combined.append(feature)

    active: list[dict] = []
    retiring: list[dict] = []
    for feature in combined:
        timestamp = feature_time(feature)
        if timestamp is None or timestamp < cutoff:
            retiring.append(feature)
        else:
            active.append(feature)

    archive = read_collection(archive_path) if archive_path.exists() else []
    archived_keys = {source_of(feature) for feature in archive}
    added = 0
    for feature in retiring:
        key = source_of(feature)
        if key and key not in archived_keys:
            archive.append(feature)
            archived_keys.add(key)
            added += 1
    write_collection(hotspots_path, active, generated_at=generated_at)
    if archive_path.exists() or added:
        write_collection(archive_path, archive)
    return {"active": len(active), "archived": added}


def response_text(response: object) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "output_text":
                text = getattr(content, "text", "")
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("response JSON was not an object")
    return payload


def make_client(api_key: str):
    try:
        import httpx
        from openai import OpenAI
    except ImportError:
        print("The openai package is not installed. Run: pip install -r requirements.txt")
        sys.exit(1)
    # Every HTTP request uses timeout=10. xAI calls pass timeout=25 themselves.
    http_client = httpx.Client(timeout=HTTP_TIMEOUT)
    return OpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=HTTP_TIMEOUT,
        http_client=http_client,
        max_retries=0,
    )


def source_status(name: str, exc: BaseException, secret: str = "") -> None:
    detail = public_error(exc, secret).replace("\n", " ")
    if len(detail) > 140:
        detail = detail[:137] + "..."
    print(f"{name}: {detail}; continuing")


def run_source(name: str, fn, secret: str = ""):
    """Run one source. A stall or error prints one line and does not stop the run."""
    try:
        return fn()
    except Exception as exc:
        source_status(name, exc, secret)
        return None


def call_model(client, tool: dict, prompt: str, schema_name: str, schema: dict, api_key: str):
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        tools=[tool],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        extra_body={"max_turns": 4},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=API_TIMEOUT,
    )
    text = response_text(response)
    if not text:
        raise RuntimeError("xAI response did not include output text")
    payload = parse_json_object(text)
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        raise RuntimeError("xAI response JSON did not contain an incidents array")
    return incidents


def chunked(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def x_prompt(batch: str, handles: list[str], excluded: list[str], start: datetime, end: datetime) -> str:
    handle_line = ", ".join(f"@{handle}" for handle in handles)
    excluded_line = ", ".join(f"@{handle}" for handle in excluded) or "(none)"
    return (
        "Extract physical security incidents (strikes, explosions, clashes, shootings, "
        "and similar), plus maritime or travel advisories when this batch's accounts post them.\n"
        f"Batch: {batch}\n"
        f"Search only these X accounts: {handle_line}\n"
        f"Never include, cite, quote, or mention these accounts: {excluded_line}\n"
        f"UTC window: {format_timestamp(start)} through {format_timestamp(end)}. "
        "Keep posts from the last 24 hours only. Omit anything outside that window.\n"
        "Translate Arabic, Persian, and French into one concise factual English sentence.\n"
        "Do not invent coordinates. Omit an incident if the post does not locate it.\n"
        "severity is exactly high, medium, or low.\n"
        "timestamp is UTC ISO 8601 with a Z suffix, for example 2026-11-21T15:00:00Z.\n"
        "source is the post URL https://x.com/<handle>/status/<id>.\n"
        "account is the handle without @.\n"
        "region is exactly one of: Gulf (Bahrain, Kuwait, Qatar, UAE, Oman); "
        "Saudi & Yemen (Saudi Arabia, Yemen, Red Sea coast); "
        "Levant (Israel, Palestine, Lebanon, Jordan, Syria); "
        "Iran & Iraq (Iran, Iraq, Kurdistan Region); "
        "Maritime (Strait of Hormuz, Bab el-Mandeb, Gulf of Oman, Arabian Sea, UKMTO/JMIC); "
        "Other / International (Turkey, US/UN diplomatic statements, broader advisories).\n"
        "category is exactly one of: Security Incident; Maritime Advisory; Travel / Embassy Advisory.\n"
        "Travel and embassy accounts should use Travel / Embassy Advisory. "
        "UKMTO and other maritime warnings should use Maritime Advisory. "
        "Kinetic events should use Security Incident.\n"
        "Government, military, UKMTO, CENTCOM, Reuters, AP, and AFP (including araReuters, "
        "ReutersIran, and AFPar) are primary official sources. Accounts in "
        "travel_advisories_and_embassies and diplomatic_and_international are also primary official sources.\n"
        "BBCArabic, AJEnglish, AlHadath, and AlarabyTV are official only when the post cites "
        "a named official or a Reuters, AP, or AFP report. Otherwise name the outlet in the summary.\n"
        "PressTV, tasnimarabic1, Yah_Saree, and MMY1444 are state-affiliated single sources. "
        "The summary must name the claimant.\n"
        "aggregators_leads_only accounts are unverified leads. Still return a real in-window "
        "incident from them. Set cites_primary_official true only when that post itself cites "
        "a primary official source. The caller drops uncorroborated leads.\n"
        "Set cites_named_official_or_wire true only when the post names an official body or a wire. "
        "cited_handle is that source without @, or an empty string. "
        "claimant is the person or outlet making the claim, or an empty string for a direct official statement."
    )


def main() -> None:
    api_key = exit_if_no_key()
    sources = load_sources()
    batches = sources["batches"]
    excluded_list = [clean_handle(handle) for handle in sources.get("excluded_handles") or []]
    excluded = excluded_set(excluded_list)
    batches_by_handle = handle_batches(batches)
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=24)
    # to_date is exclusive at 00:00 UTC, so tomorrow includes all of today.
    from_date = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    to_date = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    client = make_client(api_key)

    collected: list[dict] = []
    attempted = 0
    succeeded = 0
    for batch_name, raw_handles in batches.items():
        if not isinstance(raw_handles, list):
            print(f"Skipping {batch_name}: handles are not a list.", file=sys.stderr)
            continue
        allowed = []
        for handle in raw_handles:
            cleaned = clean_handle(handle)
            kept = cleaned.lower() in BAHRAIN_HANDLES or cleaned.lower() not in excluded
            if cleaned and kept and cleaned not in allowed:
                allowed.append(cleaned)
        if not allowed:
            continue
        for group in chunked(allowed, HANDLE_LIMIT):
            attempted += 1
            label = f"{batch_name} ({len(group)} handles)"
            print(f"Searching {label}")
            tool = {
                "type": "x_search",
                "included_x_handles": group,
                # Current X Search docs still filter on allowed_x_handles.
                "allowed_x_handles": group,
                "from_date": from_date,
                "to_date": to_date,
            }

            def search_group(group=group, tool=tool, batch_name=batch_name):
                incidents = call_model(
                    client,
                    tool,
                    x_prompt(str(batch_name), group, excluded_list, window_start, now_utc),
                    "incident_batch",
                    INCIDENT_SCHEMA,
                    api_key,
                )
                prepared = prepare_incidents(
                    incidents,
                    batch=str(batch_name),
                    allowed=group,
                    excluded=excluded,
                    window_start=window_start,
                    window_end=now_utc,
                    batches_by_handle=batches_by_handle,
                    require_recent=True,
                )
                print(f"{batch_name}: kept {len(prepared)} of {len(incidents)}")
                return prepared

            prepared = run_source(label, search_group, api_key)
            if prepared is None:
                continue
            succeeded += 1
            collected.extend(prepared)

    if attempted == 0:
        print("No source batches to search. hotspots.json was not changed.")
        sys.exit(1)
    if succeeded == 0:
        print("All xAI searches failed. hotspots.json was not changed.")
        return
    if succeeded < attempted:
        print(f"{attempted - succeeded} batch search(es) failed. Merging the searches that succeeded.")

    features = apply_lead_rule(collected)
    try:
        stats = merge_active_and_archive(
            features,
            now=now_utc,
            excluded=excluded,
            generated_at=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except RuntimeError as exc:
        print(f"{exc}. hotspots.json was not changed.")
        sys.exit(1)
    print(
        f"Wrote {stats['active']} incidents to {HOTSPOTS.name} "
        f"({stats['archived']} moved to {ARCHIVE.name})."
    )


if __name__ == "__main__":
    main()
