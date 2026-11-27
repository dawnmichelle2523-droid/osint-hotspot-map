"""One-click route and corridor risk scoring.

The score is a distance-weighted reading of public OSINT inside a corridor
buffer. Driving geometry comes from the public OSRM service with a 4 second
timeout. If that service is unreachable, or the road detours across open
water or a closed border, the corridor falls back to the great-circle line.
No model API is called, and requests carry coordinates rather than personal data.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, request

ROOT = Path(__file__).resolve().parent
EARTH_KM = 6371.0088
OSRM_TIMEOUT_SECONDS = 4
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
WINDOW = timedelta(days=14)
FRESH = timedelta(hours=72)
RECENCY_OLDER = 0.7
MIN_BUFFER_KM = 5
MAX_BUFFER_KM = 100
DEFAULT_BUFFER_KM = 25
MAX_STOPS = 12
MEDICAL_SEARCH_KM = 60.0
BORDER_SEARCH_KM = 80.0
BASELINE_SCORE = 6
SCORE_SCALE = 55.0
SECTOR_BOOST = 1.15
DETOUR_RATIO = 1.85
DETOUR_EXTRA_KM = 80.0
WATER_SAMPLE_KM = 15.0
HEADERS = {"User-Agent": "GeoRiskIntel-route-risk/1.0", "Accept": "application/json"}

route_bp = Blueprint("route_risk", __name__)

SEVERITY_WEIGHT = {"critical": 36.0, "high": 24.0, "medium": 12.0, "low": 5.0, "info": 2.0}
CONFIDENCE_FACTOR = {"official": 1.0, "corroborated": 0.85, "unverified": 0.55}
CONFIDENCE_LABEL = {
    "official": "Official Advisory",
    "corroborated": "Multi-Source Corroborated",
    "unverified": "Single-Source Unverified",
}
TIER_BOUNDS = ((80, "CRITICAL"), (60, "HIGH"), (40, "ELEVATED"), (20, "MODERATE"), (0, "LOW"))

SECTOR_ALIASES = {
    "school": "schools",
    "university": "schools",
    "journalist": "media",
    "journalism": "media",
    "security": "psc",
    "humanitarian": "ngo",
}
SECTOR_FOCUS = {
    "schools": ("unrest", "protest", "kidnap", "crime", "school", "student"),
    "ngo": ("checkpoint", "aid", "health", "medical", "unrest", "humanitarian"),
    "psc": ("armed", "conflict", "attack", "ied", "military", "kidnap"),
    "media": ("journalist", "media", "detention", "protest"),
    "medical": ("health", "medical", "hospital", "disease", "outbreak"),
    "private": ("crime", "scam", "unrest", "robbery"),
}

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"\+\d{1,3}(?:[\s.-]?\d){7,14}|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
_PLACE = re.compile(r"[^A-Za-z0-9 .,'’&()/-]+")

# Open-water boxes used only to notice when a driving route leaves land.
# Coastal cities sit outside these boxes.
OPEN_WATER = (
    ("English Channel", 49.35, 51.00, -4.6, 1.65),
    ("Bay of Biscay", 43.4, 47.6, -8.5, -1.4),
    ("Eastern Mediterranean", 33.0, 36.4, 30.0, 34.4),
    ("Red Sea", 12.5, 27.8, 33.2, 43.0),
    ("Gulf of Aden", 11.2, 14.4, 43.6, 51.6),
)

DISCLAIMER = (
    "Distance-weighted reading of public OSINT inside the buffer. "
    "Not a movement clearance. Confirm incidents, hospitals, and crossings before travel."
)

PRESETS = (
    {
        "id": "beirut-airport-downtown",
        "name": "Beirut Airport → Hamra → Downtown",
        "sector": "media",
        "country": "Lebanon",
        "buffer_km": 15,
        "waypoints": (
            {"name": "Beirut Rafic Hariri Airport", "lat": 33.8209, "lng": 35.4884},
            {"name": "Hamra", "lat": 33.8969, "lng": 35.4786},
            {"name": "Downtown Beirut", "lat": 33.8959, "lng": 35.5044},
        ),
    },
    {
        "id": "kyiv-lviv",
        "name": "Kyiv → Lviv",
        "sector": "ngo",
        "country": "Ukraine",
        "buffer_km": 40,
        "waypoints": (
            {"name": "Kyiv", "lat": 50.4501, "lng": 30.5234},
            {"name": "Zhytomyr", "lat": 50.2547, "lng": 28.6587},
            {"name": "Lviv", "lat": 49.8397, "lng": 24.0297},
        ),
    },
    {
        "id": "nairobi-airport-westlands",
        "name": "Nairobi Airport → Westlands",
        "sector": "schools",
        "country": "Kenya",
        "buffer_km": 15,
        "waypoints": (
            {"name": "Jomo Kenyatta International Airport", "lat": -1.3192, "lng": 36.9278},
            {"name": "Nairobi CBD", "lat": -1.2864, "lng": 36.8172},
            {"name": "Westlands", "lat": -1.2673, "lng": 36.8025},
        ),
    },
    {
        "id": "amman-aqaba",
        "name": "Amman → Aqaba",
        "sector": "schools",
        "country": "Jordan",
        "buffer_km": 30,
        "waypoints": (
            {"name": "Amman", "lat": 31.9539, "lng": 35.9106},
            {"name": "Ma'an", "lat": 30.1926, "lng": 35.7360},
            {"name": "Aqaba", "lat": 29.5321, "lng": 35.0063},
        ),
    },
    {
        "id": "london-paris",
        "name": "London → Paris Eurostar/Coach",
        "sector": "schools",
        "country": "France",
        "buffer_km": 25,
        "waypoints": (
            {"name": "London St Pancras", "lat": 51.5305, "lng": -0.1257},
            {"name": "Lille Europe", "lat": 50.6394, "lng": 3.0758},
            {"name": "Paris Gare du Nord", "lat": 48.8809, "lng": 2.3553},
        ),
    },
    {
        "id": "amman-damascus",
        "name": "Amman → Damascus",
        "sector": "media",
        "country": "Syria",
        "buffer_km": 30,
        "waypoints": (
            {"name": "Amman", "lat": 31.9539, "lng": 35.9106},
            {"name": "Jaber–Nassib crossing", "lat": 32.4647, "lng": 36.2408},
            {"name": "Damascus", "lat": 33.5138, "lng": 36.2765},
        ),
    },
)

# Curated public references. Coordinates are city or campus anchors, not a live bed board.
FACILITIES = (
    {"name": "American University of Beirut Medical Center", "kind": "Trauma centre", "lat": 33.8992, "lng": 35.4830, "place": "Hamra, Beirut", "note": "Major teaching hospital and trauma receiving centre. Confirm emergency access before movement."},
    {"name": "Rafik Hariri University Hospital", "kind": "Public hospital", "lat": 33.8615, "lng": 35.4920, "place": "Jnah, Beirut", "note": "Large public hospital. Confirm it is receiving trauma before movement."},
    {"name": "Hôtel-Dieu de France", "kind": "Hospital", "lat": 33.8824, "lng": 35.5165, "place": "Achrafieh, Beirut", "note": "University hospital in Achrafieh. Confirm emergency acceptance."},
    {"name": "Oleksandrivska Clinical Hospital", "kind": "Hospital", "lat": 50.4586, "lng": 30.5183, "place": "Kyiv", "note": "Central Kyiv hospital. Demand spikes during attacks; confirm it is receiving."},
    {"name": "Feofaniya Clinical Hospital", "kind": "Hospital", "lat": 50.3436, "lng": 30.4896, "place": "Holosiiv, Kyiv", "note": "Large clinical hospital south of the centre. Confirm access routes."},
    {"name": "Lviv Regional Clinical Hospital", "kind": "Hospital", "lat": 49.8178, "lng": 24.0102, "place": "Lviv", "note": "Regional hospital serving Lviv. Confirm trauma acceptance before movement."},
    {"name": "Kenyatta National Hospital", "kind": "Trauma centre", "lat": -1.3010, "lng": 36.8072, "place": "Nairobi", "note": "National public referral and trauma hospital. Expect waits; confirm acceptance."},
    {"name": "Aga Khan University Hospital", "kind": "Trauma centre", "lat": -1.2618, "lng": 36.8242, "place": "Parklands, Nairobi", "note": "Private teaching hospital near Westlands with emergency care. Confirm acceptance."},
    {"name": "The Nairobi Hospital", "kind": "Hospital", "lat": -1.2963, "lng": 36.8039, "place": "Nairobi", "note": "Private hospital with an emergency department. Confirm acceptance and ambulance cover."},
    {"name": "Al-Bashir Hospital", "kind": "Public hospital", "lat": 31.9452, "lng": 35.9408, "place": "Amman", "note": "Major public hospital in Amman. Confirm emergency acceptance."},
    {"name": "Jordan Hospital", "kind": "Hospital", "lat": 31.9625, "lng": 35.8985, "place": "Amman", "note": "Private hospital in Amman. Confirm trauma capability before movement."},
    {"name": "King Abdullah University Hospital", "kind": "Trauma centre", "lat": 32.5014, "lng": 35.9915, "place": "Irbid", "note": "Teaching hospital in northern Jordan, relevant to the Syria corridor. Confirm acceptance."},
    {"name": "Aqaba public hospitals", "kind": "Hospital", "lat": 29.5321, "lng": 35.0063, "place": "Aqaba", "note": "Aqaba has public hospital cover. Serious trauma is often transferred to Amman. Confirm the receiving plan."},
    {"name": "University College Hospital", "kind": "Trauma centre", "lat": 51.5247, "lng": -0.1355, "place": "London", "note": "Major trauma hospital near St Pancras. Confirm the emergency department is receiving."},
    {"name": "St Thomas' Hospital", "kind": "Trauma centre", "lat": 51.4994, "lng": -0.1189, "place": "London", "note": "Central London major trauma centre south of the river."},
    {"name": "Hôpital Lariboisière", "kind": "Trauma centre", "lat": 48.8830, "lng": 2.3531, "place": "Paris", "note": "Public hospital beside Gare du Nord. SAMU 15 / 112. Confirm acceptance."},
    {"name": "Al-Mouwasat University Hospital", "kind": "Hospital", "lat": 33.5125, "lng": 36.2910, "place": "Damascus", "note": "University hospital in Damascus. Access and capability change quickly. Confirm before any movement."},
)

CHOKEPOINTS = (
    {"name": "Masnaa crossing", "kind": "Official border crossing", "lat": 33.6844, "lng": 35.8878, "note": "Official Lebanon–Syria crossing. Delays, closures, and screening change without notice. Use only the official post and confirm it is open."},
    {"name": "Arida crossing", "kind": "Official border crossing", "lat": 34.6380, "lng": 35.9770, "note": "Official northern Lebanon–Syria crossing. Confirm status before travel. This is not guidance to use any other route."},
    {"name": "Jaber–Nassib crossing", "kind": "Official border crossing", "lat": 32.4647, "lng": 36.2408, "note": "Official Jordan–Syria crossing. Hours and closure change without notice. Cross only at the official post after confirming it is open."},
    {"name": "Wadi Araba / Yitzhak Rabin crossing", "kind": "Official border crossing", "lat": 29.5850, "lng": 34.9790, "note": "Official Jordan–Israel crossing north of Aqaba and Eilat. Limited hours. Confirm the post before travel."},
    {"name": "Al-Durra crossing", "kind": "Official border crossing", "lat": 29.2100, "lng": 34.9600, "note": "Official Jordan–Saudi crossing south of Aqaba. Confirm opening hours and vehicle rules."},
    {"name": "London St Pancras juxtaposed controls", "kind": "Rail border control", "lat": 51.5305, "lng": -0.1257, "note": "UK and French border controls for Eurostar are at St Pancras. Allow time for document checks. Disruption and strikes affect the service."},
    {"name": "Paris Gare du Nord controls", "kind": "Rail border control", "lat": 48.8809, "lng": 2.3553, "note": "Eurostar arrival and juxtaposed controls. Allow time for document checks."},
    {"name": "Folkestone Eurotunnel terminal", "kind": "Coach and vehicle terminal", "lat": 51.0955, "lng": 1.1220, "note": "Channel Tunnel vehicle and coach terminal. Document checks apply. Strikes and weather close the shuttle."},
    {"name": "Coquelles Eurotunnel terminal", "kind": "Coach and vehicle terminal", "lat": 50.9210, "lng": 1.8120, "note": "French Channel Tunnel terminal near Calais. Document checks apply. Confirm the shuttle is running."},
    {"name": "Krakovets–Korczowa crossing", "kind": "Official border crossing", "lat": 49.9550, "lng": 23.1140, "note": "Official Ukraine–Poland crossing west of Lviv. Queues and document checks are common. Confirm the post is open."},
    {"name": "Shehyni–Medyka crossing", "kind": "Official border crossing", "lat": 49.7990, "lng": 22.9690, "note": "Official Ukraine–Poland crossing. Queues vary. Confirm status; this is not a routing instruction around other posts."},
)

EMERGENCY = (
    {"place": "Lebanon", "lat": 33.8938, "lng": 35.5018, "guidance": "Lebanese Red Cross 140. Civil Defence 125. Ambulance cover in Beirut is real but uneven outside the city. Confirm the receiving hospital."},
    {"place": "Syria", "lat": 33.5138, "lng": 36.2765, "guidance": "Do not rely on one national emergency number. Hospital access in Damascus is unpredictable. Confirm a receiving facility and a medevac plan before movement."},
    {"place": "Jordan", "lat": 31.9539, "lng": 35.9106, "guidance": "Public emergency 911. Trauma care is concentrated in Amman. Aqaba can stabilise and often transfers serious cases north."},
    {"place": "Ukraine", "lat": 50.4501, "lng": 30.5234, "guidance": "State emergency 112 where it is answering. Ambulance 103. Kyiv and Lviv hospitals have real capacity, and demand spikes during attacks. Confirm the facility is receiving."},
    {"place": "Kenya", "lat": -1.2864, "lng": 36.8172, "guidance": "National emergency 999 and 112. Nairobi ambulance response is uneven and private ambulances are widely used. Confirm the receiving hospital."},
    {"place": "United Kingdom", "lat": 51.5074, "lng": -0.1278, "guidance": "Emergency 999 or 112. London has major trauma centres. Confirm the department is receiving if the party has a known destination hospital."},
    {"place": "France", "lat": 48.8566, "lng": 2.3522, "guidance": "SAMU 15, police 17, fire 18, or 112. Paris has public trauma cover, including near Gare du Nord. Confirm acceptance."},
)

_cache_lock = threading.Lock()
_route_cache: dict[tuple, tuple[float, dict]] = {}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(_wrap_degrees(lng2 - lng1))
    hav = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    hav = min(1.0, max(0.0, hav))
    return 2 * EARTH_KM * math.asin(math.sqrt(hav))


def _wrap_degrees(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing in radians."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lam = math.radians(_wrap_degrees(lng2 - lng1))
    y = math.sin(d_lam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lam)
    return math.atan2(y, x)


def point_to_segment_km(lat: float, lng: float, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Minimum great-circle distance from a point to a geodesic segment, in kilometres."""
    chord = haversine_km(lat1, lng1, lat2, lng2)
    if chord < 0.001:
        return haversine_km(lat, lng, lat1, lng1)
    d13 = haversine_km(lat1, lng1, lat, lng) / EARTH_KM
    delta = _bearing(lat1, lng1, lat, lng) - _bearing(lat1, lng1, lat2, lng2)
    cross = math.sin(d13) * math.sin(delta)
    cross = min(1.0, max(-1.0, cross))
    along = math.atan2(math.sin(d13) * math.cos(delta), math.cos(d13))
    chord_ang = chord / EARTH_KM
    if along <= 1e-8:
        return haversine_km(lat, lng, lat1, lng1)
    if along >= chord_ang - 1e-8:
        return haversine_km(lat, lng, lat2, lng2)
    return abs(math.asin(cross)) * EARTH_KM


