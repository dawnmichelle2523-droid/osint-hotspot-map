"""On-demand Anthropic Claude drafts for risk assessments and area briefings.

This module never starts a thread, never polls, and never calls xAI.
Anthropic is contacted only when a caller invokes a draft function and
ANTHROPIC_API_KEY is set. A missing key, a rate limit, or any other API
error returns the local template instead.

The API key is read from the environment and is never placed in a response,
a prompt, or a log line.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

ROOT = Path(__file__).resolve().parent
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"

RA_MODEL_DEFAULT = "claude-sonnet-5-5"
BRIEF_MODEL_DEFAULT = "claude-haiku-5-5"
RA_MAX_TOKENS = 2400
BRIEF_MAX_TOKENS = 1200
RA_EFFORT = "medium"
BRIEF_EFFORT = "low"
PROMPT_CHAR_CAP = 8000
UK_ENGLISH = (
    "Write in UK English. Use British spelling and conventions, including travelling, "
    "organisation, defence, licence, centre, programme, labelled, and towards. "
    "Write dates as day month year. Do not use American spellings."
)

# Published list prices in USD per million tokens (input, output).
# Haiku 5.5 uses the higher pair once a prompt exceeds 100,000 tokens.
# https://platform.claude.com/docs/en/about-claude/pricing
_SONNET_55 = (2.0, 10.0)
_HAIKU_55_SHORT = (0.10, 0.50)
_HAIKU_55_LONG = (0.50, 2.50)
HAIKU_LONG_CONTEXT = 100_000
LIST_PRICES = {
    "claude-sonnet-5-5": _SONNET_55,
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

ROUTES = frozenset({
    "/api/ai/claude-status",
    "/api/risk-assessments/ai-draft",
    "/api/briefings/ai-draft",
})

# Personal fields are dropped before a prompt is built. Public OSINT text is
# kept and then scrubbed for emails, phone numbers, IMEIs, and labeled names.
_FORBIDDEN_KEYS = {
    "name", "names", "staff", "staff_name", "staff_names", "full_name",
    "first_name", "last_name", "personal_name", "traveller", "traveler",
    "travellers", "travelers", "patient", "patients", "pupil", "pupils",
    "student", "student_name", "student_names", "employee", "contractor",
    "reviewer", "reviewer_name", "advisor", "adviser", "owner", "callsign",
    "callsigns", "roster", "pupil_roster", "contact", "contacts",
    "emergency_contact", "emergency_contacts", "next_of_kin", "parent_contact",
    "email", "emails", "contact_email", "phone", "phones", "telephone",
    "mobile", "imei", "imeis", "meid", "passport", "passport_number",
    "national_id", "ssn", "nhs", "nhs_number", "medical", "medical_history",
    "diagnosis", "medication", "medications", "allergy", "allergies",
    "blood_type", "dob", "date_of_birth", "address", "home_address",
    "author_email", "author_phone", "window",
}

_INCIDENT_KEYS = (
    "title", "summary", "severity", "category", "country", "region",
    "area", "location_name", "timestamp", "source_name", "confidence",
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
INTL_PHONE_RE = re.compile(r"\+\d[\d\s().\-]{7,}\d")
GROUP_PHONE_RE = re.compile(r"\b\d{2,4}[\s.\-]\d{3,4}[\s.\-]\d{3,4}(?:[\s.\-]\d{2,4})?\b")
IMEI_LABEL_RE = re.compile(r"(?i)\bimeis?\b\s*[:#\-]?\s*(?:\d[\s\-]?){14,17}")
IMEI_BARE_RE = re.compile(r"(?<!\d)\d{15}(?!\d)")
ID_RE = re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")
ROLE_RE = re.compile(
    r"(?i)\b(?:staff|officers?|reviewers?|operators?|drivers?|medics?|guards?|analysts?|"
    r"advis[eo]rs?|travell?ers?|pupils?|students?|employees?|contractors?|callsigns?|"
    r"passengers?|principals?|names?)\b\s*[:\-]\s*[A-Z][A-Za-z'’.\-]+(?:\s+[A-Z][A-Za-z'’.\-]+){0,2}"
)
KEY_RE = re.compile(r"\b(?:sk-ant-|xai-)[A-Za-z0-9_\-]{6,}\b", re.I)

_stats = {"calls": 0}

claude_bp = Blueprint("claude_ai", __name__)


def anthropic_calls() -> int:
    """How many times this process has posted to Anthropic. Tests use this."""
    return _stats["calls"]


def reset_call_count() -> None:
    _stats["calls"] = 0


def _load_env() -> None:
    """Load a local .env without overriding variables already set on the process."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        return


