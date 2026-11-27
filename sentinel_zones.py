"""Live geofenced trip and asset watches (Sentinel Zones).

Zones live in their own SQLite file so they never share a lock with the
incident archive. Matching uses haversine distance only. Nothing here calls
xAI, and zones store places rather than personal data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, request

ROOT = Path(__file__).resolve().parent
EARTH_RADIUS_KM = 6371.0088
MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 500.0
SLACK_TIMEOUT = 4
SEVERITY_RANK = {"LOW": 1, "MED": 2, "HIGH": 3}
SEVERITIES = ("LOW", "MED", "HIGH")
CATEGORY_CODES = ("ACT", "SFA", "VC", "CU", "HS", "OD")
COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_LABEL_CATEGORY = {
    "act": "ACT",
    "armed conflict & terrorism": "ACT",
    "kinetic/military": "ACT",
    "conflict": "ACT",
    "sfa": "SFA",
    "security/clashes": "SFA",
    "security force activity": "SFA",
    "vc": "VC",
    "violent crime": "VC",
    "cu": "CU",
    "civil unrest": "CU",
    "hs": "HS",
    "hazards & safety": "HS",
    "od": "OD",
    "operational disruption": "OD",
    "political/other": "OD",
}
_WEBHOOK_RE = re.compile(
    r"^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

sentinel_bp = Blueprint("sentinel_zones", __name__)
_lock = threading.RLock()


class ZoneError(ValueError):
    """The zone payload cannot be stored."""


def resolve_db_path(
    env_value: str | None = None,
    data_dir: Path | None = None,
    fallback: Path | None = None,
) -> Path:
    """Prefer /data/sentinel_zones.db when that directory is writable."""
    if env_value is None:
        env_value = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    else:
        env_value = str(env_value).strip()
    if env_value:
        return Path(env_value)
    data = Path("/data") if data_dir is None else Path(data_dir)
    try:
        if data.is_dir() and os.access(data, os.W_OK):
            return data / "sentinel_zones.db"
    except OSError:
        pass
    if fallback is not None:
        return Path(fallback)
    return Path.cwd() / "sentinel_zones.db"


def db_path() -> Path:
    return resolve_db_path()


def utc_stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lam = math.radians(float(lng2) - float(lng1))
    haversine = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    haversine = min(1.0, max(0.0, haversine))
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(haversine))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial compass bearing from the first point to the second, 0–360."""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_lam = math.radians(float(lng2) - float(lng1))
    y = math.sin(d_lam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lam)
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return 0.0
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_point(bearing: float) -> str:
    """Sixteen-point compass label for a bearing in degrees."""
    try:
        angle = float(bearing) % 360.0
    except (TypeError, ValueError):
        angle = 0.0
    if angle != angle:
        angle = 0.0
    index = int((angle + 11.25) / 22.5) % 16
    return COMPASS_POINTS[index]


def cardinal_dir(lat1: float, lng1: float, lat2: float, lng2: float, name: str = "") -> str:
    """Distance and compass offset, e.g. '2.4 km NE of Beirut Bureau'."""
    distance = distance_km(lat1, lng1, lat2, lng2)
    point = compass_point(bearing_deg(lat1, lng1, lat2, lng2))
    phrase = f"{distance:.1f} km {point}"
    label = " ".join(str(name or "").split())
    if label:
        phrase += f" of {label}"
    return phrase


def severity_code(value: object) -> str:
    text = " ".join(str(value or "").split()).upper()
    if text in {"HIGH", "H", "CRITICAL", "EXTREME", "SEVERE"}:
        return "HIGH"
    if text in {"MED", "MEDIUM", "M", "MODERATE"}:
        return "MED"
    if text in {"LOW", "L"}:
        return "LOW"
    return ""


def severity_meets(incident: object, minimum: object) -> bool:
    floor = severity_code(minimum) or "LOW"
    found = severity_code(incident)
    if not found:
        return False
    return SEVERITY_RANK[found] >= SEVERITY_RANK[floor]


def category_tokens(value: object) -> set[str]:
    text = " ".join(str(value or "").split())
    if not text:
        return set()
    tokens = {text.lower()}
    if text.upper() in CATEGORY_CODES:
        tokens.add(text.upper().lower())
    mapped = _LABEL_CATEGORY.get(text.lower())
    if mapped:
        tokens.add(mapped.lower())
    return tokens


