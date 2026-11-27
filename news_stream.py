"""Recent public news wires for the map panel.

Each feed has its own short timeout. A failed feed keeps its last items and
does not block the map. Headlines are spam-filtered, translated, and geolocated
before they are cached.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

import feed_quality

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "news_stream_cache.json"
REFRESH_SECONDS = 10 * 60
_lock = threading.Lock()
_refreshing = False
_started = False
_memory = {"at": 0.0, "items": []}

FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "Global"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "Middle East"),
    ("DW World", "https://rss.dw.com/xml/rss-en-world", "Global"),
    ("France 24", "https://www.france24.com/en/rss", "Global"),
    ("ReliefWeb", "https://reliefweb.int/updates/rss.xml", "Global"),
    ("Amu TV", "https://amu.tv/feed/", "Afghanistan"),
    ("Afghanistan International", "https://afintl.com/feed", "Afghanistan"),
    ("InSight Crime", "https://insightcrime.org/feed/", "South America"),
    ("CBC World", "https://www.cbc.ca/webfeed/rss/rss-world", "Canada"),
    ("ABC News", "https://abcnews.go.com/abcnews/internationalheadlines", "United States"),
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(entry) -> str:
    for key in ("published", "updated"):
        raw = entry.get(key) or ""
        if not raw:
            continue
        try:
            moment = parsedate_to_datetime(raw)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, IndexError):
            text = " ".join(str(raw).split())
            if text:
                return text[:40]
    return _now()


def _fetch_feed(name: str, url: str, region: str) -> list[dict]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "GeoRiskGlobal/1.0", "Accept": "application/rss+xml, application/xml, */*"},
            timeout=(4, 8),
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception:
        return []
    items = []
    for entry in list(getattr(parsed, "entries", []) or [])[:12]:
        title = " ".join(str(entry.get("title") or "").split())
        summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary") or entry.get("description") or ""))
        summary = " ".join(summary.split())[:500]
        link = str(entry.get("link") or url)
        enriched = feed_quality.enrich_event({
            "title": title,
            "summary": summary[:500],
            "source": link,
            "source_name": name,
            "timestamp": _stamp(entry),
            "region": region,
            "id": link or title,
        }, live_translation=True, ingest=True)
        if enriched is None:
            continue
        enriched["region"] = enriched.get("region") if enriched.get("region") not in ("", "Global") else region
        if enriched.get("country"):
            enriched["region"] = feed_quality.theatre_for(enriched.get("country")) or region
        items.append(enriched)
    return items


def refresh(force: bool = False) -> None:
    global _refreshing
    with _lock:
        if not force and _memory.get("items") and time.time() - float(_memory.get("at") or 0) < REFRESH_SECONDS:
            return
        if _refreshing:
            return
        _refreshing = True
        previous = list(_memory.get("items") or [])
    found = []
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_feed, name, url, region): name for name, url, region in FEEDS}
            for future in as_completed(futures):
                try:
                    found.extend(future.result() or [])
                except Exception:
                    continue
        if not found:
            found = previous
        found = feed_quality.dedupe(found)
        found.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        found = found[:60]
    finally:
        with _lock:
            _memory["items"] = found
            _memory["at"] = time.time()
            _refreshing = False
        _write(found)


def _write(items: list[dict]) -> None:
    try:
        temporary = CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"fetched_at": _now(), "items": items}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    except OSError:
        pass


def _load() -> None:
    if _memory.get("items"):
        return
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        _memory["items"] = payload["items"]
        _memory["at"] = time.time()


def kick() -> None:
    with _lock:
        _load()
        stale = not _memory.get("items") or time.time() - float(_memory.get("at") or 0) >= REFRESH_SECONDS
        busy = _refreshing
    if stale and not busy:
        threading.Thread(target=refresh, name="news-stream", daemon=True).start()


def start() -> None:
    global _started
    with _lock:
        _load()
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="news-stream-loop", daemon=True).start()


def _loop() -> None:
    while True:
        try:
            refresh(force=True)
        except Exception:
            pass
        time.sleep(REFRESH_SECONDS)


def payload(region: str = "") -> dict:
    kick()
    with _lock:
        _load()
        items = list(_memory.get("items") or [])
    wanted = " ".join(str(region or "").split())
    if wanted and wanted.lower() not in {"all", "global"}:
        items = [item for item in items if str(item.get("region") or "") == wanted or str(item.get("country") or "") == wanted]
    public = []
    for item in items[:40]:
        title = item.get("title") or ""
        summary = item.get("summary") or ""
        try:
            title = feed_quality.translate_text(title, live=True, ingest=True) or title
            summary = feed_quality.translate_text(summary, live=True, ingest=True) if summary else ""
        except Exception:
            pass
        public.append({
            "title": title,
            "summary": summary,
            "source_name": item.get("source_name") or "",
            "source": item.get("source") or "",
            "timestamp": item.get("timestamp") or "",
            "country": item.get("country") or "",
            "area": item.get("area") or "",
            "region": item.get("region") or "",
            "lat": item.get("lat"),
            "lng": item.get("lng"),
            "original_text": item.get("original_text") or "",
        })
    return {"ok": True, "items": public, "fetched_at": _now()}