def read_api_key() -> str:
    """Return the server key. Callers must not put this value in a response or log."""
    _load_env()
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def configured() -> bool:
    return bool(read_api_key())


def _model_name(raw: object, default: str) -> str:
    text = str(raw or "").strip()
    if not text or len(text) > 80 or any(ch.isspace() for ch in text):
        return default
    if "sk-" in text.lower() or "xai-" in text.lower() or "@" in text:
        return default
    return text


def ra_model() -> str:
    return _model_name(os.environ.get("CLAUDE_RA_MODEL"), RA_MODEL_DEFAULT)


def brief_model() -> str:
    return _model_name(os.environ.get("CLAUDE_BRIEF_MODEL"), BRIEF_MODEL_DEFAULT)


def _rates(model: str, input_tokens: int) -> tuple[float, float] | None:
    if model == "claude-haiku-5-5" or model.startswith("claude-haiku-5-5"):
        if input_tokens > HAIKU_LONG_CONTEXT:
            return _HAIKU_55_LONG
        return _HAIKU_55_SHORT
    if model in LIST_PRICES:
        return LIST_PRICES[model]
    if "haiku-5-5" in model:
        return _HAIKU_55_SHORT if input_tokens <= HAIKU_LONG_CONTEXT else _HAIKU_55_LONG
    if model.startswith("claude-sonnet-5"):
        return _SONNET_55
    return None


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = _rates(model, input_tokens)
    if rates is None:
        return None
    dollars = (input_tokens / 1_000_000) * rates[0] + (output_tokens / 1_000_000) * rates[1]
    return round(dollars, 6)


