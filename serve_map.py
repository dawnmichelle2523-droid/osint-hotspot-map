#!/usr/bin/env python3
"""Serve the static map, the summary API, and live RSS/Telegram ingestion on port 8000.

POST /api/summary makes one xAI call (grok-3-mini, timeout 25, no live search).
GET /api/data returns live events, six region counts, the cached summary, and last_polled_at.
POST /api/poll-now fetches RSS and public Telegram once.
The poll never writes hotspots.json or daily_brief.json.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from difflib import SequenceMatcher
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

import fetch_x_osint as osint

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "summary_cache.json"
FEEDS_DB = ROOT / "feeds.sqlite"
POLL_SECONDS = 300
POLL_LIMIT = 5
LIVE_WINDOW = timedelta(hours=72)
SIMILAR_TITLE_LIMIT = 40
SIMILAR_TITLE_RATIO = 0.65
FETCH_TIMEOUT = 8
FIRMS_TIMEOUT = 10
FIRMS_CACHE_SECONDS = 15 * 60
FIRMS_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}"
    "/VIIRS_NOAA21_NRT/25.0,12.0,63.0,38.0/1"
)
FIRMS_PLACEHOLDERS = {
    "your_32_character_nasa_map_key_here",
    "your_nasa_firms_map_key_here",
}
USER_AGENT = "osint_tracker"
_thermal_lock = threading.Lock()
_thermal_cache: dict = {"at": 0.0, "points": None}
# Configurable public feeds. Google News RSS stands in for Reuters and AP.
RSS_FEEDS = [
    ("Al Jazeera Middle East", "https://www.aljazeera.com/xml/rss/middleeast.xml"),
    ("BBC Middle East", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
    ("Reuters Middle East & Syria", "https://news.google.com/rss/search?q=site:reuters.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en"),
    ("AP Middle East & Syria", "https://news.google.com/rss/search?q=site:apnews.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en"),
    ("Enab Baladi", "https://english.enabbaladi.net/feed/"),
    ("Syria Direct", "https://syriadirect.org/feed/"),
]
# Configurable public OSINT channels, read from the t.me/s preview page.
TELEGRAM_CHANNELS = [
    "conflictarchive",
    "Suriyak_maps",
    "Liveuamap",
    "hawran_osint",
    "npasyria",
    "SANANewsEnglish",
    "televisionsyria",
    "AlekhbariahSY",
    "SyrianArabNewsAgency",
    "sn4hr",
    "D24net",
    "SweidaGov1",
    "syrianinfowar",
    "OsintTv",
    "syriahr",
    "EnabBaladi",
    "OSINTdefender",
    "osint613",
    "OsintUpdates",
    "warmonitors",
    "gulfnewsUAE",
    "AlArabiya_Brk",
    "almasirah",
    "almanarnews",
    "SabereenNews22",
    "IranIntl_En",
    "rudaw_english",
]
X_HANDLES = [
    "syr_television",
    "SyriacOSINT",
    "QalaatAlMudiq",
    "Charles_Lister",
    "GregoryPWaters",
    "OSINTWarfare",
    "Osinttechnical",
    "AfriMEOSINT",
]
# Tried in order for each handle. {handle} is replaced. A timeout or rate limit uses the next one.
X_FEED_URLS = [
    "https://nitter.poast.org/{handle}/rss",
    "https://nitter.privacydev.net/{handle}/rss",
    "https://nitter.woodland.cafe/{handle}/rss",
    "https://xcancel.com/{handle}/rss",
    "https://rss-bridge.org/bridge01/?action=display&bridge=TwitterBridge&context=By+username&u={handle}&format=Atom",
]
X_RATE_LIMITS = {401, 403, 429, 503}
_start_lock = threading.Lock()
_poll_lock = threading.Lock()
_db_lock = threading.Lock()
_poll_started = False
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
        "category": osint.live_category(props.get("category")),
        "timestamp": osint.format_timestamp(moment) if moment else "",
        "severity": osint.live_severity(props.get("severity")),
        "governorate": osint.match_governorate(props.get("governorate"))
        or osint.match_governorate(props.get("location_name")),
        "verification_tier": osint.one_sentence(props.get("verification_tier"))[:80],
        "_ms": moment.timestamp() if moment else 0,
    }


def is_point(feature: dict) -> bool:
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates")
    return geometry.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2


def live_events_from(features: list[dict]) -> list[dict]:
    rows = []
    for feature in features:
        row = event_row(feature)
        if row:
            rows.append(row)
    rows.sort(key=lambda item: item["_ms"], reverse=True)
    for row in rows:
        row.pop("_ms", None)
    return rows


def live_events() -> list[dict]:
    return live_events_from(combined_features())


def region_counts(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in REGIONS}
    for event in events:
        counts[region_name(event.get("region"))] += 1
    return counts


def governorate_counts(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in osint.SYRIA_GOVERNORATES}
    for event in events:
        name = event.get("governorate") or ""
        if name in counts:
            counts[name] += 1
    return counts


def within_live_window(feature: dict, now: datetime | None = None) -> bool:
    moment = osint.parse_timestamp((feature.get("properties") or {}).get("timestamp"))
    if moment is None:
        return False
    cutoff = (now or datetime.now(timezone.utc)) - LIVE_WINDOW
    return moment >= cutoff


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


def ensure_feed_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            headline_hash TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT '',
            region TEXT,
            governorate TEXT,
            location_name TEXT,
            lat REAL,
            lng REAL,
            severity TEXT,
            category TEXT,
            published_at TEXT,
            ingested_at TEXT NOT NULL
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    additions = {
        "source_name": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT ''",
        "region": "TEXT",
        "governorate": "TEXT",
        "location_name": "TEXT",
        "lat": "REAL",
        "lng": "REAL",
        "severity": "TEXT",
        "category": "TEXT",
        "published_at": "TEXT",
        "ingested_at": "TEXT",
        "headline_hash": "TEXT",
        "url_hash": "TEXT",
        "source_url": "TEXT",
        "title": "TEXT",
        "summary": "TEXT",
    }
    for name, typedef in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {typedef}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_headline_hash ON items(headline_hash)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url_hash ON items(url_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(FEEDS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    ensure_feed_db(conn)
    return conn


def normalize_title(title: str) -> str:
    text = str(title or "").lower()
    text = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


def headline_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return hashlib.sha256(" ".join(str(url or "").strip().lower().split()).encode("utf-8")).hexdigest()


def recent_titles(conn: sqlite3.Connection, limit: int = SIMILAR_TITLE_LIMIT) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM items WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [str(row["title"]) for row in rows]


def similar_to_recent(title: str, titles: list[str]) -> bool:
    left = " ".join(str(title or "").split())
    if not left:
        return False
    folded = left.casefold()
    for other in titles:
        right = " ".join(str(other or "").split())
        if not right:
            continue
        if SequenceMatcher(None, folded, right.casefold()).ratio() > SIMILAR_TITLE_RATIO:
            return True
    return False


def titles_near(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 24 and len(right) >= 24 and (left in right or right in left):
        return True
    left_words, right_words = set(left.split()), set(right.split())
    if len(left_words) < 4 or len(right_words) < 4:
        return False
    return len(left_words & right_words) / len(left_words | right_words) >= 0.82


def source_label(kind: str, name: str) -> str:
    clean = str(name or "").strip()
    if " · " in clean:
        return clean
    if kind == "telegram":
        return f"Telegram · @{clean.lstrip('@')}"
    if kind == "x":
        return f"X · @{clean.lstrip('@')}"
    return f"RSS · {clean or 'feed'}"


def parse_when(value: object) -> datetime | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    parsed = osint.parse_timestamp(text)
    if parsed:
        return parsed
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def plain_text(value: object) -> str:
    raw = str(value or "")
    if "<" in raw and ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return osint.one_sentence(raw)


def seen_item(conn: sqlite3.Connection, source_url: str, digest: str, title: str = "") -> bool:
    digest_url = url_hash(source_url)
    row = conn.execute(
        "SELECT 1 FROM items WHERE source_url = ? OR headline_hash = ? OR url_hash = ? LIMIT 1",
        (source_url, digest, digest_url),
    ).fetchone()
    if row is not None:
        return True
    normalized = normalize_title(title)
    if not normalized:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        "SELECT title FROM items WHERE COALESCE(published_at, ingested_at, '') >= ?",
        (cutoff,),
    ).fetchall()
    return any(titles_near(normalized, normalize_title(item["title"])) for item in recent)


def read_last_polled(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM feed_meta WHERE key = 'last_polled_at'").fetchone()
    if not row:
        return None
    text = " ".join(str(row["value"] or "").split())
    return text or None


def write_last_polled(conn: sqlite3.Connection, stamp: str) -> None:
    conn.execute(
        "INSERT INTO feed_meta (key, value) VALUES ('last_polled_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (stamp,),
    )


def feed_features() -> list[dict]:
    features = []
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM items WHERE lat IS NOT NULL AND lng IS NOT NULL ORDER BY published_at DESC"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        lng = row["lng"]
        lat = row["lat"]
        if not osint.valid_coord(lng, lat):
            continue
        props = {
            "severity": osint.live_severity(row["severity"]),
            "title": row["title"],
            "summary": row["summary"] or row["title"],
            "timestamp": row["published_at"] or row["ingested_at"],
            "source": row["source_url"],
            "account": source_label(row["source_kind"], row["source_name"]),
            "handle": source_label(row["source_kind"], row["source_name"]),
            "source_handle": source_label(row["source_kind"], row["source_name"]),
            "source_label": source_label(row["source_kind"], row["source_name"]),
            "batch": "live_feed",
            "verification_tier": "Unverified Lead" if row["source_kind"] == "telegram" else "State-Affiliated / Single-Source",
            "region": region_name(row["region"]),
            "category": osint.live_category(row["category"]),
            "warning_number": None,
            "location_name": row["location_name"] or "",
        }
        if row["governorate"]:
            props["governorate"] = row["governorate"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": props,
            }
        )
    return features


def combined_features() -> list[dict]:
    features = []
    seen: set[str] = set()
    for feature in load_features() + feed_features():
        if not is_point(feature):
            continue
        source = str((feature.get("properties") or {}).get("source") or "")
        if source and source in seen:
            continue
        if source:
            seen.add(source)
        features.append(feature)
    features.sort(
        key=lambda feature: osint.parse_timestamp((feature.get("properties") or {}).get("timestamp"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return features


def fetch_rss_feed(name: str, url: str) -> list[dict]:
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        return []
    found = []
    for entry in (parsed.entries or [])[:20]:
        title = plain_text(getattr(entry, "title", ""))
        link = plain_text(getattr(entry, "link", ""))
        if not title or not link.startswith("http"):
            continue
        summary = plain_text(getattr(entry, "summary", "") or getattr(entry, "description", "") or title)
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")
        found.append(
            {
                "title": title[:240],
                "summary": (summary or title)[:500],
                "source_url": link,
                "source_name": source_label("rss", name),
                "source_kind": "rss",
                "published_at": published,
            }
        )
    return found


def fetch_rss_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=len(RSS_FEEDS) or 1) as pool:
        futures = [pool.submit(fetch_rss_feed, name, url) for name, url in RSS_FEEDS]
        for future in futures:
            found.extend(future.result())
    return found


def fetch_telegram_channel(channel: str) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"Telegram {channel} failed: {exc.__class__.__name__}.")
        return []
    found = []
    for wrap in soup.select(".tgme_widget_message")[:20]:
        post = " ".join(str(wrap.get("data-post") or "").split())
        node = wrap.select_one(".tgme_widget_message_text")
        if not post or node is None:
            continue
        text = plain_text(node.get_text(" ", strip=True))
        if not text:
            continue
        when = ""
        clock = wrap.select_one("time")
        if clock is not None:
            when = clock.get("datetime") or ""
        found.append(
            {
                "title": text[:180],
                "summary": text[:500],
                "source_url": f"https://t.me/{post}",
                "source_name": source_label("telegram", channel),
                "source_kind": "telegram",
                "published_at": when,
            }
        )
    return found


def mentions_syria_or_middle_east(text: str) -> bool:
    lowered = text.lower()
    latin = (
        "syria", "syrian", "middle east", "middle-east", "aleppo", "damascus", "idlib", "idleb",
        "homs", "hama", "latakia", "tartus", "raqqa", "deir ez-zor", "deir ezzor", "hasakah",
        "daraa", "suwayda", "sweida", "quneitra", "rif dimashq",
    )
    if any(term in lowered for term in latin):
        return True
    return any(term in text for term in ("سوريا", "سورية", "الشرق الأوسط", "الشرق الاوسط", "حلب", "دمشق", "إدلب", "حمص"))


def canonical_x_url(handle: str, link: str, guid: str) -> str:
    match = re.search(r"/status/(\d+)", f"{link} {guid}")
    if match:
        return f"https://x.com/{handle}/status/{match.group(1)}"
    cleaned = plain_text(link)
    return cleaned if cleaned.startswith("http") else ""


def fetch_x_handle(handle: str) -> list[dict]:
    for template in X_FEED_URLS:
        url = template.format(handle=handle)
        host = url.split("/")[2]
        try:
            response = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            print(f"X {handle} via {host} failed: {exc.__class__.__name__}.")
            continue
        if response.status_code in X_RATE_LIMITS:
            print(f"X {handle} via {host} failed: HTTP {response.status_code}.")
            continue
        if response.status_code != 200:
            print(f"X {handle} via {host} failed: HTTP {response.status_code}.")
            continue
        parsed = feedparser.parse(response.content)
        if not parsed.entries:
            print(f"X {handle} via {host} failed: no entries.")
            continue
        found = []
        for entry in parsed.entries[:20]:
            title = plain_text(getattr(entry, "title", ""))
            summary = plain_text(getattr(entry, "summary", "") or getattr(entry, "description", "") or title)
            blob = f"{title} {summary}"
            if not title or not mentions_syria_or_middle_east(blob):
                continue
            source_url = canonical_x_url(handle, getattr(entry, "link", ""), getattr(entry, "id", "") or getattr(entry, "guid", ""))
            if not source_url:
                continue
            published = getattr(entry, "published", "") or getattr(entry, "updated", "")
            found.append(
                {
                    "title": title[:240],
                    "summary": (summary or title)[:500],
                    "source_url": source_url,
                    "source_name": source_label("x", handle),
                    "source_kind": "x",
                    "published_at": published,
                }
            )
        return found
    return []


def fetch_x_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=min(4, len(X_HANDLES) or 1)) as pool:
        futures = [pool.submit(fetch_x_handle, handle) for handle in X_HANDLES]
        for future in futures:
            found.extend(future.result())
    return found


def fetch_telegram_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=len(TELEGRAM_CHANNELS) or 1) as pool:
        futures = [pool.submit(fetch_telegram_channel, channel) for channel in TELEGRAM_CHANNELS]
        for future in futures:
            found.extend(future.result())
    return found


def insert_item(item: dict, placed: dict, now: datetime) -> bool:
    coords = osint.valid_coord(placed.get("lng"), placed.get("lat"))
    source_url = item["source_url"]
    if coords is None:
        print(f"Skipped {source_url}: no coordinates.")
        return False
    moment = parse_when(item.get("published_at")) or now
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO items (
                    source_url, url_hash, headline_hash, title, summary, source_name, source_kind,
                    region, governorate, location_name, lat, lng, severity, category, published_at, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_url,
                    url_hash(source_url),
                    headline_hash(item["title"]),
                    item["title"],
                    item.get("summary") or item["title"],
                    item.get("source_name") or "",
                    item.get("source_kind") or "",
                    placed.get("region") or "Other / International",
                    placed.get("governorate") or "",
                    placed.get("location_name") or "",
                    coords[1],
                    coords[0],
                    osint.live_severity(placed.get("severity")),
                    osint.live_category(placed.get("category")),
                    osint.format_timestamp(moment),
                    osint.format_timestamp(now),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()
    return True


def run_poll() -> dict:
    with _poll_lock:
        now = datetime.now(timezone.utc)
        candidates = fetch_rss_candidates() + fetch_telegram_candidates() + fetch_x_candidates()
        candidates.sort(
            key=lambda item: parse_when(item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        inserted = 0
        for item in candidates:
            if inserted >= POLL_LIMIT:
                break
            digest = headline_hash(item["title"])
            with _db_lock:
                conn = db_connect()
                try:
                    if seen_item(conn, item["source_url"], digest, item["title"]):
                        continue
                finally:
                    conn.close()
            if item.get("source_kind") == "x" and not mentions_syria_or_middle_east(
                f"{item.get('title') or ''} {item.get('summary') or ''}"
            ):
                continue
            with _db_lock:
                conn = db_connect()
                try:
                    titles = recent_titles(conn)
                finally:
                    conn.close()
            if similar_to_recent(item["title"], titles):
                print(f"Skipped near-duplicate before classify: {item['title'][:80]}")
                continue
            placed = osint.locate_event(item["title"], location_name=item.get("summary") or item["title"])
            english = osint.one_sentence(placed.get("title") or "")
            if english:
                item = dict(item)
                item["title"] = english[:240]
            if insert_item(item, placed, now):
                inserted += 1
        stamp = osint.format_timestamp(now)
        with _db_lock:
            conn = db_connect()
            try:
                write_last_polled(conn, stamp)
                conn.commit()
            finally:
                conn.close()
        print(f"Feed poll stored {inserted} new item(s). last_polled_at {stamp}. hotspots.json was not changed.")
        return {"ok": True, "inserted": inserted, "last_polled_at": stamp}


def poll_loop() -> None:
    while True:
        try:
            run_poll()
        except Exception as exc:
            print(f"Feed poll failed: {exc.__class__.__name__}.")
        time.sleep(POLL_SECONDS)


def start_poller() -> None:
    global _poll_started
    with _start_lock:
        if _poll_started:
            return
        _poll_started = True
    threading.Thread(target=poll_loop, name="feed-poller", daemon=True).start()


def firms_key() -> str:
    osint.load_dotenv(osint.ROOT / ".env")
    return (os.environ.get("FIRMS_MAP_KEY") or "").strip()


def firms_unconfigured(key: str) -> bool:
    cleaned = key.strip()
    if not cleaned or cleaned in FIRMS_PLACEHOLDERS:
        return True
    return cleaned.lower().startswith("your_")


def parse_firms_csv(text: str) -> list[dict]:
    points = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue

        def number(name: str) -> float | None:
            raw = " ".join(str(row.get(name) or "").split())
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        points.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "bright_ti4": number("bright_ti4"),
                "frp": number("frp"),
                "acq_date": " ".join(str(row.get("acq_date") or "").split()),
                "acq_time": " ".join(str(row.get("acq_time") or "").split()),
                "confidence": " ".join(str(row.get("confidence") or "").split()),
            }
        )
    return points


def thermal_payload() -> dict:
    key = firms_key()
    if firms_unconfigured(key):
        return {
            "ok": False,
            "configured": False,
            "status": (
                "NASA FIRMS is not configured. Add a real FIRMS_MAP_KEY to .env. "
                "Satellite heat was not requested."
            ),
            "points": [],
        }
    now = time.monotonic()
    with _thermal_lock:
        cached = _thermal_cache.get("points")
        cached_at = float(_thermal_cache.get("at") or 0)
    if isinstance(cached, list) and now - cached_at < FIRMS_CACHE_SECONDS:
        return {"ok": True, "configured": True, "status": "cached", "points": cached}
    try:
        response = requests.get(
            FIRMS_URL.format(key=key),
            timeout=FIRMS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        points = parse_firms_csv(response.text)
    except Exception as exc:
        print(f"FIRMS fetch failed: {exc.__class__.__name__}.")
        return {
            "ok": False,
            "configured": True,
            "status": "NASA FIRMS could not be reached.",
            "points": [],
        }
    with _thermal_lock:
        _thermal_cache["at"] = time.monotonic()
        _thermal_cache["points"] = points
    return {"ok": True, "configured": True, "status": "ok", "points": points}


def data_payload() -> dict:
    now = datetime.now(timezone.utc)
    features = [feature for feature in combined_features() if within_live_window(feature, now)]
    events = live_events_from(features)
    with _db_lock:
        conn = db_connect()
        try:
            last_polled_at = read_last_polled(conn)
        finally:
            conn.close()
    return {
        "events": events,
        "features": features,
        "counts_by_region": region_counts(events),
        "counts_by_governorate": governorate_counts(events),
        "summary": read_cache(),
        "last_polled_at": last_polled_at,
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
    now = datetime.now(timezone.utc)
    events = live_events_from(
        [feature for feature in combined_features() if within_live_window(feature, now)]
    )
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
        name = route.rsplit("/", 1)[-1]
        return name == ".env" or name == "feeds.sqlite" or name.endswith(".sqlite")

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
        if self._route() == "/api/thermal":
            try:
                self._json(thermal_payload())
            except Exception as exc:
                print(f"GET /api/thermal failed: {exc.__class__.__name__}.")
                self._json({
                    "ok": False,
                    "configured": False,
                    "status": "Satellite heat is unavailable.",
                    "points": [],
                })
            return
        if self._route() == "/api/data":
            try:
                self._json(data_payload())
            except Exception as exc:
                print(f"GET /api/data failed: {exc.__class__.__name__}")
                self._json({
                    "events": [],
                    "features": [],
                    "counts_by_region": {name: 0 for name in REGIONS},
                    "counts_by_governorate": {name: 0 for name in osint.SYRIA_GOVERNORATES},
                    "summary": read_cache(),
                    "last_polled_at": None,
                })
            return
        super().do_GET()

    def do_POST(self) -> None:
        route = self._route()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1_000_000))
        if route == "/api/poll-now":
            try:
                self._json(run_poll())
            except Exception as exc:
                print(f"POST /api/poll-now failed: {exc.__class__.__name__}.")
                self._json({"ok": False, "inserted": 0, "last_polled_at": None})
            return
        if route != "/api/summary":
            self.send_error(404)
            return
        try:
            summary = refresh_summary()
        except Exception as exc:
            print(f"POST /api/summary failed: {exc.__class__.__name__}. Returned a fallback summary.")
            summary = fallback_summary({name: 0 for name in REGIONS})
        self._json({"summary": summary, "fallback": bool(summary.get("fallback"))})

    def log_message(self, fmt: str, *args) -> None:
        super().log_message(fmt, *args)


def main() -> None:
    with _db_lock:
        conn = db_connect()
        conn.close()
    start_poller()
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
