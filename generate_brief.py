#!/usr/bin/env python3
# One Responses API call, no live search (no x_search or web_search tools).
# Model grok-4-fast-reasoning, timeout 25.
# https://docs.x.ai/developers/model-capabilities/text/structured-outputs

"""Write the completed Europe/London 07:00 geopolitical summary.

The scheduler in serve_map calls generate_geopolitical_summary. This command uses that same path.
"""

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

SECTION_TITLES = (
    "Gulf & Maritime Security",
    "Yemen & Red Sea",
    "Levant & Regional Politics",
)
ADVISORY_TITLE = "Official Travel & Embassy Advisories"
SECTION_ALIASES = {
    "gulf": "Gulf & Maritime Security",
    "maritime": "Gulf & Maritime Security",
    "gulf & maritime": "Gulf & Maritime Security",
    "gulf & maritime security": "Gulf & Maritime Security",
    "saudi & yemen": "Yemen & Red Sea",
    "yemen": "Yemen & Red Sea",
    "red sea": "Yemen & Red Sea",
    "yemen & red sea": "Yemen & Red Sea",
    "levant": "Levant & Regional Politics",
    "levant & regional politics": "Levant & Regional Politics",
}

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
        "executive_summary is the Executive Summary: 2-4 sentences of plain English.\n"
        "regional_sections uses these region titles, in this order: "
        + "; ".join(SECTION_TITLES)
        + ". Omit a title only when that section has no incident.\n"
        "summary is concise bullets separated by newlines. "
        "Each bullet starts with exactly one of: Verified Official, Corroborated, Attributed, then an em dash. "
        "Map Official / Primary to Verified Official, a claim backed by more than one listed source to Corroborated, "
        "and State-Affiliated / Single-Source to Attributed.\n"
        f"advisory_highlights is the {ADVISORY_TITLE} section: "
        "the same tagged bullets for travel and embassy advisories. Use an empty array when there are none.\n\n"
        f"{body}"
    )


def brief_lines(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def canonical_section(name: str) -> str:
    compact = " ".join(name.split())
    return SECTION_ALIASES.get(compact.lower(), compact)


def normalize_brief(payload: dict) -> dict:
    summary = brief_lines(payload.get("executive_summary")).replace("\n", " ")
    if not summary:
        raise ValueError("brief had no executive summary")
    merged: dict[str, list[str]] = {}
    extras: list[str] = []
    for item in payload.get("regional_sections") or []:
        if not isinstance(item, dict):
            continue
        region = canonical_section(str(item.get("region") or ""))
        text = brief_lines(item.get("summary"))
        if not region or not text:
            continue
        bucket = merged.setdefault(region, [])
        for line in text.split("\n"):
            if line not in bucket:
                bucket.append(line)
        if region not in SECTION_TITLES and region not in extras:
            extras.append(region)
    order = [title for title in SECTION_TITLES if title in merged] + extras
    sections = [{"region": title, "summary": "\n".join(merged[title])} for title in order]
    highlights = []
    for item in payload.get("advisory_highlights") or []:
        text = brief_lines(item)
        if text and text not in highlights:
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
    import serve_map

    record = serve_map.generate_geopolitical_summary("completed", force=True)
    print(
        f"{record.get('window_label') or 'completed window'}: "
        f"{record.get('source_count', 0)} stored record(s)."
    )
    if record.get("fallback"):
        print("Used the stored-event summary. daily_brief.json was not changed.")
        return
    print(f"Wrote {BRIEF.name}")


if __name__ == "__main__":
    main()