def _token_guess(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def scrub_text(value: object, limit: int = 400) -> str:
    """Remove emails, phone numbers, IMEIs, ID-like tokens, and labeled staff names."""
    text = " ".join(str(value or "").split())
    key = read_api_key()
    if key:
        text = text.replace(key, "[removed]")
    text = KEY_RE.sub("[removed]", text)
    text = EMAIL_RE.sub("[removed]", text)
    text = IMEI_LABEL_RE.sub("[removed]", text)
    text = IMEI_BARE_RE.sub("[removed]", text)
    text = INTL_PHONE_RE.sub("[removed]", text)
    text = GROUP_PHONE_RE.sub("[removed]", text)
    text = ID_RE.sub("[removed]", text)
    text = ROLE_RE.sub("[removed]", text)
    text = " ".join(text.split())
    return text[:limit]


def _norm_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _withheld_key(name: str) -> bool:
    token = _norm_key(name)
    if token in _FORBIDDEN_KEYS:
        return True
    return token.rstrip("s") in _FORBIDDEN_KEYS


def public_context(body: object, *, task: str) -> tuple[dict, bool]:
    """Copy only country, region, sector, deployment scope, and public incidents.

    Hazard labels and scores are included for a risk narrative because they are
    the assessment, not a person. Every other key is ignored.
    """
    withheld = False
    if not isinstance(body, dict):
        body = {}
    else:
        for key in body:
            if _withheld_key(str(key)):
                withheld = True

    country = scrub_text(body.get("country") or body.get("theatre") or body.get("scope") or "", 80)
    region = scrub_text(body.get("region") or "", 80)
    if not region:
        region = country
    sector = scrub_text(body.get("sector") or "", 160)
    scope = scrub_text(
        body.get("deployment_scope") or body.get("operation") or body.get("purpose") or "",
        240,
    )
    standard = scrub_text(
        body.get("compliance_standard") or body.get("standard") or body.get("template") or "",
        240,
    )
    cadence = _norm_key(body.get("cadence") or body.get("period") or "")
    if cadence not in {"daily", "weekly", "monthly", "custom"}:
        cadence = "daily" if task == "briefing" else ""
    if task == "briefing" and not scope:
        label = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "custom": "Custom"}.get(cadence, "Area")
        days = body.get("days")
        if cadence == "custom" and str(days or "").isdigit():
            scope = f"Custom {int(days)}-day area-of-operations briefing"
        else:
            scope = f"{label} area-of-operations briefing"
    if task == "risk" and not scope:
        scope = "Country and sector risk assessment"

    hazards = []
    raw_hazards = body.get("hazards") if isinstance(body.get("hazards"), list) else []
    for item in raw_hazards[:12]:
        if not isinstance(item, dict):
            withheld = True
            continue
        for key in item:
            if _withheld_key(str(key)):
                withheld = True
        threat = scrub_text(item.get("threat") or item.get("hazard") or "", 80)
        if not threat or threat == "[removed]":
            continue
        hazards.append({
            "threat": threat,
            "likelihood": _score(item.get("likelihood")),
            "impact": _score(item.get("impact")),
        })

    incidents = []
    raw_incidents = body.get("incidents") or body.get("recent_incidents") or []
    if not isinstance(raw_incidents, list):
        raw_incidents = []
        withheld = True
    for item in raw_incidents[:12]:
        if not isinstance(item, dict):
            withheld = True
            continue
        for key in item:
            if _withheld_key(str(key)):
                withheld = True
        place = scrub_text(item.get("area") or item.get("location_name") or "", 80)
        title = scrub_text(item.get("title") or "", 180)
        summary = scrub_text(item.get("summary") or "", 280)
        if not title and not summary:
            continue
        incidents.append({
            "title": title or "Public incident",
            "summary": summary,
            "severity": scrub_text(item.get("severity") or "", 16).lower(),
            "category": scrub_text(item.get("category") or "", 40),
            "country": scrub_text(item.get("country") or country, 80),
            "region": scrub_text(item.get("region") or region, 80),
            "area": place,
            "timestamp": scrub_text(item.get("timestamp") or "", 40),
            "source_name": scrub_text(item.get("source_name") or "", 60),
            "confidence": scrub_text(item.get("confidence") or "", 24),
        })

    draft = ""
    action = _norm_key(body.get("action") or "draft")
    if action == "refine":
        draft = scrub_text(body.get("draft") or body.get("narrative") or "", 2000)

    context = {
        "country": country,
        "region": region,
        "sector": sector,
        "deployment_scope": scope,
        "compliance_standard": standard if task == "risk" else "",
        "cadence": cadence,
        "hazards": hazards if task == "risk" else [],
        "incidents": incidents,
        "draft": draft,
        "action": "refine" if draft else "draft",
    }
    blob = json.dumps(context, ensure_ascii=False)
    if "[removed]" in blob:
        withheld = True
    return context, withheld


def _score(value: object) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unscored"
    if number < 1 or number > 5:
        return "unscored"
    return str(number)


def _area_line(context: dict) -> str:
    country = context.get("country") or ""
    region = context.get("region") or ""
    if country and region and country != region:
        return f"Country: {country}. Region: {region}."
    if country or region:
        return f"Area: {country or region}."
    return "Area: not named."


def _incident_lines(context: dict) -> list[str]:
    lines = []
    ranked = {"high": 0, "medium": 1, "low": 2}
    items = list(context.get("incidents") or [])
    items.sort(key=lambda item: (ranked.get(item.get("severity") or "", 3), item.get("title") or ""))
    for item in items:
        bits = [
            item.get("severity") or "",
            item.get("area") or "",
            item.get("title") or "",
            item.get("source_name") or "",
        ]
        line = " | ".join(bit for bit in bits if bit)
        if item.get("summary") and item.get("summary") not in line:
            line = f"{line} — {item['summary']}"
        if line:
            lines.append(line)
    return lines