def point_along(lat1: float, lng1: float, lat2: float, lng2: float, fraction: float) -> tuple[float, float]:
    """Point at a fraction along the great circle from the first stop to the second."""
    fraction = min(1.0, max(0.0, fraction))
    if fraction <= 0:
        return lat1, lng1
    if fraction >= 1:
        return lat2, lng2
    distance = haversine_km(lat1, lng1, lat2, lng2) / EARTH_KM
    if distance < 1e-8:
        return lat1, lng1
    phi1, lam1 = math.radians(lat1), math.radians(lng1)
    phi2, lam2 = math.radians(lat2), math.radians(lng2)
    sin_d = math.sin(distance)
    weight_a = math.sin((1 - fraction) * distance) / sin_d
    weight_b = math.sin(fraction * distance) / sin_d
    x = weight_a * math.cos(phi1) * math.cos(lam1) + weight_b * math.cos(phi2) * math.cos(lam2)
    y = weight_a * math.cos(phi1) * math.sin(lam1) + weight_b * math.cos(phi2) * math.sin(lam2)
    z = weight_a * math.sin(phi1) + weight_b * math.sin(phi2)
    lat = math.degrees(math.atan2(z, math.hypot(x, y)))
    lng = math.degrees(math.atan2(y, x))
    return lat, _wrap_degrees(lng)


