"""Phase 1 platform routes: Grok ops, triage, streaming analyst calls, advisories.

XAI_API_KEY is read from the environment on the server and is never returned,
logged, or written into a page. Personal and medical fields are dropped before
any model call. Traveller PII stays in the browser.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
MODEL = "grok-3-mini"
CHAT_URL = "https://api.x.ai/v1/chat/completions"
MODELS_URL = "https://api.x.ai/v1/models"
CACHE_PATH = ROOT / "advisory_crosscheck_cache.json"
ENQUIRY_PATH = ROOT / "data" / "enquiries.jsonl"
HOTSPOTS_PATH = ROOT / "hotspots.json"

UK_INDEX_URL = "https://www.gov.uk/api/content/foreign-travel-advice"
US_ADVISORY_URL = "https://cadataapi.state.gov/api/TravelAdvisories"
CA_ADVISORY_URL = "https://data.international.gc.ca/travel-voyage/index-alpha-eng.json"
ADVISORY_TTL = 6 * 60 * 60
MAX_TRIAGE_LOG = 40
RATE_WINDOW_SECONDS = 600
RATE_MAX = 30

PAGES = {
    "/grok-ops": "grok-ops.html",
    "/risk-assessments": "risk-assessments.html",
    "/pricing": "pricing.html",
    "/enterprise": "enterprise.html",
}
STREAM_ROUTES = {
    "/api/grok/summarize-governorate",
    "/api/grok/briefing",
    "/api/grok/risk-draft",
    "/api/grok/copilot",
}
THEATRES = (
    "Syria", "Lebanon", "Israel", "Palestine", "Jordan", "Iraq", "Iran",
    "Saudi Arabia", "Yemen", "Sudan", "Chad", "Bahrain", "Kuwait", "Qatar",
    "UAE", "Oman", "Maritime",
)
SEVERITIES = {"low", "medium", "high"}
CONFIDENCE = {"confirmed", "claim", "unverified"}
FORBIDDEN_KEYS = {
    "traveller", "traveler", "travellers", "travelers", "patient", "patients",
    "pupil", "pupils", "student_name", "student_names", "full_name", "personal_name",
    "first_name", "last_name", "dob", "date_of_birth", "passport", "passport_number",
    "medical", "medical_history", "diagnosis", "medication", "medications",
    "blood_type", "blood", "nhs", "nhs_number", "ssn", "national_id",
    "email", "phone", "telephone", "mobile", "address", "home_address",
    "next_of_kin", "emergency_contact", "emergency_contacts", "allergy", "allergies",
    "condition", "conditions", "insurance", "insurance_number", "gp", "physician",
    "names", "contacts", "advisor", "callsign", "roster", "pupil_roster",
    "parent_contact", "parent_contacts",
}
SECTIONS = {"custom_risk", "human_overwatch", "tracking_app", "custom_platform"}
SECTORS = {
    "media", "corporate_ngo", "government", "schools", "close_protection", "private", "other",
}
TEAM_SIZES = {"1–5", "1-5", "6–20", "6-20", "20+"}
UK_ALERT = {
    "avoid_all_travel_to_whole_country": 4,
    "avoid_all_but_essential_travel_to_whole_country": 3,
    "avoid_all_travel_to_parts": 3,
    "avoid_all_but_essential_travel_to_parts": 2,
}
LEVEL_LABELS = {
    4: "Level 4 — Do not travel / Avoid all travel",
    3: "Level 3 — Reconsider / Avoid non-essential travel",
    2: "Level 2 — Increased caution",
    1: "Level 1 — Normal precautions",
    0: "Unrated / No data",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-])?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}[\s.-]\d{3,4}(?!\w)"
)
PASSPORT_RE = re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_lock = threading.Lock()
_rate_hits: deque[float] = deque()
_stats = {
    "requests": 0,
    "tokens_prompt": 0,
    "tokens_completion": 0,
    "tokens_total": 0,
    "rate_limit": {},
    "last_ok_at": None,
    "last_error": "",
    "last_latency_ms": None,
    "last_health": None,
}
_triage_log: deque[dict] = deque(maxlen=MAX_TRIAGE_LOG)
_advisory_lock = threading.Lock()
_advisory_memory: dict = {"at": 0.0, "payload": None}


def read_api_key() -> str:
    """Return the server key. Callers must not place it in a response or log line."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    import os

    return (os.environ.get("XAI_API_KEY") or "").strip()


def configured() -> bool:
    return bool(read_api_key())


def is_stream_route(path: str) -> bool:
    return path.split("?", 1)[0] in STREAM_ROUTES


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _slug(value: object) -> str:
    return _norm(value).replace(" ", "-")