def categories_public(stored: object):
    text = str(stored or "").strip()
    if not text or text.upper() == "ALL":
        return "ALL"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "ALL"
    if isinstance(parsed, list) and parsed:
        return [str(item) for item in parsed]
    return "ALL"


def categories_match(stored: object, incident_category: object) -> bool:
    public = categories_public(stored)
    if public == "ALL":
        return True
    have = category_tokens(incident_category)
    if not have:
        return False
    for item in public:
        if category_tokens(item) & have:
            return True
    return False


def mask_webhook(url: object) -> str:
    """Keep the Slack host visible and hide the signing secret."""
    text = str(url or "").strip()
    if not text:
        return ""
    parts = text.split("/")
    if len(parts) < 7 or parts[2] != "hooks.slack.com":
        return "https://hooks.slack.com/services/••••"
    team = parts[4] or "T"
    bot = parts[5] or "B"
    secret = parts[6]
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"https://hooks.slack.com/services/{team[:1]}.../{bot[:1]}.../••••{tail}"


def slack_url_allowed(url: object) -> bool:
    return bool(_WEBHOOK_RE.fullmatch(str(url or "").strip()))


def _validate_webhook(url: object, *, allow_empty: bool = True) -> str:
    text = str(url or "").strip()
    if not text:
        if allow_empty:
            return ""
        raise ZoneError("Webhook URL must be an https://hooks.slack.com/services/… URL.")
    if not slack_url_allowed(text):
        raise ZoneError("Webhook URL must be an https://hooks.slack.com/services/… URL.")
    return text


def _keeps_existing_webhook(incoming: object, existing: str) -> bool:
    text = str(incoming or "").strip()
    if not text or not existing:
        return False
    if "••••" in text:
        return True
    return text == mask_webhook(existing)


def _clean_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ZoneError("Zone name is required.")
    if len(name) > 120:
        raise ZoneError("Zone name must be 120 characters or fewer.")
    return name


def _clean_sector(value: object) -> str:
    sector = " ".join(str(value or "").split())
    if len(sector) > 80:
        raise ZoneError("Sector must be 80 characters or fewer.")
    return sector


def _clean_email(value: object) -> str:
    email = " ".join(str(value or "").split())
    if not email:
        return ""
    if len(email) > 200 or not _EMAIL_RE.fullmatch(email):
        raise ZoneError("Alert email must be a single email address.")
    return email


def _as_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ZoneError(f"{label} must be a number.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ZoneError(f"{label} must be a number.")
    return number


def _clean_lat(value: object) -> float:
    lat = _as_float(value, "Latitude")
    if lat < -90 or lat > 90:
        raise ZoneError("Latitude must be between -90 and 90.")
    return round(lat, 6)


def _clean_lng(value: object) -> float:
    lng = _as_float(value, "Longitude")
    if lng < -180 or lng > 180:
        raise ZoneError("Longitude must be between -180 and 180.")
    return round(lng, 6)


def _clean_radius(value: object, default: float | None = None) -> float:
    if value in (None, "") and default is not None:
        value = default
    radius = _as_float(value, "Radius")
    if radius < MIN_RADIUS_KM or radius > MAX_RADIUS_KM:
        raise ZoneError("Radius must be between 1 and 500 km.")
    return round(radius, 3)


def _clean_severity(value: object, default: str = "LOW") -> str:
    if value in (None, ""):
        return default
    code = severity_code(value)
    if code not in SEVERITIES:
        raise ZoneError("Minimum severity must be LOW, MED, or HIGH.")
    return code


def _clean_categories(value: object) -> str:
    if value is None or value == "":
        return "ALL"
    if isinstance(value, str):
        text = value.strip()
        if not text or text.upper() == "ALL":
            return "ALL"
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ZoneError("Categories must be a list or ALL.") from exc
        else:
            value = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, list):
        raise ZoneError("Categories must be a list or ALL.")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        token = " ".join(str(item or "").split())
        if not token:
            continue
        if token.upper() == "ALL":
            return "ALL"
        if len(token) > 40:
            raise ZoneError("Category names must be 40 characters or fewer.")
        key = token.upper()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    if not cleaned or len(cleaned) > 12:
        if len(cleaned) > 12:
            raise ZoneError("Select up to 12 categories.")
        return "ALL"
    return json.dumps(cleaned)