def interpolate_segment(lat1: float, lng1: float, lat2: float, lng2: float, step_km: float = 20.0) -> list[tuple[float, float]]:
    distance = haversine_km(lat1, lng1, lat2, lng2)
    if distance <= step_km or step_km <= 0:
        return [(lat1, lng1), (lat2, lng2)]
    steps = max(1, int(math.ceil(distance / step_km)))
    return [point_along(lat1, lng1, lat2, lng2, index / steps) for index in range(steps + 1)]


def destination(lat: float, lng: float, bearing: float, distance_km: float) -> tuple[float, float]:
    if distance_km <= 0:
        return lat, lng
    angular = distance_km / EARTH_KM
    phi1 = math.radians(lat)
    lam1 = math.radians(lng)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular) + math.cos(phi1) * math.sin(angular) * math.cos(bearing)
    )
    lam2 = lam1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), _wrap_degrees(math.degrees(lam2))


def corridor_polygon(points: list[tuple[float, float]], buffer_km: float) -> list[list[float]]:
    """Closed ring around the polyline, offset by the buffer on both sides."""
    if not points or buffer_km <= 0:
        return []
    if len(points) == 1 or haversine_km(points[0][0], points[0][1], points[-1][0], points[-1][1]) < 0.05 and len(points) < 3:
        lat, lng = points[0]
        ring = [destination(lat, lng, index * math.pi / 8, buffer_km) for index in range(16)]
        ring.append(ring[0])
        return [[round(lat, 5), round(lng, 5)] for lat, lng in ring]
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    last = len(points) - 1
    for index, (lat, lng) in enumerate(points):
        if index == 0:
            bearing = _bearing(lat, lng, points[1][0], points[1][1])
        elif index == last:
            bearing = _bearing(points[index - 1][0], points[index - 1][1], lat, lng)
        else:
            inbound = _bearing(points[index - 1][0], points[index - 1][1], lat, lng)
            outbound = _bearing(lat, lng, points[index + 1][0], points[index + 1][1])
            bearing = math.atan2(math.sin(inbound) + math.sin(outbound), math.cos(inbound) + math.cos(outbound))
        left.append(destination(lat, lng, bearing - math.pi / 2, buffer_km))
        right.append(destination(lat, lng, bearing + math.pi / 2, buffer_km))
    ring = left + list(reversed(right))
    ring.append(ring[0])
    return [[round(lat, 5), round(lng, 5)] for lat, lng in ring]