def scrub_text(value: object, limit: int = 1200) -> str:
    text = str(value or "")
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = PHONE_RE.sub("[redacted-phone]", text)
    text = PASSPORT_RE.sub("[redacted-id]", text)
    text = " ".join(text.split())
    return text[:limit]


def scrub_payload(value, withheld: list | None = None):
    """Drop personal and medical keys. Remaining strings are redacted."""
    found = withheld if withheld is not None else []
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            name = _norm(key).replace(" ", "_")
            if name in FORBIDDEN_KEYS or name.rstrip("s") in FORBIDDEN_KEYS:
                found.append(name)
                continue
            clean[key] = scrub_payload(item, found)
        return clean
    if isinstance(value, list):
        return [scrub_payload(item, found) for item in value[:40]]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub_text(value, 200)


def _load_json(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _limited() -> bool:
    now = time.time()
    with _lock:
        while _rate_hits and now - _rate_hits[0] > RATE_WINDOW_SECONDS:
            _rate_hits.popleft()
        if len(_rate_hits) >= RATE_MAX:
            return True
        _rate_hits.append(now)
    return False


def _note_usage(usage: dict | None, headers, latency_ms: int, error: str = "") -> None:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    limits = {}
    if headers is not None:
        for key, value in headers.items():
            lowered = key.lower()
            if lowered.startswith("x-ratelimit-"):
                limits[lowered] = str(value)[:80]
    with _lock:
        _stats["requests"] += 1
        _stats["tokens_prompt"] += prompt
        _stats["tokens_completion"] += completion
        _stats["tokens_total"] += total
        _stats["last_latency_ms"] = latency_ms
        if limits:
            _stats["rate_limit"] = limits
        if error:
            _stats["last_error"] = error[:180]
        else:
            _stats["last_ok_at"] = _now()
            _stats["last_error"] = ""


def _theatres() -> list[dict]:
    counts = {name: 0 for name in THEATRES}
    try:
        payload = json.loads(HOTSPOTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        region = str(props.get("region") or props.get("country") or "Other")
        if region not in counts:
            counts[region] = 0
        counts[region] += 1
    rows = [{"name": name, "incidents": count} for name, count in counts.items() if count or name in THEATRES]
    rows.sort(key=lambda item: (-item["incidents"], item["name"]))
    return rows[:24]


def status_payload() -> dict:
    with _lock:
        snapshot = dict(_stats)
        snapshot["rate_limit"] = dict(_stats["rate_limit"])
        log = list(_triage_log)
    return {
        "ok": True,
        "configured": configured(),
        "model": MODEL,
        "pii_policy": "Personal and medical fields are removed before any Grok call and are never stored in the triage log.",
        "requests": snapshot["requests"],
        "tokens": {
            "prompt": snapshot["tokens_prompt"],
            "completion": snapshot["tokens_completion"],
            "total": snapshot["tokens_total"],
        },
        "rate_limit": snapshot["rate_limit"],
        "last_ok_at": snapshot["last_ok_at"],
        "last_error": snapshot["last_error"],
        "last_latency_ms": snapshot["last_latency_ms"],
        "last_health": snapshot["last_health"],
        "theatres": _theatres(),
        "triage_log": log,
    }


def health_payload() -> dict:
    key = read_api_key()
    if not key:
        result = {
            "ok": True,
            "configured": False,
            "model": MODEL,
            "message": "XAI_API_KEY is not configured on the server. Grok stays off until it is set. No request was sent.",
        }
        with _lock:
            _stats["last_health"] = result
        return result
    started = time.time()
    try:
        response = requests.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=(8, 20),
        )
        latency = int((time.time() - started) * 1000)
        _note_usage({}, response.headers, latency, "" if response.ok else f"HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            result = {
                "ok": False,
                "configured": True,
                "model": MODEL,
                "message": "The server rejected the API key. The key was not shown.",
            }
        elif not response.ok:
            result = {
                "ok": False,
                "configured": True,
                "model": MODEL,
                "message": "Grok health check failed. The map and local drafts still work.",
            }
        else:
            result = {
                "ok": True,
                "configured": True,
                "model": MODEL,
                "latency_ms": latency,
                "message": "Grok API is reachable.",
            }
    except requests.RequestException:
        result = {
            "ok": False,
            "configured": True,
            "model": MODEL,
            "message": "Grok could not be reached. No key was logged.",
        }
        _note_usage({}, None, int((time.time() - started) * 1000), "network")
    with _lock:
        _stats["last_health"] = {k: v for k, v in result.items() if k != "message"} | {"message": result["message"]}
    return result


def _clip(value: object, limit: int) -> str:
    return scrub_text(value, limit)


def validate_incident(raw: dict, source: str) -> dict:
    severity = str(raw.get("severity") or "low").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium" if severity in {"med", "moderate"} else "low"
    confidence = str(raw.get("confidence") or "unverified").strip().lower()
    if confidence not in CONFIDENCE:
        confidence = "unverified"
    lat = _coord(raw.get("lat"), -90, 90)
    lng = _coord(raw.get("lng") if raw.get("lng") is not None else raw.get("lon"), -180, 180)
    incident = {
        "title": _clip(raw.get("title") or "OSINT incident", 180) or "OSINT incident",
        "summary": _clip(raw.get("summary") or raw.get("title") or "", 700),
        "location_name": _clip(raw.get("location_name") or raw.get("location") or "", 120),
        "country": _clip(raw.get("country") or "", 80),
        "region": _clip(raw.get("region") or raw.get("country") or "", 80),
        "governorate": _clip(raw.get("governorate") or "", 80),
        "severity": severity,
        "category": _clip(raw.get("category") or "Miscellaneous", 80) or "Miscellaneous",
        "lat": lat,
        "lng": lng,
        "occurred_at": _clip(raw.get("occurred_at") or raw.get("timestamp") or "", 40),
        "confidence": confidence,
        "source": source,
    }
    return incident


def _coord(value, low: float, high: float):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < low or number > high:
        return None
    return round(number, 5)


def feature_for(incident: dict) -> dict:
    geometry = None
    if incident.get("lat") is not None and incident.get("lng") is not None:
        geometry = {"type": "Point", "coordinates": [incident["lng"], incident["lat"]]}
    return {"type": "Feature", "geometry": geometry, "properties": dict(incident)}


def local_incident_from_text(text: str) -> dict:
    cleaned = scrub_text(text, 4000)
    lowered = cleaned.lower()
    high_words = ("airstrike", "missile", "explosion", "killed", "kidnap", "shelling", "drone strike", "ied")
    medium_words = ("clash", "protest", "gunfire", "arrest", "roadblock", "strike", "unrest")
    if any(word in lowered for word in high_words):
        severity = "high"
    elif any(word in lowered for word in medium_words):
        severity = "medium"
    else:
        severity = "low"
    country = ""
    for name in THEATRES:
        if name.lower() in lowered:
            country = name
            break
    title = cleaned[:140] or "OSINT note"
    if len(cleaned) > 140:
        title = title.rsplit(" ", 1)[0]
    return validate_incident(
        {
            "title": title or "OSINT note",
            "summary": cleaned[:700],
            "country": country,
            "region": country,
            "severity": severity,
            "category": "OSINT draft",
            "confidence": "unverified",
        },
        "local-draft",
    )


def _record_triage(incident: dict, source: str) -> None:
    entry = {
        "at": _now(),
        "title": incident.get("title") or "",
        "country": incident.get("country") or incident.get("region") or "",
        "severity": incident.get("severity") or "",
        "source": source,
        "ok": True,
    }
    with _lock:
        _triage_log.appendleft(entry)


def _messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


TRIAGE_SYSTEM = (
    "You convert public OSINT notes into one incident JSON object for a travel-risk map. "
    "Keys only: title, summary, location_name, country, region, governorate, severity "
    "(low, medium, or high), category, lat, lng, occurred_at, confidence "
    "(confirmed, claim, or unverified). Use null when coordinates are unknown. "
    "Do not include private individuals, medical details, contact details, or identity documents. "
    "Do not invent facts that are not in the note."
)


def triage_payload(raw: bytes) -> tuple[int, dict]:
    if _limited():
        return 429, {"ok": False, "configured": configured(), "message": "Too many Grok requests. Wait a few minutes and try again."}
    body = _load_json(raw)
    withheld: list = []
    clean = scrub_payload(body, withheld)
    text = scrub_text(clean.get("text") or "", 8000)
    events = clean.get("events") if isinstance(clean.get("events"), list) else []
    if not text and not events:
        return 400, {"ok": False, "configured": configured(), "message": "Add a public OSINT note to triage."}
    note = text
    if events:
        lines = []
        for event in events[:12]:
            if not isinstance(event, dict):
                continue
            lines.append(
                " | ".join(
                    part
                    for part in (
                        scrub_text(event.get("title") or "", 180),
                        scrub_text(event.get("location_name") or event.get("country") or "", 80),
                        scrub_text(event.get("summary") or "", 240),
                    )
                    if part
                )
            )
        note = (note + "\n" + "\n".join(lines)).strip()
    key = read_api_key()
    if not key:
        incident = local_incident_from_text(note)
        _record_triage(incident, "local")
        return 200, {
            "ok": True,
            "configured": False,
            "source": "local",
            "withheld_personal_fields": bool(withheld),
            "message": "XAI_API_KEY is not configured. This is a local draft. Nothing was sent to Grok.",
            "incident": incident,
            "feature": feature_for(incident),
        }
    started = time.time()
    try:
        response = requests.post(
            CHAT_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": _messages(TRIAGE_SYSTEM, note[:8000]),
            },
            timeout=(8, 40),
        )
        latency = int((time.time() - started) * 1000)
        data = response.json() if response.content else {}
        _note_usage(data.get("usage") if isinstance(data, dict) else {}, response.headers, latency, "" if response.ok else f"HTTP {response.status_code}")
        if not response.ok:
            raise requests.RequestException("status")
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _parse_object(content)
    except (requests.RequestException, ValueError, TypeError):
        incident = local_incident_from_text(note)
        _record_triage(incident, "local")
        return 200, {
            "ok": True,
            "configured": True,
            "source": "local",
            "withheld_personal_fields": bool(withheld),
            "message": "Grok did not return a usable incident. Showing a local draft instead.",
            "incident": incident,
            "feature": feature_for(incident),
        }
    incident = validate_incident(parsed, "grok-triage")
    _record_triage(incident, "grok")
    return 200, {
        "ok": True,
        "configured": True,
        "source": "grok",
        "withheld_personal_fields": bool(withheld),
        "message": "Structured incident ready for the map feed.",
        "incident": incident,
        "feature": feature_for(incident),
    }