def _product_band(likelihood: object, impact: object) -> str:
    """Same 1–25 bands as the on-screen matrix. Unscored if either axis is missing."""
    try:
        left = int(likelihood)
        right = int(impact)
    except (TypeError, ValueError):
        return "unscored"
    if left < 1 or left > 5 or right < 1 or right > 5:
        return "unscored"
    score = left * right
    if score <= 4:
        name = "Low"
    elif score <= 9:
        name = "Moderate"
    elif score <= 14:
        name = "High"
    elif score <= 19:
        name = "Very High"
    else:
        name = "Severe"
    return f"{left} × {right} = {score} ({name})"


def local_risk_narrative(context: dict) -> str:
    """Deterministic country and sector draft. No network call."""
    sector = context.get("sector") or "the selected sector"
    standard = context.get("compliance_standard") or "the selected compliance frame"
    scope = context.get("deployment_scope") or "the stated deployment"
    lines = _incident_lines(context)
    cited = "; ".join(line[:80] for line in lines[:4]) or "no public incidents were supplied"
    hazards = context.get("hazards") or []
    hazard_lines = []
    for hazard in hazards:
        rating = _product_band(hazard.get("likelihood"), hazard.get("impact"))
        hazard_lines.append(
            f"- {hazard.get('threat') or 'Hazard'} — inherent {rating}. "
            "Threat-actor capability and intent are not stated beyond this public line. "
            "Confirm the official travel advice, name an action owner by role on this device, "
            "and re-score the residual risk once the mitigation is in place."
        )
    if not hazard_lines:
        hazard_lines.append(
            "- General travel security — inherent unscored. "
            "Confirm the official travel advice, name an action owner by role on this device, "
            "and re-score the residual risk once the mitigation is in place."
        )
    lead = (
        f"Local risk-assessment draft for {sector} under {standard}. "
        f"{_area_line(context)} Deployment scope: {scope}. "
        "This is the on-server template. Claude was not called."
    )
    if context.get("action") == "refine" and context.get("draft"):
        lead += " The previous draft was kept on the server only as scrubbed public text and was not sent."
    return (
        lead
        + " Aligned with ISO 31000, ISO 31030 (travel risk management), and ISO 45001 (field health and safety). "
        + "Apply BS 8848 for an education or school trip, ISO 18788 for close protection, and ISO 22361 for crisis management, "
        + "alongside the selected frame. "
        + "The organisation should prioritise mitigation before the traveller deploys: a medevac centre, "
        + "a stand-off from checkpoints, and hostile environment controls. "
        + "Public incidents considered: "
        + cited
        + "\n"
        + "\n".join(hazard_lines)
        + "\nRoutes and checkpoints: use the agreed route only, and treat an unplanned checkpoint as a stop."
        + "\nAirspace: read the current NOTAM and any EASA Conflict Zone Information Bulletin (CZIB) before ticketing. Do not invent a closure."
        + "\nMedevac: in a hostile environment the golden hour is often spent reaching a trauma centre that can accept the patient. Name the hospital and the hub before departure."
        + "\n2-tap check-in ladder: overdue at +30 minutes the ops desk calls; at +60 minutes switch to the alternate channel; at +120 minutes start the missing-person protocol."
        + "\nShelter-in-place on indirect fire or a crowd at the only exit. Hibernate if the corridor closes or a severe weather warning covers the route."
        + "\nGo when residual risk is Low or Moderate and the controls, medevac, and check-in ladder are confirmed. "
        + "Conditional-Go when residual risk is High and the named conditions are met before movement. "
        + "No-Go when residual risk is Very High or Severe, or a depended-on control is absent."
        + "\nA qualified person must review this draft before it is issued. Written in UK English."
    )