def min_distance_km(lat: float, lng: float, segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> float:
    if not segments:
        return 0.0
    best = None
    for (lat1, lng1), (lat2, lng2) in segments:
        distance = point_to_segment_km(lat, lng, lat1, lng1, lat2, lng2)
        if best is None or distance < best:
            best = distance
    return best if best is not None else 0.0


def score_from_points(points: list[float]) -> int:
    """Map summed incident weight to a 0–100 score. An empty corridor stays at the baseline."""
    total = sum(points)
    if total <= 0:
        return BASELINE_SCORE
    value = 100.0 * (1.0 - math.exp(-total / SCORE_SCALE))
    return int(max(0, min(100, round(value))))


def tier_for(score: int) -> str:
    for bound, name in TIER_BOUNDS:
        if score >= bound:
            return name
    return "LOW"


def _valid_coord(lat: object, lng: object) -> tuple[float, float] | None:
    try:
        lat_f = float(lat)  # type: ignore[arg-type]
        lng_f = float(lng)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat_f) or not math.isfinite(lng_f):
        return None
    if not -90 <= lat_f <= 90 or not -180 <= lng_f <= 180:
        return None
    return lat_f, lng_f


def strip_contact(text: object) -> str:
    """Drop emails and phone-like strings from a label. Dates and short public numbers stay."""
    cleaned = _EMAIL.sub("", str(text or ""))
    cleaned = _PHONE.sub("", cleaned)
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return " ".join(cleaned.split())


def _clean_name(value: object, fallback: str) -> str:
    text = strip_contact(value)[:80]
    return text or fallback


def _clean_sector(value: object) -> str:
    text = strip_contact(value).lower()
    text = re.sub(r"[^a-z0-9 -]", "", text).strip()
    text = SECTOR_ALIASES.get(text, text)
    return text[:40]


def _clean_place(value: object) -> str:
    text = strip_contact(value)
    text = _PLACE.sub("", text)
    return " ".join(text.split())[:60]


def _default_stop_name(index: int, count: int) -> str:
    if index == 0:
        return "Origin"
    if index == count - 1:
        return "Destination"
    return f"Waypoint {index}"


def _clamp_buffer(value: object) -> tuple[int | None, str]:
    if value is None or value == "":
        return DEFAULT_BUFFER_KM, ""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "Buffer must be a number of kilometres."
    if not math.isfinite(number):
        return None, "Buffer must be a number of kilometres."
    return int(max(MIN_BUFFER_KM, min(MAX_BUFFER_KM, round(number)))), ""


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _severity(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"critical", "severe", "extreme"}:
        return "critical"
    if text in {"high", "substantial"}:
        return "high"
    if text in {"low", "minor"}:
        return "low"
    if text in {"info", "informational", "baseline"}:
        return "info"
    return "medium"