def _parse_object(text: str) -> dict:
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
        raise ValueError("object")
    nested = payload.get("incident")
    if isinstance(nested, dict):
        return nested
    return payload


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_local(text: str, meta: dict):
    words = text.split()
    step = 6
    for index in range(0, len(words), step):
        yield sse("token", {"text": " ".join(words[index : index + step]) + " "})
    yield sse("done", meta)


def _incidents_block(items) -> str:
    lines = []
    if not isinstance(items, list):
        return "No incidents were supplied."
    for item in items[:24]:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- "
            + " | ".join(
                part
                for part in (
                    scrub_text(item.get("severity") or "", 16),
                    scrub_text(item.get("country") or item.get("region") or "", 40),
                    scrub_text(item.get("location_name") or item.get("governorate") or "", 60),
                    scrub_text(item.get("title") or "", 180),
                    scrub_text(item.get("summary") or "", 280),
                )
                if part
            )
        )
    return "\n".join(lines) or "No incidents were supplied."


ANALYST_SYSTEM = (
    "You are the GeoRiskGlobal map analyst. Write for security, media, and travel-risk officers. "
    "Use only the public incident notes provided. Do not invent casualty figures or name private people. "
    "Do not give medical advice, and do not ask for or repeat personal or medical information. "
    "If the notes are thin, say what is not known. Plain text, short paragraphs, then up to four mitigations."
)