def _as_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "active"}:
        return True
    if text in {"0", "false", "no", "off", "paused", "pause"}:
        return False
    raise ZoneError("Active must be true or false.")


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sentinel_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sector TEXT NOT NULL DEFAULT '',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            radius_km REAL NOT NULL,
            min_severity TEXT NOT NULL DEFAULT 'LOW',
            categories TEXT NOT NULL DEFAULT 'ALL',
            slack_webhook_url TEXT NOT NULL DEFAULT '',
            alert_email TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sentinel_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL,
            incident_id TEXT NOT NULL,
            distance_km REAL NOT NULL,
            bearing_deg REAL NOT NULL,
            cardinal TEXT NOT NULL,
            phrase TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            lat REAL,
            lng REAL,
            created_at TEXT NOT NULL,
            UNIQUE(zone_id, incident_id),
            FOREIGN KEY(zone_id) REFERENCES sentinel_zones(id) ON DELETE CASCADE
        );
        """
    )
    return conn


def _public_zone(row: sqlite3.Row) -> dict:
    webhook = row["slack_webhook_url"] or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "sector": row["sector"] or "",
        "lat": row["lat"],
        "lng": row["lng"],
        "radius_km": row["radius_km"],
        "min_severity": row["min_severity"],
        "categories": categories_public(row["categories"]),
        "slack_webhook_url": mask_webhook(webhook) if webhook else "",
        "slack_webhook_set": bool(webhook),
        "alert_email": row["alert_email"] or "",
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def _zone_record(row: sqlite3.Row) -> dict:
    record = _public_zone(row)
    record["slack_webhook_url_raw"] = row["slack_webhook_url"] or ""
    record["categories_stored"] = row["categories"] or "ALL"
    return record


def list_zones() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM sentinel_zones ORDER BY id").fetchall()
        finally:
            conn.close()
    return [_public_zone(row) for row in rows]


def get_zone(zone_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM sentinel_zones WHERE id = ?", (zone_id,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return _public_zone(row)


def _get_record(zone_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM sentinel_zones WHERE id = ?", (zone_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _zone_record(row)


def _require_object(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ZoneError("Zone payload must be an object.")
    return payload


def create_zone(payload: object) -> dict:
    body = _require_object(payload)
    name = _clean_name(body.get("name"))
    sector = _clean_sector(body.get("sector"))
    if "lat" not in body or "lng" not in body:
        raise ZoneError("Latitude and longitude are required.")
    lat = _clean_lat(body.get("lat"))
    lng = _clean_lng(body.get("lng"))
    radius = _clean_radius(body.get("radius_km"), default=25)
    minimum = _clean_severity(body.get("min_severity"), default="LOW")
    categories = _clean_categories(body.get("categories"))
    webhook = _validate_webhook(body.get("slack_webhook_url"))
    email = _clean_email(body.get("alert_email"))
    active = 1 if _as_bool(body.get("active"), True) else 0
    created = utc_stamp()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO sentinel_zones (
                    name, sector, lat, lng, radius_km, min_severity, categories,
                    slack_webhook_url, alert_email, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, sector, lat, lng, radius, minimum, categories, webhook, email, active, created),
            )
            conn.commit()
            zone_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM sentinel_zones WHERE id = ?", (zone_id,)).fetchone()
        finally:
            conn.close()
    return _public_zone(row)


def update_zone(zone_id: int, payload: object) -> dict | None:
    body = _require_object(payload)
    with _lock:
        current = _get_record(zone_id)
        if current is None:
            return None
        name = _clean_name(body["name"]) if "name" in body else current["name"]
        sector = _clean_sector(body.get("sector")) if "sector" in body else current["sector"]
        lat = _clean_lat(body.get("lat")) if "lat" in body else current["lat"]
        lng = _clean_lng(body.get("lng")) if "lng" in body else current["lng"]
        radius = _clean_radius(body.get("radius_km")) if "radius_km" in body else current["radius_km"]
        minimum = _clean_severity(body.get("min_severity")) if "min_severity" in body else current["min_severity"]
        categories = _clean_categories(body.get("categories")) if "categories" in body else current["categories_stored"]
        if "slack_webhook_url" in body:
            incoming = body.get("slack_webhook_url")
            if _keeps_existing_webhook(incoming, current["slack_webhook_url_raw"]):
                webhook = current["slack_webhook_url_raw"]
            else:
                webhook = _validate_webhook(incoming)
        else:
            webhook = current["slack_webhook_url_raw"]
        email = _clean_email(body.get("alert_email")) if "alert_email" in body else current["alert_email"]
        active = 1 if (_as_bool(body.get("active"), current["active"]) if "active" in body else current["active"]) else 0
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE sentinel_zones
                SET name = ?, sector = ?, lat = ?, lng = ?, radius_km = ?, min_severity = ?,
                    categories = ?, slack_webhook_url = ?, alert_email = ?, active = ?
                WHERE id = ?
                """,
                (name, sector, lat, lng, radius, minimum, categories, webhook, email, active, zone_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM sentinel_zones WHERE id = ?", (zone_id,)).fetchone()
        finally:
            conn.close()
    return _public_zone(row)


def delete_zone(zone_id: int) -> bool:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute("DELETE FROM sentinel_zones WHERE id = ?", (zone_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _pick(source: dict, *keys: str):
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _coord(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _valid_pair(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180


def incident_point(raw: dict) -> tuple[float, float] | None:
    if not isinstance(raw, dict):
        return None
    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else None
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lng = _coord(coords[0])
        lat = _coord(coords[1])
        if lat is not None and lng is not None and _valid_pair(lat, lng):
            return lat, lng
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    for source in (raw, props):
        lat = _coord(_pick(source, "lat", "latitude"))
        lng = _coord(_pick(source, "lng", "lon", "longitude"))
        if lat is not None and lng is not None and _valid_pair(lat, lng):
            return lat, lng
    return None


def _incident_fields(raw: dict) -> dict:
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    title = _pick(props, "title") or _pick(raw, "title") or "Incident"
    severity = _pick(props, "severity") or _pick(raw, "severity") or ""
    category = _pick(props, "category") or _pick(raw, "category") or ""
    explicit = ""
    for source in (raw, props):
        for key in ("incident_id", "event_key", "id", "db_id"):
            value = source.get(key)
            if value not in (None, ""):
                explicit = str(value)[:180]
                break
        if explicit:
            break
    if not explicit:
        for source in (props, raw):
            for key in ("source_url", "source"):
                value = source.get(key)
                if value:
                    explicit = str(value)[:300]
                    break
            if explicit:
                break
    stamp = str(_pick(props, "timestamp", "published_at") or _pick(raw, "timestamp", "published_at") or "")
    return {
        "title": " ".join(str(title).split())[:240] or "Incident",
        "severity": " ".join(str(severity).split())[:20],
        "category": " ".join(str(category).split())[:40],
        "explicit_id": explicit,
        "timestamp": stamp[:40],
    }


def incident_identity(raw: dict, lat: float, lng: float) -> str:
    fields = _incident_fields(raw)
    if fields["explicit_id"]:
        return fields["explicit_id"]
    digest = hashlib.sha256(
        f"{fields['title']}|{fields['timestamp']}|{lat:.5f}|{lng:.5f}".encode("utf-8", "replace")
    ).hexdigest()
    return digest[:32]


def match_incident(zone: dict, raw: dict) -> dict | None:
    """Return a breach for this zone, or None when the incident is outside it."""
    point = incident_point(raw)
    if point is None:
        return None
    lat, lng = point
    fields = _incident_fields(raw)
    if not severity_meets(fields["severity"], zone.get("min_severity")):
        return None
    stored = zone.get("categories_stored", zone.get("categories"))
    if not categories_match(stored, fields["category"]):
        return None
    radius = float(zone.get("radius_km") or 0)
    distance = distance_km(zone["lat"], zone["lng"], lat, lng)
    if distance > radius + 1e-6:
        return None
    bearing = bearing_deg(zone["lat"], zone["lng"], lat, lng)
    cardinal = compass_point(bearing)
    name = zone.get("name") or "Sentinel Zone"
    return {
        "incident_id": incident_identity(raw, lat, lng),
        "title": fields["title"],
        "severity": severity_code(fields["severity"]) or fields["severity"],
        "category": fields["category"],
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "distance_km": round(distance, 3),
        "bearing_deg": round(bearing, 1),
        "cardinal": cardinal,
        "phrase": cardinal_dir(zone["lat"], zone["lng"], lat, lng, name),
    }


def _active_records() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sentinel_zones WHERE active = 1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [_zone_record(row) for row in rows]


def _insert_hit(conn: sqlite3.Connection, zone_id: int, hit: dict) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO sentinel_hits (
            zone_id, incident_id, distance_km, bearing_deg, cardinal, phrase,
            title, severity, category, lat, lng, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(zone_id, incident_id) DO NOTHING
        """,
        (
            zone_id,
            hit["incident_id"],
            hit["distance_km"],
            hit["bearing_deg"],
            hit["cardinal"],
            hit["phrase"],
            hit["title"],
            hit["severity"],
            hit["category"],
            hit["lat"],
            hit["lng"],
            utc_stamp(),
        ),
    )
    return cursor.rowcount > 0


def list_hits(limit: int = 50) -> list[dict]:
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 50
    size = min(200, max(1, size))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT h.*, z.name AS zone_name, z.sector AS zone_sector
                FROM sentinel_hits h
                JOIN sentinel_zones z ON z.id = h.zone_id
                ORDER BY h.id DESC
                LIMIT ?
                """,
                (size,),
            ).fetchall()
        finally:
            conn.close()
    hits = []
    for row in rows:
        hits.append(
            {
                "id": row["id"],
                "zone_id": row["zone_id"],
                "zone_name": row["zone_name"],
                "sector": row["zone_sector"] or "",
                "incident_id": row["incident_id"],
                "title": row["title"],
                "severity": row["severity"],
                "category": row["category"],
                "lat": row["lat"],
                "lng": row["lng"],
                "distance_km": row["distance_km"],
                "bearing_deg": row["bearing_deg"],
                "cardinal": row["cardinal"],
                "phrase": row["phrase"],
                "created_at": row["created_at"],
            }
        )
    return hits


def _slack_escape(value: object) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(zone: dict, hit: dict) -> dict:
    name = _slack_escape(zone.get("name") or "Sentinel Zone")
    title = _slack_escape(hit.get("title") or "Incident")
    phrase = _slack_escape(hit.get("phrase") or "")
    severity = _slack_escape(hit.get("severity") or "")
    category = _slack_escape(hit.get("category") or "Unspecified")
    sector = _slack_escape(zone.get("sector") or "Unspecified")
    fallback = f"Sentinel Zone {zone.get('name') or ''}: {hit.get('title') or 'Incident'} — {hit.get('phrase') or ''}"
    return {
        "text": fallback[:280],
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Sentinel Zone breach", "emoji": False},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{name}*\n{phrase}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Incident*\n{title}"},
                    {"type": "mrkdwn", "text": f"*Severity*\n{severity}"},
                    {"type": "mrkdwn", "text": f"*Category*\n{category}"},
                    {"type": "mrkdwn", "text": f"*Sector*\n{sector}"},
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "GeoRiskIntel Sentinel Zones · place watch only · no personal data",
                    }
                ],
            },
        ],
    }


