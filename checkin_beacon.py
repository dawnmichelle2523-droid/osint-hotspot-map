"""2-tap mobile check-in and panic beacon.

Ops rooms issue an opaque callsign (TEAM-ALPHA-01, LEADER-PARIS-BUS2). Field
staff open /c/<token> and tap safe, assistance, or a confirmed panic. The
database stores that callsign, a random token, sector, cadence, and optional
coarse coordinates. It does not store names, emails, phone numbers, or pupil
data.

Schools and universities: links are for adult trip leaders and staff only.
No xAI calls. Slack alerts are optional incoming webhooks, sent off the
request thread.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from flask import Blueprint, Response, redirect, request

import checkins

checkin_bp = Blueprint("checkin", __name__)

SCHOOLS_NOTICE = (
    "For Schools & Universities, check-ins are strictly for adult Trip Leaders / Staff only"
    "\u2014never issue check-in links to pupils/minors."
)
ZERO_PII_NOTICE = (
    "Zero-PII: only opaque callsigns such as TEAM-ALPHA-01 or LEADER-PARIS-BUS2 are stored. "
    "Never enter names, email addresses, phone numbers, or pupil data."
)
ADULT_ONLY_NOTICE = (
    "Check-ins are for adult staff, crew, and trip leaders only. "
    "Never issue a check-in link to a pupil or minor."
)

SECTORS = ("media", "ngo", "psc", "corporate", "government", "schools", "other")
SECTOR_LABELS = {
    "media": "Media & news desk",
    "ngo": "NGO",
    "psc": "PSC / close protection",
    "corporate": "Corporate",
    "government": "Government / public sector",
    "schools": "Schools & universities",
    "other": "Other",
}
CADENCES = (60, 120, 240, 480, 720, 1440)
STATUSES = ("OK", "OVERDUE", "ASSISTANCE", "PANIC", "PAUSED")
TAP_EVENTS = ("SAFE", "ASSISTANCE", "PANIC")
STATUS_FOR_EVENT = {"SAFE": "OK", "ASSISTANCE": "ASSISTANCE", "PANIC": "PANIC"}
TAP_MESSAGES = {
    "SAFE": "Safe check-in recorded. The ops room can see that you are on time.",
    "ASSISTANCE": "Assistance request sent to the ops room.",
    "PANIC": "Panic beacon sent to the ops room. Stay on this page if you can.",
}
BANNED_SEGMENTS = {
    "PUPIL", "PUPILS", "STUDENT", "STUDENTS", "CHILD", "CHILDREN",
    "MINOR", "MINORS", "KID", "KIDS", "INFANT", "INFANTS",
}
BADGE_CLASS = {
    "On Time": "on-time",
    "Overdue": "overdue",
    "PANIC": "panic",
    "Assistance": "assistance",
    "Paused": "paused",
}
MASKED_WEBHOOK = "hooks.slack.com/services/\u2022\u2022\u2022\u2022"
RECENT_LIMIT = 40

_CALLSIGN_RE = re.compile(r"^[A-Z][A-Z0-9]{0,14}(?:-[A-Z0-9]{1,16}){1,5}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_WEBHOOK_RE = re.compile(
    r"^https://hooks\.slack(?:-gov)?\.com/services/[A-Z0-9]{8,32}/[A-Z0-9]{8,32}/[A-Za-z0-9]{16,64}$"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")

_store: "CheckinStore | None" = None
_store_lock = threading.Lock()
_sweep_lock = threading.Lock()
_sweep_started = False


class CheckinError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class TapResult:
    def __init__(self, event_type: str, webhook: str, note: str, region: str, body: dict) -> None:
        self.event_type = event_type
        self.webhook = webhook
        self.note = note
        self.region = region
        self.body = body

    def __repr__(self) -> str:
        return f"TapResult({self.event_type})"


class OverdueAlert:
    def __init__(self, webhook: str, callsign: str, sector_label: str, cadence_minutes: int, region: str, when: str) -> None:
        self.webhook = webhook
        self.callsign = callsign
        self.sector_label = sector_label
        self.cadence_minutes = cadence_minutes
        self.region = region
        self.when = when

    def __repr__(self) -> str:
        return f"OverdueAlert({self.callsign})"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def seconds_until(due: str, now: datetime) -> int | None:
    if not due:
        return None
    return int((parse_utc(due) - now).total_seconds())


def format_remaining(seconds: int | None) -> str:
    if seconds is None:
        return "No window scheduled"
    overdue = seconds < 0
    remaining = abs(int(seconds))
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    prefix = "Overdue by " if overdue else "Due in "
    return prefix + " ".join(parts)


def cadence_label(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    if rest or hours == 0:
        return f"{minutes} minutes"
    if hours == 1:
        return "1 hour"
    return f"{hours} hours"


def badge_for(status: str, seconds: int | None) -> str:
    if status == "PAUSED":
        return "Paused"
    if status == "PANIC":
        return "PANIC"
    if status == "ASSISTANCE":
        return "Assistance"
    if status == "OVERDUE" or (seconds is not None and seconds < 0):
        return "Overdue"
    return "On Time"


def valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


def database_path(mount: Path | None = None) -> Path:
    """CHECKIN_DB_PATH, else /data/checkins.db when that folder is writable, else a writable checkins.db."""
    from analytics_store import prefer_writable_file

    override = (os.environ.get("CHECKIN_DB_PATH") or "").strip()
    if override:
        path = Path(override).expanduser()
        if path.suffix.lower() != ".db":
            path = path / "checkins.db"
        return prefer_writable_file(path)
    data = Path("/data") if mount is None else Path(mount)
    try:
        writable = data.is_dir() and os.access(data, os.W_OK)
    except OSError:
        writable = False
    if writable:
        return data / "checkins.db"
    return prefer_writable_file(Path("checkins.db"))


def mask_webhook(url: str) -> str:
    if not (url or "").strip():
        return ""
    return MASKED_WEBHOOK


def webhook_allowed(url: str) -> bool:
    return bool(_WEBHOOK_RE.match((url or "").strip()))


def normalize_callsign(value: object) -> str:
    text = " ".join(str(value or "").split()).upper()
    if not text:
        raise CheckinError("Enter an opaque callsign such as CREW-BEIRUT-1.")
    if "@" in text or _EMAIL_RE.search(text):
        raise CheckinError("Callsigns cannot be email addresses. Use an opaque ID such as TEAM-ALPHA-01.")
    if any(ch.isspace() for ch in str(value or "")):
        raise CheckinError("Callsigns cannot contain spaces or a person's name. Use CREW-BEIRUT-1 or LEADER-PARIS-BUS2.")
    if not _CALLSIGN_RE.match(text) or len(text) > 48:
        raise CheckinError("Use an opaque callsign such as CREW-BEIRUT-1, TEAM-ALPHA-01, or LEADER-PARIS-BUS2.")
    if not any(ch.isdigit() for ch in text):
        raise CheckinError("Add a number so the callsign is an ID, not a name. Example: LEADER-PARIS-BUS2.")
    if re.search(r"\d{7,}", text):
        raise CheckinError("That callsign looks like a phone number. Use a short opaque ID such as CREW-BEIRUT-1.")
    banned = BANNED_SEGMENTS.intersection(text.split("-"))
    if banned:
        raise CheckinError(SCHOOLS_NOTICE)
    return text


def normalize_note(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) > 140:
        raise CheckinError("Note must be one line of at most 140 characters, with no personal data.")
    if "@" in text or _EMAIL_RE.search(text):
        raise CheckinError("Notes cannot include email addresses.")
    if len(re.sub(r"\D", "", text)) >= 7 or re.search(r"\d{7,}", text):
        raise CheckinError("Notes cannot include phone numbers.")
    return text


def normalize_sector(value: object) -> str:
    sector = str(value or "").strip().lower()
    if sector not in SECTORS:
        raise CheckinError("Choose a sector.")
    return sector


def normalize_cadence(value: object) -> int:
    if isinstance(value, bool):
        raise CheckinError("Choose a check-in cadence.")
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise CheckinError("Choose a check-in cadence.") from None
    if minutes not in CADENCES:
        raise CheckinError("Cadence must be 60, 120, 240, 480, 720, or 1440 minutes.")
    return minutes


def normalize_webhook(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not webhook_allowed(text):
        raise CheckinError("Slack webhook must be an https://hooks.slack.com/services/… URL, or left blank.")
    return text


def _as_bool(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return False


def _coordinate(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise CheckinError(f"{name} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CheckinError(f"{name} must be a number.") from None
    if not math.isfinite(number):
        raise CheckinError(f"{name} must be a real coordinate.")
    return number


def coarse_location(lat_value: object, lng_value: object) -> tuple[float | None, float | None, str]:
    lat = _coordinate(lat_value, "Latitude")
    lng = _coordinate(lng_value, "Longitude")
    if lat is None and lng is None:
        return None, None, ""
    if lat is None or lng is None:
        raise CheckinError("Share both latitude and longitude, or leave location off.")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise CheckinError("Location is out of range.")
    lat = float(f"{lat:.1f}")
    lng = float(f"{lng:.1f}")
    if lat == 0:
        lat = 0.0
    if lng == 0:
        lng = 0.0
    return lat, lng, f"{lat:.1f},{lng:.1f}"


def require_adult_ack(sector: str, adult_staff_only: object, schools_ack: object) -> None:
    if not _as_bool(adult_staff_only):
        raise CheckinError(ADULT_ONLY_NOTICE)
    if sector == "schools" and not _as_bool(schools_ack):
        raise CheckinError(SCHOOLS_NOTICE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def slack_escape(value: str) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(kind: str, *, callsign: str, sector_label: str, cadence_minutes: int, note: str, region: str, when: str) -> dict:
    titles = {
        "PANIC": "PANIC BEACON",
        "ASSISTANCE": "ASSISTANCE REQUEST",
        "OVERDUE_ALERT": "CHECK-IN OVERDUE",
    }
    colors = {"PANIC": "#E11D48", "ASSISTANCE": "#D97706", "OVERDUE_ALERT": "#C5A059"}
    title = titles.get(kind, "CHECK-IN")
    lines = [
        f"*{slack_escape(title)}*",
        f"*Callsign:* {slack_escape(callsign)}",
        f"*Sector:* {slack_escape(sector_label)}",
        f"*Cadence:* {slack_escape(cadence_label(cadence_minutes))}",
        f"*Note:* {slack_escape(note) if note else '\u2014'}",
        f"*Approximate area:* {slack_escape(region) if region else 'not shared'}",
        f"*When (UTC):* {slack_escape(when)}",
        "_Opaque callsign only. This alert has no names, emails, phone numbers, or pupil data._",
    ]
    return {
        "text": f"{title}: {callsign}",
        "attachments": [{
            "color": colors.get(kind, "#94A3B8"),
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }],
        }],
    }


def post_slack(url: str, payload: dict) -> None:
    if not webhook_allowed(url):
        return
    outgoing = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "GeoRiskGlobal-Checkin/1.0",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(outgoing, timeout=4) as response:
        response.read(512)


def deliver_slack(url: str, payload: dict, *, background: bool = True) -> None:
    if not url or not webhook_allowed(url):
        return
    sync = (os.environ.get("CHECKIN_SLACK_SYNC") or "").strip().lower() in {"1", "true", "yes"}

    def run() -> None:
        try:
            post_slack(url, payload)
        except Exception as exc:
            print(f"Check-in Slack alert failed: {exc.__class__.__name__}.")

    if sync or not background:
        run()
        return
    threading.Thread(target=run, name="checkin-slack", daemon=True).start()


def public_beacon(row: sqlite3.Row, now: datetime) -> dict:
    due = row["next_due_utc"] or ""
    seconds = seconds_until(due, now)
    status = row["status"]
    badge = badge_for(status, seconds)
    webhook = row["slack_webhook_url"] or ""
    return {
        "id": row["id"],
        "callsign": row["callsign"],
        "sector": row["sector"],
        "sector_label": SECTOR_LABELS.get(row["sector"], row["sector"]),
        "cadence_minutes": row["cadence_minutes"],
        "cadence_label": cadence_label(row["cadence_minutes"]),
        "status": status,
        "badge": badge,
        "badge_class": BADGE_CLASS.get(badge, "on-time"),
        "last_checkin_utc": row["last_checkin_utc"],
        "next_due_utc": due or None,
        "seconds_remaining": seconds,
        "countdown": format_remaining(seconds),
        "last_approx_region": row["last_approx_region"] or "",
        "last_note": row["last_note"] or "",
        "created_at": row["created_at"],
        "mobile_path": "/c/" + row["token"],
        "slack_configured": bool(webhook),
        "slack_webhook_masked": mask_webhook(webhook),
        "paused": status == "PAUSED",
    }


def mobile_state(row: sqlite3.Row, now: datetime) -> dict:
    public = public_beacon(row, now)
    return {
        "callsign": public["callsign"],
        "sector": public["sector"],
        "sector_label": public["sector_label"],
        "status": public["status"],
        "badge": public["badge"],
        "badge_class": public["badge_class"],
        "cadence_label": public["cadence_label"],
        "next_due_utc": public["next_due_utc"],
        "seconds_remaining": public["seconds_remaining"],
        "countdown": public["countdown"],
        "last_checkin_utc": public["last_checkin_utc"],
        "paused": public["paused"],
        "schools": public["sector"] == "schools",
    }


def public_event(row: sqlite3.Row) -> dict:
    lat = row["lat"]
    lng = row["lng"]
    region = ""
    if lat is not None and lng is not None:
        region = f"{float(lat):.1f},{float(lng):.1f}"
    return {
        "id": row["id"],
        "beacon_id": row["beacon_id"],
        "callsign": row["callsign"],
        "event_type": row["event_type"],
        "note": row["note"] or "",
        "region": region,
        "created_at": row["created_at"],
    }


class CheckinStore:
    def __init__(self, path: Path) -> None:
        from analytics_store import prefer_writable_file

        self.path = prefer_writable_file(Path(path).expanduser()).resolve()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self.purge_expired_events()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS beacons (
                    id INTEGER PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    callsign TEXT NOT NULL UNIQUE,
                    sector TEXT NOT NULL,
                    cadence_minutes INTEGER NOT NULL,
                    slack_webhook_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    last_checkin_utc TEXT,
                    next_due_utc TEXT,
                    last_approx_region TEXT NOT NULL DEFAULT '',
                    last_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkin_events (
                    id INTEGER PRIMARY KEY,
                    beacon_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    lat REAL,
                    lng REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (beacon_id) REFERENCES beacons(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_beacons_due ON beacons(status, next_due_utc);
                CREATE INDEX IF NOT EXISTS idx_events_created ON checkin_events(id);
                CREATE INDEX IF NOT EXISTS idx_events_created_at ON checkin_events(created_at);
                """
            )

    def _transaction(self, work):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = work()
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def create_beacon(
        self,
        *,
        callsign: object,
        sector: object,
        cadence_minutes: object,
        slack_webhook_url: object = "",
        adult_staff_only: object = False,
        schools_ack: object = False,
        now: datetime | None = None,
    ) -> dict:
        clean_sector = normalize_sector(sector)
        require_adult_ack(clean_sector, adult_staff_only, schools_ack)
        clean_callsign = normalize_callsign(callsign)
        cadence = normalize_cadence(cadence_minutes)
        webhook = normalize_webhook(slack_webhook_url)
        moment = now or utcnow()
        created = format_utc(moment)
        due = format_utc(moment + timedelta(minutes=cadence))

        def work():
            existing = self._conn.execute("SELECT id FROM beacons WHERE callsign = ?", (clean_callsign,)).fetchone()
            if existing:
                raise CheckinError("That callsign is already in use. Choose another opaque ID.", 409)
            token = ""
            for _ in range(3):
                candidate = secrets.token_urlsafe(32)
                if not valid_token(candidate):
                    continue
                clash = self._conn.execute("SELECT id FROM beacons WHERE token = ?", (candidate,)).fetchone()
                if clash is None:
                    token = candidate
                    break
            if not token:
                raise CheckinError("Could not issue a check-in link. Try again.", 500)
            self._conn.execute(
                """
                INSERT INTO beacons (
                    token, callsign, sector, cadence_minutes, slack_webhook_url, status,
                    last_checkin_utc, next_due_utc, last_approx_region, last_note, created_at
                ) VALUES (?, ?, ?, ?, ?, 'OK', NULL, ?, '', '', ?)
                """,
                (token, clean_callsign, clean_sector, cadence, webhook, due, created),
            )
            return self._conn.execute("SELECT * FROM beacons WHERE token = ?", (token,)).fetchone()

        try:
            row = self._transaction(work)
        except sqlite3.IntegrityError as exc:
            raise CheckinError("That callsign is already in use. Choose another opaque ID.", 409) from exc
        return public_beacon(row, moment)

    def list_beacons(self, now: datetime | None = None) -> list[dict]:
        moment = now or utcnow()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM beacons
                ORDER BY CASE status
                    WHEN 'PANIC' THEN 0
                    WHEN 'ASSISTANCE' THEN 1
                    WHEN 'OVERDUE' THEN 2
                    WHEN 'OK' THEN 3
                    ELSE 4
                END, callsign
                """
            ).fetchall()
        return [public_beacon(row, moment) for row in rows]

    def recent_events(self, limit: int = RECENT_LIMIT) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.id, e.beacon_id, e.event_type, e.note, e.lat, e.lng, e.created_at, b.callsign
                FROM checkin_events e
                JOIN beacons b ON b.id = e.beacon_id
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [public_event(row) for row in rows]

    def get_by_token(self, token: str) -> sqlite3.Row | None:
        if not valid_token(token):
            return None
        with self._lock:
            return self._conn.execute("SELECT * FROM beacons WHERE token = ?", (token,)).fetchone()

    def get_public(self, beacon_id: int, now: datetime | None = None) -> dict | None:
        moment = now or utcnow()
        with self._lock:
            row = self._conn.execute("SELECT * FROM beacons WHERE id = ?", (int(beacon_id),)).fetchone()
        if row is None:
            return None
        return public_beacon(row, moment)

    def record_tap(
        self,
        token: str,
        event_type: object,
        *,
        note: object = "",
        lat: object = None,
        lng: object = None,
        confirm: object = False,
        now: datetime | None = None,
    ) -> TapResult:
        kind = str(event_type or "").strip().upper()
        if kind not in TAP_EVENTS:
            raise CheckinError("Check-in must be SAFE, ASSISTANCE, or PANIC.")
        if kind == "PANIC" and not _as_bool(confirm):
            raise CheckinError("Panic needs a second confirmation. Tap the red button again to send it.")
        clean_note = normalize_note(note)
        lat_r, lng_r, region = coarse_location(lat, lng)
        moment = now or utcnow()
        stamp = format_utc(moment)
        if not valid_token(token):
            raise CheckinError("This check-in link is not active.", 404)

        def work():
            row = self._conn.execute("SELECT * FROM beacons WHERE token = ?", (token,)).fetchone()
            if row is None:
                raise CheckinError("This check-in link is not active.", 404)
            if row["status"] == "PAUSED":
                raise CheckinError("This check-in is paused. Contact the ops room.", 409)
            due = format_utc(moment + timedelta(minutes=int(row["cadence_minutes"])))
            status = STATUS_FOR_EVENT[kind]
            self._conn.execute(
                """
                UPDATE beacons
                SET status = ?, last_checkin_utc = ?, next_due_utc = ?, last_approx_region = ?, last_note = ?
                WHERE id = ?
                """,
                (status, stamp, due, region, clean_note, row["id"]),
            )
            self._conn.execute(
                """
                INSERT INTO checkin_events (beacon_id, event_type, note, lat, lng, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], kind, clean_note, lat_r, lng_r, stamp),
            )
            updated = self._conn.execute("SELECT * FROM beacons WHERE id = ?", (row["id"],)).fetchone()
            return row, updated

        previous, updated = self._transaction(work)
        state = mobile_state(updated, moment)
        state["message"] = TAP_MESSAGES[kind]
        return TapResult(
            event_type=kind,
            webhook=previous["slack_webhook_url"] or "",
            note=clean_note,
            region=region,
            body=state,
        )

    def pause(self, beacon_id: int, now: datetime | None = None) -> dict:
        moment = now or utcnow()

        def work():
            row = self._conn.execute("SELECT * FROM beacons WHERE id = ?", (int(beacon_id),)).fetchone()
            if row is None:
                raise CheckinError("That callsign was not found.", 404)
            self._conn.execute("UPDATE beacons SET status = 'PAUSED' WHERE id = ?", (row["id"],))
            return self._conn.execute("SELECT * FROM beacons WHERE id = ?", (row["id"],)).fetchone()

        return public_beacon(self._transaction(work), moment)

    def resume(self, beacon_id: int, now: datetime | None = None) -> dict:
        moment = now or utcnow()

        def work():
            row = self._conn.execute("SELECT * FROM beacons WHERE id = ?", (int(beacon_id),)).fetchone()
            if row is None:
                raise CheckinError("That callsign was not found.", 404)
            due = format_utc(moment + timedelta(minutes=int(row["cadence_minutes"])))
            self._conn.execute(
                "UPDATE beacons SET status = 'OK', next_due_utc = ? WHERE id = ?",
                (due, row["id"]),
            )
            return self._conn.execute("SELECT * FROM beacons WHERE id = ?", (row["id"],)).fetchone()

        return public_beacon(self._transaction(work), moment)

    def delete(self, beacon_id: int) -> None:
        """Erase the callsign and every stored event for it (UK/EU GDPR Art. 17)."""

        def work():
            row = self._conn.execute("SELECT id FROM beacons WHERE id = ?", (int(beacon_id),)).fetchone()
            if row is None:
                raise CheckinError("That callsign was not found.", 404)
            self._conn.execute("DELETE FROM checkin_events WHERE beacon_id = ?", (row["id"],))
            self._conn.execute("DELETE FROM beacons WHERE id = ?", (row["id"],))

        self._transaction(work)

    def purge_expired_events(self, now: datetime | None = None) -> int:
        """Delete check-in events older than the retention window (Art. 5(1)(e))."""
        moment = now or utcnow()
        cutoff = format_utc(moment - timedelta(days=checkins.retention_days()))

        def work():
            cursor = self._conn.execute("DELETE FROM checkin_events WHERE created_at < ?", (cutoff,))
            self._conn.execute(
                """
                UPDATE beacons
                SET last_note = '', last_approx_region = ''
                WHERE last_checkin_utc IS NOT NULL AND last_checkin_utc < ?
                """,
                (cutoff,),
            )
            return max(int(cursor.rowcount), 0)

        return self._transaction(work)

    def purge_event_log(self) -> int:
        """Erase the whole event log immediately. Callsigns stay (Art. 17)."""

        def work():
            cursor = self._conn.execute("DELETE FROM checkin_events")
            self._conn.execute("UPDATE beacons SET last_note = '', last_approx_region = ''")
            return max(int(cursor.rowcount), 0)

        return self._transaction(work)

    def expire_window(self, beacon_id: int, now: datetime | None = None) -> None:
        """Move next_due_utc into the past without changing status. Used by overdue tests."""
        moment = now or utcnow()
        past = format_utc(moment - timedelta(seconds=5))

        def work():
            row = self._conn.execute("SELECT id FROM beacons WHERE id = ?", (int(beacon_id),)).fetchone()
            if row is None:
                raise CheckinError("That callsign was not found.", 404)
            self._conn.execute("UPDATE beacons SET next_due_utc = ? WHERE id = ?", (past, row["id"]))

        self._transaction(work)

    def sweep(self, now: datetime | None = None) -> list[OverdueAlert]:
        moment = now or utcnow()
        stamp = format_utc(moment)

        def work():
            rows = self._conn.execute(
                """
                SELECT * FROM beacons
                WHERE status != 'PAUSED'
                  AND status != 'OVERDUE'
                  AND next_due_utc IS NOT NULL
                  AND next_due_utc <= ?
                """,
                (stamp,),
            ).fetchall()
            alerts: list[OverdueAlert] = []
            for row in rows:
                cursor = self._conn.execute(
                    """
                    UPDATE beacons
                    SET status = 'OVERDUE'
                    WHERE id = ?
                      AND status != 'PAUSED'
                      AND status != 'OVERDUE'
                      AND next_due_utc <= ?
                    """,
                    (row["id"], stamp),
                )
                if cursor.rowcount != 1:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO checkin_events (beacon_id, event_type, note, lat, lng, created_at)
                    VALUES (?, 'OVERDUE_ALERT', 'Missed check-in window.', NULL, NULL, ?)
                    """,
                    (row["id"], stamp),
                )
                alerts.append(OverdueAlert(
                    webhook=row["slack_webhook_url"] or "",
                    callsign=row["callsign"],
                    sector_label=SECTOR_LABELS.get(row["sector"], row["sector"]),
                    cadence_minutes=int(row["cadence_minutes"]),
                    region=row["last_approx_region"] or "",
                    when=stamp,
                ))
            return alerts

        return self._transaction(work)


def get_store() -> CheckinStore:
    global _store
    path = database_path().resolve()
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = CheckinStore(path)
        return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


def sweep_and_alert(now: datetime | None = None) -> int:
    store = get_store()
    store.purge_expired_events(now=now)
    alerts = store.sweep(now=now)
    for alert in alerts:
        deliver_slack(
            alert.webhook,
            slack_payload(
                "OVERDUE_ALERT",
                callsign=alert.callsign,
                sector_label=alert.sector_label,
                cadence_minutes=alert.cadence_minutes,
                note="Missed check-in window.",
                region=alert.region,
                when=alert.when,
            ),
            background=False,
        )
    return len(alerts)


def start_background() -> None:
    global _sweep_started
    with _sweep_lock:
        if _sweep_started:
            return
        if (os.environ.get("DISABLE_BACKGROUND") or "").strip().lower() in {"1", "true", "yes"}:
            return
        _sweep_started = True

    def loop() -> None:
        time.sleep(10)
        while True:
            try:
                sweep_and_alert()
            except Exception as exc:
                print(f"Check-in sweep failed: {exc.__class__.__name__}.")
            time.sleep(30)

    threading.Thread(target=loop, name="checkin-sweep", daemon=True).start()


def _json(payload: dict, status: int = 200) -> Response:
    response = Response(json.dumps(payload, ensure_ascii=True), status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def _html(body: str, status: int = 200) -> Response:
    response = Response(body, status=status, mimetype="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response


def _read_mapping() -> tuple[dict, bool]:
    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise CheckinError("Request body must be a JSON object.")
        return body, False
    return {key: value for key, value in request.form.items()}, True


def _safe_callsign_echo(value: object) -> str:
    text = " ".join(str(value or "").split()).upper()
    if _CALLSIGN_RE.match(text) and len(text) <= 48:
        return text
    return ""


_OPS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>Check-In — GeoRiskGlobal</title>
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <style>
    :root { color-scheme: dark; --bg: #060A13; --panel: #0D1424; --line: #1E293B; --text: #E2E8F0; --muted: #94A3B8; --gold: #D4AF37; --navy: #102A63; --gold-border: #C5A059; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font: 15px/1.45 "Segoe UI", system-ui, sans-serif; }
    input:not([type="checkbox"]), select { min-height: 44px; }
    main { max-width: 1100px; margin: 0 auto; padding: 16px 14px 48px; }
    h1 { margin: 0 0 8px; font-size: clamp(26px, 4vw, 36px); }
    h2 { margin: 0 0 10px; font-size: 18px; }
    .kicker { margin: 0 0 4px; color: var(--gold); letter-spacing: 0.12em; font-size: 12px; font-weight: 800; }
    .compliance, .zero-pii, .gdpr { border: 1px solid var(--gold-border); background: #14100a; border-radius: 12px; padding: 12px 14px; margin: 10px 0; }
    .gdpr { font-size: 14px; }
    .compliance { font-weight: 700; }
    .zero-pii, .note { color: var(--muted); }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; margin: 14px 0; }
    form { display: grid; gap: 10px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
    label { display: flex; flex-direction: column; gap: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
    label.row { flex-direction: row; align-items: flex-start; gap: 10px; color: var(--text); font-size: 14px; font-weight: 650; }
    label.row span { flex: 1; line-height: 1.4; }
    input, select { width: 100%; background: #060A13; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 10px; font: inherit; font-size: 16px; }
    input[type="checkbox"] { width: 18px; height: 18px; flex: 0 0 auto; margin: 2px 0 0; padding: 0; accent-color: #D4AF37; }
    button, .btn { color: var(--gold); background: var(--navy); border: 1px solid var(--gold-border); border-radius: 8px; padding: 10px 12px; font: inherit; font-weight: 700; cursor: pointer; }
    button.ghost { background: transparent; }
    .error { color: #fecdd3; background: #4c0519; border-radius: 8px; padding: 10px; }
    .success { color: #d1fae5; background: #064e3b; border-radius: 8px; padding: 10px; }
    .cards, .log { display: grid; gap: 10px; }
    .card, .event { border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: #060A13; }
    .card.panic { border-color: #E11D48; }
    .card.overdue, .card.assistance { border-color: #D97706; }
    .card.on-time { border-color: #0f766e; }
    .badge { display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 800; color: #fff; }
    .badge.on-time { background: #0f766e; }
    .badge.overdue { background: #c2410c; }
    .badge.panic { background: #E11D48; }
    .badge.assistance { background: #D97706; }
    .badge.paused { background: #475569; }
    .link-row, .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
    .link-row input { flex: 1 1 220px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }
    .event { display: grid; gap: 2px; }
    .tag { color: var(--gold); font-weight: 800; font-size: 12px; }
    .empty { color: var(--muted); margin: 0; }
    [hidden] { display: none !important; }
    @media (max-width: 640px) { .link-row, .actions { align-items: stretch; } button, .btn { min-height: 44px; } }
  </style>
  <link rel="stylesheet" href="/static/site-nav.css">
  <script src="/static/site-nav.js" defer></script>
</head>
<body>
  <header id="live-bar" class="site-shell">
    <div class="nav-backdrop"></div>
    <div class="nav-row">
      <a class="brand-lockup" href="/">
        <img id="brandLogo" src="/static/logo.png?v=5" alt="GeoRiskGlobal">
      </a>
      <button type="button" class="nav-toggle" aria-expanded="false" aria-controls="page-nav" aria-label="Open menu">
        <span class="nav-toggle-bars" aria-hidden="true"></span>
      </button>
      <nav id="page-nav" aria-label="Sections">
        <a href="/">Live map</a>
        <a href="/risk-assessments">Risk Assessments</a>
        <a href="/route-risk">Route risk</a>
        <a href="/check-in" aria-current="page">Check-in</a>
        <a href="/pricing">Pricing</a>
        <details class="nav-more">
          <summary>Tools</summary>
          <div class="nav-menu">
            <a href="/reports">Reports</a>
            <a href="/sentinel-zones">Sentinel zones</a>
            <a href="/slack-alerts">Slack alerts</a>
            <a href="/white-label">White-label</a>
            <a href="/grok-ops">Grok Ops</a>
            <a href="/briefing">Daily briefing</a>
            <a href="/world-risk">World risk</a>
            <a href="/countries">Country guides</a>
            <a href="/enterprise">Enterprise</a>
          </div>
        </details>
        <a class="nav-cta nav-cta-drawer" href="/enterprise">Book a Pilot / Subscribe</a>
      </nav>
      <a class="nav-cta nav-cta-bar" href="/enterprise">Book a Pilot / Subscribe</a>
    </div>
  </header>
  <main>
    <p class="kicker">OPS ROOM</p>
    <h1>2-Tap Mobile Check-In</h1>
    <p class="page-lead">Give field staff a private link. They tap once to say they are safe, or twice only if they need urgent help.</p>
    <section class="how-it-works" data-how="check-in">
      <div class="how-top">
        <p class="kicker">How it works</p>
        <button type="button" class="how-dismiss ghost">Hide</button>
      </div>
      <ol>
        <li><strong>1. Create a callsign.</strong> Use something like CREW-BEIRUT-1. Do not use a real name.</li>
        <li><strong>2. Copy the link.</strong> Send that link to the adult staff member. It is the only way in.</li>
        <li><strong>3. Watch this board.</strong> Safe, assistance, and overdue updates appear here.</li>
      </ol>
    </section>
    <p class="compliance" id="compliance-schools" role="note">__SCHOOLS_NOTICE__</p>
    <p class="zero-pii" role="note">__ZERO_PII__</p>
    <p class="gdpr" id="gdpr-notice" role="note">__GDPR_NOTICE__</p>
    __BANNERS__
    <section class="panel">
      <h2>Create callsign</h2>
      <form id="create-form" method="post" action="/api/checkins" autocomplete="off">
        <div class="grid">
          <label>Callsign — no real names
            <input id="callsign" name="callsign" maxlength="48" required autocomplete="off" spellcheck="false" autocapitalize="characters" placeholder="CREW-BEIRUT-1" value="__CALLSIGN_VALUE__">
          </label>
          <label>Sector
            <select id="sector" name="sector" required>
              <option value="">Select a sector</option>
              <option value="media">Media &amp; news desk</option>
              <option value="ngo">NGO</option>
              <option value="psc">PSC / close protection</option>
              <option value="corporate">Corporate</option>
              <option value="government">Government / public sector</option>
              <option value="schools">Schools &amp; universities</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label>Cadence
            <select id="cadence" name="cadence_minutes" required>
              <option value="60">Every 1 hour</option>
              <option value="120" selected>Every 2 hours</option>
              <option value="240">Every 4 hours</option>
              <option value="480">Every 8 hours</option>
              <option value="720">Every 12 hours</option>
              <option value="1440">Every 24 hours</option>
            </select>
          </label>
          <label>Slack channel address (optional)
            <input id="webhook" name="slack_webhook_url" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://hooks.slack.com/services/…">
          </label>
        </div>
        <p class="compliance" id="schools-sector-notice" role="note" hidden>__SCHOOLS_NOTICE__</p>
        <label class="row" id="schools-ack-wrap" hidden>
          <input id="schools-ack" name="schools_ack" type="checkbox" value="true">
          <span>I am issuing this link to an adult trip leader or staff member, never to a pupil or minor.</span>
        </label>
        <label class="row">
          <input id="adult-ack" name="adult_staff_only" type="checkbox" value="true" required>
          <span>__ADULT_ONLY__</span>
        </label>
        <p>
          <button type="button" class="ghost" id="use-example">Use example</button>
          <button type="submit" id="create-submit">Create check-in link</button>
        </p>
        <p id="form-status" class="note" role="status"></p>
      </form>
    </section>
    <section class="panel">
      <div class="actions">
        <h2 style="margin-right:auto">Live board</h2>
        <button type="button" class="ghost" id="purge-log">Purge Event Log</button>
        <button type="button" class="ghost" id="sweep">Check for overdue now</button>
      </div>
      <p id="sweep-status" class="note" role="status"></p>
      <div id="board" class="cards"><p class="empty">Loading callsigns…</p></div>
    </section>
    <section class="panel">
      <h2>Recent check-ins</h2>
      <div id="log" class="log"><p class="empty">Loading the log…</p></div>
    </section>
  </main>
  <script>
    (function () {
      var form = document.getElementById("create-form");
      var sector = document.getElementById("sector");
      var schoolsNotice = document.getElementById("schools-sector-notice");
      var schoolsWrap = document.getElementById("schools-ack-wrap");
      var schoolsAck = document.getElementById("schools-ack");
      var formStatus = document.getElementById("form-status");
      var board = document.getElementById("board");
      var log = document.getElementById("log");
      var sweepButton = document.getElementById("sweep");
      var purgeButton = document.getElementById("purge-log");
      var sweepStatus = document.getElementById("sweep-status");
      var capturedAt = Date.now();
      var labels = { SAFE: "Safe", ASSISTANCE: "Assistance", PANIC: "Panic", OVERDUE_ALERT: "Overdue alert" };
      var badgeClass = { "on-time": 1, overdue: 1, panic: 1, assistance: 1, paused: 1 };

      function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
      }
      function setStatus(node, text, isError) {
        if (!node) return;
        node.textContent = text || "";
        node.className = isError ? "error" : "note";
      }
      function toggleSchools() {
        var schools = sector && sector.value === "schools";
        if (schoolsNotice) schoolsNotice.hidden = !schools;
        if (schoolsWrap) schoolsWrap.hidden = !schools;
        if (schoolsAck) schoolsAck.required = !!schools;
      }
      function formatRemaining(seconds) {
        var overdue = seconds < 0;
        var abs = Math.abs(seconds);
        var days = Math.floor(abs / 86400);
        var hours = Math.floor((abs % 86400) / 3600);
        var mins = Math.floor((abs % 3600) / 60);
        var secs = abs % 60;
        var parts = [];
        if (days) parts.push(days + "d");
        if (hours || days) parts.push(hours + "h");
        parts.push(mins + "m");
        parts.push(secs + "s");
        return (overdue ? "Overdue by " : "Due in ") + parts.join(" ");
      }
      function tick() {
        var nodes = document.querySelectorAll("[data-countdown]");
        Array.prototype.forEach.call(nodes, function (node) {
          var initial = Number(node.getAttribute("data-seconds"));
          if (!isFinite(initial)) {
            node.textContent = "Paused";
            return;
          }
          var seconds = initial - Math.floor((Date.now() - capturedAt) / 1000);
          node.textContent = formatRemaining(seconds);
        });
      }
      function copyText(value) {
        var field = document.createElement("textarea");
        field.value = value;
        field.setAttribute("readonly", "");
        field.style.position = "fixed";
        field.style.left = "-9999px";
        document.body.appendChild(field);
        field.select();
        var copied = false;
        try { copied = document.execCommand("copy"); } catch (err) { copied = false; }
        field.remove();
        if (copied) return Promise.resolve(true);
        if (navigator.clipboard && navigator.clipboard.writeText) {
          return navigator.clipboard.writeText(value).then(function () { return true; }, function () { return false; });
        }
        return Promise.resolve(false);
      }
      function card(beacon) {
        var wrap = el("article", "card " + (badgeClass[beacon.badge_class] ? beacon.badge_class : "on-time"));
        var title = el("h3", "", beacon.callsign || "Callsign");
        wrap.appendChild(title);
        var meta = el("p", "note", (beacon.sector_label || "") + " · " + (beacon.cadence_label || ""));
        wrap.appendChild(meta);
        var badge = el("span", "badge " + (badgeClass[beacon.badge_class] ? beacon.badge_class : "on-time"), beacon.badge || "");
        wrap.appendChild(badge);
        var countdown = el("p", "");
        countdown.setAttribute("data-countdown", "1");
        if (beacon.paused || beacon.seconds_remaining == null) countdown.removeAttribute("data-seconds");
        else countdown.setAttribute("data-seconds", String(beacon.seconds_remaining));
        countdown.textContent = beacon.paused ? "Paused" : (beacon.countdown || "");
        wrap.appendChild(countdown);
        var checked = beacon.last_checkin_utc ? ("Last check-in " + beacon.last_checkin_utc) : "No check-in yet";
        wrap.appendChild(el("p", "note", checked));
        if (beacon.last_note) wrap.appendChild(el("p", "", beacon.last_note));
        if (beacon.last_approx_region) wrap.appendChild(el("p", "note", "Approx area " + beacon.last_approx_region));
        wrap.appendChild(el("p", "note", beacon.slack_configured ? "Slack alerts on" : "Dashboard only"));
        var path = typeof beacon.mobile_path === "string" ? beacon.mobile_path : "";
        var url = path.charAt(0) === "/" ? (window.location.origin + path) : "";
        var row = el("div", "link-row");
        var field = document.createElement("input");
        field.readOnly = true;
        field.value = url;
        field.setAttribute("aria-label", "Mobile check-in link");
        var copy = el("button", "", "Copy link");
        copy.type = "button";
        copy.addEventListener("click", function () {
          field.focus();
          field.select();
          copyText(url).then(function (ok) {
            copy.textContent = ok ? "Copied!" : "Copy link";
            if (ok && window.georiskToast) window.georiskToast("Copied!");
            if (ok) window.setTimeout(function () { copy.textContent = "Copy link"; }, 1600);
          });
        });
        row.appendChild(field);
        row.appendChild(copy);
        wrap.appendChild(row);
        var actions = el("div", "actions");
        if (typeof beacon.id === "number") {
          var pause = el("button", "ghost", beacon.paused ? "Resume" : "Pause");
          pause.type = "button";
          pause.addEventListener("click", function () {
            var route = beacon.paused ? "/resume" : "/pause";
            fetch("/api/checkins/" + beacon.id + route, { method: "POST", headers: { "Accept": "application/json" } })
              .then(function () { return refresh(); })
              .catch(function () { setStatus(sweepStatus, "Could not update that callsign.", true); });
          });
          var remove = el("button", "ghost", "Delete");
          remove.type = "button";
          remove.addEventListener("click", function () {
            if (!window.confirm("Delete " + (beacon.callsign || "this callsign") + "? The mobile link stops working.")) return;
            fetch("/api/checkins/" + beacon.id, { method: "DELETE", headers: { "Accept": "application/json" } })
              .then(function () { return refresh(); })
              .catch(function () { setStatus(sweepStatus, "Could not delete that callsign.", true); });
          });
          actions.appendChild(pause);
          actions.appendChild(remove);
        }
        wrap.appendChild(actions);
        return wrap;
      }
      function renderBoard(data) {
        if (!board) return;
        board.textContent = "";
        var beacons = data && data.beacons ? data.beacons : [];
        if (!beacons.length) {
          board.appendChild(el("p", "empty", "No callsigns yet. Create one above. The mobile link is the only credential."));
          return;
        }
        beacons.forEach(function (beacon) { board.appendChild(card(beacon)); });
      }
      function renderLog(events) {
        if (!log) return;
        log.textContent = "";
        if (!events || !events.length) {
          log.appendChild(el("p", "empty", "No check-ins yet."));
          return;
        }
        events.forEach(function (event) {
          var row = el("article", "event");
          row.appendChild(el("span", "tag", labels[event.event_type] || event.event_type || "Event"));
          row.appendChild(el("strong", "", event.callsign || ""));
          row.appendChild(el("time", "", event.created_at || ""));
          if (event.note) row.appendChild(el("p", "", event.note));
          if (event.region) row.appendChild(el("p", "note", "Approx " + event.region));
          log.appendChild(row);
        });
      }
      function readJson(response) {
        return response.text().then(function (text) {
          try { return JSON.parse(text); } catch (err) { return { ok: false, message: "Unexpected response from the server." }; }
        });
      }
      function refresh() {
        return fetch("/api/checkins", { headers: { "Accept": "application/json" } }).then(function (response) {
          if (response.redirected || response.status === 401) {
            window.location.assign("/login?next=/check-in");
            return null;
          }
          return readJson(response).then(function (data) {
            if (!response.ok || !data.ok) {
              if (board) {
                board.textContent = "";
                board.appendChild(el("p", "error", data.message || "The board could not be loaded."));
              }
              return null;
            }
            capturedAt = Date.now();
            renderBoard(data);
            renderLog(data.events || []);
            tick();
            return data;
          });
        });
      }
      var example = document.getElementById("use-example");
      if (example) example.addEventListener("click", function () {
        var callsign = document.getElementById("callsign");
        if (callsign && !callsign.value) callsign.value = "CREW-BEIRUT-1";
        if (sector && !sector.value) sector.value = "media";
        var cadence = document.getElementById("cadence");
        if (cadence && !cadence.value) cadence.value = "120";
        toggleSchools();
      });
      if (sector) sector.addEventListener("change", toggleSchools);
      toggleSchools();
      if (form) {
        form.addEventListener("submit", function (event) {
          if (!window.fetch) return;
          event.preventDefault();
          var adult = document.getElementById("adult-ack");
          if (!adult || !adult.checked) {
            setStatus(formStatus, "Confirm these links are for adult staff only.", true);
            return;
          }
          if (sector && sector.value === "schools" && (!schoolsAck || !schoolsAck.checked)) {
            setStatus(formStatus, "Schools links require the adult trip-leader confirmation.", true);
            return;
          }
          var payload = {
            callsign: document.getElementById("callsign").value,
            sector: sector.value,
            cadence_minutes: Number(document.getElementById("cadence").value),
            slack_webhook_url: document.getElementById("webhook").value,
            adult_staff_only: true,
            schools_ack: !!(schoolsAck && schoolsAck.checked)
          };
          var submit = document.getElementById("create-submit");
          if (submit) { submit.disabled = true; submit.classList.add("is-busy"); }
          fetch("/api/checkins", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify(payload)
          }).then(function (response) {
            return readJson(response).then(function (data) {
              if (!response.ok || !data.ok) {
                setStatus(formStatus, data.message || "Could not create that callsign.", true);
                return null;
              }
              document.getElementById("webhook").value = "";
              document.getElementById("callsign").value = "";
              if (schoolsAck) schoolsAck.checked = false;
              setStatus(formStatus, "Link created. Copy it from the board. The Slack address is stored and is not shown again.", false);
              return refresh();
            });
          }).catch(function () {
            setStatus(formStatus, "Could not create that callsign.", true);
          }).then(function () {
            if (submit) { submit.disabled = false; submit.classList.remove("is-busy"); }
          });
        });
      }
      if (purgeButton) {
        purgeButton.addEventListener("click", function () {
          if (!window.confirm("Erase the check-in event log now? Callsigns stay. This cannot be undone.")) return;
          purgeButton.disabled = true;
          fetch("/api/checkins/purge-events", { method: "POST", headers: { "Accept": "application/json" } })
            .then(function (response) { return readJson(response).then(function (data) { return { response: response, data: data }; }); })
            .then(function (result) {
              var deleted = result.data && typeof result.data.deleted === "number" ? result.data.deleted : 0;
              setStatus(sweepStatus, result.response.ok ? ("Erased " + deleted + " check-in event" + (deleted === 1 ? "" : "s") + ".") : ((result.data && result.data.message) || "Could not erase the event log."), !result.response.ok);
              return refresh();
            })
            .catch(function () { setStatus(sweepStatus, "Could not erase the event log.", true); })
            .then(function () { purgeButton.disabled = false; });
        });
      }
      if (sweepButton) {
        sweepButton.addEventListener("click", function () {
          sweepButton.disabled = true;
          fetch("/api/checkins/sweep-overdue", { method: "POST", headers: { "Accept": "application/json" } })
            .then(function (response) { return readJson(response).then(function (data) { return { response: response, data: data }; }); })
            .then(function (result) {
              var count = result.data && typeof result.data.alerts === "number" ? result.data.alerts : 0;
              setStatus(sweepStatus, count ? (count + " overdue alert" + (count === 1 ? "" : "s") + " sent.") : "No new overdue alerts.", !result.response.ok);
              return refresh();
            })
            .catch(function () { setStatus(sweepStatus, "Could not check overdue callsigns.", true); })
            .then(function () { sweepButton.disabled = false; });
        });
      }
      refresh().catch(function () {
        if (board) {
          board.textContent = "";
          board.appendChild(el("p", "error", "The board could not be loaded."));
        }
      });
      window.setInterval(function () { refresh().catch(function () {}); }, 15000);
      window.setInterval(tick, 1000);
    }());
  </script>
</body>
</html>
"""