def stream_governorate(payload: dict):
    clean = scrub_payload(payload)
    name = scrub_text(clean.get("governorate") or clean.get("name") or "", 80)
    if not name:
        yield sse("error", {"ok": False, "message": "Choose a governorate first."})
        return
    notes = _incidents_block(clean.get("incidents"))
    user = f"Governorate: {name}\nVisible incidents:\n{notes}"
    fallback = (
        f"{name}: local read of the incidents on screen. "
        f"{notes.replace('- ', '')[:500]} "
        "Mitigations: confirm the latest official advisory, keep movement plans flexible, "
        "and avoid treating a single social post as confirmed."
    )
    yield from _stream_model(ANALYST_SYSTEM, user, fallback, {"kind": "governorate", "governorate": name})


def stream_briefing(payload: dict):
    clean = scrub_payload(payload)
    theatre = scrub_text(clean.get("theatre") or clean.get("country") or "the selected theatre", 80)
    notes = _incidents_block(clean.get("incidents"))
    user = f"Theatre briefing for {theatre}.\nVisible incidents:\n{notes}"
    fallback = (
        f"Theatre briefing for {theatre}. Grok is not available, so this is a local grouping of the notes already on screen. "
        f"{notes[:700]} "
        "Treat unverified claims as claims. Check the UK, US, and Canadian advisories before movement."
    )
    yield from _stream_model(ANALYST_SYSTEM, user, fallback, {"kind": "briefing", "theatre": theatre})


