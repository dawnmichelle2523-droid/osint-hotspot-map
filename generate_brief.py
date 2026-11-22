#!/usr/bin/env python3
# One Responses API call, no live search (no x_search or web_search tools).
# Model grok-4-fast-reasoning, timeout 25.
# https://docs.x.ai/developers/model-capabilities/text/structured-outputs

"""Write daily_brief.json from hotspots in the last 24 hours."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_x_osint as osint

ROOT = Path(__file__).resolve().parent
BRIEF = ROOT / "daily_brief.json"
MODEL = "grok-4-fast-reasoning"
TIMEOUT = 25
MAX_OUTPUT_TOKENS = 1000

BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "executive_summary": {"type": "string"},
        "regional_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "region": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["region", "summary"],
            },
        },
        "advisory_highlights": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "regional_sections", "advisory_highlights"],
}


def recent_incidents(features: list, now: datetime) -> list[dict]:
    """Incidents whose timestamp is within the last 24 hours."""
    cutoff = now - timedelta(hours=24)
    latest = now + timedelta(minutes=15)
    kept = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        moment = osint.parse_timestamp(props.get("timestamp"))
        if moment is None or moment < cutoff or moment > latest:
            continue
        kept.append(
            {
                "title": osint.one_sentence(props.get("title") or props.get("summary")),
                "summary": osint.one_sentence(props.get("summary")),
                "timestamp": osint.format_timestamp(moment),
                "region": props.get("region") or "",
                "category": props.get("category") or "",
                "severity": props.get("severity") or "",
                "verification_tier": props.get("verification_tier") or "",
                "source": props.get("source") or "",
            }
        )
    kept.sort(key=lambda item: item["timestamp"], reverse=True)
    return kept


def brief_prompt(incidents: list[dict], now: datetime) -> str:
    lines = []
    for item in incidents:
        lines.append(
            f"- [{item['timestamp']}] {item['region']} | {item['category']} | {item['severity']} | "
            f"{item['verification_tier']} | {item['title']} | {item['summary']}"
        )
    body = "\n".join(lines) if lines else "(no incidents in the last 24 hours)"
    return (
        "Write a Daily Geopolitical Brief for the Middle East from the incidents below only. "
        "Do not use live search and do not add events that are not listed.\n"
        f"As of {osint.format_timestamp(now)}. Window: the last 24 hours.\n"
        "executive_summary is 2-4 sentences of plain English.\n"
        "regional_sections is one object per region that has an incident or advisory. "
        "region is exactly one of: Gulf, Saudi & Yemen, Levant, Iran & Iraq, Maritime, Other / International. "
        "summary is 1-2 sentences.\n"
        "advisory_highlights lists maritime and travel or embassy advisories as short sentences. "
        "Use an empty array when there are none.\n"
        "Name the verification tier when a claim is not an official or primary source.\n\n"
        f"{body}"
    )


def normalize_brief(payload: dict) -> dict:
    summary = osint.one_sentence(payload.get("executive_summary"))
    if not summary:
        raise ValueError("brief had no executive summary")
    sections = []
    for item in payload.get("regional_sections") or []:
        if not isinstance(item, dict):
            continue
        region = osint.one_sentence(item.get("region"))
        text = osint.one_sentence(item.get("summary"))
        if region and text:
            sections.append({"region": region, "summary": text})
    highlights = []
    for item in payload.get("advisory_highlights") or []:
        text = osint.one_sentence(item)
        if text:
            highlights.append(text)
    return {
        "executive_summary": summary,
        "regional_sections": sections,
        "advisory_highlights": highlights,
    }


def call_brief(client, prompt: str) -> dict:
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "daily_brief",
                "schema": BRIEF_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=TIMEOUT,
    )
    text = osint.response_text(response)
    if not text:
        raise RuntimeError("xAI response did not include output text")
    return normalize_brief(osint.parse_json_object(text))


def write_brief(payload: dict, path: Path = BRIEF) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    key = osint.read_api_key()
    if not key:
        print("XAI_API_KEY is missing. Add it to the environment or a .env file. daily_brief.json was not changed.")
        return
    try:
        features = osint.read_collection(osint.HOTSPOTS)
    except RuntimeError as exc:
        print(f"{exc}. daily_brief.json was not changed.")
        sys.exit(1)
    now = datetime.now(timezone.utc)
    incidents = recent_incidents(features, now)
    print(f"Briefing {len(incidents)} incident(s) from the last 24 hours")
    client = osint.make_client(key)
    try:
        brief = call_brief(client, brief_prompt(incidents, now))
    except Exception as exc:
        print(f"Brief failed: {osint.public_error(exc, key)}. daily_brief.json was not changed.")
        sys.exit(1)
    brief["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ordered = {
        "generated_at": brief["generated_at"],
        "executive_summary": brief["executive_summary"],
        "regional_sections": brief["regional_sections"],
        "advisory_highlights": brief["advisory_highlights"],
    }
    write_brief(ordered)
    print(f"Wrote {BRIEF.name}")


if __name__ == "__main__":
    main()