def _slack_blocking() -> bool:
    return (os.environ.get("SENTINEL_SLACK_SYNC") or "").strip().lower() in {"1", "true", "yes"}


def post_slack(url: str, payload: dict) -> None:
    try:
        requests.post(url, json=payload, timeout=SLACK_TIMEOUT)
    except Exception as exc:
        print(f"Sentinel Slack alert was not delivered: {exc.__class__.__name__}.")


def dispatch_slack(pairs: list[tuple[dict, dict]]) -> int:
    queued: list[tuple[str, dict]] = []
    for zone, hit in pairs:
        url = str(zone.get("slack_webhook_url_raw") or "")
        if not slack_url_allowed(url):
            continue
        queued.append((url, slack_payload(zone, hit)))
    if not queued:
        return 0

    def _send() -> None:
        for url, payload in queued:
            post_slack(url, payload)

    try:
        if _slack_blocking():
            _send()
        else:
            threading.Thread(target=_send, name="sentinel-slack", daemon=True).start()
    except Exception as exc:
        print(f"Sentinel Slack alert was not scheduled: {exc.__class__.__name__}.")
    return len(queued)


def evaluate_incidents_for_zones(incidents) -> dict:
    """Find new incidents inside active zones, store each pair once, and alert."""
    rows = incidents if isinstance(incidents, list) else []
    stats = {
        "ok": True,
        "zones": 0,
        "scanned": len(rows),
        "matches": 0,
        "new_hits": 0,
        "alerts": 0,
        "hits": [],
    }
    with _lock:
        zones = _active_records()
        stats["zones"] = len(zones)
        fresh: list[tuple[dict, dict]] = []
        conn = _connect()
        try:
            for zone in zones:
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    hit = match_incident(zone, raw)
                    if hit is None:
                        continue
                    stats["matches"] += 1
                    if not _insert_hit(conn, int(zone["id"]), hit):
                        continue
                    stats["new_hits"] += 1
                    stored = dict(hit)
                    stored["zone_id"] = zone["id"]
                    stored["zone_name"] = zone["name"]
                    stats["hits"].append(stored)
                    fresh.append((zone, hit))
            conn.commit()
        finally:
            conn.close()
    stats["alerts"] = dispatch_slack(fresh)
    return stats


