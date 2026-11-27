"""Spam filtering, English translation, and place resolution for OSINT items.

Translation uses Grok only when XAI_API_KEY is set on the server. Otherwise it
tries a public translation endpoint and keeps the original text if that is
offline. Results are cached by a hash of the source text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "translation_cache.json"
_lock = threading.Lock()
_cache: dict[str, str] | None = None
_live_budget = {"n": 0, "reset": 0.0}

PROMO_RE = re.compile(
    r"\b(subscribe|join our|follow us|promo code|airdrop|giveaway|1xbet|"
    r"casino|sportsbook|betting tips|forex signals?|crypto signals?|"
    r"onlyfans|binance)\b",
    re.IGNORECASE,
)
ARABIC_PROMO = ("اشترك", "تابعونا", "تابع القناة", "قناتنا على", "للاشتراك")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF]{3,}")
FOREIGN_SCRIPT_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\u0400-\u04FF\u0590-\u05FF"
    r"\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]"
)
ACCENT_RE = re.compile(r"[áéíóúãõâêôçñüÁÉÍÓÚÃÕÂÊÔÇÑÜ]")
ROMANCE_RE = re.compile(
    r"\b(dans|pour|avec|cette|selon|après|está|não|de la|en el|del|los|las|uma|para|avis|vigueur)\b",
    re.IGNORECASE,
)

# name aliases, latitude, longitude, country, area label
_PLACES = [
    (("damascus", "دمشق"), 33.5138, 36.2765, "Syria", "Damascus"),
    (("aleppo", "حلب"), 36.2021, 37.1343, "Syria", "Aleppo"),
    (("idlib", "إدلب", "ادلب"), 35.9306, 36.6339, "Syria", "Idlib"),
    (("homs", "حمص"), 34.7324, 36.7137, "Syria", "Homs"),
    (("hama", "حماة"), 35.1318, 36.7578, "Syria", "Hama"),
    (("latakia", "اللاذقية"), 35.5317, 35.7901, "Syria", "Latakia"),
    (("tartus", "طرطوس"), 34.889, 35.8866, "Syria", "Tartus"),
    (("deir ez-zor", "deir ezzor", "دير الزور"), 35.336, 40.141, "Syria", "Deir ez-Zor"),
    (("raqqa", "الرقة"), 35.95, 39.01, "Syria", "Raqqa"),
    (("daraa", "درعا"), 32.6189, 36.1021, "Syria", "Daraa"),
    (("as-suwayda", "suwayda", "السويداء"), 32.709, 36.569, "Syria", "As-Suwayda"),
    (("qamishli", "القامشلي"), 37.052, 41.231, "Syria", "Qamishli"),
    (("baghad", "baghdad", "بغداد"), 33.3152, 44.3661, "Iraq", "Baghdad"),
    (("mosul", "الموصل"), 36.335, 43.119, "Iraq", "Mosul"),
    (("erbil", "أربيل", "اربيل"), 36.1911, 44.0092, "Iraq", "Erbil"),
    (("basra", "البصرة"), 30.5081, 47.7835, "Iraq", "Basra"),
    (("kirkuk", "كركوك"), 35.4681, 44.3922, "Iraq", "Kirkuk"),
    (("beirut", "بيروت"), 33.8938, 35.5018, "Lebanon", "Beirut"),
    (("gaza", "غزة"), 31.5017, 34.4668, "Palestine", "Gaza"),
    (("ramallah", "رام الله"), 31.9038, 35.2034, "Palestine", "Ramallah"),
    (("jerusalem", "القدس"), 31.7683, 35.2137, "Israel", "Jerusalem"),
    (("tel aviv", "تل أبيب"), 32.0853, 34.7818, "Israel", "Tel Aviv"),
    (("amman", "عمّان", "عمان"), 31.9454, 35.9284, "Jordan", "Amman"),
    (("sanaa", "sana'a", "صنعاء"), 15.3694, 44.191, "Yemen", "Sanaa"),
    (("aden", "عدن"), 12.7855, 45.0187, "Yemen", "Aden"),
    (("hodeidah", "الحديدة"), 14.802, 42.951, "Yemen", "Hodeidah"),
    (("khartoum", "الخرطوم"), 15.5007, 32.5599, "Sudan", "Khartoum"),
    (("port sudan", "بورتسودان"), 19.6158, 37.2164, "Sudan", "Port Sudan"),
    (("tehran", "طهران"), 35.6892, 51.389, "Iran", "Tehran"),
    (("isfahan", "اصفهان"), 32.6546, 51.668, "Iran", "Isfahan"),
    (("riyadh", "الرياض"), 24.7136, 46.6753, "Saudi Arabia", "Riyadh"),
    (("jeddah", "جدة"), 21.5433, 39.1728, "Saudi Arabia", "Jeddah"),
    (("dammam", "الدمام"), 26.4207, 50.0888, "Saudi Arabia", "Dammam"),
    (("dubai", "دبي"), 25.2048, 55.2708, "UAE", "Dubai"),
    (("abu dhabi", "أبوظبي"), 24.4539, 54.3773, "UAE", "Abu Dhabi"),
    (("doha", "الدوحة"), 25.2854, 51.531, "Qatar", "Doha"),
    (("manama", "المنامة"), 26.2235, 50.5876, "Bahrain", "Manama"),
    (("kuwait city", "الكويت"), 29.3759, 47.9774, "Kuwait", "Kuwait City"),
    (("muscat", "مسقط"), 23.588, 58.3829, "Oman", "Muscat"),
    (("strait of hormuz", "hormuz"), 26.5667, 56.2667, "Maritime", "Strait of Hormuz"),
    (("bab el-mandeb", "bab al mandab"), 12.5833, 43.3333, "Maritime", "Bab el-Mandeb"),
    (("new york",), 40.7128, -74.006, "United States", "New York"),
    (("los angeles",), 34.0522, -118.2437, "United States", "Los Angeles"),
    (("chicago",), 41.8781, -87.6298, "United States", "Chicago"),
    (("houston",), 29.7604, -95.3698, "United States", "Houston"),
    (("miami",), 25.7617, -80.1918, "United States", "Miami"),
    (("washington",), 38.9072, -77.0369, "United States", "Washington"),
    (("seattle",), 47.6062, -122.3321, "United States", "Seattle"),
    (("san francisco",), 37.7749, -122.4194, "United States", "San Francisco"),
    (("dallas",), 32.7767, -96.797, "United States", "Dallas"),
    (("atlanta",), 33.749, -84.388, "United States", "Atlanta"),
    (("denver",), 39.7392, -104.9903, "United States", "Denver"),
    (("boston",), 42.3601, -71.0589, "United States", "Boston"),
    (("new orleans",), 29.9511, -90.0715, "United States", "New Orleans"),
    (("anchorage",), 61.2181, -149.9003, "United States", "Anchorage"),
    (("honolulu",), 21.3069, -157.8583, "United States", "Honolulu"),
    (("toronto",), 43.6532, -79.3832, "Canada", "Toronto"),
    (("vancouver",), 49.2827, -123.1207, "Canada", "Vancouver"),
    (("montreal",), 45.5017, -73.5673, "Canada", "Montreal"),
    (("ottawa",), 45.4215, -75.6972, "Canada", "Ottawa"),
    (("calgary",), 51.0447, -114.0719, "Canada", "Calgary"),
    (("winnipeg",), 49.8951, -97.1384, "Canada", "Winnipeg"),
    (("halifax",), 44.6488, -63.5752, "Canada", "Halifax"),
    (("sao paulo", "são paulo"), -23.5505, -46.6333, "Brazil", "Sao Paulo"),
    (("rio de janeiro",), -22.9068, -43.1729, "Brazil", "Rio de Janeiro"),
    (("brasilia", "brasília"), -15.7975, -47.8919, "Brazil", "Brasilia"),
    (("bogota", "bogotá"), 4.711, -74.0721, "Colombia", "Bogota"),
    (("caracas",), 10.4806, -66.9036, "Venezuela", "Caracas"),
    (("lima",), -12.0464, -77.0428, "Peru", "Lima"),
    (("santiago",), -33.4489, -70.6693, "Chile", "Santiago"),
    (("buenos aires",), -34.6037, -58.3816, "Argentina", "Buenos Aires"),
    (("quito",), -0.1807, -78.4678, "Ecuador", "Quito"),
    (("tokyo",), 35.6762, 139.6503, "Japan", "Tokyo"),
    (("osaka",), 34.6937, 135.5023, "Japan", "Osaka"),
    (("taipei",), 25.033, 121.5654, "Taiwan", "Taipei"),
    (("beijing",), 39.9042, 116.4074, "China", "Beijing"),
    (("shanghai",), 31.2304, 121.4737, "China", "Shanghai"),
    (("manila",), 14.5995, 120.9842, "Philippines", "Manila"),
    (("seoul",), 37.5665, 126.978, "South Korea", "Seoul"),
    (("jakarta",), -6.2088, 106.8456, "Indonesia", "Jakarta"),
    (("hanoi",), 21.0278, 105.8342, "Vietnam", "Hanoi"),
    (("bangkok",), 13.7563, 100.5018, "Thailand", "Bangkok"),
    (("singapore",), 1.3521, 103.8198, "Singapore", "Singapore"),
    (("kuala lumpur",), 3.139, 101.6869, "Malaysia", "Kuala Lumpur"),
    (("hong kong",), 22.3193, 114.1694, "China", "Hong Kong"),
    (("sydney",), -33.8688, 151.2093, "Australia", "Sydney"),
    (("melbourne",), -37.8136, 144.9631, "Australia", "Melbourne"),
    (("brisbane",), -27.4698, 153.0251, "Australia", "Brisbane"),
    (("perth",), -31.9505, 115.8605, "Australia", "Perth"),
    (("adelaide",), -34.9285, 138.6007, "Australia", "Adelaide"),
    (("canberra",), -35.2809, 149.13, "Australia", "Canberra"),
    (("darwin",), -12.4634, 130.8456, "Australia", "Darwin"),
    (("kabul", "کابل", "كابل"), 34.5553, 69.2075, "Afghanistan", "Kabul"),
    (("kandahar", "قندهار"), 31.6289, 65.7372, "Afghanistan", "Kandahar"),
    (("herat", "هرات"), 34.3529, 62.204, "Afghanistan", "Herat"),
    (("mazar-i-sharif", "mazar i sharif"), 36.708, 67.1109, "Afghanistan", "Mazar-i-Sharif"),
    (("jalalabad",), 34.4415, 70.4361, "Afghanistan", "Jalalabad"),
    (("kyiv", "kiev", "київ", "киев"), 50.4501, 30.5234, "Ukraine", "Kyiv"),
    (("kharkiv", "харків"), 49.9935, 36.2304, "Ukraine", "Kharkiv"),
    (("odesa", "odessa"), 46.4825, 30.7233, "Ukraine", "Odesa"),
    (("donetsk", "донецк"), 48.0159, 37.8028, "Ukraine", "Donetsk"),
    (("luhansk",), 48.574, 39.3078, "Ukraine", "Luhansk"),
    (("mariupol",), 47.0971, 37.5434, "Ukraine", "Mariupol"),
    (("sevastopol",), 44.6166, 33.5254, "Ukraine", "Sevastopol"),
    (("moscow", "москва"), 55.7558, 37.6173, "Russia", "Moscow"),
    (("saint petersburg", "st petersburg", "санкт-петербург"), 59.9311, 30.3609, "Russia", "Saint Petersburg"),
    (("rostov",), 47.2357, 39.7015, "Russia", "Rostov"),
    (("london",), 51.5072, -0.1276, "United Kingdom", "London"),
    (("paris",), 48.8566, 2.3522, "France", "Paris"),
    (("berlin",), 52.52, 13.405, "Germany", "Berlin"),
    (("rome",), 41.9028, 12.4964, "Italy", "Rome"),
    (("madrid",), 40.4168, -3.7038, "Spain", "Madrid"),
    (("warsaw",), 52.2297, 21.0122, "Poland", "Warsaw"),
    (("athens",), 37.9838, 23.7275, "Greece", "Athens"),
    (("istanbul",), 41.0082, 28.9784, "Turkey", "Istanbul"),
    (("ankara",), 39.9334, 32.8597, "Turkey", "Ankara"),
    (("cairo", "القاهرة"), 30.0444, 31.2357, "Egypt", "Cairo"),
    (("tripoli", "طرابلس"), 32.8872, 13.1913, "Libya", "Tripoli"),
    (("tunis",), 36.8065, 10.1815, "Tunisia", "Tunis"),
    (("algiers",), 36.7538, 3.0588, "Algeria", "Algiers"),
    (("lagos",), 6.5244, 3.3792, "Nigeria", "Lagos"),
    (("nairobi",), -1.2921, 36.8219, "Kenya", "Nairobi"),
    (("addis ababa",), 9.032, 38.7469, "Ethiopia", "Addis Ababa"),
    (("mogadishu",), 2.0469, 45.3182, "Somalia", "Mogadishu"),
    (("johannesburg",), -26.2041, 28.0473, "South Africa", "Johannesburg"),
    (("kinshasa",), -4.4419, 15.2663, "Democratic Republic of the Congo", "Kinshasa"),
    (("bamako",), 12.6392, -8.0029, "Mali", "Bamako"),
    (("ndjamena", "n'djamena"), 12.1348, 15.0557, "Chad", "N'Djamena"),
]
COUNTRIES = {
    "syria": (34.8, 38.99, "Syria"),
    "iraq": (33.22, 43.68, "Iraq"),
    "iran": (32.43, 53.69, "Iran"),
    "lebanon": (33.85, 35.86, "Lebanon"),
    "israel": (31.05, 34.85, "Israel"),
    "palestine": (31.95, 35.23, "Palestine"),
    "jordan": (30.59, 36.24, "Jordan"),
    "yemen": (15.55, 48.52, "Yemen"),
    "saudi arabia": (23.89, 45.08, "Saudi Arabia"),
    "sudan": (12.86, 30.22, "Sudan"),
    "chad": (15.45, 18.73, "Chad"),
    "uae": (23.42, 53.85, "UAE"),
    "united arab emirates": (23.42, 53.85, "UAE"),
    "qatar": (25.35, 51.18, "Qatar"),
    "bahrain": (26.07, 50.55, "Bahrain"),
    "kuwait": (29.31, 47.48, "Kuwait"),
    "oman": (21.47, 55.98, "Oman"),
    "united states": (39.83, -98.58, "United States"),
    "usa": (39.83, -98.58, "United States"),
    "canada": (56.13, -106.35, "Canada"),
    "brazil": (-14.24, -51.93, "Brazil"),
    "colombia": (4.57, -74.3, "Colombia"),
    "venezuela": (6.42, -66.59, "Venezuela"),
    "peru": (-9.19, -75.02, "Peru"),
    "chile": (-35.68, -71.54, "Chile"),
    "argentina": (-38.42, -63.62, "Argentina"),
    "japan": (36.2, 138.25, "Japan"),
    "taiwan": (23.7, 120.96, "Taiwan"),
    "china": (35.86, 104.2, "China"),
    "philippines": (12.88, 121.77, "Philippines"),
    "south korea": (35.91, 127.77, "South Korea"),
    "indonesia": (-0.79, 113.92, "Indonesia"),
    "vietnam": (14.06, 108.28, "Vietnam"),
    "thailand": (15.87, 100.99, "Thailand"),
    "australia": (-25.27, 133.78, "Australia"),
    "afghanistan": (33.94, 67.71, "Afghanistan"),
    "ukraine": (48.38, 31.17, "Ukraine"),
    "russia": (61.52, 105.32, "Russia"),
    "united kingdom": (55.38, -3.44, "United Kingdom"),
    "france": (46.23, 2.21, "France"),
    "germany": (51.17, 10.45, "Germany"),
    "italy": (41.87, 12.57, "Italy"),
    "spain": (40.46, -3.75, "Spain"),
    "turkey": (38.96, 35.24, "Turkey"),
    "egypt": (26.82, 30.8, "Egypt"),
    "libya": (26.34, 17.23, "Libya"),
    "somalia": (5.15, 46.2, "Somalia"),
    "nigeria": (9.08, 8.68, "Nigeria"),
    "kenya": (0.02, 37.91, "Kenya"),
    "ethiopia": (9.15, 40.49, "Ethiopia"),
    "south africa": (-30.56, 22.94, "South Africa"),
    "mali": (17.57, -4.0, "Mali"),
    "maritime": (20.0, 38.5, "Maritime"),
}
THEATRE = {
    "Syria": "Middle East", "Iraq": "Middle East", "Iran": "Middle East",
    "Lebanon": "Middle East", "Israel": "Middle East", "Palestine": "Middle East",
    "Jordan": "Middle East", "Yemen": "Middle East", "Saudi Arabia": "Middle East",
    "Sudan": "Middle East", "Chad": "Middle East", "UAE": "Middle East",
    "Qatar": "Middle East", "Bahrain": "Middle East", "Kuwait": "Middle East",
    "Oman": "Middle East", "Maritime": "Middle East",
    "United States": "United States", "Canada": "Canada",
    "Brazil": "South America", "Colombia": "South America", "Venezuela": "South America",
    "Peru": "South America", "Chile": "South America", "Argentina": "South America",
    "Ecuador": "South America",
    "Japan": "APAC", "Taiwan": "APAC", "China": "APAC", "Philippines": "APAC",
    "South Korea": "APAC", "Indonesia": "APAC", "Vietnam": "APAC", "Thailand": "APAC",
    "Singapore": "APAC", "Malaysia": "APAC",
    "Australia": "Australia", "Afghanistan": "Afghanistan",
    "Ukraine": "Russia & Ukraine", "Russia": "Russia & Ukraine",
    "United Kingdom": "Europe", "France": "Europe", "Germany": "Europe",
    "Italy": "Europe", "Spain": "Europe", "Poland": "Europe", "Greece": "Europe",
    "Turkey": "Europe", "Egypt": "Africa", "Libya": "Africa", "Tunisia": "Africa",
    "Algeria": "Africa", "Nigeria": "Africa", "Kenya": "Africa", "Ethiopia": "Africa",
    "Somalia": "Africa", "South Africa": "Africa", "Mali": "Africa",
    "Democratic Republic of the Congo": "Africa",
}


def is_noise(title: object, summary: object = "", url: object = "") -> bool:
    """Drop bare links, promo posts, emoji-only notes, and empty items."""
    title_text = " ".join(str(title or "").split())
    summary_text = " ".join(str(summary or "").split())
    blob = " ".join(part for part in (title_text, summary_text) if part).strip()
    if not blob:
        return True
    if PROMO_RE.search(blob) or any(token in blob for token in ARABIC_PROMO):
        words = WORD_RE.findall(blob)
        if len(words) < 16:
            return True
    remainder = URL_RE.sub(" ", blob)
    remainder = re.sub(r"\b(?:www\.)?[a-z0-9.-]+\.(?:me|com|net|org|tv|io)/\S*", " ", remainder, flags=re.I)
    words = WORD_RE.findall(remainder)
    if not words:
        return True
    if URL_RE.fullmatch(title_text):
        return True
    return False


def dedupe(items: list[dict], title_key: str = "title") -> list[dict]:
    """Drop exact and near-duplicate headlines across channels."""
    kept = []
    seen: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _norm_title(item.get(title_key) or item.get("summary") or "")
        if not key:
            continue
        if any(key == previous or _near(key, previous) for previous in seen):
            continue
        seen.append(key)
        kept.append(item)
    return kept


def _norm_title(value: object) -> str:
    text = URL_RE.sub(" ", str(value or "").lower())
    text = re.sub(r"[^a-z0-9\u0600-\u06ff\u0400-\u04ff]+", " ", text)
    return " ".join(text.split())


def _near(left: str, right: str) -> bool:
    if len(left) < 24 or len(right) < 24:
        return False
    if left in right or right in left:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer.startswith(shorter[: max(24, int(len(shorter) * 0.85))])


def needs_translation(text: object) -> bool:
    raw = " ".join(str(text or "").split())
    if len(raw) < 2:
        return False
    if FOREIGN_SCRIPT_RE.search(raw):
        return True
    if len(ACCENT_RE.findall(raw)) >= 2:
        return True
    return len(ROMANCE_RE.findall(raw)) >= 2


def translate_text(text: object, live: bool = True) -> str:
    raw = " ".join(str(text or "").split())
    if not raw or not needs_translation(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cached = _cache_get(digest)
    if cached:
        return cached
    if not live or not _allow_live():
        return raw
    translated = _grok_translate(raw) or _google_translate(raw) or _mymemory_translate(raw)
    cleaned = " ".join(str(translated or "").split())
    if cleaned and cleaned.lower() != raw.lower():
        _cache_put(digest, cleaned[:800])
        return cleaned[:800]
    return raw


def geolocate(title: object = "", summary: object = "", country: object = "", lat=None, lng=None, salt: object = "") -> dict:
    """Prefer feed coordinates, then a named place, then a country centroid."""
    text = f"{title or ''} {summary or ''} {country or ''}"
    feed_point = _pair(lat, lng)
    place = _match_place(text)
    named_country = _match_country(text) or _clean_country(country)
    if feed_point:
        area = place[4] if place else (named_country or "")
        resolved = place[3] if place else (named_country or _clean_country(country))
        return {
            "lat": feed_point[0],
            "lng": feed_point[1],
            "country": resolved,
            "area": area,
            "geo_precision": "Exact",
        }
    if place:
        jittered = _jitter(place[1], place[2], str(salt or title or ""), 0.06)
        return {
            "lat": jittered[0],
            "lng": jittered[1],
            "country": place[3],
            "area": place[4],
            "geo_precision": "City / Area",
        }
    if named_country and named_country.lower() in COUNTRIES:
        base = COUNTRIES[named_country.lower()]
        jittered = _jitter(base[0], base[1], str(salt or title or named_country), 0.35)
        return {
            "lat": jittered[0],
            "lng": jittered[1],
            "country": base[2],
            "area": base[2],
            "geo_precision": "Country",
        }
    return {"lat": None, "lng": None, "country": named_country, "area": "", "geo_precision": ""}


def theatre_for(country: object) -> str:
    return THEATRE.get(str(country or ""), "Global")


def enrich_event(event: dict, live_translation: bool = True) -> dict | None:
    if not isinstance(event, dict):
        return None
    if is_noise(event.get("title"), event.get("summary"), event.get("source") or event.get("source_url")):
        return None
    item = dict(event)
    original_title = " ".join(str(item.get("title") or "").split())
    original_summary = " ".join(str(item.get("summary") or "").split())
    title = translate_text(original_title, live=live_translation)
    summary = translate_text(original_summary, live=live_translation) if original_summary else ""
    if title != original_title and original_title:
        item["original_text"] = original_title[:500]
    item["title"] = title or original_title
    item["summary"] = summary or title or original_summary
    geo = geolocate(
        original_title,
        original_summary,
        item.get("country") or "",
        item.get("lat"),
        item.get("lng"),
        item.get("id") or item.get("source") or original_title,
    )
    item["lat"] = geo["lat"]
    item["lng"] = geo["lng"]
    if geo.get("country"):
        item["country"] = geo["country"]
    if geo.get("area"):
        item["area"] = geo["area"]
        item["location_name"] = geo["area"]
    if geo.get("geo_precision"):
        item["geo_precision"] = geo["geo_precision"]
    if not item.get("region") or item.get("region") == "Global":
        item["region"] = theatre_for(item.get("country"))
    return item


def annotate_feature(feature: dict) -> dict | None:
    """Apply cached English text and coordinates without a fresh translation call."""
    if not isinstance(feature, dict):
        return None
    props = dict(feature.get("properties") or {})
    if is_noise(props.get("title"), props.get("summary"), props.get("source")):
        return None
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    lng = lat = None
    if isinstance(coords, list) and len(coords) >= 2:
        lng, lat = coords[0], coords[1]
    enriched = enrich_event({
        **props,
        "lat": props.get("lat", lat),
        "lng": props.get("lng", lng),
    }, live_translation=False)
    if enriched is None:
        return None
    # A cache hit can still translate. One live call per item is capped.
    if needs_translation(enriched.get("title")):
        enriched["title"] = translate_text(enriched.get("title"), live=True)
    if needs_translation(enriched.get("summary")):
        enriched["summary"] = translate_text(enriched.get("summary"), live=True)
    props.update({
        "title": enriched.get("title") or props.get("title") or "",
        "summary": enriched.get("summary") or props.get("summary") or "",
        "country": enriched.get("country") or props.get("country") or "",
        "area": enriched.get("area") or props.get("area") or "",
        "location_name": enriched.get("location_name") or props.get("location_name") or enriched.get("area") or "",
        "geo_precision": enriched.get("geo_precision") or props.get("geo_precision") or "",
        "original_text": enriched.get("original_text") or props.get("original_text") or "",
    })
    if enriched.get("lat") is not None and enriched.get("lng") is not None:
        props["lat"] = enriched["lat"]
        props["lng"] = enriched["lng"]
        geometry = {"type": "Point", "coordinates": [enriched["lng"], enriched["lat"]]}
    elif not geometry:
        return None
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _match_place(text: str):
    lowered = f" {text.lower()} "
    best = None
    best_len = 0
    for aliases, lat, lng, country, area in _PLACES:
        for alias in aliases:
            if len(alias) < 4:
                continue
            if any("\u0600" <= ch <= "\u06FF" or "\u0400" <= ch <= "\u04FF" for ch in alias):
                found = alias in text
            else:
                found = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered) is not None
            if found and len(alias) > best_len:
                best = (aliases, lat, lng, country, area)
                best_len = len(alias)
    return best


def _match_country(text: str) -> str:
    lowered = f" {text.lower()} "
    best = ""
    best_len = 0
    for name, payload in COUNTRIES.items():
        if len(name) < 4:
            continue
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", lowered) and len(name) > best_len:
            best = payload[2]
            best_len = len(name)
    return best


def _clean_country(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    mapped = COUNTRIES.get(text.lower())
    return mapped[2] if mapped else text


def _pair(lat, lng):
    try:
        lat_n = float(lat)
        lng_n = float(lng)
    except (TypeError, ValueError):
        return None
    if lat_n != lat_n or lng_n != lng_n:
        return None
    if not (-90 <= lat_n <= 90 and -180 <= lng_n <= 180):
        return None
    if abs(lat_n) < 0.001 and abs(lng_n) < 0.001:
        return None
    return round(lat_n, 5), round(lng_n, 5)


def _jitter(lat: float, lng: float, salt: str, scale: float):
    digest = hashlib.sha1(salt.encode("utf-8", "replace")).hexdigest()
    dlat = ((int(digest[:4], 16) / 65535) - 0.5) * scale
    dlng = ((int(digest[4:8], 16) / 65535) - 0.5) * scale
    return round(max(-90, min(90, lat + dlat)), 5), round(max(-180, min(180, lng + dlng)), 5)


def _allow_live() -> bool:
    now = time.time()
    if now - _live_budget["reset"] > 30:
        _live_budget["n"] = 0
        _live_budget["reset"] = now
    if _live_budget["n"] >= 8:
        return False
    _live_budget["n"] += 1
    return True


def _cache_get(digest: str) -> str:
    global _cache
    with _lock:
        if _cache is None:
            try:
                payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                _cache = payload if isinstance(payload, dict) else {}
            except (OSError, json.JSONDecodeError):
                _cache = {}
        return str(_cache.get(digest) or "")


def _cache_put(digest: str, text: str) -> None:
    global _cache
    with _lock:
        if _cache is None:
            _cache = {}
        _cache[digest] = text
        try:
            temporary = CACHE_PATH.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
            temporary.replace(CACHE_PATH)
        except OSError:
            pass


def _api_key() -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    return (os.environ.get("XAI_API_KEY") or "").strip()


def _grok_translate(text: str) -> str:
    key = _api_key()
    if not key:
        return ""
    try:
        response = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "grok-3-mini",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "Translate the user's text into clear English. Return only the translation."},
                    {"role": "user", "content": text[:800]},
                ],
            },
            timeout=(4, 12),
        )
        if not response.ok:
            return ""
        data = response.json()
        return str((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
    except (requests.RequestException, ValueError, TypeError):
        return ""


def _google_translate(text: str) -> str:
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text[:800]},
            timeout=(4, 8),
            headers={"User-Agent": "GeoRiskIntel/1.0"},
        )
        if not response.ok:
            return ""
        payload = response.json()
        parts = payload[0] if isinstance(payload, list) and payload else []
        return "".join(str(part[0]) for part in parts if isinstance(part, list) and part)
    except (requests.RequestException, ValueError, TypeError, IndexError):
        return ""


def _mymemory_translate(text: str) -> str:
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:400], "langpair": "autodetect|en"},
            timeout=(4, 8),
            headers={"User-Agent": "GeoRiskIntel/1.0"},
        )
        if not response.ok:
            return ""
        data = response.json()
        return str(((data.get("responseData") or {}).get("translatedText") or "")).strip()
    except (requests.RequestException, ValueError, TypeError):
        return ""