def ops_page(error: str = "", created: bool = False, callsign: str = "") -> str:
    banners = []
    if error:
        banners.append(f'<p class="error" role="alert">{escape(error)}</p>')
    if created:
        banners.append('<p class="success" role="status">Callsign created. Copy the mobile link from the board. That link is the only way in.</p>')
    page = _OPS_TEMPLATE
    page = page.replace("__SCHOOLS_NOTICE__", escape(SCHOOLS_NOTICE))
    page = page.replace("__ZERO_PII__", escape(ZERO_PII_NOTICE))
    page = page.replace("__GDPR_NOTICE__", escape(checkins.gdpr_notice(checkins.retention_days())))
    page = page.replace("__ADULT_ONLY__", escape(ADULT_ONLY_NOTICE))
    page = page.replace("__BANNERS__", "".join(banners))
    page = page.replace("__CALLSIGN_VALUE__", escape(_safe_callsign_echo(callsign), quote=True))
    return page


_MOBILE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="robots" content="noindex, nofollow">
  <meta name="theme-color" content="#060A13">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="mobile-web-app-capable" content="yes">
  <title>Check in — __CALLSIGN__</title>
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <style>
    :root { color-scheme: dark; --bg: #060A13; --text: #F8FAFC; --muted: #94A3B8; --gold: #D4AF37; }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }
    body { font: 16px/1.4 "Segoe UI", system-ui, sans-serif; padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }
    main { width: min(480px, 100%); margin: 0 auto; padding: 16px 16px 32px; display: grid; gap: 12px; }
    img { height: 40px; width: auto; max-width: 70vw; object-fit: contain; }
    .kicker { margin: 0; color: var(--gold); letter-spacing: 0.14em; font-size: 12px; font-weight: 800; }
    h1 { margin: 0; font-size: clamp(28px, 8vw, 40px); letter-spacing: 0.02em; overflow-wrap: anywhere; }
    .meta, .note { margin: 0; color: var(--muted); }
    .compliance, .zero-pii { border: 1px solid #C5A059; background: #14100a; border-radius: 12px; padding: 12px; margin: 0; }
    .compliance.schools { background: #1c1406; }
    .badge { display: inline-block; border-radius: 999px; padding: 4px 10px; font-weight: 800; color: #fff; }
    .badge.on-time { background: #0f766e; }
    .badge.overdue { background: #c2410c; }
    .badge.panic { background: #E11D48; }
    .badge.assistance { background: #D97706; }
    .badge.paused { background: #475569; }
    form { display: grid; gap: 12px; }
    .btn, button, a.btn { display: flex; align-items: center; justify-content: center; text-align: center; width: 100%; min-height: 72px; border-radius: 16px; border: 0; font: inherit; font-weight: 800; text-decoration: none; padding: 16px; cursor: pointer; touch-action: manipulation; }
    .safe { min-height: 128px; background: #047857; color: #fff; font-size: clamp(22px, 6vw, 28px); }
    .assist { background: #B45309; color: #fff; font-size: 18px; }
    .panic, a.panic { background: #BE123C; color: #fff; font-size: 18px; }
    #send-panic:not([hidden]) { box-shadow: 0 0 0 3px #fecdd3; }
    button:disabled, a[aria-disabled="true"] { opacity: 0.45; cursor: not-allowed; }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 13px; font-weight: 700; }
    input[type="text"] { width: 100%; font-size: 16px; padding: 12px; border-radius: 10px; border: 1px solid #1E293B; background: #0D1424; color: var(--text); }
    .secondary { min-height: 48px; background: transparent; color: var(--gold); border: 1px solid #C5A059; }
    .cancel { color: var(--muted); text-align: center; }
    .result { margin: 0; border-radius: 12px; padding: 12px; font-weight: 700; }
    .result:empty { display: none; }
    .ack-safe { background: #064e3b; color: #d1fae5; }
    .ack-assistance { background: #78350f; color: #ffedd5; }
    .ack-panic { background: #4c0519; color: #ffe4e6; }
    .ack-error { background: #4c0519; color: #fecdd3; }
    .paused-banner { background: #1e293b; border-radius: 12px; padding: 12px; margin: 0; }
    :focus-visible { outline: 3px solid var(--gold); outline-offset: 2px; }
    @media (prefers-reduced-motion: reduce) { #send-panic { box-shadow: none; } }
    [hidden] { display: none !important; }
  </style>
</head>
<body data-due="__DUE__" data-paused="__PAUSED__" data-status="__STATUS__">
  <main>
    <img src="/static/logo.png?v=5" alt="GeoRiskGlobal">
    <p class="kicker">CHECK-IN</p>
    <h1>__CALLSIGN__</h1>
    <p class="meta">__SECTOR__ · __CADENCE__</p>
    <p><span id="badge" class="badge __BADGE_CLASS__">__BADGE__</span></p>
    <p id="countdown" class="meta">__COUNTDOWN__</p>
    <p class="compliance __SCHOOLS_CLASS__" role="note">__SCHOOLS_NOTICE__</p>
    <p class="zero-pii" role="note">__ZERO_PII__</p>
    __PAUSED_BANNER__
    <p id="result" class="result __ACK_CLASS__" role="status">__ACK__</p>
    <form id="checkin-form" method="post" action="__ACTION__" autocomplete="off">
      <button class="btn safe" type="submit" name="event" value="SAFE" id="safe" __DISABLED__>TAP TO CONFIRM SAFE</button>
      <button class="btn assist" type="submit" name="event" value="ASSISTANCE" id="assistance" __DISABLED__>Need Non-Urgent Assistance</button>
      <a id="arm-panic" class="btn panic" href="__PATH__?arm=panic" __ARM_HIDDEN__ __ARM_DISABLED__>HOLD / 2-TAP PANIC BEACON</a>
      <button class="btn panic" type="submit" name="event" value="PANIC" id="send-panic" __PANIC_HIDDEN__ __PANIC_DISABLED__>TAP AGAIN TO SEND PANIC</button>
      <a id="cancel-panic" class="cancel" href="__PATH__" __CANCEL_HIDDEN__>Cancel panic</a>
      __CONFIRM__
      <label>Optional note — no names, emails, or phone numbers
        <input id="note" name="note" type="text" maxlength="140" autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false" placeholder="AT HOTEL" __DISABLED__>
      </label>
      <button class="btn secondary" type="button" id="locate" __DISABLED__>Share approximate area (optional)</button>
      <button class="btn secondary" type="button" id="clear-location" hidden>Don't send location</button>
      <p id="locate-status" class="note" role="status"></p>
      <input id="lat" name="lat" type="hidden" value="">
      <input id="lng" name="lng" type="hidden" value="">
    </form>
    <p class="note">One tap confirms safe or asks for non-urgent help. Panic sends only after a second tap, so a pocket press does not alert the ops room.</p>
  </main>
  <noscript><p class="note" style="padding:16px">JavaScript is off. The green and amber buttons still submit, and the red control asks you to tap again before a panic is sent.</p></noscript>
  <script>
    (function () {
      var form = document.getElementById("checkin-form");
      var result = document.getElementById("result");
      var arm = document.getElementById("arm-panic");
      var sendPanic = document.getElementById("send-panic");
      var cancel = document.getElementById("cancel-panic");
      var locate = document.getElementById("locate");
      var clearLocation = document.getElementById("clear-location");
      var locateStatus = document.getElementById("locate-status");
      var latField = document.getElementById("lat");
      var lngField = document.getElementById("lng");
      var note = document.getElementById("note");
      var countdown = document.getElementById("countdown");
      var badge = document.getElementById("badge");
      var paused = document.body.getAttribute("data-paused") === "true";
      var dueMs = Date.parse(document.body.getAttribute("data-due") || "");
      var armTimer = 0;
      var sending = false;
      var known = { "on-time": 1, overdue: 1, panic: 1, assistance: 1, paused: 1 };
      var messages = {
        SAFE: "Safe check-in recorded. The ops room can see that you are on time.",
        ASSISTANCE: "Assistance request sent to the ops room.",
        PANIC: "Panic beacon sent to the ops room. Stay on this page if you can."
      };

      function formatRemaining(seconds) {
        var overdue = seconds < 0;
        var abs = Math.abs(seconds);
        var days = Math.floor(abs / 86400);
        var hours = Math.floor((abs % 86400) / 3600);
        var mins = Math.floor((abs % 3600) / 60);
        var secs = Math.abs(abs % 60);
        var parts = [];
        if (days) parts.push(days + "d");
        if (hours || days) parts.push(hours + "h");
        parts.push(mins + "m");
        parts.push(secs + "s");
        return (overdue ? "Overdue by " : "Due in ") + parts.join(" ");
      }
      function tick() {
        if (!countdown || paused || !isFinite(dueMs)) return;
        countdown.textContent = formatRemaining(Math.round((dueMs - Date.now()) / 1000));
      }
      function showResult(kind, message) {
        if (!result) return;
        var cls = kind === "SAFE" || kind === "ASSISTANCE" || kind === "PANIC" ? ("ack-" + kind.toLowerCase()) : "ack-error";
        result.className = "result " + cls;
        result.textContent = message || "";
      }
      function disarm() {
        window.clearTimeout(armTimer);
        if (arm) { arm.hidden = false; }
        if (sendPanic) { sendPanic.hidden = true; sendPanic.disabled = true; }
        if (cancel) cancel.hidden = true;
      }
      function armPanic() {
        if (paused || sending) return;
        if (arm) arm.hidden = true;
        if (sendPanic) { sendPanic.hidden = false; sendPanic.disabled = false; }
        if (cancel) cancel.hidden = false;
        window.clearTimeout(armTimer);
        armTimer = window.setTimeout(disarm, 8000);
      }
      function setBusy(busy) {
        sending = busy;
        ["safe", "assistance", "locate"].forEach(function (id) {
          var node = document.getElementById(id);
          if (node) node.disabled = busy || paused;
        });
        if (sendPanic && (busy || paused)) sendPanic.disabled = true;
      }
      function buzz(pattern) {
        try { if (navigator.vibrate) navigator.vibrate(pattern); } catch (err) { /* optional */ }
      }
      if (arm) {
        arm.addEventListener("click", function (event) {
          if (paused) { event.preventDefault(); return; }
          if (!window.fetch) return;
          event.preventDefault();
          armPanic();
        });
      }
      if (cancel) {
        cancel.addEventListener("click", function (event) {
          if (!window.fetch) return;
          event.preventDefault();
          disarm();
        });
      }
      if (sendPanic && !sendPanic.hidden && !sendPanic.disabled) armTimer = window.setTimeout(disarm, 8000);
      if (locate) {
        locate.addEventListener("click", function () {
          if (paused) return;
          if (!navigator.geolocation) {
            if (locateStatus) locateStatus.textContent = "This device cannot share a location. You can still check in.";
            return;
          }
          if (locateStatus) locateStatus.textContent = "Requesting an approximate area…";
          navigator.geolocation.getCurrentPosition(function (pos) {
            var coarseLat = Math.round(pos.coords.latitude * 10) / 10;
            var coarseLng = Math.round(pos.coords.longitude * 10) / 10;
            if (latField) latField.value = coarseLat.toFixed(1);
            if (lngField) lngField.value = coarseLng.toFixed(1);
            if (locateStatus) locateStatus.textContent = "Approximate area ready (" + coarseLat.toFixed(1) + ", " + coarseLng.toFixed(1) + "). It is rounded before it is stored.";
            if (clearLocation) clearLocation.hidden = false;
          }, function () {
            if (locateStatus) locateStatus.textContent = "Location was not shared. You can still check in.";
          }, { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 });
        });
      }
      if (clearLocation) {
        clearLocation.addEventListener("click", function () {
          if (latField) latField.value = "";
          if (lngField) lngField.value = "";
          clearLocation.hidden = true;
          if (locateStatus) locateStatus.textContent = "Location removed.";
        });
      }
      if (form) {
        form.addEventListener("submit", function (event) {
          var submitter = event.submitter;
          if (!submitter || !window.fetch) return;
          event.preventDefault();
          if (paused || sending) return;
          var kind = submitter.value || "SAFE";
          if (kind === "PANIC" && (!sendPanic || sendPanic.hidden || sendPanic.disabled)) return;
          var payload = {
            event: kind,
            note: note ? note.value : "",
            lat: latField && latField.value ? Number(latField.value) : null,
            lng: lngField && lngField.value ? Number(lngField.value) : null
          };
          if (kind === "PANIC") payload.confirm = true;
          setBusy(true);
          fetch(form.getAttribute("action"), {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify(payload)
          }).then(function (response) {
            return response.text().then(function (text) {
              var data = {};
              try { data = JSON.parse(text); } catch (err) { data = { ok: false, message: "Unexpected response from the server." }; }
              return { ok: response.ok, data: data };
            });
          }).then(function (result) {
            if (!result.ok || !result.data.ok) {
              showResult("ERROR", (result.data && result.data.message) || "The check-in could not be sent. Try again.");
              disarm();
              return;
            }
            showResult(kind, result.data.message || messages[kind] || "Recorded.");
            if (badge && known[result.data.badge_class]) {
              badge.className = "badge " + result.data.badge_class;
              badge.textContent = result.data.badge || "";
            }
            if (result.data.next_due_utc) {
              dueMs = Date.parse(result.data.next_due_utc);
              document.body.setAttribute("data-due", result.data.next_due_utc);
            }
            if (countdown && result.data.countdown) countdown.textContent = result.data.countdown;
            disarm();
            if (kind === "PANIC") buzz([80, 40, 80]);
            else buzz(30);
          }).catch(function () {
            showResult("ERROR", "The check-in could not be sent. Try again.");
            disarm();
          }).then(function () { setBusy(false); });
        });
      }
      tick();
      window.setInterval(tick, 1000);
    }());
  </script>
</body>
</html>
"""


_SIMPLE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>__TITLE__</title>
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #060A13; color: #E2E8F0; font: 16px/1.45 "Segoe UI", system-ui, sans-serif; padding: 24px; }
    main { width: min(480px, 100%); display: grid; gap: 12px; }
    a { color: #D4AF37; }
    .compliance { border: 1px solid #C5A059; border-radius: 12px; padding: 12px; }
  </style>
</head>
<body>
  <main>
    <h1>__TITLE__</h1>
    <p>__MESSAGE__</p>
    <p class="compliance" role="note">__SCHOOLS_NOTICE__</p>
    <p>__LINK__</p>
  </main>
</body>
</html>
"""


def mobile_page(row: sqlite3.Row, *, armed: bool = False, ack: str = "") -> str:
    now = utcnow()
    state = mobile_state(row, now)
    token = row["token"]
    path = "/c/" + token
    paused = bool(state["paused"])
    show_armed = armed and not paused
    ack_text = TAP_MESSAGES.get(ack, "")
    ack_class = "ack-" + ack.lower() if ack in TAP_MESSAGES else ""
    paused_banner = '<p class="paused-banner">The ops room paused this check-in.</p>' if paused else ""
    confirm = '<input type="hidden" name="confirm" value="1">' if show_armed else ""
    disabled = "disabled" if paused else ""
    page = _MOBILE_TEMPLATE
    replacements = {
        "__CALLSIGN__": escape(state["callsign"]),
        "__SECTOR__": escape(state["sector_label"]),
        "__CADENCE__": escape(state["cadence_label"]),
        "__BADGE__": escape(state["badge"]),
        "__BADGE_CLASS__": escape(state["badge_class"]),
        "__COUNTDOWN__": escape(state["countdown"]),
        "__DUE__": escape(state["next_due_utc"] or "", quote=True),
        "__STATUS__": escape(state["status"], quote=True),
        "__PAUSED__": "true" if paused else "false",
        "__DISABLED__": disabled,
        "__SCHOOLS_NOTICE__": escape(SCHOOLS_NOTICE),
        "__ZERO_PII__": escape(ZERO_PII_NOTICE),
        "__SCHOOLS_CLASS__": "schools" if state["schools"] else "",
        "__ACTION__": escape("/api/checkin/tap/" + token, quote=True),
        "__PATH__": escape(path, quote=True),
        "__ACK__": escape(ack_text),
        "__ACK_CLASS__": ack_class,
        "__PAUSED_BANNER__": paused_banner,
        "__CONFIRM__": confirm,
        "__ARM_HIDDEN__": "hidden" if show_armed or paused else "",
        "__ARM_DISABLED__": 'aria-disabled="true" tabindex="-1"' if paused else "",
        "__PANIC_HIDDEN__": "" if show_armed else "hidden",
        "__PANIC_DISABLED__": "" if show_armed else "disabled",
        "__CANCEL_HIDDEN__": "" if show_armed else "hidden",
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def simple_page(title: str, message: str, link: str = "") -> str:
    page = _SIMPLE_TEMPLATE
    page = page.replace("__TITLE__", escape(title))
    page = page.replace("__MESSAGE__", escape(message))
    page = page.replace("__SCHOOLS_NOTICE__", escape(SCHOOLS_NOTICE))
    page = page.replace("__LINK__", link)
    return page


@checkin_bp.after_request
def _checkin_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    path = request.path or ""
    if path.startswith("/c/") or path.startswith("/api/checkin/tap/"):
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@checkin_bp.get("/check-in")
def ops_room() -> Response:
    created = request.args.get("created") == "1"
    return _html(ops_page(created=created))


@checkin_bp.get("/c/<token>")
def mobile(token: str) -> Response:
    if not valid_token(token):
        return _html(simple_page("Link not active", "This check-in link is not active."), 404)
    row = get_store().get_by_token(token)
    if row is None:
        return _html(simple_page("Link not active", "This check-in link is not active."), 404)
    armed = request.args.get("arm") == "panic"
    ack = request.args.get("ack") or ""
    if ack not in TAP_MESSAGES:
        ack = ""
    return _html(mobile_page(row, armed=armed, ack=ack))


@checkin_bp.post("/api/checkins")
def create_beacon_route() -> Response:
    form = False
    body: dict = {}
    try:
        body, form = _read_mapping()
        beacon = get_store().create_beacon(
            callsign=body.get("callsign"),
            sector=body.get("sector"),
            cadence_minutes=body.get("cadence_minutes"),
            slack_webhook_url=body.get("slack_webhook_url"),
            adult_staff_only=body.get("adult_staff_only"),
            schools_ack=body.get("schools_ack"),
        )
    except CheckinError as exc:
        if form:
            return _html(ops_page(error=str(exc), callsign=_safe_callsign_echo(body.get("callsign"))), exc.status)
        return _json({"ok": False, "message": str(exc)}, exc.status)
    if form:
        return redirect("/check-in?created=1", code=303)
    return _json({"ok": True, "beacon": beacon})


@checkin_bp.get("/api/checkins")
def list_beacons_route() -> Response:
    store = get_store()
    return _json({
        "ok": True,
        "notice": SCHOOLS_NOTICE,
        "zero_pii": ZERO_PII_NOTICE,
        "beacons": store.list_beacons(),
        "events": store.recent_events(),
    })


@checkin_bp.post("/api/checkin/tap/<token>")
def tap_route(token: str) -> Response:
    form = False
    try:
        body, form = _read_mapping()
        result = get_store().record_tap(
            token,
            body.get("event") or body.get("event_type"),
            note=body.get("note"),
            lat=body.get("lat"),
            lng=body.get("lng"),
            confirm=body.get("confirm"),
        )
        if result.event_type in {"ASSISTANCE", "PANIC"}:
            row = get_store().get_by_token(token)
            cadence = int(row["cadence_minutes"]) if row is not None else 60
            deliver_slack(
                result.webhook,
                slack_payload(
                    result.event_type,
                    callsign=result.body.get("callsign") or "",
                    sector_label=result.body.get("sector_label") or "",
                    cadence_minutes=cadence,
                    note=result.note,
                    region=result.region,
                    when=format_utc(utcnow()),
                ),
                background=True,
            )
    except CheckinError as exc:
        if form:
            back = f'<a href="/c/{escape(token, quote=True)}">Back to check-in</a>' if valid_token(token) else ""
            return _html(simple_page("Check-in not sent", str(exc), back), exc.status)
        return _json({"ok": False, "message": str(exc)}, exc.status)
    if form:
        return redirect(f"/c/{token}?ack={result.event_type}", code=303)
    return _json({"ok": True, **result.body})


@checkin_bp.post("/api/checkins/sweep-overdue")
def sweep_route() -> Response:
    try:
        count = sweep_and_alert()
    except CheckinError as exc:
        return _json({"ok": False, "message": str(exc)}, exc.status)
    return _json({"ok": True, "alerts": count})


@checkin_bp.post("/api/checkins/purge-events")
def purge_events_route() -> Response:
    try:
        deleted = get_store().purge_event_log()
    except CheckinError as exc:
        return _json({"ok": False, "message": str(exc)}, exc.status)
    return _json({"ok": True, "deleted": deleted, "retention_days": checkins.retention_days()})


@checkin_bp.post("/api/checkins/<int:beacon_id>/pause")
def pause_route(beacon_id: int) -> Response:
    try:
        beacon = get_store().pause(beacon_id)
    except CheckinError as exc:
        return _json({"ok": False, "message": str(exc)}, exc.status)
    return _json({"ok": True, "beacon": beacon})


@checkin_bp.post("/api/checkins/<int:beacon_id>/resume")
def resume_route(beacon_id: int) -> Response:
    try:
        beacon = get_store().resume(beacon_id)
    except CheckinError as exc:
        return _json({"ok": False, "message": str(exc)}, exc.status)
    return _json({"ok": True, "beacon": beacon})


@checkin_bp.delete("/api/checkins/<int:beacon_id>")
def delete_route(beacon_id: int) -> Response:
    try:
        get_store().delete(beacon_id)
    except CheckinError as exc:
        return _json({"ok": False, "message": str(exc)}, exc.status)
    return _json({"ok": True})


@checkin_bp.record_once
def _start_sweep(state) -> None:  # noqa: ARG001
    start_background()
