"""Pre-launch search block and optional preview password.

Passwords are read only from the environment. Nothing in this module is a
password. Removing PREVIEW_PASSWORD and BASIC_AUTH_PASSWORD leaves the site
open. Indexing stays off until ALLOW_INDEXING is exactly "true" and
SITE_PRIVATE_MODE is not enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
from urllib.parse import quote

from flask import Flask, Response, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "georisk_preview"
COOKIE_MAX_AGE = 14 * 24 * 60 * 60
EXEMPT = {"/robots.txt", "/healthz", "/login"}
# Field check-in only. Tokens are url-safe and at least 32 characters.
# The ops dashboard (/check-in) and every other /api/checkins route stay locked.
_PUBLIC_CHECKIN = re.compile(
    r"^/(?:c/[A-Za-z0-9_-]{32,128}|api/checkin/tap/[A-Za-z0-9_-]{32,128})/?$"
)
ROBOTS_OFF = "User-agent: *\nDisallow: /\n"
ROBOTS_ON = "User-agent: *\nAllow: /\n"
ROBOTS_META = '<meta name="robots" content="noindex, nofollow">'
ROBOTS_HEADER = "noindex, nofollow, noarchive"


def indexing_allowed() -> bool:
    private = (os.environ.get("SITE_PRIVATE_MODE") or "").strip().lower()
    if private in {"1", "true", "yes", "on"}:
        return False
    return (os.environ.get("ALLOW_INDEXING") or "").strip() == "true"


def preview_password() -> str:
    """Return the preview password, or an empty string when the lock is off."""
    configured = os.environ.get("PREVIEW_PASSWORD")
    if configured:
        return configured
    basic = os.environ.get("BASIC_AUTH_PASSWORD")
    if basic:
        return basic
    return ""


def preview_user() -> str:
    return (os.environ.get("PREVIEW_USER") or "georiskintel").strip() or "georiskintel"


def lock_enabled() -> bool:
    return bool(preview_password())


def is_public_checkin_path(path: str) -> bool:
    """True for a mobile check-in link or its tap. Nothing else is public."""
    return bool(_PUBLIC_CHECKIN.match(path or ""))


def _signing_key() -> str:
    password = preview_password().encode("utf-8")
    return hashlib.sha256(b"georisk-preview-v1:" + password).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_key(), salt="georisk-preview")


def issue_token() -> str:
    return _serializer().dumps({"ok": True})


def token_is_valid(token: str) -> bool:
    if not token or not lock_enabled():
        return False
    try:
        payload = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def passwords_match(supplied: str) -> bool:
    expected = preview_password()
    if not expected or supplied is None:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def basic_auth_ok(header: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    import base64

    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(username.encode("utf-8"), preview_user().encode("utf-8")) and passwords_match(password)


def safe_next(target: str) -> str:
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if any(char in target for char in '"\'<>\\\n\r '):
        return "/"
    return target


def login_page(error: str = "", nxt: str = "/") -> str:
    message = f'<p class="error">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {ROBOTS_META}
  <title>Private Preview Access — GeoRiskIntel</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #060A13; color: #E2E8F0; font: 15px/1.45 "Segoe UI", system-ui, sans-serif;
      padding: 24px;
    }}
    form {{
      width: min(420px, 100%); background: #0D1424; border: 1px solid #C5A059; border-radius: 14px;
      padding: 28px 24px; box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45);
    }}
    p.kicker {{ margin: 0 0 6px; color: #D4AF37; letter-spacing: 0.14em; font-size: 11px; font-weight: 800; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p.note {{ margin: 0 0 18px; color: #94A3B8; }}
    label {{ display: block; color: #94A3B8; font-size: 12px; font-weight: 700; margin-bottom: 6px; }}
    input {{
      width: 100%; background: #060A13; color: #E2E8F0; border: 1px solid #1E293B; border-radius: 8px;
      padding: 12px; font: inherit;
    }}
    button {{
      margin-top: 14px; width: 100%; background: #102A63; color: #D4AF37; border: 1px solid #C5A059;
      border-radius: 8px; padding: 12px; font: inherit; font-weight: 800; cursor: pointer;
    }}
    .error {{ color: #E11D48; font-weight: 700; }}
  </style>
</head>
<body>
  <form method="post" action="/login" autocomplete="off">
    <p class="kicker">GEORISKINTEL</p>
    <h1>Private Preview Access</h1>
    <p class="note">This preview is locked. Enter the preview password to continue.</p>
    {message}
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
    <input type="hidden" name="next" value="{html.escape(safe_next(nxt), quote=True)}">
    <button type="submit">Unlock preview</button>
  </form>
</body>
</html>
"""


def _cookie_secure() -> bool:
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return request.is_secure or forwarded == "https"


def _authorized() -> bool:
    if token_is_valid(request.cookies.get(COOKIE, "")):
        return True
    return basic_auth_ok(request.headers.get("Authorization", ""))


def install(app: Flask) -> None:
    """Attach the preview lock and the indexing headers to a Flask app."""

    @app.get("/robots.txt")
    def robots_txt() -> Response:
        body = ROBOTS_ON if indexing_allowed() else ROBOTS_OFF
        response = Response(body, mimetype="text/plain")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz() -> Response:
        response = Response('{"ok":true}\n', mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response:
        if not lock_enabled():
            return redirect("/")
        nxt = safe_next(request.values.get("next") or "/")
        if request.method == "POST":
            if passwords_match(request.form.get("password") or ""):
                response = redirect(nxt)
                response.set_cookie(
                    COOKIE,
                    issue_token(),
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="Lax",
                    secure=_cookie_secure(),
                    path="/",
                )
                return response
            return Response(login_page("That password was not accepted.", nxt), mimetype="text/html")
        if _authorized():
            return redirect(nxt)
        return Response(login_page(nxt=nxt), mimetype="text/html")

    @app.before_request
    def _preview_gate():
        path = request.path or "/"
        if path in EXEMPT or is_public_checkin_path(path):
            return None
        if not lock_enabled():
            return None
        if _authorized():
            return None
        if path.startswith("/api/"):
            body = '{"ok":false,"message":"Preview authentication is required."}\n'
            response = Response(body, status=401, mimetype="application/json")
            response.headers["WWW-Authenticate"] = 'Basic realm="GeoRiskIntel Preview"'
            return response
        target = path
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", "replace")
        return redirect("/login?next=" + quote(target, safe="/?=&"))

    @app.after_request
    def _robots_headers(response: Response) -> Response:
        if indexing_allowed():
            return response
        response.headers["X-Robots-Tag"] = ROBOTS_HEADER
        content_type = (response.content_type or "").lower()
        if "text/html" not in content_type:
            return response
        try:
            # send_file / send_from_directory mark the body as direct
            # passthrough. get_data() then raises RuntimeError unless the
            # flag is cleared first. Close the original file wrapper after
            # the body is buffered so the descriptor is not left open.
            # Any failure must not become a 500.
            if getattr(response, "direct_passthrough", False):
                response.direct_passthrough = False
            body = getattr(response, "response", None)
            try:
                html = response.get_data(as_text=True)
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
        except Exception:
            return response
        if "name=\"robots\"" in html:
            return response
        if "<head>" in html:
            html = html.replace("<head>", "<head>\n  " + ROBOTS_META, 1)
            response.set_data(html)
        return response