def local_briefing_narrative(context: dict) -> str:
    """Deterministic daily, weekly, or monthly area briefing. No network call."""
    cadence = context.get("cadence") or "daily"
    label = {"daily": "daily", "weekly": "weekly", "monthly": "monthly", "custom": "custom range"}.get(cadence, "area")
    sector = context.get("sector") or "unspecified sector"
    scope = context.get("deployment_scope") or f"{label.title()} area-of-operations briefing"
    lines = _incident_lines(context)
    if not lines:
        picture = "No public incidents were supplied for this window. That is not the same as a quiet ground picture."
    else:
        picture = (
            f"{len(lines)} public incident{'s' if len(lines) != 1 else ''} "
            f"{'were' if len(lines) != 1 else 'was'} supplied. "
            f"Leading item: {lines[0]}."
        )
        if len(lines) > 1:
            picture += " Also noted: " + " ".join(lines[1:4])
    return (
        f"Local {label} area briefing. {_area_line(context)} "
        f"Sector: {sector}. Deployment scope: {scope}. "
        "This is the on-server template. Claude was not called. Written in UK English.\n"
        f"BLUF: {picture} Check the official travel advice before any movement. "
        "The organisation should prioritise the traveller's duty of care. "
        "This template does not authorise a Go, a Conditional-Go, or a No-Go.\n"
        f"Verified Security Incidents: {picture} Treat unverified claims as claims.\n"
        "Sub-Regional/Governorate Status: No governorate picture is invented beyond the public incidents supplied. "
        "Confirm the control lines before movement.\n"
        "Aviation/Routes/Infrastructure: Read the current NOTAM and any EASA CZIB for the flight information region. "
        "Do not treat a quiet feed as an open airspace or an open road.\n"
        "24h Operational Advice: Keep a stand-off from checkpoints and gatherings. "
        "Confirm the medevac centre and the trauma route. "
        "Run the 2-tap check-in ladder if a check-in is overdue: +30 minutes call, +60 minutes alternate channel, "
        "+120 minutes missing-person protocol. Shelter in place if the only exit is blocked. "
        "Hibernate if the corridor closes.\n"
        "24–72h Outlook: Re-read the official travel advice and the live feed before the next movement window. "
        "What is not in the public incidents above is not known.\n"
        "A qualified person must review this draft before it is issued."
    )


def _local_narrative(task: str, context: dict) -> str:
    if task == "briefing":
        return local_briefing_narrative(context)
    return local_risk_narrative(context)


