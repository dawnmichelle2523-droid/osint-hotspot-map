"""Live Slack ops-room alerts for GeoRiskIntel.

Channels live in their own SQLite file so they never lock the incidents
database. Matching incidents are pushed as Slack Block Kit cards. The
dispatcher does not call xAI and never raises into the feed poller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
DISPATCH_SECONDS = 20
WEBHOOK_TIMEOUT = 6
MAX_SENDS_PER_CYCLE = 20
MAX_ATTEMPTS = 3
COLOR_HIGH = "#dc2626"
COLOR_MED = "#f59e0b"
COLOR_LOW = "#3b82f6"
CATEGORY_CODES = ("ACT", "SFA", "VC", "CU", "HS", "OD", "TSUNAMI")
SEVERITY_CODES = ("HIGH", "MED", "LOW", "ALL")
SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MED": 2, "HIGH": 3}
GULF_COUNTRIES = {"bahrain", "kuwait", "qatar", "uae", "united arab emirates", "oman", "gulf states"}
_WEBHOOK_RE = re.compile(
    r"^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+$"
)
_LABEL_CATEGORY = {
    "act": "ACT",
    "sfa": "SFA",
    "saf": "SFA",
    "vc": "VC",
    "cu": "CU",
    "hs": "HS",
    "od": "OD",
    "tsunami": "TSUNAMI",
    "small arms fire (saf)": "SFA",
    "security/clashes": "SFA",
    "security": "SFA",
    "kinetic/military": "ACT",
    "conflict": "ACT",
    "indirect fire (idf) and air strikes": "ACT",
    "suicide / vehicle-borne improvised explosive device (s/vbied)": "ACT",
    "improvised explosive device (ied)": "ACT",
    "explosively formed projectile (efp)": "ACT",
    "unexploded ordnance (uxo)": "ACT",
    "kidnap": "VC",
    "trauma / casualty": "VC",
    "civil unrest": "CU",
    "detention / arrest (checkpoints)": "HS",
    "maritime": "HS",
    "travel advisory": "HS",
    "weather": "HS",
    "disaster": "HS",
    "seismic": "HS",
    "volcano": "HS",
    "cyclone": "HS",
    "political/other": "OD",
    "other / advisory": "OD",
    "miscellaneous": "OD",
    "cyber": "OD",
    "aviation": "OD",
    "road traffic collision (rtc)": "OD",
}
_THREAT_CATEGORY = {
    1: "SFA",
    2: "ACT",
    3: "ACT",
    4: "ACT",
    5: "ACT",
    6: "ACT",
    7: "VC",
    8: "OD",
    9: "CU",
    10: "VC",
    11: "HS",
    12: "OD",
}
_SAMPLE_COPY = {
    "ACT": (
        "Indirect fire reported near Aleppo",
        "Multiple impacts were reported on the northern edge of Aleppo. Local channels describe ongoing artillery fire and movement restrictions on the airport road.",
    ),
    "SFA": (
        "Small-arms fire reported in eastern Mosul",
        "Short bursts of gunfire were reported near a market street in eastern Mosul. Traffic is moving, and no wider cordon has been confirmed.",
    ),
    "VC": (
        "Casualty report after an explosion in Khartoum",
        "Medical channels report casualties after an explosion in central Khartoum. The exact site and responsible party are not confirmed.",
    ),
    "CU": (
        "Protest gathering reported in central Beirut",
        "A crowd has gathered near a government building in Beirut. Roads in the immediate area are slow, and no shots have been reported.",
    ),
    "HS": (
        "Travel advisory raised for the Red Sea approaches",
        "An official advisory warns of elevated risk for vessels near the Bab el-Mandeb approaches. Mariners are told to review routing before departure.",
    ),
    "OD": (
        "Airport disruption reported in Baghdad",
        "Departure boards show delays after an operational disruption at Baghdad airport. The cause is still being confirmed by local channels.",
    ),
    "TSUNAMI": (
        "Tsunami warning issued for the Pacific coast",
        "A tsunami warning is in effect for coastal zones after a strong offshore earthquake. People near the shoreline are advised to move to higher ground.",
    ),
}

slack_bp = Blueprint("slack_alerts", __name__)
_lock = threading.RLock()
_kick = threading.Event()
_started = False


class SlackConfigError(ValueError):
    """The channel payload cannot be stored."""


def resolve_db_path(
    env_value: str | None = None,
    data_dir: Path | None = None,
    fallback: Path | None = None,
) -> Path:
    """Prefer /data/slack_alerts.db when that directory is writable."""
    if env_value is None:
        env_value = (os.environ.get("SLACK_ALERTS_DB") or "").strip()
    if env_value:
        return Path(env_value)
    data = Path("/data") if data_dir is None else data_dir
    try:
        if data.is_dir() and os.access(data, os.W_OK):
            return data / "slack_alerts.db"
    except OSError:
        pass
    if fallback is not None:
        return fallback
    return Path.cwd() / "slack_alerts.db"


def db_path() -> Path:
    return resolve_db_path()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: datetime | None = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_webhook(url: object) -> str:
    text = str(url or "").strip()
    if not _WEBHOOK_RE.fullmatch(text):
        raise SlackConfigError("Webhook URL must be an https://hooks.slack.com/services/… URL.")
    return text


def mask_webhook(url: object) -> str:
    """Show the team and bot initials and only the last four secret characters."""
    text = str(url or "").strip()
    parts = text.split("/")
    if len(parts) < 7 or parts[2] != "hooks.slack.com":
        return "https://hooks.slack.com/services/••••"
    team = parts[4] or "T"
    bot = parts[5] or "B"
    secret = parts[6]
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"https://hooks.slack.com/services/{team[:1]}.../{bot[:1]}.../••••{tail}"


def _clean_base(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith("http://") and not text.startswith("https://"):
        text = "https://" + text
    return text


def public_base_url(explicit: str | None = None) -> str:
    cleaned = _clean_base(explicit)
    if cleaned:
        return cleaned
    for key in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        cleaned = _clean_base(os.environ.get(key) or "")
        if cleaned:
            return cleaned
    railway = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if railway:
        return _clean_base(railway)
    try:
        from flask import has_request_context, request as flask_request

        if has_request_context():
            return flask_request.host_url.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:5000"


def map_link(country: object, base_url: str | None = None) -> str:
    base = public_base_url(base_url)
    name = " ".join(str(country or "").split())
    if not name:
        return base + "/"
    return base + "/?country=" + quote(name, safe="")


def _loads(value: object, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _country_keys(name: object) -> set[str]:
    text = " ".join(str(name or "").lower().replace("&", " and ").split())
    groups = {
        "uae": {"uae", "united arab emirates"},
        "united arab emirates": {"uae", "united arab emirates"},
        "united states": {"united states", "usa", "us", "united states of america"},
        "usa": {"united states", "usa", "us", "united states of america"},
        "us": {"united states", "usa", "us", "united states of america"},
        "united states of america": {"united states", "usa", "us", "united states of america"},
        "uk": {"uk", "united kingdom", "great britain"},
        "united kingdom": {"uk", "united kingdom", "great britain"},
        "great britain": {"uk", "united kingdom", "great britain"},
    }
    return groups.get(text, {text} if text else set())


def _theatre_names(country: object) -> set[str]:
    names = set()
    try:
        import feed_quality

        theatre = feed_quality.theatre_for(str(country or ""))
        if theatre:
            names.add(str(theatre).strip().lower())
    except Exception:
        pass
    return names


def category_code(incident: dict) -> str:
    flat = _flat(incident)
    texts = []
    for key in ("category_code", "category", "feed_category"):
        raw = " ".join(str(flat.get(key) or "").split())
        if raw:
            texts.append(raw)
    for raw in texts:
        if raw.upper() in CATEGORY_CODES:
            return raw.upper()
    blob = " ".join(texts + [str(flat.get("title") or ""), str(flat.get("summary") or "")]).lower()
    if re.search(r"\btsunami\b", blob):
        return "TSUNAMI"
    for raw in texts:
        mapped = _LABEL_CATEGORY.get(raw.lower())
        if mapped:
            return mapped
    try:
        code = int(flat.get("threat_code"))
    except (TypeError, ValueError):
        code = None
    if code in _THREAT_CATEGORY:
        return _THREAT_CATEGORY[code]
    return "OD"


def normalize_severity(value: object, category: str = "") -> str:
    text = " ".join(str(value or "").split()).upper()
    if text in {"HIGH", "H", "CRITICAL", "EXTREME", "SEVERE"}:
        level = "HIGH"
    elif text in {"MED", "MEDIUM", "M"}:
        level = "MED"
    elif text in {"LOW", "L"}:
        level = "LOW"
    elif text in {"INFO", "INFORMATION"}:
        level = "INFO"
    else:
        level = "MED"
    if category == "TSUNAMI" and SEVERITY_RANK[level] < SEVERITY_RANK["HIGH"]:
        return "HIGH"
    return level


def _flat(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    props = raw.get("properties")
    if isinstance(props, dict):
        merged = dict(props)
        for key in ("id", "feed_id", "event_key", "db_id"):
            if raw.get(key) not in (None, "") and merged.get(key) in (None, ""):
                merged[key] = raw[key]
        return merged
    return dict(raw)


def event_key_for(raw: dict) -> str:
    flat = _flat(raw)
    explicit = str(flat.get("event_key") or "").strip()
    if explicit:
        return explicit[:180]
    db_id = flat.get("db_id")
    if db_id not in (None, ""):
        return f"item:{db_id}"
    feed = str(flat.get("feed_id") or "").strip()
    external = str(flat.get("id") or "").strip()
    if feed and external:
        return f"feed:{feed}:{external}"[:180]
    source = str(flat.get("source") or flat.get("source_url") or "")
    title = str(flat.get("title") or "")
    stamp = str(flat.get("timestamp") or flat.get("published_at") or flat.get("ingested_at") or "")
    digest = hashlib.sha256(f"{feed}|{source}|{title}|{stamp}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"evt:{digest}"


def normalize_incident(raw: dict) -> dict:
    flat = _flat(raw)
    category = category_code(flat)
    severity = normalize_severity(flat.get("severity"), category)
    country = " ".join(str(flat.get("country") or "").split())
    city = " ".join(
        str(flat.get("city") or flat.get("location_name") or flat.get("governorate") or flat.get("area") or "").split()
    )
    title = " ".join(str(flat.get("title") or "").split()) or "Incident"
    summary = " ".join(str(flat.get("summary") or "").split())
    source_name = " ".join(
        str(flat.get("source_name") or flat.get("source_label") or flat.get("account") or "").split()
    )
    source = str(flat.get("source") or flat.get("source_url") or "").strip()
    timestamp = str(flat.get("timestamp") or flat.get("published_at") or flat.get("ingested_at") or "").strip()
    region = " ".join(str(flat.get("theatre") or flat.get("region") or "").split())
    incident = {
        "event_key": event_key_for(flat),
        "title": title[:240],
        "summary": summary[:700],
        "country": country[:80],
        "city": city[:80],
        "region": region[:80],
        "category": category,
        "severity": severity,
        "source_name": source_name[:120],
        "source": source[:300],
        "timestamp": timestamp or utc_stamp(),
        "threat_code": flat.get("threat_code"),
    }
    for key in ("confidence_tier", "confidence_label", "confidence_score", "corroborating_sources", "corroboration_count"):
        if key not in flat:
            continue
        value = flat.get(key)
        if value is None or value == "":
            continue
        incident[key] = list(value) if key == "corroborating_sources" and isinstance(value, list) else value
    return incident


def _region_candidates(incident: dict) -> set[str]:
    names = set()
    for value in (incident.get("region"), incident.get("country")):
        text = " ".join(str(value or "").split()).lower()
        if text:
            names.add(text)
    names.update(_theatre_names(incident.get("country")))
    if incident.get("region"):
        names.update(_theatre_names(incident.get("region")))
    return names


def _list_has_all(values: list) -> bool:
    return any(str(item or "").strip().upper() == "ALL" for item in values)


def country_selected(wanted: list, country: object) -> bool:
    if not wanted or _list_has_all(wanted):
        return True
    incident_keys = _country_keys(country)
    if not incident_keys:
        return False
    for item in wanted:
        label = " ".join(str(item or "").split())
        if not label:
            continue
        if label.lower() == "gulf states" and incident_keys & GULF_COUNTRIES:
            return True
        if _country_keys(label) & incident_keys:
            return True
    return False


def region_selected(wanted: list, incident: dict) -> bool:
    if not wanted or _list_has_all(wanted):
        return True
    candidates = _region_candidates(incident)
    for item in wanted:
        label = " ".join(str(item or "").split()).lower()
        if label and label in candidates:
            return True
        if label == "gulf states" and _country_keys(incident.get("country")) & GULF_COUNTRIES:
            return True
    return False


def severity_selected(minimum: object, severity: str) -> bool:
    floor = str(minimum or "ALL").strip().upper()
    if floor in {"", "ALL"}:
        return True
    if floor == "MEDIUM":
        floor = "MED"
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(floor, 3)


def channel_matches(channel: dict, incident: dict) -> bool:
    item = incident if incident.get("event_key") and incident.get("category") in CATEGORY_CODES else normalize_incident(incident)
    if not channel.get("enabled", True):
        return False
    categories = list(channel.get("categories") or ["ALL"])
    if not _list_has_all(categories) and item["category"] not in {str(code).upper() for code in categories}:
        return False
    if not severity_selected(channel.get("min_severity") or "ALL", item["severity"]):
        return False
    if not region_selected(list(channel.get("regions") or ["ALL"]), item):
        return False
    if not country_selected(list(channel.get("countries") or []), item.get("country")):
        return False
    return True


def slack_escape(value: object) -> str:
    text = str(value or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("*", "∗").replace("`", "'")


def two_line_summary(value: object, width: int = 120) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= width:
        return clean
    cut = clean.rfind(" ", 0, width + 1)
    if cut < width // 2:
        cut = width
    first = clean[:cut].rstrip()
    second = clean[cut:].strip()
    if len(second) > width:
        second = second[: width - 1].rstrip() + "…"
    return first + "\n" + second


def display_stamp(value: object) -> str:
    text = str(value or "").strip().replace("T", " ").replace("Z", "")
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    text = " ".join(text.split())
    if len(text) >= 16:
        text = text[:16]
    return (text or utc_stamp().replace("T", " ").replace("Z", "")[:16]) + " UTC"


def alert_color(severity: str, category: str) -> str:
    if category == "TSUNAMI" or severity == "HIGH":
        return COLOR_HIGH
    if severity == "MED":
        return COLOR_MED
    return COLOR_LOW


def _source_label(incident: dict) -> str:
    if incident.get("source_name"):
        return str(incident["source_name"])
    source = str(incident.get("source") or "")
    if "://" in source:
        host = source.split("://", 1)[1].split("/", 1)[0]
        if host:
            return host
    return "OSINT feed"


def _header_text(incident: dict) -> str:
    parts = [incident.get("severity") or "MED"]
    if incident.get("category"):
        parts.append(incident["category"])
    if incident.get("country"):
        parts.append(incident["country"])
    if incident.get("city"):
        parts.append(incident["city"])
    return " · ".join(str(part) for part in parts if part)[:150]


def confidence_badge_text(incident: dict) -> str:
    """Short confidence line for a Block Kit card. Empty when the incident has no tier."""
    if not isinstance(incident, dict):
        return ""
    label = " ".join(str(incident.get("confidence_label") or "").split())
    tier = " ".join(str(incident.get("confidence_tier") or "").split()).lower()
    if not label and tier in {"official", "corroborated", "unverified"}:
        label = {
            "official": "Official Advisory",
            "corroborated": "Multi-Source Corroborated",
            "unverified": "Single-Source Unverified",
        }[tier]
    if not label:
        return ""
    parts = [label]
    score = incident.get("confidence_score")
    if isinstance(score, bool):
        score = None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        parts.append(str(int(score)))
    elif isinstance(score, str) and score.strip().isdigit():
        parts.append(score.strip())
    sources = incident.get("corroborating_sources")
    try:
        count = int(incident.get("corroboration_count"))
    except (TypeError, ValueError):
        count = len(sources) if isinstance(sources, list) else 0
    if count >= 2 and isinstance(sources, list):
        names = [" ".join(str(name).split()) for name in sources if " ".join(str(name).split())]
        if names:
            parts.append(", ".join(names))
    return " · ".join(parts)


def _button(country: object, base_url: str | None) -> dict:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "View on Live Map", "emoji": True},
        "url": map_link(country, base_url),
        "action_id": "view_live_map",
    }


def build_block_kit(incident: dict, base_url: str | None = None) -> dict:
    item = normalize_incident(incident)
    color = alert_color(item["severity"], item["category"])
    summary = two_line_summary(item["summary"])
    section = f"*{slack_escape(item['title'])}*"
    if summary:
        section += "\n" + slack_escape(summary)
    fallback = f"GeoRiskIntel {item['severity']} · {item['country']} · {item['title']}"[:300]
    context = f"Source: *{slack_escape(_source_label(item))}* · {display_stamp(item['timestamp'])}"
    elements = [{"type": "mrkdwn", "text": context[:300]}]
    badge = confidence_badge_text(item)
    if badge:
        elements.append({"type": "mrkdwn", "text": ("Confidence: " + slack_escape(badge))[:300]})
    return {
        "text": fallback,
        "attachments": [
            {
                "color": color,
                "fallback": fallback,
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": _header_text(item), "emoji": True}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": section[:3000]}},
                    {"type": "context", "elements": elements},
                    {"type": "actions", "elements": [_button(item.get("country"), base_url)]},
                ],
            }
        ],
    }


def build_digest_payload(incidents: list[dict], base_url: str | None = None) -> dict:
    items = [normalize_incident(item) for item in incidents if isinstance(item, dict)]
    if not items:
        return build_block_kit(sample_incident(), base_url)
    top = items[0]
    color = COLOR_LOW
    for item in items:
        if alert_color(item["severity"], item["category"]) == COLOR_HIGH:
            color = COLOR_HIGH
            break
        if item["severity"] == "MED":
            color = COLOR_MED
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"GeoRiskIntel digest · {len(items)} incidents"[:150], "emoji": True},
        }
    ]
    for item in items[:8]:
        line = two_line_summary(item["summary"], 90).split("\n", 1)[0]
        place = " · ".join(part for part in (item["severity"], item["category"], item["country"], item["city"]) if part)
        text = f"*{slack_escape(place)}*\n*{slack_escape(item['title'])}*"
        if line:
            text += "\n" + slack_escape(line)
        badge = confidence_badge_text(item)
        if badge:
            text += "\n" + slack_escape("Confidence: " + badge)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Batched MED/LOW incidents. HIGH and Tsunami alerts are not held."}],
        }
    )
    blocks.append({"type": "actions", "elements": [_button(top.get("country"), base_url)]})
    fallback = f"GeoRiskIntel digest · {len(items)} incidents"
    return {"text": fallback, "attachments": [{"color": color, "fallback": fallback, "blocks": blocks}]}


def sample_incident() -> dict:
    title, summary = _SAMPLE_COPY["ACT"]
    return {
        "event_key": "sample",
        "title": title,
        "summary": summary,
        "country": "Syria",
        "city": "Aleppo",
        "region": "Middle East",
        "category": "ACT",
        "severity": "HIGH",
        "source_name": "GeoRiskIntel desk",
        "source": "https://georiskintel.example/sources/sample",
        "timestamp": utc_stamp(),
    }


def preview_incident(overrides: dict | None = None) -> dict:
    raw = dict(overrides or {})
    category = category_code(raw) if any(raw.get(key) for key in ("category", "feed_category", "category_code", "title")) else "ACT"
    if str(raw.get("category") or raw.get("feed_category") or "").strip().upper() == "TSUNAMI":
        category = "TSUNAMI"
    title, summary = _SAMPLE_COPY.get(category, _SAMPLE_COPY["ACT"])
    base = sample_incident()
    base.update(
        {
            "title": raw.get("title") or title,
            "summary": raw.get("summary") or summary,
            "country": raw.get("country") or "Syria",
            "city": raw.get("city") or raw.get("location_name") or "Aleppo",
            "region": raw.get("region") or raw.get("theatre") or "Middle East",
            "category": category,
            "severity": raw.get("severity") or ("HIGH" if category == "TSUNAMI" else "HIGH"),
            "source_name": raw.get("source_name") or "GeoRiskIntel desk",
            "timestamp": raw.get("timestamp") or utc_stamp(),
        }
    )
    return normalize_incident(base)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            regions TEXT NOT NULL DEFAULT '["ALL"]',
            countries TEXT NOT NULL DEFAULT '[]',
            categories TEXT NOT NULL DEFAULT '["ALL"]',
            min_severity TEXT NOT NULL DEFAULT 'ALL',
            rate_limit_per_hour INTEGER NOT NULL DEFAULT 20,
            digest_minutes INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_sent_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            http_status INTEGER,
            detail TEXT NOT NULL DEFAULT '',
            channel_label TEXT NOT NULL DEFAULT '',
            UNIQUE(channel_id, event_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_pending (
            channel_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            incident_json TEXT NOT NULL,
            queued_at TEXT NOT NULL,
            flush_after TEXT NOT NULL,
            reason TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (channel_id, event_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_slack_deliveries_channel_sent ON slack_deliveries(channel_id, sent_at)"
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM slack_meta WHERE key = ?", (key,)).fetchone()
    return "" if row is None else str(row["value"])


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO slack_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _row_channel(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "label": row["label"],
        "webhook_url": row["webhook_url"],
        "regions": list(_loads(row["regions"], ["ALL"])),
        "countries": list(_loads(row["countries"], [])),
        "categories": list(_loads(row["categories"], ["ALL"])),
        "min_severity": row["min_severity"] or "ALL",
        "rate_limit_per_hour": int(row["rate_limit_per_hour"] or 20),
        "digest_minutes": int(row["digest_minutes"] or 0),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_sent_at": row["last_sent_at"] or "",
    }


def public_channel(channel: dict) -> dict:
    shown = dict(channel)
    shown.pop("webhook_url", None)
    shown["webhook_url_masked"] = mask_webhook(channel.get("webhook_url") or "")
    return shown


def list_channels() -> list[dict]:
    with _lock:
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM slack_channels ORDER BY id ASC").fetchall()
            return [_row_channel(row) for row in rows]
        finally:
            conn.close()


def get_channel(channel_id: int) -> dict | None:
    with _lock:
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM slack_channels WHERE id = ?", (int(channel_id),)).fetchone()
            return None if row is None else _row_channel(row)
        finally:
            conn.close()


def _choice_list(value, *, allowed: set[str] | None, default: list[str], label: str) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            value = _loads(text, None)
            if not isinstance(value, list):
                raise SlackConfigError(f"{label} must be a list.")
        else:
            value = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, list):
        raise SlackConfigError(f"{label} must be a list.")
    cleaned: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        upper = text.upper()
        if upper == "ALL":
            return ["ALL"]
        if allowed is not None and upper not in allowed:
            raise SlackConfigError(f"{label} must be one of {', '.join(sorted(allowed))}, or ALL.")
        canonical = upper if allowed is not None else text
        if canonical not in cleaned:
            cleaned.append(canonical)
        if len(cleaned) >= 40:
            break
    return cleaned or list(default)


def _as_int(value, default: int, low: int, high: int, label: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SlackConfigError(f"{label} must be a whole number.") from None
    if number < low or number > high:
        raise SlackConfigError(f"{label} must be between {low} and {high}.")
    return number


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise SlackConfigError("enabled must be true or false.")


def _keep_webhook(value: object, existing: str) -> bool:
    text = str(value or "").strip()
    if not text or "•" in text:
        return True
    return bool(existing) and text == mask_webhook(existing)


def save_channel(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise SlackConfigError("Channel settings must be an object.")
    raw_id = payload.get("id")
    existing = None
    if raw_id not in (None, ""):
        try:
            channel_id = int(raw_id)
        except (TypeError, ValueError):
            raise SlackConfigError("Channel id must be a number.") from None
        existing = get_channel(channel_id)
        if existing is None:
            raise SlackConfigError("Channel not found.")
    label = payload.get("label", None if existing is None else existing["label"])
    label = " ".join(str(label or "").split())
    if not label:
        raise SlackConfigError("A channel label is required.")
    if len(label) > 80:
        raise SlackConfigError("Channel label must be 80 characters or fewer.")
    if existing is None:
        webhook = validate_webhook(payload.get("webhook_url"))
    elif "webhook_url" not in payload or _keep_webhook(payload.get("webhook_url"), existing["webhook_url"]):
        webhook = existing["webhook_url"]
    else:
        webhook = validate_webhook(payload.get("webhook_url"))
    regions = _choice_list(
        payload.get("regions", None if existing is None else existing["regions"]),
        allowed=None,
        default=["ALL"],
        label="Regions",
    )
    countries = _choice_list(
        payload.get("countries", None if existing is None else existing["countries"]),
        allowed=None,
        default=[],
        label="Countries",
    )
    if countries == ["ALL"]:
        countries = []
    categories = _choice_list(
        payload.get("categories", None if existing is None else existing["categories"]),
        allowed=set(CATEGORY_CODES),
        default=["ALL"],
        label="Categories",
    )
    severity = str(
        payload.get("min_severity", None if existing is None else existing["min_severity"]) or "ALL"
    ).strip().upper()
    if severity == "MEDIUM":
        severity = "MED"
    if severity not in SEVERITY_CODES:
        raise SlackConfigError("Minimum severity must be HIGH, MED, LOW, or ALL.")
    rate = _as_int(
        payload.get("rate_limit_per_hour", None if existing is None else existing["rate_limit_per_hour"]),
        20,
        1,
        500,
        "Hourly rate limit",
    )
    digest = _as_int(
        payload.get("digest_minutes", None if existing is None else existing["digest_minutes"]),
        0,
        0,
        1440,
        "Digest window",
    )
    enabled = _as_bool(payload.get("enabled", None if existing is None else existing["enabled"]), True)
    stamp = utc_stamp()
    with _lock:
        conn = connect()
        try:
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO slack_channels (
                        label, webhook_url, regions, countries, categories, min_severity,
                        rate_limit_per_hour, digest_minutes, enabled, created_at, last_sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        label,
                        webhook,
                        json.dumps(regions, ensure_ascii=False),
                        json.dumps(countries, ensure_ascii=False),
                        json.dumps(categories, ensure_ascii=False),
                        severity,
                        rate,
                        digest,
                        1 if enabled else 0,
                        stamp,
                    ),
                )
                channel_id = int(cursor.lastrowid)
            else:
                channel_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE slack_channels
                    SET label = ?, webhook_url = ?, regions = ?, countries = ?, categories = ?,
                        min_severity = ?, rate_limit_per_hour = ?, digest_minutes = ?, enabled = ?
                    WHERE id = ?
                    """,
                    (
                        label,
                        webhook,
                        json.dumps(regions, ensure_ascii=False),
                        json.dumps(countries, ensure_ascii=False),
                        json.dumps(categories, ensure_ascii=False),
                        severity,
                        rate,
                        digest,
                        1 if enabled else 0,
                        channel_id,
                    ),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM slack_channels WHERE id = ?", (channel_id,)).fetchone()
            return _row_channel(row)
        finally:
            conn.close()