def scan_zone(zone_id: int, incidents: list | None = None) -> dict | None:
    """Immediate read-only scan of one zone against the supplied or live incidents."""
    with _lock:
        zone = _get_record(zone_id)
    if zone is None:
        return None
    rows = incidents if isinstance(incidents, list) else load_live_incidents()
    matches = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        hit = match_incident(zone, raw)
        if hit is not None:
            matches.append(hit)
    return {
        "ok": True,
        "zone_id": zone_id,
        "active": zone["active"],
        "scanned": len(rows) if isinstance(rows, list) else 0,
        "matches": matches,
    }


def load_live_incidents() -> list:
    """Current map incidents. A feed failure returns an empty list."""
    try:
        import serve_map

        payload = serve_map.data_response()
    except Exception as exc:
        print(f"Sentinel could not read live incidents: {exc.__class__.__name__}.")
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    features = payload.get("features")
    if isinstance(features, list) and features:
        return [item for item in features if isinstance(item, dict)]
    events = payload.get("events")
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    return []


def _page_bytes() -> bytes:
    path = ROOT / "static" / "sentinel-zones.html"
    if not path.is_file():
        return b""
    return path.read_bytes()


@sentinel_bp.after_request
def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


@sentinel_bp.get("/sentinel-zones")
def sentinel_page() -> Response:
    body = _page_bytes()
    if not body:
        return Response("Sentinel Zones page is not available.", status=404, mimetype="text/plain")
    return Response(body, mimetype="text/html; charset=utf-8")