def stream_risk_draft(payload: dict):
    clean = scrub_payload(payload)
    template = scrub_text(clean.get("template") or "Security & Travel Risk Assessment", 120)
    sector = scrub_text(clean.get("sector") or "", 160)
    standard = scrub_text(clean.get("compliance_standard") or clean.get("standard") or "", 240)
    operation = scrub_text(clean.get("operation") or clean.get("purpose") or "", 160)
    country = scrub_text(clean.get("country") or "", 80)
    city = scrub_text(clean.get("city") or "", 80)
    transport = scrub_text(clean.get("transport") or "", 160)
    accommodation = scrub_text(clean.get("accommodation") or "", 160)
    comms = scrub_text(clean.get("comms") or "", 160)
    hazards = clean.get("hazards") if isinstance(clean.get("hazards"), list) else []
    lines = []
    for hazard in hazards[:12]:
        if not isinstance(hazard, dict):
            continue
        lines.append(
            f"- {scrub_text(hazard.get('threat') or 'Hazard', 80)} "
            f"(likelihood {hazard.get('likelihood')}, impact {hazard.get('impact')})"
        )
    recent = clean.get("recent_incidents") if isinstance(clean.get("recent_incidents"), list) else []
    incident_lines = []
    for item in recent[:12]:
        if not isinstance(item, dict):
            continue
        incident_lines.append(
            "- "
            + " | ".join(
                part
                for part in (
                    scrub_text(item.get("timestamp") or "", 40),
                    scrub_text(item.get("severity") or "", 16),
                    scrub_text(item.get("area") or item.get("location_name") or "", 60),
                    scrub_text(item.get("title") or "", 180),
                    scrub_text(item.get("source_name") or "", 60),
                )
                if part
            )
        )
    protocols = []
    if transport:
        protocols.append(f"Transport: {transport}")
    if accommodation:
        protocols.append(f"Accommodation: {accommodation}")
    if comms:
        protocols.append(f"Comms: {comms}")
    user = (
        f"Sector: {sector or 'unspecified'}\n"
        f"Compliance standard: {standard or template}\n"
        f"Template: {template}\n"
        f"Operation / assignment: {operation or 'unspecified'}\n"
        f"Destination: {city + ', ' if city else ''}{country or 'unspecified'}\n"
        + ("\n".join(protocols) + "\n" if protocols else "")
        + "Write one short mitigation paragraph per hazard, aligned to this sector and compliance standard. "
        "Cite the public 7-day incidents when they support a hazard. "
        "Destination threat context only. Do not mention personal identities, callsigns, pupil rosters, or medical histories.\n"
        + ("\n".join(lines) or "- General travel security")
        + "\nPublic incidents, last 7 days:\n"
        + ("\n".join(incident_lines) or "None logged.")
    )
    system = (
        "You draft mitigations for a travel and operational risk assessment. "
        "Align the wording to the stated sector and compliance standard. "
        "Discuss places, routes, healthcare access, and security conditions. "
        "Use only the public incidents supplied. If none are listed, say no verified incidents were logged in the last 7 days. "
        "Never request or repeat traveller names, pupil rosters, callsigns, medical history, blood type, passport numbers, or contact details. Plain text."
    )
    cited = "; ".join(line[2:80] for line in incident_lines[:4]) or "no verified incidents logged in the last 7 days"
    fallback = (
        f"Local mitigation draft for {sector or 'the selected sector'} under {standard or template}. "
        "Grok is not configured, so nothing was sent. Recent public incidents: " + cited + "\n"
        + "\n".join(
            f"{line} — Confirm the official advisory, assign an owner, and re-score residual risk after the control is in place."
            for line in (lines or ["- General travel security"])
        )
    )
    yield from _stream_model(system, user, fallback, {
        "kind": "risk-draft",
        "template": template,
        "sector": sector,
    })


def stream_copilot(payload: dict):
    clean = scrub_payload(payload)
    question = scrub_text(clean.get("question") or "", 1200)
    if not question:
        yield sse("error", {"ok": False, "message": "Ask a question about the incidents on the map."})
        return
    filters = clean.get("filters") if isinstance(clean.get("filters"), dict) else {}
    theatre = scrub_text(filters.get("country") or filters.get("region") or filters.get("governorate") or "all theatres", 80)
    notes = _incidents_block(clean.get("incidents"))
    user = f"Active theatre/filters: {json.dumps({k: scrub_text(v, 60) for k, v in filters.items()})}\nQuestion: {question}\nVisible incidents:\n{notes}"
    system = (
        "You are the GeoRiskGlobal map co-pilot. Answer only from the visible public incidents and filters. "
        "If they do not answer the question, say so. Refuse requests for personal data, medical detail, "
        "or help targeting a person. Situational awareness and travel-risk context only."
    )
    fallback = (
        f"Local co-pilot read for {theatre}. Grok is not configured, so this uses only the incidents already on screen. "
        f"Question noted: {question[:240]}. Notes: {notes[:650]}"
    )
    yield from _stream_model(system, user, fallback, {"kind": "copilot"})