def _prompt(task: str, context: dict) -> tuple[str, str]:
    if task == "briefing":
        system = (
            "You are a senior international security-risk adviser drafting a daily, weekly, or monthly "
            "area-of-operations briefing for a security director. Write with institutional authority, in strict UK English, "
            "using British spelling and dates as day month year. "
            "Use only the public context below. Do not invent incidents, casualty figures, threat-actor names, or private people. "
            "Do not mention staff names, traveller names, phone numbers, emails, medical records, passport details, or device identifiers. "
            "If the notes are thin, say what is not known. This is decision support. A qualified person must review it before issue.\n"
            "Use exactly these six sections, in this order, with these headings:\n"
            "1. BLUF\n"
            "2. Verified Security Incidents\n"
            "3. Sub-Regional/Governorate Status\n"
            "4. Aviation/Routes/Infrastructure\n"
            "5. 24h Operational Advice\n"
            "6. 24–72h Outlook\n"
            "BLUF is the judgement in a few lines, including whether the public picture leans Go, Conditional-Go, or No-Go, and the single priority. "
            "Verified Security Incidents lists only items supplied in the public context. "
            "Sub-Regional/Governorate Status covers the named area and does not invent a governorate picture. "
            "Aviation/Routes/Infrastructure covers airspace, NOTAM and EASA Conflict Zone Information Bulletin (CZIB) context, routes, and checkpoints only from the public context. "
            "24h Operational Advice must prioritise movement, accommodation, communications, medevac, and the 2-tap check-in ladder "
            "(overdue at +30 minutes, +60 minutes, and +120 minutes), plus shelter-in-place and hibernation triggers. "
            "Keep the ops centre on that ladder. 24–72h Outlook states what to watch and what is not known. "
            "Refer to the organisation and the traveller. Hold a stand-off from unverified claims. "
            + UK_ENGLISH
        )
    else:
        system = (
            "You are a senior international security-risk adviser drafting a country and sector risk assessment "
            "for a security director. Write with institutional authority, in strict UK English. "
            "This is decision support. A qualified person must review it before it is issued.\n"
            "Align the assessment with ISO 31000 (risk management), ISO 31030 (travel risk management), "
            "and ISO 45001 (occupational health and safety in the field). Apply the sector frame that fits: "
            "BS 8848 for education and school trips, ISO 18788 for close protection and private security operations, "
            "and ISO 22361 for crisis management. Use the compliance frame supplied when it names one of these.\n"
            "Use only the public context below. Do not invent incidents, casualty figures, threat-actor names, or private people. "
            "Do not mention staff names, traveller names, phone numbers, emails, medical records, passport details, or device identifiers. "
            "If the notes are thin, say what is not known.\n"
            "Score with a 5×5 Likelihood × Impact matrix. Likelihood runs from 1 Rare to 5 Almost Certain. "
            "Impact runs from 1 Negligible to 5 Catastrophic. Score = Likelihood × Impact (1–25). "
            "Bands: Low 1–4, Moderate 5–9, High 10–14, Very High 15–19, Severe 20–25. "
            "State each inherent and residual rating as L × I = Score and name the band.\n"
            "For each hazard cover, in UK English: threat-actor capability and intent only where the public context supports it; "
            "route and checkpoint considerations; airspace and aviation context, including NOTAM and EASA CZIB; "
            "trauma and medevac realities, including the golden hour in a hostile environment and the named trauma centre; "
            "a 2-tap check-in escalation ladder (overdue at +30 minutes, +60 minutes, and +120 minutes); "
            "shelter-in-place and hibernation triggers; and mandatory mitigation for pre-deployment, movement, accommodation, "
            "communications, and contingency. Prioritise the controls that reduce residual risk before the traveller deploys. "
            "Name an action owner by role (Security Advisor, Team Leader, Driver/Fixer, or Ops Desk), never by personal name. "
            "Hold a stand-off from clashes and from a crowd.\n"
            "Close with sign-off criteria. Go when residual risk is Low or Moderate and the controls, medevac, and check-in ladder are in place. "
            "Conditional-Go when residual risk is High and named conditions must be met before movement. "
            "No-Go when residual risk is Very High or Severe, or a depended-on control is absent. "
            "The organisation remains responsible for acceptance of the residual risk. "
            + UK_ENGLISH
        )
    user_lines = [
        _area_line(context),
        f"Sector: {context.get('sector') or 'unspecified'}.",
        f"Deployment scope: {context.get('deployment_scope') or 'unspecified'}.",
    ]
    if task == "briefing":
        user_lines.append(f"Cadence: {context.get('cadence') or 'daily'}.")
        user_lines.append(
            "Write the six sections in order: BLUF; Verified Security Incidents; "
            "Sub-Regional/Governorate Status; Aviation/Routes/Infrastructure; "
            "24h Operational Advice; 24–72h Outlook. Use UK English."
        )
    if context.get("compliance_standard"):
        user_lines.append(f"Compliance frame: {context['compliance_standard']}.")
    if task == "risk":
        user_lines.append(
            "Apply ISO 31000, ISO 31030, and ISO 45001. Apply BS 8848, ISO 18788, or ISO 22361 when the sector calls for it. "
            "For each hazard give the inherent rating as L × I = Score with the band "
            "(Low 1–4, Moderate 5–9, High 10–14, Very High 15–19, Severe 20–25), "
            "the country and operational context, the mandatory mitigation, a residual rating, and an action owner by role. "
            "Cover threat-actor capability and intent, routes and checkpoints, NOTAM and EASA CZIB, "
            "medevac and the golden hour, the 2-tap ladder (+30 / +60 / +120 minutes), "
            "shelter-in-place and hibernation, and Go / Conditional-Go / No-Go. Write in UK English."
        )
    user_lines.append("Public incidents:")
    incident_lines = _incident_lines(context)
    user_lines.extend(f"- {line}" for line in incident_lines)
    if not incident_lines:
        user_lines.append("- None supplied.")
    if task == "risk":
        user_lines.append("Hazards:")
        hazards = context.get("hazards") or []
        if hazards:
            for hazard in hazards:
                user_lines.append(
                    f"- {hazard.get('threat')} (likelihood {hazard.get('likelihood')}, impact {hazard.get('impact')})"
                )
        else:
            user_lines.append("- General travel security (unscored).")
    if context.get("action") == "refine" and context.get("draft"):
        user_lines.append(
            "Refine this earlier public draft in UK English. "
            "Drop anything that is not country, region, sector, scope, or public incident context."
        )
        user_lines.append(context["draft"])
    user = "\n".join(user_lines)[:PROMPT_CHAR_CAP]
    return system, user