@sentinel_bp.get("/api/sentinel-zones")
def api_list_zones():
    return jsonify({"ok": True, "zones": list_zones()})


@sentinel_bp.post("/api/sentinel-zones")
def api_create_zone():
    try:
        zone = create_zone(request.get_json(silent=True) or {})
    except ZoneError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "zone": zone})


@sentinel_bp.get("/api/sentinel-zones/hits")
def api_hits():
    return jsonify({"ok": True, "hits": list_hits(request.args.get("limit", 50))})


@sentinel_bp.put("/api/sentinel-zones/<int:zone_id>")
def api_update_zone(zone_id: int):
    try:
        zone = update_zone(zone_id, request.get_json(silent=True) or {})
    except ZoneError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if zone is None:
        return jsonify({"ok": False, "message": "Zone not found."}), 404
    return jsonify({"ok": True, "zone": zone})


@sentinel_bp.delete("/api/sentinel-zones/<int:zone_id>")
def api_delete_zone(zone_id: int):
    if not delete_zone(zone_id):
        return jsonify({"ok": False, "message": "Zone not found."}), 404
    return jsonify({"ok": True, "deleted": zone_id})


@sentinel_bp.post("/api/sentinel-zones/<int:zone_id>/check")
def api_check_zone(zone_id: int):
    try:
        result = scan_zone(zone_id)
    except Exception as exc:
        print(f"Sentinel zone scan failed: {exc.__class__.__name__}.")
        return jsonify({"ok": False, "message": "Scan failed."}), 500
    if result is None:
        return jsonify({"ok": False, "message": "Zone not found."}), 404
    return jsonify(result)