def _stream_model(system: str, user: str, fallback: str, meta: dict):
    key = read_api_key()
    if not key:
        yield from _stream_local(
            fallback,
            {
                "ok": True,
                "configured": False,
                "source": "local",
                "message": "XAI_API_KEY is not configured. Showing a local draft. Nothing was sent to Grok.",
                **meta,
            },
        )
        return
    if _limited():
        yield sse("error", {"ok": False, "configured": True, "message": "Too many Grok requests. Wait a few minutes and try again."})
        return
    started = time.time()
    try:
        response = requests.post(
            CHAT_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "temperature": 0.2,
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": _messages(system, user[:12000]),
            },
            stream=True,
            timeout=(8, 50),
        )
    except requests.RequestException:
        _note_usage({}, None, int((time.time() - started) * 1000), "network")
        yield sse("error", {"ok": False, "configured": True, "message": "Grok could not be reached. Nothing personal was stored."})
        return
    if not response.ok:
        _note_usage({}, response.headers, int((time.time() - started) * 1000), f"HTTP {response.status_code}")
        response.close()
        yield sse("error", {"ok": False, "configured": True, "message": "Grok did not complete this draft."})
        return
    usage = {}
    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict) and chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}
            text = delta.get("content") or ""
            if text:
                yield sse("token", {"text": text})
    finally:
        response.close()
    _note_usage(usage, response.headers, int((time.time() - started) * 1000))
    yield sse("done", {"ok": True, "configured": True, "source": "grok", "usage": {
        "prompt": int((usage or {}).get("prompt_tokens") or 0),
        "completion": int((usage or {}).get("completion_tokens") or 0),
        "total": int((usage or {}).get("total_tokens") or 0),
    }, **meta})


def stream_for(path: str, raw: bytes):
    route = path.split("?", 1)[0]
    payload = scrub_payload(_load_json(raw))
    if route == "/api/grok/summarize-governorate":
        return stream_governorate(payload)
    if route == "/api/grok/briefing":
        return stream_briefing(payload)
    if route == "/api/grok/risk-draft":
        return stream_risk_draft(payload)
    if route == "/api/grok/copilot":
        return stream_copilot(payload)
    return _stream_local("", {"ok": False, "message": "Unknown stream."})


def _level_payload(level: int, summary: str, url: str, updated: str, agency: str, country: str, ok: bool = True) -> dict:
    level = level if level in LEVEL_LABELS else 0
    return {
        "ok": ok,
        "agency": agency,
        "country": country,
        "level": level,
        "label": LEVEL_LABELS[level],
        "summary": scrub_text(summary, 420),
        "url": url if str(url).startswith("https://") else "",
        "updated": scrub_text(updated, 40),
    }


def _empty_source(agency: str, message: str) -> dict:
    payload = _level_payload(0, "", "", "", agency, "")
    payload["ok"] = False
    payload["message"] = message
    return payload


def parse_us_advisories(payload) -> list[dict]:
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("TravelAdvisories") or payload.get("advisories") or []
    if not isinstance(rows, list):
        return []
    parsed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = str(item.get("Title") or item.get("title") or item.get("name") or "")
        country = re.sub(r"\s+travel advisory.*$", "", title, flags=re.I).strip() or title
        blob = " ".join(
            str(part)
            for part in (
                title,
                item.get("Summary") or item.get("summary") or item.get("Description") or "",
                _category_text(item.get("Category") or item.get("category")),
            )
        )
        match = re.search(r"level\s*([1-4])", blob, re.I)
        level = int(match.group(1)) if match else 0
        summary = str(item.get("Summary") or item.get("summary") or item.get("Description") or "")
        url = str(item.get("Link") or item.get("link") or item.get("url") or "")
        updated = str(item.get("Updated") or item.get("Published") or item.get("date") or item.get("published") or "")
        if country:
            parsed.append(_level_payload(level, _strip_html(summary), url, updated, "US State Department", country))
    return parsed


def _category_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("CategoryName") or item.get("name") or ""))
        return " ".join(parts)
    if isinstance(value, dict):
        return str(value.get("CategoryName") or value.get("name") or "")
    return ""


def parse_canada_advisories(payload) -> list[dict]:
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("countries") or payload.get("items") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    parsed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        country = str(item.get("country-eng") or item.get("country") or item.get("name") or item.get("Country") or "")
        advice = str(
            item.get("advisory-text")
            or item.get("advisory-state")
            or item.get("risk-level")
            or item.get("advisory")
            or ""
        )
        level = _canada_level(advice)
        url = str(item.get("url") or item.get("url-eng") or item.get("link") or "")
        if url and url.startswith("/"):
            url = "https://travel.gc.ca" + url
        updated = str(item.get("date-published") or item.get("date-updated") or item.get("updated") or "")
        if country:
            parsed.append(_level_payload(level, advice, url, updated, "Global Affairs Canada", country))
    return parsed


