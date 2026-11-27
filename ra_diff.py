"""What changed since a saved risk-assessment baseline.

The endpoint is read-only. It accepts a country, a region, the baseline time,
the incident ids already reviewed, and the original risk level. Personal fields
are dropped before the comparison and are never written to disk.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

ra_diff_bp = Blueprint("ra_diff", __name__)

ALLOWED_FIELDS = {
    "country",
    "region",
    "since_iso",
    "original_incident_ids",
    "original_risk_level",
}
# Personal keys are never copied. Only ALLOWED_FIELDS are read.
TIER_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "VERY HIGH": 3, "EXTREME": 4}
SEVERITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2, "INFO": 3}
CATEGORY_LABELS = {
    "ACT": "Armed Conflict & Terrorism",
    "SFA": "Security Force Activity",
    "VC": "Violent Crime",
    "CU": "Civil Unrest",
    "HS": "Hazards & Safety",
    "OD": "Operational Disruption",
}
LABEL_TO_CODE = {label.casefold(): code for code, label in CATEGORY_LABELS.items()}
CATEGORY_WORDS = (
    ("ACT", ("armed conflict", "terrorism", "terror", "conflict", "airstrike", "missile", "shelling", "drone")),
    ("SFA", ("security force", "security incident", "checkpoint", "arrest", "raid")),
    ("VC", ("violent crime", "crime", "kidnap", "robbery", "homicide", "shooting")),
    ("CU", ("civil unrest", "unrest", "protest", "riot", "demonstration")),
    ("HS", ("hazard", "medical", "health", "weather", "earthquake", "flood", "disease", "wildfire")),
    ("OD", ("operational disruption", "travel advisory", "disruption", "airport", "closure", "outage", "transport")),
)
EXACT_CATEGORY = {
    "security": "SFA",
    "advisory": "OD",
}
# Sector is the control family. Category picks the threat inside that family.
CONTROL_UPGRADES = {
    "ACT": (
        "Security control — Armed conflict and terrorism: upgrade movement to hardened transport, "
        "confirm the shelter plan, and raise the indirect-fire control before departure."
    ),
    "SFA": (
        "Security control — Security-force activity: add checkpoint, detention, and duty-of-care "
        "contact controls, and brief the team on identification handling."
    ),
    "VC": (
        "Security control — Violent crime: upgrade to paired movement, a reviewed place to stay, "
        "and a reduced-profile protocol."
    ),
    "CU": (
        "Movement control — Civil unrest: reroute away from gathering sites and set a 24-hour stand-fast trigger."
    ),
    "HS": (
        "Medical control — Health and natural hazards: reconfirm the medevac trigger, hospital viability, "
        "and stand-down criteria."
    ),
    "OD": (
        "Movement control — Operational disruption: add an alternate route, a communications-loss rally point, "
        "and a delayed-movement trigger."
    ),
}
GENERIC_UPGRADE = (
    "Risk tier increased since the baseline. Re-score the matrix, name an owner for each control, "
    "and complete human sign-off before departure."
)
def current_time() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def strip_pii(payload: object) -> dict:
    """Keep only the operational fields. Anything personal is discarded."""
    if not isinstance(payload, dict):
        return {
            "country": "",
            "region": "",
            "since_iso": "",
            "original_incident_ids": [],
            "original_risk_level": "",
        }
    clean = {
        "country": _text(payload.get("country"), 80),
        "region": _text(payload.get("region"), 80),
        "since_iso": _text(payload.get("since_iso"), 40),
        "original_risk_level": _text(payload.get("original_risk_level"), 20),
        "original_incident_ids": [],
    }
    raw_ids = payload.get("original_incident_ids")
    if isinstance(raw_ids, list):
        seen: set[str] = set()
        for item in raw_ids[:500]:
            text = _text(item, 64)
            if not text or "@" in text or " " in text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            clean["original_incident_ids"].append(text)
    return clean


def public_id(title: str, timestamp: str) -> str:
    key = f"{' '.join(title.split()).casefold()}|{timestamp}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def normalize_level(value: object) -> str:
    key = " ".join(_text(value, 24).upper().replace("-", " ").split())
    if key in {"MED", "MEDIUM", "MODERATE"}:
        return "MEDIUM"
    if key in {"VERY HIGH", "VERYHIGH"}:
        return "VERY HIGH"
    if key == "HIGH":
        return "HIGH"
    if key in {"EXTREME", "SEVERE"}:
        return "EXTREME"
    return "LOW"


def normalize_severity(value: object) -> str:
    key = _text(value, 20).upper()
    if key in {"HIGH", "H", "EXTREME", "CRITICAL"}:
        return "HIGH"
    if key in {"MED", "MEDIUM", "MODERATE", "M"}:
        return "MED"
    if key in {"INFO", "INFORMATIONAL", "NOTICE"}:
        return "INFO"
    return "LOW"


def category_code(value: object) -> str:
    raw = _text(value, 80)
    upper = raw.upper()
    if upper in CATEGORY_LABELS:
        return upper
    named = LABEL_TO_CODE.get(raw.casefold())
    if named:
        return named
    lowered = raw.casefold()
    if lowered in EXACT_CATEGORY:
        return EXACT_CATEGORY[lowered]
    for code, words in CATEGORY_WORDS:
        if any(word in lowered for word in words):
            return code
    return ""


def display_category(value: object) -> str:
    code = category_code(value)
    if code:
        return CATEGORY_LABELS[code]
    text = _text(value, 80)
    return text or "OSINT"


def confidence_tier(props: dict, origin: str) -> str:
    explicit = _text(props.get("confidence") or props.get("confidence_tier"), 40).casefold()
    if explicit in {"confirmed", "corroborated", "high"}:
        return "Corroborated"
    if explicit in {"claim", "state"}:
        return "State / Party-Aligned Claim"
    if explicit in {"unverified", "lead"}:
        return "Unverified Lead"
    verification = _text(props.get("verification_tier"), 80)
    if verification:
        return verification
    source_tier = _text(props.get("source_tier"), 80)
    if source_tier:
        return source_tier
    try:
        corroborated = int(props.get("corroboration_count") or 0)
    except (TypeError, ValueError):
        corroborated = 0
    if corroborated >= 2:
        return "Corroborated"
    if origin == "archive":
        return "Historical Archive"
    return "Single-Source"


def _props(feature: dict) -> dict:
    nested = feature.get("properties")
    if isinstance(nested, dict):
        return nested
    return feature


def _noisy(title: str, summary: str, source: str) -> bool:
    try:
        import feed_quality

        return bool(feed_quality.is_noise(title, summary, source))
    except Exception:
        return False


def incident_from_mapping(raw: dict, origin: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    props = _props(raw)
    title = " ".join(str(props.get("title") or "").split())
    summary = " ".join(str(props.get("summary") or "").split())
    source_url = " ".join(str(props.get("source_url") or "").split())
    source_field = " ".join(str(props.get("source") or "").split())
    if source_field.startswith("http") and not source_url:
        source_url = source_field
        source_field = ""
    source_name = " ".join(
        str(props.get("source_name") or props.get("source_label") or props.get("account") or source_field).split()
    )
    if not title or _noisy(title, summary, source_url or source_name):
        return None
    from analytics_store import format_ts, parse_ts

    when = parse_ts(props.get("timestamp") or props.get("published_at") or props.get("occurred_at"))
    if when is None:
        return None
    stamp = format_ts(when)
    code = category_code(props.get("category") or props.get("feed_category") or "")
    return {
        "id": public_id(title, stamp),
        "title": title[:180],
        "category": display_category(props.get("category") or props.get("feed_category") or ""),
        "category_code": code,
        "severity": normalize_severity(props.get("severity")),
        "confidence_tier": confidence_tier(props, origin),
        "timestamp": stamp,
        "when": when,
        "source": (source_url or source_name or "OSINT")[:300],
        "country": props.get("country") or "",
        "region": props.get("region") or props.get("theatre") or "",
        "area": props.get("area") or props.get("location_name") or props.get("subregion") or props.get("city") or "",
    }


def country_matches(have: object, wanted: str) -> bool:
    from analytics_store import canonical_country

    want = canonical_country(wanted) or " ".join(wanted.split())
    got = canonical_country(have) or " ".join(str(have or "").split())
    if not want or not got:
        return False
    if want.casefold() == got.casefold():
        return True
    pattern = re.compile(rf"\b{re.escape(want)}\b", re.I)
    return bool(pattern.search(got))


def place_matches(item: dict, country: str, region: str) -> bool:
    from analytics_store import canonical_region, region_for_country

    if country:
        for field in (item.get("country"), item.get("region"), item.get("area")):
            if country_matches(field, country):
                return True
        return False
    named = canonical_region(region)
    if not named or named == "Global":
        return False
    have = canonical_region(item.get("region"))
    derived = region_for_country(
        " ".join(str(item.get("country") or "").split()),
        item.get("region") or "",
    )
    return have == named or derived == named


def load_live_features() -> list[dict]:
    """Live map snapshot. Failures become an empty list so the archive can still answer."""
    features: list[dict] = []
    try:
        import serve_map

        payload = serve_map.data_response() or {}
        if isinstance(payload, dict):
            features.extend(item for item in (payload.get("features") or []) if isinstance(item, dict))
    except Exception:
        return features
    return features


def load_archive_rows(since: datetime, country: str, region: str) -> list[dict]:
    """SQLite history since the baseline. The request itself is not stored."""
    from analytics_store import canonical_country, canonical_region, country_match_values, format_ts, get_store

    canon_country = canonical_country(country)
    canon_region = canonical_region(region)
    clauses = ["timestamp >= ?"]
    params: list[object] = [format_ts(since)]
    if canon_country or country:
        values = country_match_values(country) or ([" ".join(country.split())] if country else [])
        if not values:
            return []
        marks = ",".join("?" for _ in values)
        clauses.append(f"country IN ({marks})")
        params.extend(values)
    elif canon_region and canon_region != "Global":
        clauses.append("region = ?")
        params.append(canon_region)
    else:
        return []
    sql = (
        "SELECT timestamp, title, summary, category, severity, country, region, subregion, "
        "city, source_name, source_url FROM incidents WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp DESC LIMIT 500"
    )
    store = get_store()
    with store._lock:
        rows = store._conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _upgrades(incidents: list[dict], escalated: bool) -> list[str]:
    if not escalated:
        return []
    found = {item["category_code"] for item in incidents if item.get("category_code")}
    ordered = [CONTROL_UPGRADES[code] for code in ("ACT", "SFA", "VC", "CU", "HS", "OD") if code in found]
    if not ordered:
        return [GENERIC_UPGRADE]
    return ordered


def _within_pass(country: str, region: str) -> bool:
    """Honor a single-area pass when one is active. Open sessions are unchanged."""
    try:
        import ppv

        grant = ppv.current()
    except Exception:
        return True
    if not grant:
        return True
    return bool(ppv.matches({"country": country, "region": region or country}, grant))


def _empty_result(moment: datetime) -> dict:
    from analytics_store import format_ts

    return {
        "new_incidents_count": 0,
        "new_high_count": 0,
        "new_med_count": 0,
        "new_incidents": [],
        "escalated": False,
        "recommended_control_upgrades": [],
        "checked_at_utc": format_ts(moment),
    }


def build_diff(
    payload: object,
    *,
    now: datetime | None = None,
    live_features: list[dict] | None = None,
    archive_rows: list[dict] | None = None,
) -> dict:
    clean = strip_pii(payload)
    country = clean["country"]
    region = clean["region"]
    if not country and not region:
        raise ValueError("scope")
    from analytics_store import parse_ts

    since = parse_ts(clean["since_iso"]) if clean["since_iso"] else None
    if since is None:
        raise ValueError("since_iso")
    moment = now or current_time()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if not _within_pass(country, region):
        return _empty_result(moment)
    known = {item.casefold() for item in clean["original_incident_ids"]}
    features = load_live_features() if live_features is None else live_features
    rows = load_archive_rows(since, country, region) if archive_rows is None else archive_rows
    collected: list[dict] = []
    for feature in features or []:
        item = incident_from_mapping(feature, "live")
        if item is not None:
            collected.append(item)
    for row in rows or []:
        item = incident_from_mapping(row, "archive")
        if item is not None:
            collected.append(item)
    fresh: list[dict] = []
    seen: set[str] = set()
    for item in collected:
        if not place_matches(item, country, region):
            continue
        if item["when"] < since:
            continue
        if item["id"] in seen or item["id"].casefold() in known:
            continue
        seen.add(item["id"])
        fresh.append(item)
    fresh.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), -item["when"].timestamp()))
    counts = {"HIGH": 0, "MED": 0, "LOW": 0, "INFO": 0}
    for item in fresh:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    from analytics_store import format_ts, risk_index, risk_level

    elapsed_days = max((moment - since).total_seconds() / 86400.0, 1.0)
    current = risk_level(risk_index(counts, elapsed_days))
    original = normalize_level(clean["original_risk_level"])
    tier_increased = TIER_RANK[current] > TIER_RANK[original]
    escalated = counts["HIGH"] > 0 or counts["MED"] >= 3 or tier_increased
    public = []
    for item in fresh[:40]:
        public.append({
            "id": item["id"],
            "title": item["title"],
            "category": item["category"],
            "severity": item["severity"],
            "confidence_tier": item["confidence_tier"],
            "timestamp": item["timestamp"],
            "source": item["source"],
        })
    return {
        "new_incidents_count": len(fresh),
        "new_high_count": counts["HIGH"],
        "new_med_count": counts["MED"],
        "new_incidents": public,
        "escalated": escalated,
        "recommended_control_upgrades": _upgrades(fresh, escalated),
        "checked_at_utc": format_ts(moment),
    }


def _error(message: str, status: int):
    response = jsonify({"ok": False, "error": message})
    response.status_code = status
    return response


@ra_diff_bp.post("/api/ra/diff")
def api_ra_diff():
    raw = request.get_json(silent=True)
    try:
        result = build_diff(raw, now=current_time())
    except ValueError as exc:
        code = str(exc)
        if code == "since_iso":
            return _error("since_iso must be an ISO-8601 timestamp.", 400)
        if code == "scope":
            return _error("A country or region is required.", 400)
        return _error("The diff request could not be read.", 400)
    except Exception:
        return _error("The diff could not be completed.", 500)
    return jsonify(result)