def _status_unconfigured(task: str) -> str:
    if task == "briefing":
        return (
            "Claude is not connected. Add ANTHROPIC_API_KEY on the server to draft briefings with Claude. "
            "This is the local template. Nothing was sent to Anthropic, and no xAI credit was used."
        )
    return (
        "Claude is not connected. Add ANTHROPIC_API_KEY on the server to draft risk assessments with Claude. "
        "This is the local template. Nothing was sent to Anthropic, and no xAI credit was used."
    )


def _status_error(kind: str) -> str:
    if kind == "rate":
        reason = "Claude is busy because of a rate limit."
    elif kind == "auth":
        reason = "Claude rejected the API key. The key was not shown."
    elif kind == "network":
        reason = "Claude could not be reached."
    else:
        reason = "Claude did not return a usable draft."
    return (
        f"{reason} This is the local template instead. "
        "Nothing further was sent, and no xAI credit was used."
    )


def _status_ok(model: str, task: str) -> str:
    kind = "briefing" if task == "briefing" else "risk assessment"
    return f"Drafted with Claude ({model}) for this {kind}. A person should review it before it is issued."


def _estimate_block(model: str, system: str, user: str, output_cap: int, *, sent: bool) -> dict:
    input_tokens = _token_guess(system + "\n" + user)
    rates = _rates(model, input_tokens)
    return {
        "input_tokens": input_tokens,
        "output_cap": output_cap,
        "usd_cap": estimate_usd(model, input_tokens, output_cap),
        "input_usd_per_mtok": None if rates is None else rates[0],
        "output_usd_per_mtok": None if rates is None else rates[1],
        "sent": sent,
        "method": "About 4 characters per token. The dollar figure is a published list-price ceiling, not an invoice.",
    }


def _pack_result(
    *,
    task: str,
    context: dict,
    withheld: bool,
    narrative: str,
    status: str,
    source: str,
    model: str | None,
    estimate: dict,
    usage: dict,
) -> dict:
    return {
        "ok": True,
        "configured": configured(),
        "source": source,
        "provider": "anthropic" if source == "claude" else "local",
        "model": model,
        "task": task,
        "narrative": narrative,
        "status": status,
        "pii_withheld": withheld,
        "on_demand": True,
        "usage": usage,
        "estimate": estimate,
        "context_sent": {
            "country": context.get("country") or "",
            "region": context.get("region") or "",
            "sector": context.get("sector") or "",
            "deployment_scope": context.get("deployment_scope") or "",
        },
    }