def _confidence(props: dict) -> tuple[str, str]:
    tier = str(props.get("confidence_tier") or "").strip().lower()
    if tier in CONFIDENCE_FACTOR:
        label = str(props.get("confidence_label") or CONFIDENCE_LABEL[tier])
        return tier, label
    blob = " ".join(
        str(props.get(key) or "")
        for key in ("verification_tier", "confidence", "source_tier")
    ).lower()
    if "official" in blob or "primary" in blob:
        return "official", CONFIDENCE_LABEL["official"]
    if "corrobor" in blob or "multi-source" in blob or "multi source" in blob:
        return "corroborated", CONFIDENCE_LABEL["corroborated"]
    return "unverified", CONFIDENCE_LABEL["unverified"]


def normalize_incident(feature: dict) -> dict | None:
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else feature
    if not isinstance(props, dict):
        return None
    geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        point = _valid_coord(coords[1], coords[0])
    else:
        point = _valid_coord(props.get("lat"), props.get("lng"))
    if point is None:
        return None
    title = strip_contact(props.get("title") or props.get("summary") or "OSINT incident")[:240]
    if not title:
        return None
    tier, label = _confidence(props)
    return {
        "title": title,
        "severity": _severity(props.get("severity")),
        "category": strip_contact(props.get("category") or "")[:80],
        "confidence": tier,
        "confidence_label": label,
        "lat": point[0],
        "lng": point[1],
        "source": strip_contact(props.get("source") or props.get("source_url") or "")[:300],
        "timestamp": str(props.get("timestamp") or props.get("published_at") or "")[:40],
        "region": strip_contact(props.get("region") or "")[:80],
        "country": strip_contact(props.get("country") or "")[:80],
        "area": strip_contact(props.get("area") or props.get("location_name") or props.get("governorate") or "")[:80],
    }


def _read_hotspots() -> list[dict]:
    path = ROOT / "hotspots.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return []
    return [item for item in features if isinstance(item, dict)]


def load_live_incidents() -> list[dict]:
    """Incidents from the local OSINT feed. A feed failure uses the hotspot file."""
    features: list[dict] = []
    try:
        import serve_map

        features = [item for item in serve_map.combined_features() if isinstance(item, dict)]
    except Exception as exc:
        print(f"route-risk live feed unavailable: {exc.__class__.__name__}.")
        features = []
    if not features:
        features = _read_hotspots()
    cloned = []
    for feature in features:
        clone = dict(feature)
        props = feature.get("properties")
        if isinstance(props, dict):
            clone["properties"] = dict(props)
        cloned.append(clone)
    try:
        import confidence_scoring

        confidence_scoring.score_incidents(cloned)
    except Exception as exc:
        print(f"route-risk confidence scoring skipped: {exc.__class__.__name__}.")
    incidents = []
    for feature in cloned:
        item = normalize_incident(feature)
        if item:
            incidents.append(item)
    return incidents


def _apply_pass(incidents: list[dict]) -> list[dict]:
    """Keep an active single-region pass from seeing incidents outside its scope."""
    try:
        import ppv

        grant = ppv.current()
    except Exception:
        return incidents
    if not grant:
        return incidents
    kept = []
    for item in incidents:
        probe = {
            "country": item.get("country") or "",
            "region": item.get("region") or "",
            "area": item.get("area") or "",
        }
        try:
            visible = ppv.matches(probe, grant)
        except Exception:
            visible = False
        if visible:
            kept.append(item)
    return kept


def _recency(incident: dict, now: datetime) -> float | None:
    moment = _parse_time(incident.get("timestamp"))
    if moment is None:
        return 1.0
    age = now - moment
    if age < timedelta(0):
        return 1.0
    if age <= FRESH:
        return 1.0
    if age <= WINDOW:
        return RECENCY_OLDER
    return None


def _focus_hit(sector: str, incident: dict) -> bool:
    words = SECTOR_FOCUS.get(sector)
    if not words:
        return False
    blob = f"{incident.get('title', '')} {incident.get('category', '')}".lower()
    return any(word in blob for word in words)


def _contribution(distance_km: float, buffer_km: float, incident: dict, sector: str, recency: float) -> float:
    proximity = 1.0 - 0.5 * min(1.0, distance_km / buffer_km) if buffer_km else 1.0
    points = (
        SEVERITY_WEIGHT.get(incident.get("severity") or "medium", 12.0)
        * CONFIDENCE_FACTOR.get(incident.get("confidence") or "unverified", 0.55)
        * proximity
        * recency
    )
    if _focus_hit(sector, incident):
        points *= SECTOR_BOOST
    return points


def osrm_enabled() -> bool:
    flag = (os.environ.get("ROUTE_RISK_OSRM") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def fetch_osrm(waypoints: list[tuple[float, float]], timeout: float = OSRM_TIMEOUT_SECONDS) -> dict:
    """Ask the public OSRM router for a driving line. Only coordinates are sent."""
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in waypoints)
    url = OSRM_URL + coords + "?overview=full&geometries=geojson&steps=false"
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.Timeout:
        return {"ok": False, "reason": "timeout"}
    except requests.RequestException:
        return {"ok": False, "reason": "unreachable"}
    if response.status_code != 200:
        return {"ok": False, "reason": "unreachable"}
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "reason": "unreachable"}
    if not isinstance(payload, dict) or payload.get("code") != "Ok":
        return {"ok": False, "reason": "no-route"}
    routes = payload.get("routes") or []
    if not routes or not isinstance(routes[0], dict):
        return {"ok": False, "reason": "no-route"}
    route = routes[0]
    geometry = (route.get("geometry") or {}).get("coordinates") if isinstance(route.get("geometry"), dict) else None
    points: list[tuple[float, float]] = []
    if isinstance(geometry, list):
        for pair in geometry:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                point = _valid_coord(pair[1], pair[0])
                if point:
                    points.append(point)
    if len(points) < 2:
        return {"ok": False, "reason": "no-route"}
    try:
        distance_km = float(route.get("distance") or 0) / 1000.0
    except (TypeError, ValueError):
        distance_km = 0.0
    if distance_km <= 0:
        distance_km = _polyline_length(points)
    return {"ok": True, "reason": "ok", "distance_km": distance_km, "points": points}


