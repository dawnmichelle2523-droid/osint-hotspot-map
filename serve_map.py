#!/usr/bin/env python3
"""Serve the static map plus the live summary API on port 8000.

POST /api/summary makes one xAI call (grok-3-mini, timeout 25, no live search).
GET /api/data returns the live hotspots, six region counts, and the cached summary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fetch_x_osint as osint

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "summary_cache.json"
MODEL = "grok-3-mini"
TIMEOUT = 25
MAX_OUTPUT_TOKENS = 800
HOST = "0.0.0.0"
PORT = 8000
REGIONS = (
    "Gulf",
    "Saudi & Yemen",
    "Levant",
    "Iran & Iraq",
    "Maritime",
    "Other / International",
)

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
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
    "required": ["summary", "regional_notes"],
}


def region_name(value: object) -> str:
    text = " ".join(str(value or "").split())
    if text in REGIONS:
        return text
    return "Other / International"


def load_features() -> list[dict]:
    try:
        features = [item for item in osint.read_collection(osint.HOTSPOTS) if isinstance(item, dict)]
    except RuntimeError as exc:
        print(f"{exc}. Using no live events.")
        features = []
    if features or not osint.ARCHIVE.exists() or osint.ARCHIVE.stat().st_size == 0:
        return features
    try:
        archived = [item for item in osint.read_collection(osint.ARCHIVE) if isinstance(item, dict)]
    except RuntimeError as exc:
        print(f"{exc}. Archive was not used.")
        return features
    return archived


def event_row(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    if not isinstance(props, dict):
        return None
    moment = osint.parse_timestamp(props.get("timestamp"))
    title = osint.one_sentence(props.get("title") or props.get("summary"))
    if not title and moment is None:
        return None
    return {
        "title": title[:240],
        "summary": osint.one_sentence(props.get("summary"))[:500],
        "region": region_name(props.get("region")),
        "category": osint.one_sentence(props.get("category"))[:80],
        "timestamp": osint.format_timestamp(moment) if moment else "",
        "severity": osint.one_sentence(props.get("severity"))[:20],
        "verification_tier": osint.one_sentence(props.get("verification_tier"))[:80],
        "_ms": moment.timestamp() if moment else 0,
    }


def live_events() -> list[dict]:
    rows = []
    for feature in load_features():
        row = event_row(feature)
        if row:
            rows.append(row)
    rows.sort(key=lambda item: item["_ms"], reverse=True)
    for row in rows:
        row.pop("_ms", None)
    return rows


def region_counts(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in REGIONS}
    for event in events:
        counts[region_name(event.get("region"))] += 1
    return counts


def read_cache() -> dict | None:
    if not CACHE.exists() or CACHE.stat().st_size == 0:
        return None
    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"summary_cache.json was not readable ({exc.__class__.__name__}).")
        return None
    if not isinstance(payload, dict):
        return None
    text = " ".join(str(payload.get("text") or "").split())
    if not text:
        return None
    notes = []
    for item in payload.get("regional_notes") or []:
        if not isinstance(item, dict):
            continue
        region = " ".join(str(item.get("region") or "").split())
        note = " ".join(str(item.get("note") or "").split())
        if region and note:
            notes.append({"region": region, "note": note})
    return {
        "generated_at": " ".join(str(payload.get("generated_at") or "").split()),
        "text": text,
        "regional_notes": notes,
    }


def write_cache(summary: dict) -> None:
    payload = {
        "generated_at": summary["generated_at"],
        "text": summary["text"],
        "regional_notes": summary["regional_notes"],
    }
    temporary = CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(CACHE)


def data_payload() -> dict:
    events = live_events()
    return {
        "events": events,
        "counts_by_region": region_counts(events),
        "summary": read_cache(),
    }


def fallback_summary(counts: dict[str, int]) -> dict:
    parts = [f"{name} {counts[name]}" for name in REGIONS]
    notes = [
        {"region": name, "note": f"{name}: {counts[name]} live incident(s)."}
        for name in REGIONS
    ]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "text": "Analysis is unavailable. Live incident counts: " + ", ".join(parts) + ".",
        "regional_notes": notes,
        "fallback": True,
    }


def summary_prompt(events: list[dict], counts: dict[str, int], now: datetime) -> str:
    sample = events[:40]
    return (
        "Write a concise real-time geopolitical situation summary in English. "
        "Use only the live incidents and counts below. Do not use live search and do not add events "
        "that are not in this list.\n"
        f"As of {osint.format_timestamp(now)} UTC.\n"
        "Cover Gulf, Saudi & Yemen, Levant, Iran & Iraq, Maritime, and Other / International.\n"
        "summary is 2-4 sentences for the whole theater.\n"
        "regional_notes is one short sentence per region whose count is above zero. "
        "Use the region name exactly. Omit regions with a count of 0.\n\n"
        + json.dumps({"counts_by_region": counts, "events": sample}, ensure_ascii=False)
    )


def call_summary(client, prompt: str, now: datetime) -> dict:
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "geopolitical_summary",
                "schema": SUMMARY_SCHEMA,
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
    summary = " ".join(str(payload.get("summary") or "").split())
    if not summary:
        raise RuntimeError("xAI response had no summary")
    notes = []
    for item in payload.get("regional_notes") or []:
        if not isinstance(item, dict):
            continue
        region = " ".join(str(item.get("region") or "").split())
        note = " ".join(str(item.get("note") or "").split())
        if region and note:
            notes.append({"region": region, "note": note})
    return {
        "generated_at": osint.format_timestamp(now),
        "text": summary,
        "regional_notes": notes,
        "fallback": False,
    }


def refresh_summary() -> dict:
    events = live_events()
    counts = region_counts(events)
    key = osint.read_api_key()
    if not key:
        print("XAI_API_KEY is missing. Returned a fallback summary. daily_brief.json and hotspots.json were not changed.")
        return fallback_summary(counts)
    try:
        summary = call_summary(
            osint.make_client(key),
            summary_prompt(events, counts, datetime.now(timezone.utc)),
            datetime.now(timezone.utc),
        )
    except Exception as exc:
        print(
            f"Summary failed: {osint.public_error(exc, key)}. "
            "Returned a fallback summary. daily_brief.json and hotspots.json were not changed."
        )
        return fallback_summary(counts)
    write_cache(summary)
    print(f"Wrote {CACHE.name}")
    return summary


class MapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _route(self) -> str:
        return self.path.split("?", 1)[0]

    def _blocked(self) -> bool:
        route = self._route()
        return route == "/.env" or route.endswith("/.env")

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self._blocked():
            self.send_error(404)
            return
        if self._route() == "/api/data":
            try:
                self._json(data_payload())
            except Exception as exc:
                print(f"GET /api/data failed: {exc.__class__.__name__}")
                self._json({"events": [], "counts_by_region": {name: 0 for name in REGIONS}, "summary": read_cache()})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self._route() != "/api/summary":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1_000_000))
        try:
            summary = refresh_summary()
        except Exception as exc:
            print(f"POST /api/summary failed: {exc.__class__.__name__}. Returned a fallback summary.")
            summary = fallback_summary({name: 0 for name in REGIONS})
        self._json({"summary": summary, "fallback": bool(summary.get("fallback"))})

    def log_message(self, fmt: str, *args) -> None:
        super().log_message(fmt, *args)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MapHandler)
    print(f"Serving map and summary API on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