def _draft(task: str, body: object) -> dict:
    context, withheld = public_context(body, task=task)
    model = brief_model() if task == "briefing" else ra_model()
    output_cap = BRIEF_MAX_TOKENS if task == "briefing" else RA_MAX_TOKENS
    effort = BRIEF_EFFORT if task == "briefing" else RA_EFFORT
    system, user = _prompt(task, context)
    local = _local_narrative(task, context)
    key = read_api_key()
    if not key:
        return _pack_result(
            task=task,
            context=context,
            withheld=withheld,
            narrative=local,
            status=_status_unconfigured(task),
            source="local",
            model=None,
            estimate=_estimate_block(model, system, user, output_cap, sent=False),
            usage={"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0},
        )
    text, usage, failure = _call_anthropic(key, model, system, user, output_cap, effort)
    if failure or not text:
        kind = failure or "empty"
        print(f"Claude draft was not completed ({kind}). Local template used.")
        return _pack_result(
            task=task,
            context=context,
            withheld=withheld,
            narrative=local,
            status=_status_error(kind),
            source="local",
            model=model,
            estimate=_estimate_block(model, system, user, output_cap, sent=False),
            usage={"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0},
        )
    actual_in = int(usage.get("input_tokens") or 0)
    actual_out = int(usage.get("output_tokens") or 0)
    return _pack_result(
        task=task,
        context=context,
        withheld=withheld,
        narrative=scrub_text(text, 6000),
        status=_status_ok(model, task),
        source="claude",
        model=model,
        estimate=_estimate_block(model, system, user, output_cap, sent=True),
        usage={
            "input_tokens": actual_in,
            "output_tokens": actual_out,
            "estimated_usd": estimate_usd(model, actual_in, actual_out),
        },
    )


def _call_anthropic(
    key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    effort: str,
) -> tuple[str, dict, str]:
    """One Messages API call. Returns (text, usage, failure code).

    failure is empty on success. This function does not retry.
    """
    _stats["calls"] += 1
    try:
        response = requests.post(
            API_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "output_config": {"effort": effort},
            },
            timeout=(8, 45),
        )
    except requests.RequestException:
        return "", {}, "network"
    if response.status_code == 429 or response.status_code == 529:
        return "", {}, "rate"
    if response.status_code in {401, 403}:
        return "", {}, "auth"
    if response.status_code >= 400:
        return "", {}, "error"
    try:
        payload = response.json()
    except ValueError:
        return "", {}, "error"
    if not isinstance(payload, dict):
        return "", {}, "error"
    text = _message_text(payload)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not text:
        return "", usage, "empty"
    return text, usage, ""


def _message_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    return "\n".join(part.strip() for part in parts if str(part or "").strip()).strip()


def draft_risk_assessment(body: object) -> dict:
    """On-demand Sonnet draft. Falls back to the local template."""
    return _draft("risk", body)


def draft_briefing(body: object) -> dict:
    """On-demand Haiku draft for a daily, weekly, or monthly briefing."""
    return _draft("briefing", body)


def status_payload() -> dict:
    """Configuration only. Does not call Anthropic."""
    ready = configured()
    ra = ra_model()
    brief = brief_model()
    ra_rates = _rates(ra, 1) or (None, None)
    brief_rates = _rates(brief, 1) or (None, None)
    if ready:
        message = (
            f"Claude is connected. Risk drafts use {ra}. Briefings use {brief}. "
            "Nothing is sent until you click Draft with Claude."
        )
    else:
        message = (
            "Claude is ready for an ANTHROPIC_API_KEY. "
            f"Once set, risk drafts use {ra} and briefings use {brief}. "
            "Nothing is sent until you click Draft with Claude."
        )
    return {
        "configured": ready,
        "ra_model": ra,
        "brief_model": brief,
        "status": message,
        "on_demand": True,
        "rates": {
            "ra": {
                "model": ra,
                "input_usd_per_mtok": ra_rates[0],
                "output_usd_per_mtok": ra_rates[1],
                "note": "Published list price per million tokens. An estimate, not an invoice.",
            },
            "brief": {
                "model": brief,
                "input_usd_per_mtok": brief_rates[0],
                "output_usd_per_mtok": brief_rates[1],
                "note": "Haiku 5.5 list price for prompts up to 100,000 tokens. Longer prompts use the higher published tier.",
            },
        },
    }


def _load_body(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pack(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")


def dispatch(method: str, path: str, body: bytes = b"") -> tuple[int, str, bytes] | None:
    """Stdlib-server entry. Draft routes are the only ones that can call Anthropic."""
    route = path.split("?", 1)[0]
    verb = str(method or "GET").upper()
    if route not in ROUTES:
        return None
    if route == "/api/ai/claude-status" and verb in {"GET", "POST"}:
        return _pack(200, status_payload())
    if verb != "POST":
        return _pack(405, {
            "ok": False,
            "configured": configured(),
            "status": "Use POST to draft. Checking Claude does not send a prompt.",
        })
    payload = _load_body(body)
    if route == "/api/risk-assessments/ai-draft":
        return _pack(200, draft_risk_assessment(payload))
    if route == "/api/briefings/ai-draft":
        return _pack(200, draft_briefing(payload))
    return None


def _respond(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@claude_bp.route("/api/ai/claude-status", methods=["GET", "POST"])
def api_claude_status():
    return _respond(status_payload())


@claude_bp.post("/api/risk-assessments/ai-draft")
def api_risk_ai_draft():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    return _respond(draft_risk_assessment(body))


@claude_bp.post("/api/briefings/ai-draft")
def api_briefing_ai_draft():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    return _respond(draft_briefing(body))
