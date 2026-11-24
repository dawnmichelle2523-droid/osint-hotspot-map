#!/usr/bin/env python3
# One Responses API call, no live search (no x_search or web_search tools).
# Model grok-4-fast-reasoning, max_output_tokens 1000, timeout 25.
# Counts are computed here. The model only writes the narrative.

"""Write reports.json with daily, weekly, and monthly incident statistics."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_x_osint as osint

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports.json"
MODEL = "grok-4-fast-reasoning"
TIMEOUT = 25
MAX_OUTPUT_TOKENS = 1000

REGIONS = (
    "Syria",
    "Lebanon",
    "Israel",
    "Palestine",
    "Jordan",
    "Iraq",
    "Iran",
    "Saudi Arabia",
    "Yemen",
    "Sudan",
    "Chad",
    "Bahrain",
    "Kuwait",
    "Qatar",
    "UAE",
    "Oman",
    "Maritime",
)
CATEGORIES = (
    "Security Incident",
    "Maritime Advisory",
    "Travel / Embassy Advisory",
)
TIERS = (
    "Verified Official",
    "Corroborated",
    "Attributed",
    "Unverified Lead",
)
WINDOWS = {
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}

def cadence_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "executive_assessment": {"type": "string"},
            "regional_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "region": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["region", "note"],
                },
            },
        },
        "required": ["executive_assessment", "regional_notes"],
    }


NARRATIVE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "daily": cadence_schema(),
        "weekly": cadence_schema(),
        "monthly": cadence_schema(),
    },
    "required": ["daily", "weekly", "monthly"],
}


def tier_label(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    compact = text.lower().replace(" ", "")
    if compact in {"official/primary", "officialprimary"}:
        return "Verified Official"
    if compact in {"state-affiliated/single-source", "state-affiliated/singlesource"}:
        return "Attributed"
    if text in TIERS:
        return text
    if compact == "attributed":
        return "Attributed"
    if compact == "corroborated":
        return "Corroborated"
    if compact in {"unverifiedlead", "unverified/lead"}:
        return "Unverified Lead"
    return None


def region_label(value: object) -> str:
    text = " ".join(str(value or "").split())
    if text in REGIONS:
        return text
    return "Other / International"


def category_label(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    if text in CATEGORIES:
        return text
    return None


def feature_key(feature: dict) -> str:
    props = feature.get("properties") or {}
    return "|".join(
        [
            str(props.get("timestamp") or ""),
            str(props.get("title") or props.get("summary") or ""),
            str(props.get("source") or ""),
            str(props.get("region") or ""),
        ]
    )


def load_features() -> list[dict]:
    features = list(osint.read_collection(osint.HOTSPOTS))
    archive = osint.ARCHIVE
    if archive.exists() and archive.stat().st_size > 0:
        try:
            features.extend(osint.read_collection(archive))
        except RuntimeError:
            print("archive.json was not usable; continuing with hotspots only.")
    seen: set[str] = set()
    unique: list[dict] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        key = feature_key(feature)
        if key in seen:
            continue
        seen.add(key)
        unique.append(feature)
    return unique


def in_window(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


def percent_change(current: int, previous: int) -> int | float | None:
    if previous == 0:
        return None
    value = (current - previous) * 100 / previous
    rounded = round(value, 2)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def window_stats(features: list[dict], now: datetime, span: timedelta) -> dict:
    current_start = now - span
    previous_start = now - (span * 2)
    current_rows = []
    previous_total = 0
    for feature in features:
        props = feature.get("properties") or {}
        moment = osint.parse_timestamp(props.get("timestamp"))
        if moment is None or moment >= now:
            continue
        if in_window(moment, current_start, now):
            current_rows.append(props)
        elif in_window(moment, previous_start, current_start):
            previous_total += 1
    regions = {name: 0 for name in REGIONS}
    categories = {name: 0 for name in CATEGORIES}
    tiers = {name: 0 for name in TIERS}
    for props in current_rows:
        regions[region_label(props.get("region"))] += 1
        category = category_label(props.get("category"))
        if category:
            categories[category] += 1
        tier = tier_label(props.get("verification_tier"))
        if tier:
            tiers[tier] += 1
    total = len(current_rows)
    return {
        "total": total,
        "previous_total": previous_total,
        "percent_change": percent_change(total, previous_total),
        "counts_by_region": regions,
        "counts_by_category": categories,
        "verification_tier": tiers,
    }


def compute_stats(features: list[dict], now: datetime) -> dict:
    return {name: window_stats(features, now, span) for name, span in WINDOWS.items()}


def fallback_narrative(cadence: str, stats: dict) -> dict:
    if stats["percent_change"] is None:
        change = f"the previous period had {stats['previous_total']} incidents, so percent change is not available"
    else:
        change = (
            f"{stats['percent_change']}% versus the previous period "
            f"({stats['previous_total']} incidents)"
        )
    notes = []
    for region, count in stats["counts_by_region"].items():
        if count:
            noun = "incident" if count == 1 else "incidents"
            notes.append({"region": region, "note": f"{region} accounts for {count} {noun} in this window."})
    if not notes:
        notes.append({"region": "All regions", "note": "No incidents were recorded in this window."})
    return {
        "executive_assessment": (
            f"Analysis is unavailable. {stats['total']} incidents were counted for the {cadence} window; {change}."
        ),
        "regional_notes": notes,
    }


def stats_prompt(stats: dict, now: datetime) -> str:
    return (
        "Write a concise English geopolitical analysis for each cadence. "
        "Use only the computed statistics below. Do not use live search and do not add events, places, "
        "or actors that are not implied by these counts.\n"
        f"As of {osint.format_timestamp(now)} UTC.\n"
        "daily is the last 24 hours, weekly the last 7 days, monthly the last 30 days. "
        "percent_change is versus the previous window of the same length and is null when that previous total is 0.\n"
        "executive_assessment is 2-3 sentences.\n"
        "regional_notes is one short sentence per region that has a count above zero. "
        "Use the region name exactly as given. Omit regions whose count is 0.\n\n"
        + json.dumps(stats, ensure_ascii=False)
    )


def call_narratives(client, prompt: str) -> dict:
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "intelligence_reports",
                "schema": NARRATIVE_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=TIMEOUT,
    )
    text = osint.response_text(response)
    if not text:
        raise RuntimeError("xAI response did not include output text")
    payload = osint.parse_json_object(text)
    narratives = {}
    for cadence in WINDOWS:
        block = payload.get(cadence)
        if not isinstance(block, dict):
            raise RuntimeError(f"missing {cadence} narrative")
        assessment = " ".join(str(block.get("executive_assessment") or "").split())
        if not assessment:
            raise RuntimeError(f"missing {cadence} assessment")
        notes = []
        for item in block.get("regional_notes") or []:
            if not isinstance(item, dict):
                continue
            region = " ".join(str(item.get("region") or "").split())
            note = " ".join(str(item.get("note") or "").split())
            if region and note:
                notes.append({"region": region, "note": note})
        narratives[cadence] = {"executive_assessment": assessment, "regional_notes": notes}
    return narratives


def assemble(stats: dict, narratives: dict, now: datetime) -> dict:
    payload = {"generated_at": osint.format_timestamp(now)}
    for cadence in WINDOWS:
        block = dict(stats[cadence])
        story = narratives.get(cadence) or fallback_narrative(cadence, stats[cadence])
        block["executive_assessment"] = story["executive_assessment"]
        block["regional_notes"] = story["regional_notes"]
        payload[cadence] = block
    return payload


def write_reports(payload: dict) -> None:
    temporary = REPORTS.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(REPORTS)


def main() -> None:
    try:
        features = load_features()
    except RuntimeError as exc:
        print(f"{exc}. reports.json was not changed.")
        return
    now = datetime.now(timezone.utc)
    stats = compute_stats(features, now)
    key = osint.read_api_key()
    narratives = None
    if not key:
        print("XAI_API_KEY is missing. reports.json saved with stats only.")
    else:
        try:
            narratives = call_narratives(osint.make_client(key), stats_prompt(stats, now))
        except Exception as exc:
            print(f"Report analysis failed: {osint.public_error(exc, key)}. reports.json saved with stats only.")
    if narratives is None:
        narratives = {cadence: fallback_narrative(cadence, stats[cadence]) for cadence in WINDOWS}
    write_reports(assemble(stats, narratives, now))
    totals = ", ".join(f"{name} {stats[name]['total']}" for name in WINDOWS)
    print(f"Wrote {REPORTS.name} ({totals})")


if __name__ == "__main__":
    main()