def cached_osrm(waypoints: list[tuple[float, float]], timeout: float = OSRM_TIMEOUT_SECONDS) -> dict:
    key = tuple((round(lat, 4), round(lng, 4)) for lat, lng in waypoints)
    now = time.time()
    with _cache_lock:
        hit = _route_cache.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    result = fetch_osrm(waypoints, timeout)
    if result.get("ok"):
        with _cache_lock:
            _route_cache[key] = (now, result)
            if len(_route_cache) > 32:
                oldest = min(_route_cache, key=lambda item: _route_cache[item][0])
                _route_cache.pop(oldest, None)
    return result


def _polyline_length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for start, end in zip(points, points[1:]):
        total += haversine_km(start[0], start[1], end[0], end[1])
    return total


def _simplify(points: list[tuple[float, float]], spacing_km: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return list(points)
    kept = [points[0]]
    for point in points[1:-1]:
        if haversine_km(kept[-1][0], kept[-1][1], point[0], point[1]) >= spacing_km:
            kept.append(point)
    kept.append(points[-1])
    return kept


def _water_name(lat: float, lng: float) -> str:
    for name, min_lat, max_lat, min_lng, max_lng in OPEN_WATER:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return name
    return ""


def crosses_open_water(points: list[tuple[float, float]]) -> str:
    """Return the water name when an interior sample of a long segment is offshore."""
    for start, end in zip(points, points[1:]):
        length = haversine_km(start[0], start[1], end[0], end[1])
        if length < 12:
            continue
        steps = max(1, int(length / WATER_SAMPLE_KM))
        for index in range(1, steps):
            lat, lng = point_along(start[0], start[1], end[0], end[1], index / steps)
            name = _water_name(lat, lng)
            if name:
                return name
    return ""


def is_border_detour(road_km: float, direct_km: float) -> bool:
    if direct_km < 1:
        return False
    return road_km > direct_km * DETOUR_RATIO and (road_km - direct_km) > DETOUR_EXTRA_KM


def _segments_from(points: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(points, points[1:]))


def _display_line(waypoints: list[tuple[float, float]]) -> list[tuple[float, float]]:
    line: list[tuple[float, float]] = []
    for start, end in zip(waypoints, waypoints[1:]):
        piece = interpolate_segment(start[0], start[1], end[0], end[1], 20.0)
        if line and piece:
            piece = piece[1:]
        line.extend(piece)
    return line or list(waypoints)


def _routing_note(reason: str, water: str = "") -> str:
    if reason == "ok":
        return "This follows the driving roads. The distance is the road distance."
    if reason == "timeout":
        return "Road directions did not answer within 4 seconds. The band is a straight line between stops."
    if reason == "unreachable":
        return "Road directions could not be reached. The band is a straight line between stops."
    if reason == "no-route":
        return "No driving road was found, which often means water or a closed border. The band is a straight line between stops."
    if reason == "disabled":
        return "Road directions are turned off. The band is a straight line between stops."
    if reason == "sea_or_border":
        where = water or "open water or a border detour"
        return (
            f"The driving route crosses {where}. "
            "The band is scored on a straight line instead."
        )
    return "The band is a straight line between stops."


def resolve_route(waypoints: list[tuple[float, float]], buffer_km: float, osrm_fetch=None) -> dict:
    direct = _polyline_length(waypoints)
    fetch = osrm_fetch
    if fetch is None:
        fetch = cached_osrm if osrm_enabled() else (lambda points, timeout: {"ok": False, "reason": "disabled"})
    try:
        result = fetch(waypoints, OSRM_TIMEOUT_SECONDS)
    except TimeoutError:
        result = {"ok": False, "reason": "timeout"}
    except Exception:
        result = {"ok": False, "reason": "unreachable"}
    if not isinstance(result, dict):
        result = {"ok": False, "reason": "unreachable"}
    reason = str(result.get("reason") or ("ok" if result.get("ok") else "unreachable"))
    if result.get("ok") and isinstance(result.get("points"), list) and len(result["points"]) >= 2:
        road_points = []
        for point in result["points"]:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                checked = _valid_coord(point[0], point[1])
                if checked:
                    road_points.append(checked)
        if len(road_points) < 2:
            road_points = []
        road_km = float(result.get("distance_km") or _polyline_length(road_points))
        water = crosses_open_water(road_points) if road_points else ""
        if road_points and not water and not is_border_detour(road_km, direct):
            spacing = max(2.0, min(8.0, buffer_km / 3))
            simplified = _simplify(road_points, spacing)
            return {
                "routing": "osrm",
                "routing_reason": "ok",
                "routing_note": _routing_note("ok"),
                "distance_km": road_km,
                "geodesic_km": direct,
                "line": simplified,
                "segments": _segments_from(simplified),
            }
        if water or is_border_detour(road_km, direct):
            reason = "sea_or_border"
            result = {"water": water}
    line = _display_line(waypoints)
    score_points = waypoints if len(waypoints) >= 2 else line
    return {
        "routing": "geodesic",
        "routing_reason": reason if reason != "ok" else "unreachable",
        "routing_note": _routing_note(reason if reason != "ok" else "unreachable", str((result or {}).get("water") or "")),
        "distance_km": direct,
        "geodesic_km": direct,
        "line": line,
        "segments": _segments_from(score_points),
    }


def _nearby(records: tuple, segments: list, limit_km: float, buffer_km: float, limit: int) -> list[dict]:
    found = []
    for record in records:
        distance = min_distance_km(record["lat"], record["lng"], segments)
        if distance > max(limit_km, buffer_km):
            continue
        found.append({
            "name": record["name"],
            "kind": record["kind"],
            "place": record.get("place") or "",
            "lat": record["lat"],
            "lng": record["lng"],
            "distance_km": round(distance, 1),
            "inside_buffer": distance <= buffer_km + 0.05,
            "note": record["note"],
        })
    found.sort(key=lambda item: (item["distance_km"], item["name"]))
    return found[:limit]


def _emergency_notes(segments: list) -> list[dict]:
    ranked = []
    for record in EMERGENCY:
        distance = min_distance_km(record["lat"], record["lng"], segments)
        ranked.append((distance, record))
    ranked.sort(key=lambda item: item[0])
    chosen = [item for item in ranked if item[0] <= 80]
    if not chosen and ranked and ranked[0][0] <= 800:
        chosen = ranked[:1]
    notes = []
    for distance, record in chosen[:2]:
        notes.append({
            "place": record["place"],
            "distance_km": round(distance, 1),
            "guidance": record["guidance"],
        })
    if notes:
        return notes
    return [{
        "place": "Route",
        "distance_km": None,
        "guidance": "Confirm local ambulance, police, and fire numbers, plus the nearest embassy, before movement.",
    }]


def _scan_incidents(incidents: list[dict], segments: list, buffer_km: float, sector: str, now: datetime) -> list[dict]:
    hits = []
    seen = set()
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        point = _valid_coord(incident.get("lat"), incident.get("lng"))
        if point is None:
            continue
        recency = _recency(incident, now)
        if recency is None:
            continue
        distance = min_distance_km(point[0], point[1], segments)
        if distance > buffer_km + 0.05:
            continue
        title = incident.get("title") or "OSINT incident"
        key = (round(point[0], 3), round(point[1], 3), title.casefold())
        if key in seen:
            continue
        seen.add(key)
        points = _contribution(distance, buffer_km, incident, sector, recency)
        hits.append({
            "title": title,
            "severity": incident.get("severity") or "medium",
            "category": incident.get("category") or "",
            "distance_km": round(distance, 1),
            "confidence": incident.get("confidence_label") or CONFIDENCE_LABEL["unverified"],
            "confidence_tier": incident.get("confidence") or "unverified",
            "lat": round(point[0], 5),
            "lng": round(point[1], 5),
            "source": incident.get("source") or "",
            "timestamp": incident.get("timestamp") or "",
            "region": incident.get("region") or "",
            "country": incident.get("country") or "",
            "points": round(points, 2),
        })
    hits.sort(key=lambda item: (item["distance_km"], -SEVERITY_WEIGHT.get(item["severity"], 0)))
    return hits


def _validate(payload: dict) -> tuple[dict | None, str]:
    if not isinstance(payload, dict):
        return None, "Expected a JSON object."
    raw_stops = payload.get("waypoints")
    if not isinstance(raw_stops, list) or len(raw_stops) < 2:
        return None, "Add an origin and a destination."
    if len(raw_stops) > MAX_STOPS:
        return None, "Use at most 12 stops."
    buffer_km, buffer_error = _clamp_buffer(payload.get("buffer_km"))
    if buffer_error or buffer_km is None:
        return None, buffer_error or "Buffer must be a number of kilometres."
    stops = []
    count = len(raw_stops)
    for index, raw in enumerate(raw_stops):
        if not isinstance(raw, dict):
            return None, "Each stop needs a latitude and longitude."
        point = _valid_coord(raw.get("lat"), raw.get("lng"))
        if point is None:
            return None, "Each stop needs a latitude between -90 and 90 and a longitude between -180 and 180."
        stops.append({
            "name": _clean_name(raw.get("name"), _default_stop_name(index, count)),
            "lat": round(point[0], 5),
            "lng": round(point[1], 5),
        })
    return {
        "waypoints": stops,
        "buffer_km": buffer_km,
        "sector": _clean_sector(payload.get("sector")),
        "country": _clean_place(payload.get("country")),
    }, ""


def _distance_phrase(route: dict) -> str:
    road = float(route["distance_km"])
    direct = float(route["geodesic_km"])
    if route["routing"] == "osrm" and abs(road - direct) >= 3:
        return f"{road:.1f} km by road ({direct:.1f} km great-circle)"
    if route["routing"] == "osrm":
        return f"{road:.1f} km by road"
    return f"{direct:.1f} km great-circle"


def build_summary(result: dict) -> str:
    stops = result["waypoints"]
    origin = stops[0]["name"]
    destination = stops[-1]["name"]
    middle = ", ".join(stop["name"] for stop in stops[1:-1]) or "None"
    lines = [
        "CORRIDOR RISK BRIEF",
        f"Origin: {origin}",
        f"Waypoints: {middle}",
        f"Destination: {destination}",
        f"Distance: {_distance_phrase(result)}",
        f"Buffer: {result['buffer_km']} km",
        f"Sector: {result['sector'] or 'general'}",
        f"Corridor Risk Score: {result['score']} / 100",
        f"Tier: {result['tier']}",
        f"Routing: {result['routing_note']}",
        "Window: public OSINT from the last 14 days. Items older than 72 hours count at 70 percent.",
        "",
        f"Incidents inside the corridor: {result['incident_count']}",
    ]
    if not result["incidents"]:
        lines.append("No live OSINT incidents in the last 14 days fall inside this buffer. Baseline monitoring score applied.")
    else:
        shown = result["incidents"]
        for item in shown:
            lines.append(
                f"- {item['distance_km']} km off route · {item['severity'].upper()} · {item['confidence']} · {item['title']}"
            )
        if result["incident_count"] > len(shown):
            lines.append(f"- {result['incident_count'] - len(shown)} further incidents are included in the score and omitted from this list.")
    lines.append("")
    lines.append("Medical and trauma guidance:")
    if result["medical"]:
        for item in result["medical"]:
            where = "inside the buffer" if item["inside_buffer"] else "outside the buffer"
            place = f", {item['place']}" if item.get("place") else ""
            lines.append(f"- {item['name']} ({item['kind']}{place}) — {item['distance_km']} km from the route, {where}. {item['note']}")
    else:
        lines.append("- No curated trauma centre within the search radius. Confirm the nearest emergency department before movement.")
    lines.append("")
    lines.append("Border and chokepoint notes:")
    if result["borders"]:
        for item in result["borders"]:
            where = "inside the buffer" if item["inside_buffer"] else "outside the buffer"
            lines.append(f"- {item['name']} ({item['kind']}) — {item['distance_km']} km from the route, {where}. {item['note']}")
    else:
        lines.append("- No curated official crossing or transport chokepoint within 80 km.")
    lines.append("")
    lines.append("Emergency:")
    for item in result["emergency"]:
        lines.append(f"- {item['place']}: {item['guidance']}")
    lines.append("")
    lines.append(
        "This brief has no traveller names, contacts, or medical histories. "
        "It is not a movement clearance. No model credits were used."
    )
    return "\n".join(lines)


def analyze_route(payload: dict, incidents: list | None = None, osrm_fetch=None, now: datetime | None = None) -> dict:
    """Score a corridor. Pass incidents to avoid reading the live feed."""
    cleaned, error = _validate(payload if isinstance(payload, dict) else {})
    if cleaned is None:
        return {"ok": False, "error": error, "xai_credits": 0}
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    source = incidents if incidents is not None else load_live_incidents()
    visible = _apply_pass([item for item in source if isinstance(item, dict)])
    coords = [(stop["lat"], stop["lng"]) for stop in cleaned["waypoints"]]
    route = resolve_route(coords, cleaned["buffer_km"], osrm_fetch)
    hits = _scan_incidents(visible, route["segments"], cleaned["buffer_km"], cleaned["sector"], moment)
    listed = hits[:40]
    score = score_from_points([item["points"] for item in hits])
    medical = _nearby(FACILITIES, route["segments"], MEDICAL_SEARCH_KM, cleaned["buffer_km"], 6)
    borders = _nearby(CHOKEPOINTS, route["segments"], BORDER_SEARCH_KM, cleaned["buffer_km"], 6)
    emergency = _emergency_notes(route["segments"])
    legs = []
    for start, end in zip(cleaned["waypoints"], cleaned["waypoints"][1:]):
        legs.append({
            "from": start["name"],
            "to": end["name"],
            "distance_km": round(haversine_km(start["lat"], start["lng"], end["lat"], end["lng"]), 1),
        })
    result = {
        "ok": True,
        "xai_credits": 0,
        "routing": route["routing"],
        "routing_reason": route["routing_reason"],
        "routing_note": route["routing_note"],
        "distance_km": round(float(route["distance_km"]), 1),
        "geodesic_km": round(float(route["geodesic_km"]), 1),
        "buffer_km": cleaned["buffer_km"],
        "sector": cleaned["sector"],
        "country": cleaned["country"],
        "window_days": 14,
        "score": score,
        "tier": tier_for(score),
        "waypoints": cleaned["waypoints"],
        "legs": legs,
        "route": [[round(lat, 5), round(lng, 5)] for lat, lng in route["line"]],
        "corridor": corridor_polygon(route["line"], cleaned["buffer_km"]),
        "incident_count": len(hits),
        "incidents": listed,
        "medical": medical,
        "borders": borders,
        "emergency": emergency,
        "disclaimer": DISCLAIMER,
        "generated_at": moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result["summary"] = build_summary(result)
    return result


def presets_payload() -> dict:
    presets = []
    for preset in PRESETS:
        presets.append({
            "id": preset["id"],
            "name": preset["name"],
            "sector": preset["sector"],
            "country": preset["country"],
            "buffer_km": preset["buffer_km"],
            "waypoints": [dict(stop) for stop in preset["waypoints"]],
        })
    return {"ok": True, "xai_credits": 0, "presets": presets}


def _page_bytes() -> bytes:
    path = ROOT / "static" / "route-risk.html"
    if not path.is_file():
        return b""
    return path.read_bytes()


@route_bp.after_request
def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


@route_bp.get("/route-risk")
def route_risk_page() -> Response:
    body = _page_bytes()
    if not body:
        return Response("Route risk page is not available.", status=404, mimetype="text/plain")
    return Response(body, mimetype="text/html; charset=utf-8")


@route_bp.get("/api/route-risk/presets")
def api_presets():
    return jsonify(presets_payload())


@route_bp.post("/api/route-risk/analyze")
def api_analyze():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object.", "xai_credits": 0}), 400
    try:
        result = analyze_route(payload)
    except Exception as exc:
        print(f"route-risk analyze failed: {exc.__class__.__name__}.")
        return jsonify({"ok": False, "error": "Corridor analysis failed.", "xai_credits": 0}), 500
    status = 200 if result.get("ok") else 400
    return jsonify(result), status