def delete_channel(channel_id: int) -> bool:
    with _lock:
        conn = connect()
        try:
            cursor = conn.execute("DELETE FROM slack_channels WHERE id = ?", (int(channel_id),))
            conn.execute("DELETE FROM slack_pending WHERE channel_id = ?", (int(channel_id),))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def list_deliveries(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with _lock:
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT d.channel_id, d.event_key, d.sent_at, d.status, d.http_status, d.detail, d.channel_label,
                       c.label AS live_label
                FROM slack_deliveries d
                LEFT JOIN slack_channels c ON c.id = d.channel_id
                ORDER BY d.sent_at DESC, d.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    items = []
    for row in rows:
        items.append(
            {
                "channel_id": int(row["channel_id"]),
                "label": row["live_label"] or row["channel_label"] or "",
                "event_key": row["event_key"],
                "sent_at": row["sent_at"],
                "status": row["status"],
                "http_status": row["http_status"],
                "detail": row["detail"] or "",
            }
        )
    return items


def _delivery_status(conn: sqlite3.Connection, channel_id: int, event_key: str) -> str:
    row = conn.execute(
        "SELECT status FROM slack_deliveries WHERE channel_id = ? AND event_key = ?",
        (channel_id, event_key),
    ).fetchone()
    return "" if row is None else str(row["status"] or "")


def _record_delivery(
    conn: sqlite3.Connection,
    channel: dict,
    event_key: str,
    *,
    status: str,
    http_status: int,
    detail: str,
    moment: datetime,
) -> None:
    stamp = utc_stamp(moment)
    conn.execute(
        """
        INSERT INTO slack_deliveries (
            channel_id, event_key, sent_at, status, http_status, detail, channel_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, event_key) DO UPDATE SET
            sent_at = excluded.sent_at,
            status = excluded.status,
            http_status = excluded.http_status,
            detail = excluded.detail,
            channel_label = excluded.channel_label
        """,
        (channel["id"], event_key, stamp, status, http_status, detail[:300], channel.get("label") or ""),
    )
    if status == "sent":
        conn.execute("UPDATE slack_channels SET last_sent_at = ? WHERE id = ?", (stamp, channel["id"]))
    if status in {"sent", "error"}:
        conn.execute(
            "DELETE FROM slack_pending WHERE channel_id = ? AND event_key = ?",
            (channel["id"], event_key),
        )


def _sent_in_last_hour(conn: sqlite3.Connection, channel_id: int, moment: datetime) -> int:
    cutoff = utc_stamp(moment - timedelta(hours=1))
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM slack_deliveries
        WHERE channel_id = ? AND status = 'sent' AND sent_at >= ?
          AND event_key NOT LIKE 'test:%'
        """,
        (channel_id, cutoff),
    ).fetchone()
    return int(row["n"] or 0)


def _enqueue(
    conn: sqlite3.Connection,
    channel: dict,
    incident: dict,
    *,
    moment: datetime,
    reason: str,
    flush_after: datetime,
    attempts: int = 0,
) -> None:
    existing = conn.execute(
        "SELECT attempts FROM slack_pending WHERE channel_id = ? AND event_key = ?",
        (channel["id"], incident["event_key"]),
    ).fetchone()
    if existing is not None and reason != "error":
        return
    conn.execute(
        """
        INSERT INTO slack_pending (
            channel_id, event_key, incident_json, queued_at, flush_after, reason, attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, event_key) DO UPDATE SET
            incident_json = excluded.incident_json,
            flush_after = excluded.flush_after,
            reason = excluded.reason,
            attempts = excluded.attempts
        """,
        (
            channel["id"],
            incident["event_key"],
            json.dumps(incident, ensure_ascii=False),
            utc_stamp(moment),
            utc_stamp(flush_after),
            reason,
            attempts if existing is None else max(attempts, int(existing["attempts"] or 0)),
        ),
    )


def post_webhook(url: str, payload: dict, timeout: float = WEBHOOK_TIMEOUT) -> tuple[int, str]:
    try:
        validate_webhook(url)
    except SlackConfigError:
        return 0, "invalid webhook"
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout,
            allow_redirects=False,
            headers={"Content-Type": "application/json", "User-Agent": "GeoRiskIntel-SlackAlerts/1.0"},
        )
        return int(response.status_code), (response.text or "")[:500]
    except requests.Timeout:
        return 0, "timeout"
    except requests.RequestException as exc:
        return 0, exc.__class__.__name__
    except Exception as exc:
        return 0, exc.__class__.__name__


def _call_poster(poster, url: str, payload: dict) -> tuple[int, str]:
    try:
        result = poster(url, payload, timeout=WEBHOOK_TIMEOUT)
    except Exception as exc:
        return 0, exc.__class__.__name__
    if isinstance(result, tuple) and len(result) >= 2:
        try:
            status = int(result[0])
        except (TypeError, ValueError):
            status = 0
        return status, str(result[1])[:500]
    return 0, "invalid poster response"


def _send_payload(conn, channel, payload, incidents, *, moment, poster, stats, reason: str) -> bool:
    status, detail = _call_poster(poster, channel["webhook_url"], payload)
    ok = 200 <= status < 300
    if ok:
        for incident in incidents:
            _record_delivery(
                conn,
                channel,
                incident["event_key"],
                status="sent",
                http_status=status,
                detail=detail,
                moment=moment,
            )
        stats["sent"] += len(incidents)
        print(f"Slack alert sent to channel {channel['id']} ({len(incidents)} incident(s), HTTP {status}).")
        return True
    stats["errors"] += 1
    print(f"Slack alert failed for channel {channel['id']}: HTTP {status}.")
    for incident in incidents:
        state = _delivery_status(conn, channel["id"], incident["event_key"])
        if state == "sent":
            continue
        row = conn.execute(
            "SELECT attempts FROM slack_pending WHERE channel_id = ? AND event_key = ?",
            (channel["id"], incident["event_key"]),
        ).fetchone()
        attempts = (int(row["attempts"]) if row else 0) + 1
        if attempts >= MAX_ATTEMPTS:
            _record_delivery(
                conn,
                channel,
                incident["event_key"],
                status="error",
                http_status=status,
                detail=detail or reason,
                moment=moment,
            )
        else:
            _enqueue(
                conn,
                channel,
                incident,
                moment=moment,
                reason="error",
                flush_after=moment + timedelta(minutes=2),
                attempts=attempts,
            )
    return False


def _immediate(channel: dict, incident: dict) -> bool:
    if incident["category"] == "TSUNAMI" or incident["severity"] == "HIGH":
        return True
    return int(channel.get("digest_minutes") or 0) <= 0


def _digest_flush_at(conn, channel: dict, moment: datetime) -> datetime:
    row = conn.execute(
        """
        SELECT flush_after FROM slack_pending
        WHERE channel_id = ? AND reason = 'digest' AND flush_after > ?
        ORDER BY flush_after ASC LIMIT 1
        """,
        (channel["id"], utc_stamp(moment)),
    ).fetchone()
    if row and row["flush_after"]:
        try:
            return datetime.strptime(row["flush_after"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return moment + timedelta(minutes=max(1, int(channel.get("digest_minutes") or 0)))


def _dispatch_unlocked(incidents: list[dict], *, moment: datetime, poster, base_url: str | None, stats: dict) -> None:
    channels = [channel for channel in _load_channels_conn_ready() if channel.get("enabled")]
    sends = 0
    conn = connect()
    try:
        for raw in incidents:
            if not isinstance(raw, dict):
                stats["skipped"] += 1
                continue
            incident = normalize_incident(raw)
            for channel in channels:
                if sends >= MAX_SENDS_PER_CYCLE:
                    break
                if not channel_matches(channel, incident):
                    stats["skipped"] += 1
                    continue
                state = _delivery_status(conn, channel["id"], incident["event_key"])
                if state in {"sent", "error"}:
                    stats["skipped"] += 1
                    continue
                if not _immediate(channel, incident):
                    _enqueue(
                        conn,
                        channel,
                        incident,
                        moment=moment,
                        reason="digest",
                        flush_after=_digest_flush_at(conn, channel, moment),
                    )
                    stats["queued"] += 1
                    continue
                if _sent_in_last_hour(conn, channel["id"], moment) >= int(channel["rate_limit_per_hour"]):
                    _enqueue(
                        conn,
                        channel,
                        incident,
                        moment=moment,
                        reason="rate",
                        flush_after=moment + timedelta(minutes=1),
                    )
                    stats["queued"] += 1
                    continue
                _send_payload(
                    conn,
                    channel,
                    build_block_kit(incident, base_url),
                    [incident],
                    moment=moment,
                    poster=poster,
                    stats=stats,
                    reason="immediate",
                )
                sends += 1
        _flush_unlocked(conn, moment=moment, poster=poster, base_url=base_url, stats=stats, budget=MAX_SENDS_PER_CYCLE - sends)
        conn.commit()
    finally:
        conn.close()


def _load_channels_conn_ready() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM slack_channels ORDER BY id ASC").fetchall()
        return [_row_channel(row) for row in rows]
    finally:
        conn.close()


def _flush_unlocked(conn, *, moment, poster, base_url, stats, budget: int) -> None:
    if budget <= 0:
        return
    rows = conn.execute(
        "SELECT * FROM slack_pending WHERE flush_after <= ? ORDER BY flush_after ASC, event_key ASC",
        (utc_stamp(moment),),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["channel_id"]), []).append(row)
    channels = {channel["id"]: channel for channel in _load_channels_conn_ready() if channel.get("enabled")}
    for channel_id, pending_rows in grouped.items():
        channel = channels.get(channel_id)
        if channel is None or budget <= 0:
            continue
        ready = []
        for row in pending_rows:
            state = _delivery_status(conn, channel_id, row["event_key"])
            if state in {"sent", "error"}:
                conn.execute(
                    "DELETE FROM slack_pending WHERE channel_id = ? AND event_key = ?",
                    (channel_id, row["event_key"]),
                )
                continue
            try:
                incident = json.loads(row["incident_json"])
            except json.JSONDecodeError:
                continue
            if not channel_matches(channel, incident):
                conn.execute(
                    "DELETE FROM slack_pending WHERE channel_id = ? AND event_key = ?",
                    (channel_id, row["event_key"]),
                )
                continue
            ready.append(normalize_incident(incident))
        if not ready:
            continue
        ready.sort(key=lambda item: (0 if item["severity"] == "HIGH" or item["category"] == "TSUNAMI" else 1, item["event_key"]))
        remaining = int(channel["rate_limit_per_hour"]) - _sent_in_last_hour(conn, channel_id, moment)
        if remaining <= 0:
            continue
        batch = ready[: min(remaining, budget, 8)]
        digest_only = all(item["severity"] in {"MED", "LOW", "INFO"} and item["category"] != "TSUNAMI" for item in batch)
        payload = build_digest_payload(batch, base_url) if digest_only and len(batch) > 1 else build_block_kit(batch[0], base_url)
        if not digest_only or len(batch) == 1:
            batch = batch[:1]
        if _send_payload(conn, channel, payload, batch, moment=moment, poster=poster, stats=stats, reason="flush"):
            budget -= len(batch)


def dispatch_incidents(
    incidents: list[dict],
    *,
    now: datetime | None = None,
    poster=None,
    base_url: str | None = None,
) -> dict:
    """Match incidents to enabled channels and post. Never raises."""
    stats = {"ok": True, "sent": 0, "queued": 0, "skipped": 0, "errors": 0}
    moment = now or utc_now()
    sender = poster or post_webhook
    try:
        with _lock:
            _dispatch_unlocked(list(incidents or []), moment=moment, poster=sender, base_url=base_url, stats=stats)
    except Exception as exc:
        print(f"Slack alert dispatch failed: {exc.__class__.__name__}.")
        stats["ok"] = False
        stats["error"] = exc.__class__.__name__
    return stats


def send_test_alert(channel_id: int, base_url: str | None = None, poster=None) -> dict:
    channel = get_channel(channel_id)
    if channel is None:
        return {"ok": False, "status": 404, "error": "Channel not found."}
    incident = sample_incident()
    incident["event_key"] = "test:" + utc_stamp().replace(":", "").replace("-", "")
    payload = build_block_kit(incident, base_url)
    sender = poster or post_webhook
    status, detail = _call_poster(sender, channel["webhook_url"], payload)
    ok = 200 <= status < 300
    with _lock:
        conn = connect()
        try:
            _record_delivery(
                conn,
                channel,
                incident["event_key"],
                status="sent" if ok else "error",
                http_status=status,
                detail=detail,
                moment=utc_now(),
            )
            conn.commit()
        finally:
            conn.close()
    result = {"ok": ok, "status": status, "slack": detail, "channel_id": channel["id"]}
    if not ok:
        result["error"] = detail or "Slack did not accept the test alert."
    return result


def incidents_db_path() -> Path | None:
    override = (os.environ.get("SLACK_INCIDENTS_DB") or "").strip()
    if override:
        return Path(override)
    if (os.environ.get("SLACK_DISABLE_ITEMS") or "").strip() == "1":
        return None
    try:
        import serve_map

        return Path(serve_map.FEEDS_DB)
    except Exception as exc:
        print(f"Slack alerts could not find the incidents database: {exc.__class__.__name__}.")
        return None


def _open_items(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        print(f"Slack alerts could not read incidents: {exc.__class__.__name__}.")
        return None


def _max_item_id(path: Path | None) -> int:
    if path is None:
        return 0
    conn = _open_items(path)
    if conn is None:
        return 0
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        if "id" not in columns:
            return 0
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM items").fetchone()
        return int(row["n"] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def read_new_items(after_id: int) -> tuple[list[dict], int]:
    path = incidents_db_path()
    if path is None:
        return [], after_id
    conn = _open_items(path)
    if conn is None:
        return [], after_id
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        if "id" not in columns:
            return [], after_id
        wanted = [
            "id",
            "title",
            "summary",
            "country",
            "region",
            "severity",
            "category",
            "threat_code",
            "source_url",
            "source_name",
            "location_name",
            "governorate",
            "published_at",
            "ingested_at",
            "feed_category",
        ]
        selected = [name for name in wanted if name in columns]
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM items WHERE id > ? ORDER BY id ASC LIMIT 100",
            (int(after_id),),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"Slack alerts could not scan incidents: {exc.__class__.__name__}.")
        return [], after_id
    finally:
        conn.close()
    incidents = []
    cursor = after_id
    for row in rows:
        keys = set(row.keys())
        cursor = int(row["id"])
        incidents.append(
            {
                "db_id": row["id"],
                "title": row["title"] if "title" in keys else "",
                "summary": row["summary"] if "summary" in keys else "",
                "country": row["country"] if "country" in keys else "",
                "region": row["region"] if "region" in keys else "",
                "severity": row["severity"] if "severity" in keys else "",
                "category": row["category"] if "category" in keys else "",
                "feed_category": row["feed_category"] if "feed_category" in keys else "",
                "threat_code": row["threat_code"] if "threat_code" in keys else None,
                "source": row["source_url"] if "source_url" in keys else "",
                "source_name": row["source_name"] if "source_name" in keys else "",
                "location_name": row["location_name"] if "location_name" in keys else "",
                "governorate": row["governorate"] if "governorate" in keys else "",
                "timestamp": (row["published_at"] if "published_at" in keys else "")
                or (row["ingested_at"] if "ingested_at" in keys else ""),
            }
        )
    return incidents, cursor


def _feed_features() -> list[dict]:
    if (os.environ.get("SLACK_DISABLE_FEEDS") or "").strip() == "1":
        return []
    try:
        import global_feeds

        features = global_feeds.features()
    except Exception as exc:
        print(f"Slack alerts could not read global feeds: {exc.__class__.__name__}.")
        return []
    return features if isinstance(features, list) else []


def _feed_keys(features: list[dict]) -> list[str]:
    keys = []
    for feature in features:
        if isinstance(feature, dict):
            keys.append(event_key_for(feature))
    return keys


def run_cycle(*, now: datetime | None = None, poster=None, base_url: str | None = None) -> dict:
    """Scan newly stored incidents and flush due digests. Never raises."""
    stats = {"ok": True, "sent": 0, "queued": 0, "skipped": 0, "errors": 0, "bootstrapped": False}
    moment = now or utc_now()
    sender = poster or post_webhook
    try:
        with _lock:
            conn = connect()
            try:
                if _meta_get(conn, "bootstrapped") != "1":
                    _meta_set(conn, "items_cursor", str(_max_item_id(incidents_db_path())))
                    _meta_set(conn, "feed_seen", json.dumps(_feed_keys(_feed_features())[:2000]))
                    _meta_set(conn, "bootstrapped", "1")
                    conn.commit()
                    stats["bootstrapped"] = True
                    return stats
                after_id = int(_meta_get(conn, "items_cursor") or "0")
                incidents, cursor = read_new_items(after_id)
                seen = set(_loads(_meta_get(conn, "feed_seen"), []))
                features = _feed_features()
                fresh = []
                current = []
                for feature in features:
                    if not isinstance(feature, dict):
                        continue
                    key = event_key_for(feature)
                    current.append(key)
                    if key not in seen:
                        fresh.append(feature)
                _dispatch_unlocked(incidents + fresh, moment=moment, poster=sender, base_url=base_url, stats=stats)
                _meta_set(conn, "items_cursor", str(cursor))
                ordered = list(dict.fromkeys(current + [key for key in seen]))
                _meta_set(conn, "feed_seen", json.dumps(ordered[:2000]))
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        print(f"Slack alert cycle failed: {exc.__class__.__name__}.")
        stats["ok"] = False
        stats["error"] = exc.__class__.__name__
    return stats


def notify_after_ingest() -> None:
    """Wake the dispatcher without blocking the poller."""
    try:
        _kick.set()
    except Exception as exc:
        print(f"Slack alert hook failed: {exc.__class__.__name__}.")


def _loop() -> None:
    while True:
        try:
            _kick.wait(DISPATCH_SECONDS)
            _kick.clear()
            run_cycle()
        except Exception as exc:
            print(f"Slack alert dispatch failed: {exc.__class__.__name__}.")
            time.sleep(DISPATCH_SECONDS)


def start_dispatcher() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="slack-alerts", daemon=True).start()


def _request_base() -> str:
    return public_base_url(None)


@slack_bp.get("/slack-alerts")
def slack_alerts_page():
    return send_from_directory(ROOT, "slack-alerts.html")


@slack_bp.get("/api/slack/channels")
def api_list_channels():
    return jsonify({"ok": True, "channels": [public_channel(channel) for channel in list_channels()]})


@slack_bp.post("/api/slack/channels")
def api_save_channel():
    try:
        channel = save_channel(request.get_json(silent=True) or {})
    except SlackConfigError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "channel": public_channel(channel)})


@slack_bp.delete("/api/slack/channels/<int:channel_id>")
def api_delete_channel(channel_id: int):
    if not delete_channel(channel_id):
        return jsonify({"ok": False, "error": "Channel not found."}), 404
    return jsonify({"ok": True, "id": channel_id})


@slack_bp.post("/api/slack/channels/<int:channel_id>/test")
def api_test_channel(channel_id: int):
    result = send_test_alert(channel_id, base_url=_request_base())
    status = 404 if result.get("status") == 404 and not result.get("ok") and result.get("error") == "Channel not found." else 200
    return jsonify(result), status


@slack_bp.get("/api/slack/deliveries")
def api_deliveries():
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"ok": True, "deliveries": list_deliveries(limit)})


@slack_bp.post("/api/slack/preview")
def api_preview():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    incident = preview_incident(body)
    return jsonify({"ok": True, "payload": build_block_kit(incident, _request_base()), "incident": public_preview(incident)})


def public_preview(incident: dict) -> dict:
    shown = dict(incident)
    shown.pop("source", None)
    return shown