def _canada_level(text: str) -> int:
    lowered = text.lower()
    if "avoid all travel" in lowered:
        return 4
    if "avoid non-essential" in lowered or "avoid non essential" in lowered:
        return 3
    if "high degree of caution" in lowered:
        return 2
    if "normal security" in lowered or "take normal" in lowered or "exercise normal" in lowered:
        return 1
    return 0


def parse_uk_index(payload) -> dict[str, dict]:
    links = {}
    if not isinstance(payload, dict):
        return links
    children = ((payload.get("links") or {}).get("children") or [])
    if not children:
        children = (payload.get("links") or {}).get("ordered_related_items") or []
    for item in children:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        base = str(item.get("base_path") or "")
        api = str(item.get("api_path") or (("/api/content" + base) if base.startswith("/") else ""))
        if title and api:
            links[_norm(title)] = {"title": title, "api": api, "web": "https://www.gov.uk" + base if base.startswith("/") else ""}
    return links


def parse_uk_country(payload, fallback_name: str = "") -> dict:
    if not isinstance(payload, dict):
        return _empty_source("UK FCDO", "UK travel advice was not available.")
    details = payload.get("details") or {}
    country = str((details.get("country") or {}).get("name") or fallback_name or payload.get("title") or "")
    alerts = details.get("alert_status") or []
    level = 1
    if isinstance(alerts, list) and alerts:
        level = max((UK_ALERT.get(str(item), 1) for item in alerts), default=1)
    parts = details.get("parts") or []
    summary = ""
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("body"):
                summary = _strip_html(part.get("body"))
                break
    if not summary:
        summary = _strip_html(payload.get("description") or details.get("change_description") or "")
    url = str(payload.get("web_url") or "")
    updated = str(payload.get("public_updated_at") or details.get("updated_at") or "")
    return _level_payload(level, summary, url, updated, "UK FCDO", country)


def _strip_html(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.replace("&nbsp;", " ").replace("&amp;", "&").split())


def _read_cache() -> dict:
    if _advisory_memory.get("payload") and time.time() - float(_advisory_memory.get("at") or 0) < ADVISORY_TTL:
        return _advisory_memory["payload"]
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        _advisory_memory["at"] = time.time()
        _advisory_memory["payload"] = payload
        return payload
    return {}


def _write_cache(payload: dict) -> None:
    _advisory_memory["at"] = time.time()
    _advisory_memory["payload"] = payload
    try:
        temporary = CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    except OSError:
        pass


def _get_json(url: str):
    response = requests.get(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=(8, 20),
    )
    response.raise_for_status()
    return response.json()


def _match_country(rows: list[dict], country: str) -> dict | None:
    want = _norm(country)
    if not want:
        return None
    for row in rows:
        if _norm(row.get("country")) == want:
            return row
    for row in rows:
        name = _norm(row.get("country"))
        if want in name or name in want:
            return row
    return None


def crosscheck_payload(country: str, refresh: bool = False) -> dict:
    country = scrub_text(country, 80)
    if not country:
        return {"ok": False, "message": "Name a country to compare UK, US, and Canadian advice."}
    with _advisory_lock:
        cache = {} if refresh else dict(_read_cache() or {})
        cached_flag = bool(cache)
        note = ""
        try:
            if refresh or not cache.get("us"):
                cache["us"] = parse_us_advisories(_get_json(US_ADVISORY_URL))
                cache["us_at"] = _now()
        except (requests.RequestException, ValueError, TypeError):
            note = "One or more official sources were unavailable. Showing the last saved copy where we have one."
        try:
            if refresh or not cache.get("ca"):
                cache["ca"] = parse_canada_advisories(_get_json(CA_ADVISORY_URL))
                cache["ca_at"] = _now()
        except (requests.RequestException, ValueError, TypeError):
            note = "One or more official sources were unavailable. Showing the last saved copy where we have one."
        try:
            if refresh or not cache.get("uk_index"):
                cache["uk_index"] = parse_uk_index(_get_json(UK_INDEX_URL))
                cache["uk_index_at"] = _now()
        except (requests.RequestException, ValueError, TypeError):
            note = "One or more official sources were unavailable. Showing the last saved copy where we have one."
        uk_pages = cache.get("uk_pages") if isinstance(cache.get("uk_pages"), dict) else {}
        uk_key = _norm(country)
        uk_row = uk_pages.get(uk_key)
        fresh = False
        if isinstance(uk_row, dict) and not refresh:
            fresh = True
        if not fresh:
            index = cache.get("uk_index") if isinstance(cache.get("uk_index"), dict) else {}
            entry = index.get(uk_key)
            api = ""
            if isinstance(entry, dict):
                api = str(entry.get("api") or "")
            if not api:
                api = "/api/content/foreign-travel-advice/" + _slug(country)
            url = api if api.startswith("https://") else "https://www.gov.uk" + (api if api.startswith("/") else "/" + api)
            try:
                uk_row = parse_uk_country(_get_json(url), country)
                if uk_row.get("level"):
                    uk_pages[uk_key] = uk_row
                    cache["uk_pages"] = uk_pages
            except (requests.RequestException, ValueError, TypeError):
                note = "One or more official sources were unavailable. Showing the last saved copy where we have one."
                uk_row = uk_pages.get(uk_key)
        cache["fetched_at"] = _now()
        if cache.get("us") or cache.get("ca") or cache.get("uk_index"):
            _write_cache(cache)
        us_row = _match_country(cache.get("us") or [], country)
        ca_row = _match_country(cache.get("ca") or [], country)
        sources = {
            "uk": uk_row or _empty_source("UK FCDO", "UK FCDO advice was not available for this country."),
            "us": us_row or _empty_source("US State Department", "US advice was not available for this country."),
            "ca": ca_row or _empty_source("Global Affairs Canada", "Canadian advice was not available for this country."),
        }
        ok = any(item.get("ok") and item.get("level") for item in sources.values())
        return {
            "ok": ok or bool(note),
            "country": country,
            "cached": cached_flag and not refresh,
            "fetched_at": cache.get("fetched_at"),
            "note": note,
            "sources": sources,
        }


