"""Technical and cyber-compliance controls for GeoRiskGlobal.

The module is intentionally standalone. ``app.py`` only calls ``install``.
Preview-lock behaviour is unchanged: public compliance documents are added to
its existing exemption set, and this file does not replace that module.

Personal, medical, and pupil fields on the risk-assessment page stay in the
browser. Nothing in this module reads or stores them.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from flask import Flask, Response, current_app, request
from flask.sessions import SecureCookieSessionInterface

# RFC 9116 expiry. Refresh this date before it passes.
SECURITY_TXT_EXPIRES = "2027-11-27T23:59:59.000Z"
STATEMENT_DATE = "27 November 2026"

HSTS_VALUE = "max-age=31536000; includeSubDomains"
REFERRER_POLICY = "strict-origin-when-cross-origin"
PERMISSIONS_POLICY = "camera=(), microphone=(), usb=(), payment=(), geolocation=(self)"

WINDOW_SECONDS = 60
GENERAL_LIMIT = 120
MUTATION_LIMIT = 30
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_TRACKED_IPS = 4096

PUBLIC_PATHS = frozenset({
    "/.well-known/security.txt",
    "/accessibility",
    "/trust",
    "/privacy",
    "/api/health/security",
})
RETENTION_DAYS = 30
_CALLSIGN_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,15}$")
_TAP_FIELDS = frozenset({"callsign", "lat", "lng"})
_db_lock = threading.Lock()
RATE_LIMIT_EXEMPT = frozenset({"/api/health/security"})
AUTH_COOKIE_NAMES = frozenset({
    "georisk_preview",
    "georisk_pass",
    "session",
    "sessionid",
})

_PAGE_CSS = """
.skip {
  position: absolute; left: 8px; top: -48px; z-index: 20;
  background: #D4AF37; color: #060A13; padding: 10px 14px; font-weight: 800;
  border-radius: 8px; text-decoration: none;
}
.skip:focus { top: 8px; }
.on-page { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 0; }
.on-page a { color: #F6E7B4; }
main a { color: #F6E7B4; text-underline-offset: 3px; }
main a:hover, main a:focus-visible { color: #FFFFFF; }
a:focus-visible, button:focus-visible, summary:focus-visible, .skip:focus {
  outline: 3px solid #F6E7B4; outline-offset: 2px;
}
.scroll { overflow-x: auto; }
table.compliance { width: 100%; border-collapse: collapse; }
table.compliance caption { text-align: left; font-weight: 800; color: #F6E7B4; margin: 0 0 8px; }
table.compliance th, table.compliance td { text-align: left; vertical-align: top; }
.disclaimer {
  border: 2px solid #C5A059; background: #102A63; border-radius: 12px; padding: 16px;
}
.disclaimer h2 { margin-top: 0; }
.status { font-weight: 800; white-space: nowrap; }
ol.steps, ul.clean { margin: 8px 0 0; padding-left: 1.2rem; }
ol.steps li, ul.clean li { margin: 6px 0; }
"""


def clock() -> float:
    """Monotonic clock used by the rate limiter. Tests replace this."""
    return time.monotonic()


def public_base_url() -> str:
    raw = _single_line(os.environ.get("PUBLIC_BASE_URL") or "") or "https://georiskglobal.com"
    lowered = raw.lower()
    if not lowered.startswith(("https://", "http://")) or any(char in raw for char in " \t\\"):
        return "https://georiskglobal.com"
    return raw.rstrip("/")


def security_contact() -> str:
    return _single_line(os.environ.get("SECURITY_CONTACT") or "") or "security@georiskglobal.com"


def accessibility_contact() -> str:
    return _single_line(os.environ.get("ACCESSIBILITY_CONTACT") or "") or security_contact()


def privacy_contact() -> str:
    return _single_line(os.environ.get("PRIVACY_CONTACT") or "") or "privacy@georiskglobal.com"


def request_is_https() -> bool:
    """True when the browser used HTTPS, including TLS terminated at a proxy."""
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if forwarded in {"https", "http"}:
        return forwarded == "https"
    return bool(request.is_secure)


def trust_proxy() -> bool:
    """Honour X-Forwarded-For only when a trusted edge is in front of the app.

    Cloud Run and Railway put every visitor behind one address. Without the
    forwarded client IP, those visitors would share a single rate-limit bucket.
    Direct test clients leave TRUST_PROXY unset and are keyed by REMOTE_ADDR,
    so a spoofed forwarding header cannot evade the limiter.
    """
    raw = (os.environ.get("TRUST_PROXY") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
    )


def is_embed_path(path: str) -> bool:
    path = path or "/"
    return path == "/embed" or path.startswith("/embed/")


_MAP_HOSTS = (
    "https://unpkg.com",
    "https://server.arcgisonline.com",
    "https://services.arcgisonline.com",
    "https://*.arcgisonline.com",
    "https://*.arcgis.com",
    "https://tile.openstreetmap.org",
    "https://*.tile.openstreetmap.org",
    "https://basemaps.cartocdn.com",
    "https://*.basemaps.cartocdn.com",
    "https://*.cartocdn.com",
)


def content_security_policy(path: str) -> str:
    """CSP that matches the pages already shipped.

    Inline scripts and styles stay allowed because the map, pricing page, and
    assessment builder use them. Leaflet, Esri, OpenStreetMap, Carto tiles,
    and Google Fonts are named explicitly. Framing is same-origin except on
    ``/embed/*``, which is intentionally embeddable.
    """
    ancestors = "*" if is_embed_path(path) else "'self'"
    hosts = " ".join(_MAP_HOSTS)
    return "; ".join((
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self'",
        f"frame-ancestors {ancestors}",
        "frame-src 'self'",
        "script-src 'self' 'unsafe-inline' https://unpkg.com",
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        f"img-src 'self' data: blob: {hosts}",
        f"connect-src 'self' {hosts}",
        "worker-src 'self'",
        "manifest-src 'self'",
    ))


def rate_limiting_enabled(app: Flask | None = None) -> bool:
    """Off during ``app.testing`` unless SECURITY_RATE_LIMIT_FORCE is set.

    Existing tests construct the real app and set ``testing`` so a shared
    process cannot trip a 429. Set the config flag in a test that wants the
    limiter on, or leave testing false in production.
    """
    app = app or current_app
    if app.config.get("SECURITY_RATE_LIMIT_FORCE"):
        return True
    if app.testing or bool(app.config.get("TESTING")):
        return False
    raw = (os.environ.get("SECURITY_RATE_LIMIT") or "on").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def preview_lock_active() -> bool:
    try:
        import preview_lock
    except ImportError:
        return False
    return bool(preview_lock.lock_enabled())


def security_posture(app: Flask | None = None) -> dict:
    """Non-sensitive booleans. No secrets, counts, or addresses."""
    app = app or current_app
    return {
        "ok": True,
        "hsts_enabled": True,
        "csp_enabled": True,
        "rate_limiting_enabled": rate_limiting_enabled(app),
        "zero_pii_ra_mode": True,
        "preview_lock_active": preview_lock_active(),
        "security_txt_present": _rule_exists(app, "/.well-known/security.txt"),
        "wcag_statement_present": _rule_exists(app, "/accessibility"),
    }


def security_txt() -> str:
    base = public_base_url()
    lines = [
        "# GeoRiskGlobal vulnerability disclosure",
        "# Do not include personal, medical, or pupil data in a report.",
        f"Contact: {_mailto(security_contact())}",
        f"Expires: {SECURITY_TXT_EXPIRES}",
        "Preferred-Languages: en",
        f"Canonical: {base}/.well-known/security.txt",
        f"Policy: {base}/trust",
        f"Acknowledgments: {base}/trust",
        "",
    ]
    return "\n".join(lines)


def harden_cookie_header(header_value: str, *, secure: bool, names: frozenset[str] | set[str] | None = None) -> str:
    """Ensure an auth or session cookie is HttpOnly, SameSite=Lax, and Secure on HTTPS."""
    if not header_value or "=" not in header_value:
        return header_value
    parts = [part.strip() for part in header_value.split(";")]
    pair, attrs = parts[0], parts[1:]
    name = pair.split("=", 1)[0].strip().lower()
    allowed = {item.lower() for item in (names or AUTH_COOKIE_NAMES)}
    if name not in allowed:
        return header_value
    kept: list[str] = []
    saw_httponly = False
    saw_secure = False
    saw_samesite = False
    for attr in attrs:
        if not attr:
            continue
        lowered = attr.lower()
        if lowered == "httponly":
            saw_httponly = True
            kept.append("HttpOnly")
            continue
        if lowered == "secure":
            saw_secure = True
            kept.append("Secure")
            continue
        if lowered.startswith("samesite="):
            saw_samesite = True
            value = attr.split("=", 1)[1].strip().lower()
            kept.append("SameSite=Lax" if value in {"", "none"} else f"SameSite={attr.split('=', 1)[1].strip()}")
            continue
        kept.append(attr)
    if not saw_httponly:
        kept.append("HttpOnly")
    if not saw_samesite:
        kept.append("SameSite=Lax")
    if secure and not saw_secure:
        kept.append("Secure")
    return "; ".join([pair, *kept])


def set_auth_cookie(response: Response, name: str, value: str, *, max_age: int | None = None, path: str = "/") -> Response:
    """Set an auth cookie with the hardened flags for this request."""
    secure = False
    try:
        secure = request_is_https()
    except RuntimeError:
        secure = False
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="Lax",
        secure=secure,
        path=path,
    )
    return response


def apply_auth_cookie_policy(response: Response, *, secure: bool | None = None) -> Response:
    """Rewrite auth and session Set-Cookie headers already on the response."""
    if secure is None:
        try:
            secure = request_is_https()
        except RuntimeError:
            secure = False
    raw = response.headers.getlist("Set-Cookie")
    if not raw:
        return response
    names = set(AUTH_COOKIE_NAMES)
    try:
        custom = (current_app.config.get("SESSION_COOKIE_NAME") or "").strip().lower()
    except RuntimeError:
        custom = ""
    if custom:
        names.add(custom)
    updated = [harden_cookie_header(item, secure=bool(secure), names=names) for item in raw]
    if updated == list(raw):
        return response
    from werkzeug.datastructures import Headers

    rebuilt = Headers()
    for key, value in response.headers:
        if key.lower() != "set-cookie":
            rebuilt.add(key, value)
    for value in updated:
        rebuilt.add("Set-Cookie", value)
    response.headers = rebuilt
    return response


class _LimiterState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buckets: dict[str, dict[str, deque[float] | float]] = {}


class HardenedSessionInterface(SecureCookieSessionInterface):
    """Thread-safe cookie flags. Secure follows the current request, not a global."""

    def get_cookie_httponly(self, app: Flask) -> bool:
        return True

    def get_cookie_samesite(self, app: Flask) -> str:
        configured = app.config.get("SESSION_COOKIE_SAMESITE")
        if configured is None or str(configured).strip().lower() in {"", "none"}:
            return "Lax"
        return str(configured)

    def get_cookie_secure(self, app: Flask) -> bool:
        try:
            if request_is_https():
                return True
        except RuntimeError:
            pass
        return bool(app.config.get("SESSION_COOKIE_SECURE"))


def install(app: Flask) -> None:
    """Attach headers, the rate limiter, and the public compliance routes."""
    if app.extensions.get("security_compliance_installed"):
        return
    app.extensions["security_compliance_installed"] = True
    app.extensions["security_compliance"] = _LimiterState()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if type(app.session_interface) is SecureCookieSessionInterface:
        app.session_interface = HardenedSessionInterface()
    _expose_public_paths()

    @app.before_request
    def _security_before_request():
        limited = _rate_limit_response(app)
        if limited is not None:
            return limited
        return None

    @app.after_request
    def _security_headers(response: Response) -> Response:
        try:
            path = request.path or "/"
            https = request_is_https()
        except RuntimeError:
            path = "/"
            https = False
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        if is_embed_path(path):
            # /embed/widget publishes its own policy: script-src 'none',
            # img-src https: for a client logo, frame-ancestors *, and
            # Referrer-Policy no-referrer. Leave that policy in place.
            if "X-Frame-Options" in response.headers:
                del response.headers["X-Frame-Options"]
            existing = response.headers.get("Content-Security-Policy") or ""
            if "frame-ancestors *" not in existing:
                response.headers["Content-Security-Policy"] = content_security_policy(path)
            if not (response.headers.get("Referrer-Policy") or "").strip():
                response.headers["Referrer-Policy"] = REFERRER_POLICY
        else:
            response.headers["Referrer-Policy"] = REFERRER_POLICY
            response.headers["Content-Security-Policy"] = content_security_policy(path)
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if https:
            response.headers["Strict-Transport-Security"] = HSTS_VALUE
        elif "Strict-Transport-Security" in response.headers:
            del response.headers["Strict-Transport-Security"]
        return apply_auth_cookie_policy(response, secure=https)

    @app.get("/.well-known/security.txt")
    def well_known_security_txt() -> Response:
        response = Response(security_txt(), mimetype="text/plain")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/accessibility")
    def accessibility_statement() -> Response:
        return _html_page(_accessibility_document())

    @app.get("/trust")
    def trust_centre() -> Response:
        return _html_page(_trust_document())

    @app.get("/privacy")
    def privacy_notice() -> Response:
        purge_pseudonymous_taps()
        return _html_page(_privacy_document())

    @app.post("/api/privacy/check-ins")
    def privacy_check_in() -> Response:
        payload = request.get_json(silent=True)
        ok, message, tap = validate_pseudonymous_tap(payload)
        if not ok:
            response = Response(
                json.dumps({"ok": False, "message": message}),
                status=400,
                mimetype="application/json",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        record_pseudonymous_tap(tap)
        body = {"ok": True, "retention_days": RETENTION_DAYS, "stored": tap}
        response = Response(json.dumps(body), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/privacy/erase")
    def privacy_erase() -> Response:
        if not erase_authorised():
            response = Response(
                json.dumps({"ok": False, "message": "An administrator session or erasure token is required."}),
                status=403,
                mimetype="application/json",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        payload = request.get_json(silent=True) or {}
        callsign = ""
        if isinstance(payload, dict):
            callsign = str(payload.get("callsign") or "")
        deleted = erase_pseudonymous_taps(callsign)
        response = Response(
            json.dumps({"ok": True, "deleted": deleted}),
            mimetype="application/json",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health/security")
    def security_health() -> Response:
        response = Response(
            json.dumps(security_posture(app), ensure_ascii=False),
            mimetype="application/json",
        )
        response.headers["Cache-Control"] = "no-store"
        return response


def _expose_public_paths() -> None:
    """Let the preview lock serve disclosure and accessibility documents."""
    try:
        import preview_lock
    except ImportError:
        return
    exempt = getattr(preview_lock, "EXEMPT", None)
    if isinstance(exempt, set):
        exempt.update(PUBLIC_PATHS)
        return
    if exempt is not None:
        preview_lock.EXEMPT = set(exempt).union(PUBLIC_PATHS)


def pseudonymous_db_path() -> Path:
    from analytics_store import prefer_writable_file

    configured = _single_line(os.environ.get("PSEUDONYMOUS_DB") or "")
    if configured:
        return prefer_writable_file(Path(configured))
    try:
        override = current_app.config.get("PSEUDONYMOUS_DB")
    except RuntimeError:
        override = ""
    if override:
        return prefer_writable_file(Path(str(override)))
    return prefer_writable_file(Path(__file__).resolve().parent / "data" / "pseudonymous_ops.sqlite")


def round_coordinate(value: object) -> float:
    """Coarsen a coordinate to one decimal degree. About 11 km."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("location must be numeric")
    number = Decimal(str(value).strip())
    return float(number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def validate_pseudonymous_tap(payload: object) -> tuple[bool, str, dict]:
    """Accept only an opaque callsign and a coarse location. Reject every other field."""
    if not isinstance(payload, dict):
        return False, "Send a callsign and a location only.", {}
    if set(payload) - _TAP_FIELDS:
        return False, "Names, passport, medical details, and sign-offs are not accepted.", {}
    callsign = str(payload.get("callsign") or "").strip().upper()
    if not _CALLSIGN_RE.fullmatch(callsign):
        return False, "Use an opaque callsign, not a name, email, or document number.", {}
    try:
        lat = round_coordinate(payload.get("lat"))
        lng = round_coordinate(payload.get("lng"))
    except (ValueError, ArithmeticError):
        return False, "Location must be a decimal latitude and longitude.", {}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False, "Location is out of range.", {}
    return True, "", {"callsign": callsign, "lat": lat, "lng": lng}


def record_pseudonymous_tap(tap: dict, *, now: float | None = None) -> None:
    moment = time.time() if now is None else now
    path = pseudonymous_db_path()
    with _db_lock:
        conn = _open_taps(path)
        try:
            _purge_conn(conn, moment)
            conn.execute(
                "INSERT INTO taps (callsign, lat, lng, created_at) VALUES (?, ?, ?, ?)",
                (tap["callsign"], tap["lat"], tap["lng"], moment),
            )
            conn.commit()
        finally:
            conn.close()


def purge_pseudonymous_taps(*, now: float | None = None) -> int:
    path = pseudonymous_db_path()
    if not path.exists():
        return 0
    moment = time.time() if now is None else now
    with _db_lock:
        conn = _open_taps(path)
        try:
            deleted = _purge_conn(conn, moment)
            conn.commit()
            return deleted
        finally:
            conn.close()


def erase_pseudonymous_taps(callsign: str = "") -> int:
    path = pseudonymous_db_path()
    if not path.exists():
        return 0
    target = callsign.strip().upper()
    with _db_lock:
        conn = _open_taps(path)
        try:
            if target:
                cursor = conn.execute("DELETE FROM taps WHERE callsign = ?", (target,))
            else:
                cursor = conn.execute("DELETE FROM taps")
            conn.commit()
            return int(cursor.rowcount)
        finally:
            conn.close()


def erase_authorised() -> bool:
    """Preview session, or a dedicated erasure token when the preview lock is off."""
    token = os.environ.get("PRIVACY_ERASE_TOKEN") or ""
    header = request.headers.get("Authorization") or ""
    if token and header.lower().startswith("bearer "):
        supplied = header.split(" ", 1)[1].strip()
        if hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            return True
    try:
        import preview_lock
    except ImportError:
        return False
    if not preview_lock.lock_enabled():
        return False
    if preview_lock.token_is_valid(request.cookies.get(preview_lock.COOKIE, "")):
        return True
    return preview_lock.basic_auth_ok(header)


def _open_taps(path: Path) -> sqlite3.Connection:
    from analytics_store import prefer_writable_file

    path = prefer_writable_file(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS taps ("
        "id INTEGER PRIMARY KEY, callsign TEXT NOT NULL, lat REAL NOT NULL, "
        "lng REAL NOT NULL, created_at REAL NOT NULL)"
    )
    return conn


def _purge_conn(conn: sqlite3.Connection, now: float) -> int:
    cutoff = now - (RETENTION_DAYS * 24 * 60 * 60)
    cursor = conn.execute("DELETE FROM taps WHERE created_at <= ?", (cutoff,))
    return int(cursor.rowcount)


def _html_page(document: str) -> Response:
    return Response(document, mimetype="text/html; charset=utf-8")


def _rate_limit_response(app: Flask) -> Response | None:
    if not rate_limiting_enabled(app):
        return None
    path = request.path or "/"
    if path in RATE_LIMIT_EXEMPT:
        return None
    if not (path == "/api" or path.startswith("/api/")):
        return None
    if request.method == "OPTIONS":
        return None
    ip = _client_ip()
    now = clock()
    mutation = request.method in MUTATION_METHODS
    state: _LimiterState = app.extensions["security_compliance"]
    with state.lock:
        _evict_if_needed(state, ip, now)
        bucket = state.buckets.setdefault(ip, {"general": deque(), "mutation": deque(), "seen": now})
        bucket["seen"] = now
        general: deque[float] = bucket["general"]  # type: ignore[assignment]
        mutations: deque[float] = bucket["mutation"]  # type: ignore[assignment]
        retry = _retry_after(general, GENERAL_LIMIT, now)
        if mutation:
            mutation_retry = _retry_after(mutations, MUTATION_LIMIT, now)
            if mutation_retry:
                retry = max(retry, mutation_retry)
        if retry:
            return _too_many(retry)
        general.append(now)
        if mutation:
            mutations.append(now)
    return None


def _retry_after(bucket: deque[float], limit: int, now: float) -> int:
    cutoff = now - WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) < limit:
        return 0
    wait = bucket[0] + WINDOW_SECONDS - now
    return max(1, int(wait + 0.999))


def _too_many(retry_after: int) -> Response:
    body = {
        "ok": False,
        "error": "too_many_requests",
        "message": "Too many requests. Retry after the indicated number of seconds.",
    }
    response = Response(json.dumps(body), status=429, mimetype="application/json")
    response.headers["Retry-After"] = str(retry_after)
    response.headers["Cache-Control"] = "no-store"
    return response


def _client_ip() -> str:
    if trust_proxy():
        forwarded = request.headers.get("X-Forwarded-For", "")
        hop = forwarded.split(",")[0].strip()
        if hop:
            return hop[:128]
    return (request.remote_addr or "unknown")[:128]


def _evict_if_needed(state: _LimiterState, ip: str, now: float) -> None:
    if len(state.buckets) < MAX_TRACKED_IPS or ip in state.buckets:
        return
    cutoff = now - WINDOW_SECONDS
    stale = [
        key for key, bucket in state.buckets.items()
        if float(bucket.get("seen") or 0) < cutoff
    ]
    for key in stale:
        state.buckets.pop(key, None)
    if len(state.buckets) >= MAX_TRACKED_IPS:
        oldest = min(state.buckets, key=lambda key: float(state.buckets[key].get("seen") or 0))
        state.buckets.pop(oldest, None)


def _rule_exists(app: Flask, path: str) -> bool:
    return any(rule.rule == path for rule in app.url_map.iter_rules())


def _single_line(value: str | None) -> str:
    return (value or "").replace("\r", "").replace("\n", "").strip()


def _mailto(value: str) -> str:
    if value.lower().startswith(("mailto:", "https://", "http://", "tel:")):
        return value
    return "mailto:" + value


def _contact_label(value: str) -> str:
    if value.lower().startswith("mailto:"):
        return value[7:]
    return value


def _contact_link(value: str) -> str:
    href = html.escape(_mailto(value), quote=True)
    label = html.escape(_contact_label(_mailto(value)))
    return f'<a href="{href}">{label}</a>'


def _nav_link(href: str, label: str, current: str) -> str:
    current_attr = ' aria-current="page"' if href == current else ""
    return f'<a href="{href}"{current_attr}>{html.escape(label)}</a>'


def _shell(*, title: str, kicker: str, heading: str, lede: str, body: str, current: str) -> str:
    primary = (
        ("/map", "Live Map"),
        ("/risk-assessments", "Risk Assessments"),
        ("/route-risk", "Route risk"),
        ("/check-in", "Check-in"),
        ("/pricing", "Pricing"),
    )
    tools = (
        ("/reports", "Reports"),
        ("/sentinel-zones", "Sentinel zones"),
        ("/slack-alerts", "Slack alerts"),
        ("/white-label", "White-label"),
        ("/grok-ops", "Grok Ops"),
        ("/briefing", "Daily briefing"),
        ("/world-risk", "World risk"),
        ("/countries", "Country guides"),
        ("/enterprise", "Enterprise"),
        ("/trust", "Trust"),
        ("/privacy", "Privacy"),
        ("/accessibility", "Accessibility"),
    )
    nav = "".join(_nav_link(href, label, current) for href, label in primary)
    menu = "".join(_nav_link(href, label, current) for href, label in tools)
    security = _contact_link(security_contact())
    privacy = _contact_link(privacy_contact())
    accessibility = _contact_link(accessibility_contact())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="stylesheet" href="/static/platform.css">
  <link rel="stylesheet" href="/static/site-nav.css">
  <script src="/static/site-nav.js" defer></script>
  <style>{_PAGE_CSS}</style>
</head>
<body>
  <a class="skip" href="#main">Skip to content</a>
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
        <a href="/">Home</a>
        <a href="/map">Live Map</a>
        <a href="/briefing">Daily Briefings</a>
        <a href="/pricing">Pricing</a>
        <a href="/methodology">Methodology</a>
        <a href="/contact">Contact</a>
        <a class="nav-cta nav-cta-drawer" href="/map">Open Live Map</a>
      </nav>
      <a class="nav-cta nav-cta-bar" href="/map">Open Live Map</a>
</div>
  </header>
  <div class="section-banner">
    <p class="kicker">{html.escape(kicker)}</p>
    <h1>{html.escape(heading)}</h1>
    <p class="page-lead">{lede}</p>
  </div>
  <main id="main" tabindex="-1">
    {body}
  </main>
  <footer>
    <p>GeoRiskGlobal · <a href="/trust">Trust</a> · <a href="/privacy">Privacy</a> · <a href="/accessibility">Accessibility</a> · <a href="/.well-known/security.txt">security.txt</a></p>
    <p>Vulnerability reports: {security}. Data-subject requests: {privacy}. Accessibility barriers: {accessibility}.</p>
  </footer>
<footer class="site-footer">
  <div class="site-footer-grid">
    <div>
      <p class="site-footer-label">GeoRiskGlobal</p>
      <p>Worldwide geopolitical intelligence for organisations that move people.</p>
    </div>
    <nav aria-label="Explore">
      <p class="site-footer-label">Explore</p>
      <a href="/">Home</a>
      <a href="/map">Live Map</a>
      <a href="/map?country=Syria">Syria on the Risk Map</a>
      <a href="/briefing">Daily Briefings</a>
      <a href="/briefing#export-pdf">Briefing PDF</a>
      <a href="/pricing">Pricing</a>
      <a href="/risk-assessments">Risk assessments</a>
      <a href="/methodology">Methodology</a>
      <a href="/contact">Contact</a>
    </nav>
    <nav aria-label="Platform">
      <p class="site-footer-label">Platform</p>
      <a href="/countries">Country guides</a>
      <a href="/world-risk">World risk</a>
      <a href="/route-risk">Route risk</a>
      <a href="/check-in">Check-in</a>
      <a href="/reports">Reports</a>
      <a href="/sentinel-zones">Sentinel zones</a>
      <a href="/slack-alerts">Slack alerts</a>
      <a href="/white-label">White-label</a>
      <a href="/grok-ops">Grok Ops</a>
      <a href="/sso-setup">Okta SSO</a>
      <a href="/enterprise">Book a Pilot / Subscribe</a>
      <a href="/trust">Trust</a>
      <a href="/privacy">Privacy</a>
      <a href="/accessibility">Accessibility</a>
    </nav>
    <div>
      <p class="site-footer-label">Standards</p>
      <p>ISO 31000 · ISO 31030 · BS 8848 · ISO 18788</p>
      <p>Zero-PII. No third-party tracking. UK GDPR and EU GDPR.</p>
    </div>
  </div>
  <p class="site-footer-powered">Powered by GeoRiskGlobal</p>
</footer>

</body>
</html>
"""


def _rows(rows: tuple[tuple[str, str, str], ...]) -> str:
    rendered = []
    for framework, status, detail in rows:
        rendered.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(framework)}</th>"
            f"<td class=\"status\">{html.escape(status)}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    return "".join(rendered)


_FRAMEWORK_ROWS = (
    (
        "UK Cyber Essentials",
        "In place",
        "Boundary protection, secure configuration, access control, and update management are implemented in the application. A Cyber Essentials certificate is a separate procurement step and is not claimed here.",
    ),
    (
        "NCSC 14 Cloud Security Principles",
        "In place",
        "The fourteen principles are mapped below. Items that belong to the cloud tenant or the customer are marked Shared. Formal audit packaging is marked Roadmap.",
    ),
    (
        "ISO 31000 / ISO 31030 / BS 8848",
        "In place",
        "The risk-assessment builder scores travel, medical-context, school, and close-protection hazards against these standards. Scores are decision support entered by a person.",
    ),
    (
        "EU AI Act",
        "In place",
        "Grok drafts are human-in-the-loop. The analyst accepts, edits, or discards them. The product does not make an automated decision with legal or similarly significant effect.",
    ),
    (
        "WCAG 2.2 AA",
        "Partial",
        "Information pages and the assessment forms are built for WCAG 2.2 Level AA. The live map canvas has known limitations, described in the accessibility statement.",
    ),
    (
        "ISO 27001 / SOC 2 Type II",
        "Roadmap",
        "Not certified. The controls on this page are the technical baseline a future certification audit would test.",
    ),
    (
        "EU DORA",
        "Readiness",
        "ICT third-party dependencies are named: the chosen Google Cloud region, and optional server-side model calls. Personal data is not part of the model dependency.",
    ),
    (
        "UK / EU / US data residency",
        "In place",
        "A tenant deploys to Google Cloud London (europe-west2) or Northern Virginia (us-east4). The application does not copy its SQLite database to another region.",
    ),
    (
        "UK GDPR and EU GDPR",
        "In place",
        "The client organisation is the data controller. GeoRiskGlobal is the processor. Assessment identity data stays in the browser. Roles, retention, subprocessors, the Children's Code, and breach notice are set out on the privacy page.",
    ),
)

_NCSC_ROWS = (
    ("1. Data in transit protection", "In place", "TLS is terminated at the cloud edge, including TLS 1.3. The app sends HSTS for one year, including subdomains, when the request is HTTPS."),
    ("2. Asset protection and resilience", "In place", "Incident data sits in a SQLite file on a deployment volume. Personal assessment fields are not in that file."),
    ("3. Separation between consumers", "In place", "The service does not run a multi-tenant store of traveller, medical, or pupil records."),
    ("4. Governance framework", "In place", "This centre, the accessibility statement, and RFC 9116 security.txt are the public governance documents."),
    ("5. Operational security", "In place", "Security headers, auth-cookie flags, API rate limits, and environment-only secrets are on in this deployment."),
    ("6. Personnel security", "Shared", "Hiring and vetting sit with the customer and the cloud provider. The application has no staff directory."),
    ("7. Secure development", "In place", "Dependencies are pinned. Header, rate-limit, disclosure, and accessibility behaviour is covered by automated tests."),
    ("8. Supply chain security", "In place", "Python dependencies are pinned to current patched releases. The cloud region is chosen per tenant and written down."),
    ("9. Secure consumer management", "Readiness", "Preview access is an environment password. Enterprise SSO is ready for an Okta tenant in front of the app."),
    ("10. Identity and authentication", "Readiness", "Session and auth cookies are HttpOnly and SameSite=Lax, and Secure on HTTPS, so an Okta OIDC front door can issue them."),
    ("11. External interface protection", "In place", "Content-Security-Policy, frame controls, and per-IP API rate limits protect the public interface."),
    ("12. Secure service administration", "Shared", "Cloud IAM administers the project. Secrets are not stored in the repository."),
    ("13. Audit information for consumers", "Roadmap", "This page is the customer-facing summary. A formal evidence pack is not yet issued."),
    ("14. Secure use of the service", "In place", "Assessments tell the user which fields stay on the device. This page states what the service is not."),
)


def _trust_document() -> str:
    security = _contact_link(security_contact())
    body = f"""
    <nav class="on-page" aria-label="On this page">
      <a href="#zero-pii">Zero-PII architecture</a>
      <a href="#gdpr">UK and EU GDPR</a>
      <a href="#controls">Technical controls</a>
      <a href="#alignment">Compliance matrix</a>
      <a href="#disclaimer">Decision-support disclaimer</a>
    </nav>
    <section class="panel" id="zero-pii">
      <h2>Zero-PII client-side architecture</h2>
      <p>Personal, medical, and pupil data never has to reach a GeoRiskGlobal server for an assessment to be written.</p>
      <ol class="steps">
        <li><strong>On the device.</strong> Traveller or party names, emergency contacts, medical notes, allergies, medication, passport or ID references, and pupil details are local-only fields on the risk-assessment page. The browser keeps that draft. Auto-Draft does not put those fields in the request.</li>
        <li><strong>What is sent.</strong> A mitigation draft sends the destination, the template name, and the hazard scores the analyst entered, plus incident titles already on the map. It does not send the local-only fields.</li>
        <li><strong>A second strip.</strong> Before any model call, the server drops personal and medical keys and redacts emails, phone numbers, and identity-document patterns. The model key stays in the server environment and is not written into the page.</li>
      </ol>
      <p class="note">The read-only check at <a href="/api/health/security">/api/health/security</a> returns booleans only. It does not return secrets, drafts, or visitor data. <code>zero_pii_ra_mode</code> is true because this architecture is how the assessment builder works, not because a switch was turned on for the request.</p>
      <p>The full notice is the <a href="/privacy">privacy page</a>. It is also linked from the footer.</p>
    </section>
    <section class="panel" id="gdpr">
      <h2>UK GDPR and EU GDPR</h2>
      <p>For traveller, medical, pupil, and risk-assessment data, the <strong>client organisation is the Data Controller</strong>. <strong>GeoRiskGlobal is the Processor.</strong> The architecture is zero-PII and local-first. Traveller names, passport and medical details, and reviewer sign-offs stay in the user's browser <code>localStorage</code> (draft key <code>georisk.ra.v1</code>) and are not transmitted to GeoRiskGlobal servers.</p>
      <p><strong>Pseudonymised operations.</strong> UK and EU GDPR Article 4(5), Article 5(1)(e), and Article 17: an operational check-in is an opaque callsign and a location rounded to one decimal degree. Those records auto-purge after 30 days. An administrator can erase them immediately. There is no analytics cookie. The only cookies are strictly necessary session and preview cookies, <code>HttpOnly</code> and <code>SameSite=Lax</code>.</p>
      <p><strong>Subprocessors.</strong> Hosting is Railway by default, or Google Cloud London <code>europe-west2</code> when a UK or EU tenant needs in-country residency. xAI Grok is called only when someone clicks an AI draft, and that call does not include personal data. Public OSRM is used only for anonymous route coordinates on <code>/route-risk</code>. Slack webhooks and Okta OIDC are optional and customer-configured. If they are not configured, they are not called.</p>
      <p><strong>ICO Children's Code.</strong> School and university features are for adult Trip Leaders and staff only. They are never for pupils or minors. Do not make a pupil an account holder.</p>
      <p><strong>Rights and breaches.</strong> Data-subject requests, including access and erasure, go to the privacy contact on the <a href="/privacy">privacy page</a>. GeoRiskGlobal will tell the controller without undue delay, and within the time the controller needs to notify the UK ICO or the relevant EU supervisory authority inside 72 hours of becoming aware of a notifiable breach.</p>
    </section>
    <section class="panel" id="controls">
      <h2>Technical controls live today</h2>
      <div class="cards">
        <article class="card"><h3>TLS 1.3 and HSTS</h3><p>The hosting edge terminates TLS, including TLS 1.3. On HTTPS, or when <code>X-Forwarded-Proto</code> is <code>https</code>, the response sends <code>Strict-Transport-Security: max-age=31536000; includeSubDomains</code>.</p></article>
        <article class="card"><h3>Content-Security-Policy</h3><p>Pages allow the Leaflet, Esri, OpenStreetMap, and Carto tile hosts already used by the map, Google Fonts, and the inline scripts the current pages ship. Other framing is limited to this origin. <code>/embed/</code> routes stay embeddable.</p></article>
        <article class="card"><h3>HttpOnly and SameSite cookies</h3><p>Preview, pass, and session cookies are HttpOnly and <code>SameSite=Lax</code>. <code>Secure</code> is added when the request arrived over HTTPS. JavaScript on the page cannot read them.</p></article>
        <article class="card"><h3>API rate limiting</h3><p>Each IP address gets a sliding window of {GENERAL_LIMIT} API requests a minute, and {MUTATION_LIMIT} writes a minute for POST, PUT, PATCH, and DELETE. The 429 response includes <code>Retry-After</code>. The limiter is off while the app is in test mode so the suite does not flake.</p></article>
        <article class="card"><h3>Masked secrets</h3><p>Model, mail, preview, and pass credentials are read from the environment. They are not embedded in HTML, JavaScript, or the security health response.</p></article>
        <article class="card"><h3>Isolated SQLite volume</h3><p>The incident database is a SQLite file on the deployment data directory or a mounted volume. It is separate from the browser store used for personal fields, and a tenant deploy does not replicate it to another region.</p></article>
        <article class="card"><h3>Okta SSO readiness</h3><p>The app does not store a second copy of a directory password. Cookie flags match what an Okta OpenID Connect front door expects. Connecting a tenant is an enterprise deployment step; Okta is not bundled in this process.</p></article>
        <article class="card"><h3>RFC 9116 security.txt</h3><p>Vulnerability reports go to {security}. The policy file is <a href="/.well-known/security.txt">/.well-known/security.txt</a> and it names this page.</p></article>
      </div>
    </section>
    <section class="panel" id="alignment">
      <h2>Compliance alignment matrix</h2>
      <p class="note">“In place” means the control runs in this deployment. “Roadmap” means it is not certified or not packaged yet. Nothing in this table is a certificate.</p>
      <div class="scroll">
        <table class="compliance">
          <caption>Framework alignment</caption>
          <thead><tr><th scope="col">Framework</th><th scope="col">Status</th><th scope="col">How this deployment lines up</th></tr></thead>
          <tbody>{_rows(_FRAMEWORK_ROWS)}</tbody>
        </table>
      </div>
      <div class="scroll">
        <table class="compliance">
          <caption>NCSC Cloud Security Principles</caption>
          <thead><tr><th scope="col">Principle</th><th scope="col">Status</th><th scope="col">This deployment</th></tr></thead>
          <tbody>{_rows(_NCSC_ROWS)}</tbody>
        </table>
      </div>
      <p>UK and EU public-sector tenants deploy to Google Cloud London, region <code>europe-west2</code>. US tenants deploy to Northern Virginia, region <code>us-east4</code>. The commands are in <code>docs/GCP_UK_RESIDENCY.md</code> in the repository.</p>
      <p>The accessibility commitment, including keyboard use, contrast, and screen readers, is the <a href="/accessibility">WCAG 2.2 AA accessibility statement</a>.</p>
    </section>
    <section class="panel disclaimer" id="disclaimer">
      <h2>Decision-support and non-medical-device disclaimer</h2>
      <p>GeoRiskGlobal is decision-support software for security and travel-risk planning. It is not a medical device. It does not provide a diagnosis, a treatment, or a clinical measurement, and it is not a substitute for a qualified medic, clinical judgement, or a government travel advisory.</p>
      <p>Risk scores and Grok drafts are inputs to a human decision. A person chooses the score, reads the draft, and decides what to do. The service does not make an automated decision with legal or similarly significant effect, and it does not carry a medical-device conformity claim under the UK Medical Devices Regulations or the EU Medical Devices Regulation.</p>
      <p>Open-source incident reports can be incomplete, contested, or duplicated. Confirm conditions with the responsible authority before travel or deployment.</p>
    </section>
    """
    return _shell(
        title="Trust & Cyber Compliance — GeoRiskGlobal",
        kicker="Trust",
        heading="GeoRiskGlobal Trust & Cyber Compliance Center",
        lede="How the service handles data, which controls are on today, and what the product is not.",
        body=body,
        current="/trust",
    )


def _privacy_document() -> str:
    privacy = _contact_link(privacy_contact())
    body = f"""
    <nav class="on-page" aria-label="On this page">
      <a href="#roles">Controller and processor</a>
      <a href="#local">Local-first assessments</a>
      <a href="#pseudonymous">Pseudonymous data</a>
      <a href="#cookies">Cookies</a>
      <a href="#subprocessors">Subprocessors</a>
      <a href="#children">Children's Code</a>
      <a href="#rights">Your rights and breaches</a>
    </nav>
    <section class="panel" id="roles">
      <h2>UK GDPR and EU GDPR roles</h2>
      <p>This notice covers the United Kingdom General Data Protection Regulation and the EU General Data Protection Regulation.</p>
      <p>For staff, traveller, pupil, and risk-assessment information, the <strong>client organisation is the Data Controller</strong>. It decides why that information is needed and how long the organisation keeps its own copy. <strong>GeoRiskGlobal is the Processor.</strong> It hosts the map and the assessment builder for the controller and does not decide the purpose of a client's travel-risk file.</p>
      <p>GeoRiskGlobal is the controller only for the small amount of information it collects for its own operations: an enterprise enquiry the sender chooses to submit, and short-lived abuse-protection counters. Those are described below. They are not the assessment file.</p>
    </section>
    <section class="panel" id="local">
      <h2>Zero-PII local-first architecture</h2>
      <p>Traveller names, passport and medical details, emergency contacts, and risk-assessment reviewer sign-offs stay strictly in the user's browser. The assessment draft is stored in <code>localStorage</code> under the key <code>georisk.ra.v1</code>. Those fields are marked local-only. They are not included in Auto-Draft, and they are not written to the GeoRiskGlobal server.</p>
      <p>Clearing the local draft in the browser deletes that copy. GeoRiskGlobal cannot retrieve it, because it was never sent.</p>
    </section>
    <section class="panel" id="pseudonymous">
      <h2>Pseudonymised data and storage limitation</h2>
      <p>Where an operational check-in is recorded, it is pseudonymous under <strong>Article 4(5)</strong>. The record is an opaque callsign and a location rounded to one decimal degree (about 11 km). It is not a name, a passport number, a medical note, or a reviewer sign-off. A request that includes any of those fields is refused and nothing is stored.</p>
      <p>Storage is limited under <strong>Article 5(1)(e)</strong>. Check-in records auto-purge after <strong>{RETENTION_DAYS} days</strong>. Opening this notice, recording a new tap, or running erasure deletes anything older than that.</p>
      <p>Field staff use <a href="/check-in">check-in</a>. The ops room issues an opaque callsign. The phone stores a coarse location only when the adult staff member turns location on. <strong>Article 17</strong> erasure is immediate: an administrator can delete one callsign or the whole check-in store, including <code>POST /api/privacy/erase</code>. The response is a count, not the records.</p>
    </section>
    <section class="panel" id="cookies">
      <h2>Cookies</h2>
      <p>GeoRiskGlobal does not use analytics or advertising cookies. The only cookies are strictly necessary:</p>
      <ul class="clean">
        <li><code>georisk_preview</code> keeps a preview sign-in.</li>
        <li><code>georisk_pass</code> keeps a single-area pass the viewer opened.</li>
        <li>A session cookie, if the server starts one.</li>
      </ul>
      <p>Each of those is <code>HttpOnly</code> and <code>SameSite=Lax</code>. <code>Secure</code> is set when the page was served over HTTPS. JavaScript on the page cannot read them. They are not shared with an advertising network.</p>
      <p>Abuse protection keeps an IP address in memory for about a minute so the API rate limit can work. That counter is not written into the assessment database and is not a usage profile.</p>
    </section>
    <section class="panel" id="subprocessors">
      <h2>Subprocessor and external network call register</h2>
      <p>A service below is called only in the case described. If that case does not happen on a deployment, that party receives nothing from it.</p>
      <div class="scroll">
        <table class="compliance">
          <caption>Who is called, and with what</caption>
          <thead><tr><th scope="col">Party</th><th scope="col">When</th><th scope="col">What leaves the server</th></tr></thead>
          <tbody>
            <tr><th scope="row">Railway</th><td>Default host of this application.</td><td>The incident database and strictly necessary cookies. Not the local assessment draft.</td></tr>
            <tr><th scope="row">Google Cloud, London (<code>europe-west2</code>)</th><td>UK and EU tenants that deploy for in-country residency.</td><td>The same application data, kept in London. The app does not copy the database to another region.</td></tr>
            <tr><th scope="row">Google Cloud, Northern Virginia (<code>us-east4</code>)</th><td>US tenants only.</td><td>The same application data, in that US region. This is not the UK or EU default.</td></tr>
            <tr><th scope="row">xAI Grok API</th><td>Only when a person clicks an AI draft or asks Grok.</td><td>Destination, template, and hazard scores, plus incident titles already on the map. No traveller name, passport, medical detail, or sign-off.</td></tr>
            <tr><th scope="row">Public OSRM</th><td>Only for <code>/route-risk</code>.</td><td>Anonymous route coordinates. No callsign, name, or device identity.</td></tr>
            <tr><th scope="row">Slack</th><td>Only if the customer configures a webhook.</td><td>The operational message the customer set up. GeoRiskGlobal does not run the Slack workspace. Nothing is sent when no webhook is set.</td></tr>
            <tr><th scope="row">Okta</th><td>Only if the customer connects Okta OIDC single sign-on.</td><td>The customer's own authentication. Directory passwords are not copied. Nothing is sent to Okta when it is not connected.</td></tr>
          </tbody>
        </table>
      </div>
      <p>An enterprise enquiry form can store an organisation name and a work email so the team can reply. That form is not sent to Grok. Ask for it to be deleted with a data-subject request.</p>
    </section>
    <section class="panel" id="children">
      <h2>ICO Children's Code</h2>
      <p>The Age Appropriate Design Code (the ICO Children's Code) applies to services likely to be accessed by children. GeoRiskGlobal is not offered to children.</p>
      <p>Schools and universities features are strictly for adult Trip Leaders and staff. They are never for pupils or minors. Do not create an account for a pupil. Do not type a pupil's name, medical note, or contact into a field that is sent to the server. Those details belong in the local-only part of the assessment and stay on the trip leader's device.</p>
    </section>
    <section class="panel" id="rights">
      <h2>Data-subject rights and breach notification</h2>
      <p>People whose information is in a client's assessment can ask the controller for access, correction, erasure, restriction, portability, and to object. Because that assessment copy is in the browser, the controller and the user can do this on the device. Where GeoRiskGlobal holds something itself, send a data-subject access request (DSAR) to {privacy}. Say which right you are using. Do not include passport or medical details in the email if you can avoid it.</p>
      <p>You can complain to the UK Information Commissioner's Office (ICO), or to the EU supervisory authority in your country, if you think the rules have not been followed.</p>
      <p><strong>72-hour breach notification.</strong> If GeoRiskGlobal becomes aware of a breach of data it processes for a client, it will tell that controller without undue delay and in time for the controller to notify the UK ICO or the relevant EU supervisory authority within 72 hours, where Article 33 requires notification. Where GeoRiskGlobal is itself the controller, it will make that notification within 72 hours of becoming aware of a notifiable breach.</p>
      <p>This notice was prepared on {STATEMENT_DATE}. The privacy contact above is the mailbox for requests. A deployment can point it at another address with <code>PRIVACY_CONTACT</code>.</p>
    </section>
    """
    return _shell(
        title="Privacy notice — GeoRiskGlobal",
        kicker="Privacy",
        heading="Privacy notice",
        lede="UK GDPR and EU GDPR roles, what stays in the browser, what is pseudonymous, who is called, and how to use your rights.",
        body=body,
        current="/privacy",
    )


def _accessibility_document() -> str:
    contact = _contact_link(accessibility_contact())
    body = f"""
    <section class="panel">
      <h2>How accessible this website is</h2>
      <p>GeoRiskGlobal wants people to be able to use the map, the risk assessments, and these information pages with a keyboard, a screen reader, and without relying on colour alone. We aim to meet <strong>WCAG 2.2 Level AA</strong>. That is the bar named by the UK Public Sector Bodies Accessibility Regulations and the standard we use for US public-sector customers under <strong>ADA Title II</strong>.</p>
      <p>The information pages and the assessment forms are designed to that bar. The live map is partially there: it is a visual canvas and some of its controls come from the map library. This statement says what works, what does not, and how to ask for an alternative.</p>
    </section>
    <section class="panel" id="keyboard">
      <h2>Keyboard navigation</h2>
      <ul class="clean">
        <li>This statement and the trust centre start with a skip link. Tab to it, then activate it to move to the content.</li>
        <li>Links, buttons, and form fields show a visible focus outline. Focus is not hidden behind the header on these pages.</li>
        <li>These pages do not open a dialog that keeps keyboard focus. There is no keyboard trap on the trust centre, this statement, pricing, or the assessment form.</li>
        <li>The assessment builder uses labelled native fields, buttons, and a table. The hazard matrix can be reached without dragging.</li>
        <li>The live map can be panned with a pointer. Incident lists, page links, and the assessment form are the keyboard path to the same information. Dragging is not required to read an assessment or to move between sections.</li>
        <li>If a map control traps the keyboard, that is a defect. Please report the page address and the control name.</li>
      </ul>
    </section>
    <section class="panel" id="contrast">
      <h2>Contrast and colour</h2>
      <ul class="clean">
        <li>Body text is light slate on navy (<code>#E2E8F0</code> on <code>#060A13</code>), which exceeds the WCAG 2.2 AA contrast ratio for normal text.</li>
        <li>Links on these pages are underlined as well as coloured, so meaning is not carried by colour alone.</li>
        <li>Risk bands use a text label (Low, Moderate, High, Very High, Severe) in addition to colour. Incident severity keeps its own text label (Low, Medium, High).</li>
        <li>Focus outlines are a light gold ring on the dark background and meet the non-text contrast expectation.</li>
      </ul>
    </section>
    <section class="panel" id="screen-readers">
      <h2>Screen readers</h2>
      <ul class="clean">
        <li>Pages use one top-level heading, then section headings in order. Landmarks mark the header, navigation, main content, and footer.</li>
        <li>The logo has alternative text. Decorative document marks are empty for assistive technology where they repeat the heading.</li>
        <li>Form fields on the assessment builder have visible labels. Buttons have names.</li>
        <li>Data tables on this site use header cells. The compliance tables on the trust centre use row and column headers.</li>
        <li>Status is written in words (In place, Shared, Roadmap), not by colour alone.</li>
        <li>Some live map markers and third-party map controls are not named as clearly as the rest of the interface. That is a known limitation.</li>
      </ul>
    </section>
    <section class="panel">
      <h2>Known limitations</h2>
      <ul class="clean">
        <li>The map canvas and some Leaflet controls are not a complete WCAG 2.2 AA experience. Lists and forms are the accessible path we provide beside the map.</li>
        <li>Target sizes in the shared header meet the 24 by 24 CSS pixel minimum on these pages. Dense map controls may be smaller.</li>
        <li>We have not commissioned an external accessibility audit. This statement is an internal review.</li>
      </ul>
    </section>
    <section class="panel" id="contact">
      <h2>Reporting a barrier</h2>
      <p>Email {contact}. Please include the page address, what you were trying to do, and the browser or assistive technology you used. You do not need to include personal, medical, or pupil data to report a barrier.</p>
      <p>We aim to acknowledge a report within five working days and to say what we will change, or how you can get the information another way.</p>
      <p>Public-sector customers who need an alternative format of an assessment or a statement can ask the same address. ADA Title II covered entities can point their users here and to that mailbox.</p>
    </section>
    <section class="panel">
      <h2>Enforcement</h2>
      <p>If you are in the United Kingdom and you are not satisfied with our response, you can contact the Equality Advisory and Support Service (EASS). Public-sector bodies have duties under the Public Sector Bodies (Websites and Mobile Applications) Accessibility Regulations 2018. The Equality and Human Rights Commission enforces those regulations for most public bodies.</p>
      <p>In the United States, ADA Title II complaints about a covered public entity’s digital service go to the responsible agency, often the US Department of Justice or the US Department of Education for schools.</p>
    </section>
    <section class="panel">
      <h2>Technical information</h2>
      <p>This website is GeoRiskGlobal. The pages are HTML and CSS, with JavaScript for the map and the assessment builder. Map tiles are loaded from Esri. The assessment builder is designed so personal fields stay in the browser.</p>
      <p>We test against WCAG 2.2 Level AA success criteria, including the 2.2 additions we rely on here: focus not obscured, target size, and accessible authentication for the preview password (a visible label, no cognitive puzzle, paste allowed).</p>
    </section>
    <section class="panel">
      <h2>Compliance status</h2>
      <p>This website is <strong>partially compliant</strong> with WCAG 2.2 Level AA. The exceptions we know about are the live map canvas and some third-party map controls, listed under known limitations.</p>
    </section>
    <section class="panel">
      <h2>Preparation of this statement</h2>
      <p>This statement was prepared on {STATEMENT_DATE} by an internal review of the templates, the trust centre, pricing, and the risk-assessment form against WCAG 2.2 AA. It was last reviewed on {STATEMENT_DATE}. We will review it when the map or the assessment templates change in a way that affects access, and at least once a year.</p>
    </section>
    """
    return _shell(
        title="Accessibility statement — GeoRiskGlobal",
        kicker="Accessibility",
        heading="Accessibility statement",
        lede="WCAG 2.2 Level AA commitment for GeoRiskGlobal, including keyboard use, contrast, screen readers, and how to report a barrier.",
        body=body,
        current="/accessibility",
    )