def enquiry_payload(raw: bytes) -> tuple[int, dict]:
    body = _load_json(raw)
    if scrub_text(body.get("website") or "", 40):
        return 200, {"ok": True, "reference": "ENQ-IGNORED", "message": "Enquiry received."}
    kind = "individual" if str(body.get("kind") or "").lower() == "individual" else "organisation"
    sector = str(body.get("sector") or "other").lower()
    if sector not in SECTORS:
        sector = "other"
    team = str(body.get("team_size") or "1–5")
    if team not in TEAM_SIZES:
        team = "1–5"
    interests = body.get("interests") if isinstance(body.get("interests"), list) else []
    interests = [item for item in interests if str(item) in SECTIONS][:4]
    organisation = scrub_text(body.get("organisation") or "", 160)
    if len(organisation) < 2:
        return 400, {"ok": False, "message": "Add an organisation or individual name."}
    record = {
        "at": _now(),
        "reference": "ENQ-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + format(int(time.time()) % 10000, "04d"),
        "kind": kind,
        "organisation": organisation,
        "sector": sector,
        "team_size": team.replace("-", "–") if team in {"1-5", "6-20"} else team,
        "regions": scrub_text(body.get("regions") or "", 300),
        "interests": interests,
        "contact_name": scrub_text(body.get("contact_name") or "", 80),
        "contact_email": "",
    }
    email = str(body.get("contact_email") or "").strip()
    if email and EMAIL_RE.fullmatch(email) and len(email) <= 120:
        record["contact_email"] = email
    try:
        ENQUIRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ENQUIRY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return 200, {
            "ok": True,
            "stored": False,
            "reference": record["reference"],
            "message": "Enquiry prepared on this server session but could not be written to disk. Keep the reference.",
        }
    return 200, {
        "ok": True,
        "stored": True,
        "reference": record["reference"],
        "message": "Enquiry saved for follow-up. It was not sent to Grok.",
    }


def dispatch(method: str, path: str, body: bytes = b"") -> tuple[int, str, bytes] | None:
    """JSON routes used by Flask and the built-in server. Streams are handled separately."""
    route = path.split("?", 1)[0]
    query = path.split("?", 1)[1] if "?" in path else ""
    verb = str(method or "GET").upper()
    try:
        if verb == "GET" and route == "/api/grok/status":
            return _json(200, status_payload())
        if verb == "POST" and route == "/api/grok/health":
            return _json(200, health_payload())
        if verb == "POST" and route == "/api/grok/triage":
            status, payload = triage_payload(body)
            return _json(status, payload)
        if verb == "GET" and route == "/api/advisories/crosscheck":
            params = _query(query)
            refresh = params.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            country = params.get("country", [""])[0]
            return _json(200, crosscheck_payload(country, refresh))
        if verb == "POST" and route == "/api/enterprise/enquiry":
            status, payload = enquiry_payload(body)
            return _json(status, payload)
        if verb == "POST" and route in STREAM_ROUTES:
            chunks = "".join(stream_for(route, body))
            return 200, "text/event-stream; charset=utf-8", chunks.encode("utf-8")
    except Exception:
        if route.startswith("/api/grok") or route.startswith("/api/advisories") or route.startswith("/api/enterprise"):
            return _json(200, {"ok": False, "configured": configured(), "message": "That request could not be completed."})
    return None


def _query(query: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs

    return parse_qs(query, keep_blank_values=True)


def _json(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")
