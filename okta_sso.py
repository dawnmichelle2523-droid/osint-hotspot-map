"""Okta OIDC (Authorization Code + PKCE) and dashboard-tile launch.

Configured only from the environment. When OKTA_ISSUER and OKTA_CLIENT_ID
are unset, nothing in this module changes the preview lock. The ID token is
checked and discarded. The session cookie stores a role and a hash of the
subject — not the email, name, or group list.

Stdlib plus the itsdangerous cookie signer Flask already uses. No XML stack
and no native JWT package. RS256 is verified with Python's built-in integers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping

from flask import Flask, Response, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import preview_lock

SCOPES = "openid profile email groups"
TXN_COOKIE = "georisk_okta_txn"
TXN_MAX_AGE = 10 * 60
CLOCK_SKEW = 120
_PROCESS_KEY = os.urandom(32)
_JWKS_TTL = 10 * 60
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, list]] = {}
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class TokenExchangeError(Exception):
    """The token endpoint or JWKS document could not be read."""


def process_key() -> bytes:
    return _PROCESS_KEY


def issuer() -> str:
    return (os.environ.get("OKTA_ISSUER") or "").strip().rstrip("/")


def client_id() -> str:
    value = (os.environ.get("OKTA_CLIENT_ID") or "").strip()
    if not value or len(value) > 200:
        return ""
    if any(ch.isspace() or ch in "\"'<>&#?" for ch in value):
        return ""
    return value


def client_secret() -> str:
    return (os.environ.get("OKTA_CLIENT_SECRET") or "").strip()


def allowed_groups() -> list[str]:
    raw = os.environ.get("OKTA_ALLOWED_GROUPS") or ""
    found: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen or len(name) > 200:
            continue
        seen.add(name)
        found.append(name)
    return found


def valid_issuer(value: str) -> bool:
    parts = urllib.parse.urlsplit((value or "").strip())
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        return False
    if parts.username or parts.password:
        return False
    return True


def configured() -> bool:
    return valid_issuer(issuer()) and bool(client_id())


def oauth_base(issuer_url: str) -> str:
    value = (issuer_url or "").strip().rstrip("/")
    tail = value.split("://", 1)[-1]
    if "/oauth2" not in tail:
        return value + "/oauth2"
    return value


def authorize_endpoint(issuer_url: str | None = None) -> str:
    return oauth_base(issuer_url or issuer()) + "/v1/authorize"


def token_endpoint(issuer_url: str | None = None) -> str:
    return oauth_base(issuer_url or issuer()) + "/v1/token"


def jwks_endpoint(issuer_url: str | None = None) -> str:
    return oauth_base(issuer_url or issuer()) + "/v1/keys"


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def login_markup(nxt: str) -> str:
    if not configured():
        return ""
    target = preview_lock.safe_next(nxt)
    href = "/auth/okta/login?next=" + urllib.parse.quote(target, safe="")
    return f'<a class="okta-btn" href="{html.escape(href, quote=True)}">Sign in with Okta</a>'


def integration_urls() -> dict[str, str]:
    forced = (os.environ.get("OKTA_REDIRECT_URI") or "").strip()
    origin = _origin_of(forced) if forced else ""
    if not origin:
        origin = _configured_origin() or _request_origin() or "http://127.0.0.1:5000"
    sign_in = forced if _origin_of(forced) else origin.rstrip("/") + "/auth/okta/callback"
    origin = _origin_of(sign_in) or origin.rstrip("/")
    return {
        "origin": origin,
        "sign_in": sign_in,
        "sign_out": origin + "/auth/okta/logout",
        "initiate": origin + "/auth/okta/login",
        "scopes": SCOPES,
    }


def issuer_compatible(candidate: str) -> bool:
    """Accept the Okta tile `iss` when it is the configured issuer or its org URL."""
    left = (candidate or "").strip().rstrip("/")
    right = issuer()
    if not valid_issuer(left) or not valid_issuer(right):
        return False
    if urllib.parse.urlsplit(left).hostname != urllib.parse.urlsplit(right).hostname:
        return False
    if left == right:
        return True
    return right.startswith(left + "/") or left.startswith(right + "/")


def read_transaction(token: str) -> dict | None:
    if not token or len(token) > 4096:
        return None
    try:
        payload = _txn_serializer().loads(token, max_age=TXN_MAX_AGE)
    except (BadSignature, SignatureExpired, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    cleaned: dict[str, str] = {}
    for key in ("state", "nonce", "verifier", "redirect_uri", "next"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > 2048:
            return None
        cleaned[key] = value
    if not 43 <= len(cleaned["verifier"]) <= 128:
        return None
    return cleaned


def post_form(url: str, form: Mapping[str, str]) -> dict:
    if not url.startswith("https://"):
        raise TokenExchangeError("scheme")
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "GeoRiskGlobal-OktaSSO/1",
    }
    secret = form.get("client_secret") or ""
    ident = form.get("client_id") or ""
    if ident and secret:
        headers["Authorization"] = _basic_auth(ident, secret)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(_read_limited(resp).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise TokenExchangeError(exc.__class__.__name__) from exc
    if not isinstance(payload, dict):
        raise TokenExchangeError("payload")
    return payload


def fetch_json(url: str) -> dict:
    if not url.startswith("https://"):
        raise TokenExchangeError("scheme")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "GeoRiskGlobal-OktaSSO/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(_read_limited(resp).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise TokenExchangeError(exc.__class__.__name__) from exc
    if not isinstance(payload, dict):
        raise TokenExchangeError("payload")
    return payload


def fetch_jwks(url: str) -> list:
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
    payload = fetch_json(url)
    keys = payload.get("keys")
    if not isinstance(keys, list):
        raise TokenExchangeError("jwks")
    trimmed = [key for key in keys[:8] if isinstance(key, dict)]
    with _jwks_lock:
        _jwks_cache[url] = (now + _JWKS_TTL, trimmed)
    return trimmed


def verify_id_token(token: str, *, nonce: str, jwks: list | None = None) -> dict | None:
    if not token or token.count(".") != 2 or len(token) > 12000:
        return None
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict) or not signature:
        return None
    if header.get("jwk") or header.get("jku") or header.get("x5c"):
        return None
    alg = header.get("alg")
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    if alg == "HS256":
        secret = client_secret()
        if not secret or not _verify_hs256(signing_input, signature, secret):
            return None
    elif alg == "RS256":
        try:
            keys = jwks if jwks is not None else fetch_jwks(jwks_endpoint())
        except TokenExchangeError:
            return None
        kid = header.get("kid") if isinstance(header.get("kid"), str) else ""
        if not any(_verify_rs256(signing_input, signature, key) for key in _select_keys(keys, kid)):
            return None
    else:
        return None
    if not _claims_ok(claims, nonce):
        return None
    return claims


def groups_allowed(claims: Mapping[str, object]) -> bool:
    required = allowed_groups()
    if not required:
        return True
    return any(name in _claim_groups(claims) for name in required)


def install(app: Flask) -> None:
    @app.get("/auth/okta/login")
    def okta_login() -> Response:
        return start_login()

    @app.route("/auth/okta/callback", methods=["GET", "POST"])
    def okta_callback() -> Response:
        return finish_login()

    @app.get("/auth/okta/logout")
    def okta_logout() -> Response:
        return logout()

    @app.get("/sso-setup")
    def sso_setup() -> Response:
        response = Response(render_setup_page(), mimetype="text/html; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def start_login() -> Response:
    if not configured():
        return _html(_message_page("Okta sign-in is not configured."), 404)
    supplied = (request.args.get("iss") or "").strip()
    if supplied and not issuer_compatible(supplied):
        return _html(_message_page("This Okta tile does not match the configured issuer."), 403)
    urls = integration_urls()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    nxt = preview_lock.safe_next(request.args.get("next") or "/")
    params = [
        ("client_id", client_id()),
        ("response_type", "code"),
        ("scope", SCOPES),
        ("redirect_uri", urls["sign_in"]),
        ("state", state),
        ("nonce", nonce),
        ("code_challenge", code_challenge(verifier)),
        ("code_challenge_method", "S256"),
        ("response_mode", "query"),
    ]
    location = authorize_endpoint() + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    if not _same_host(location, issuer()) or any(ch in location for ch in "\r\n"):
        return _html(_message_page("The Okta issuer URL was rejected."), 403)
    response = _redirect(location)
    _set_txn(
        response,
        {"state": state, "nonce": nonce, "verifier": verifier, "redirect_uri": urls["sign_in"], "next": nxt},
    )
    return response


def finish_login() -> Response:
    if not configured():
        return _html(_message_page("Okta sign-in is not configured."), 404)
    if (request.values.get("error") or "").strip():
        response = _html(_message_page("Okta did not complete sign-in. Start again from the dashboard tile."), 403)
        _clear_txn(response)
        return response
    code = _clean_code(request.values.get("code") or "")
    state = (request.values.get("state") or "").strip()
    txn = read_transaction(request.cookies.get(TXN_COOKIE, ""))
    if not code or not txn or not _same(state, txn["state"]):
        response = _html(_message_page("The sign-in session expired or did not match. Start again from the Okta tile."), 403)
        _clear_txn(response)
        return response
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": txn["redirect_uri"],
        "client_id": client_id(),
        "code_verifier": txn["verifier"],
    }
    secret = client_secret()
    if secret:
        form["client_secret"] = secret
    try:
        payload = post_form(token_endpoint(), form)
    except TokenExchangeError:
        response = _html(_message_page("Okta did not accept this sign-in."), 403)
        _clear_txn(response)
        return response
    raw_token = payload.get("id_token")
    claims = verify_id_token(raw_token, nonce=txn["nonce"]) if isinstance(raw_token, str) else None
    if claims is None:
        response = _html(_message_page("Okta did not accept this sign-in."), 403)
        _clear_txn(response)
        return response
    if not groups_allowed(claims):
        response = _html(_message_page("Your Okta account is not in an allowed group."), 403)
        _clear_txn(response)
        return response
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 256:
        response = _html(_message_page("Okta did not accept this sign-in."), 403)
        _clear_txn(response)
        return response
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    response = _redirect(preview_lock.safe_next(txn["next"]))
    preview_lock.attach_session_cookie(response, preview_lock.issue_token({"role": "okta", "uh": digest}))
    _clear_txn(response)
    return response


def logout() -> Response:
    target = "/login" if preview_lock.lock_enabled() else "/"
    response = _redirect(target)
    preview_lock.clear_session_cookie(response)
    _clear_txn(response)
    return response


def render_setup_page() -> str:
    urls = integration_urls()
    active = configured()
    status = "Configured" if active else "Ready to Configure"
    groups = allowed_groups()
    group_text = ", ".join(groups) if groups else "Not set — every authenticated Okta user is accepted"
    secret_text = "Set in the environment (not shown)" if client_secret() else "Not set — add OKTA_CLIENT_SECRET before production sign-in"
    issuer_text = issuer() if issuer() else "Not set"
    client_text = client_id() if client_id() else "Not set"
    derived = "Set explicitly with OKTA_REDIRECT_URI." if (os.environ.get("OKTA_REDIRECT_URI") or "").strip() else (
        "Derived from PUBLIC_BASE_URL, the Railway or Render hostname, or this request. "
        "Set OKTA_REDIRECT_URI when the public URL is different."
    )
    page = _SETUP_PAGE
    replacements = {
        "@@STATUS@@": _esc(status),
        "@@STATUS_CLASS@@": "live" if active else "ready",
        "@@ISSUER@@": _esc(issuer_text),
        "@@CLIENT@@": _esc(client_text),
        "@@SECRET@@": _esc(secret_text),
        "@@GROUPS@@": _esc(group_text),
        "@@SIGN_IN@@": _esc(urls["sign_in"]),
        "@@SIGN_OUT@@": _esc(urls["sign_out"]),
        "@@INITIATE@@": _esc(urls["initiate"]),
        "@@SCOPES@@": _esc(urls["scopes"]),
        "@@AUTHORIZE@@": _esc(authorize_endpoint() if active else "Set OKTA_ISSUER first"),
        "@@TOKEN@@": _esc(token_endpoint() if active else "Set OKTA_ISSUER first"),
        "@@DERIVED@@": _esc(derived),
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def _claims_ok(claims: Mapping[str, object], nonce: str) -> bool:
    iss = claims.get("iss")
    if not isinstance(iss, str) or iss.rstrip("/") != issuer():
        return False
    audiences = _audiences(claims.get("aud"))
    ident = client_id()
    if ident not in audiences:
        return False
    if len(audiences) > 1 and claims.get("azp") != ident:
        return False
    now = time.time()
    exp = _number(claims.get("exp"))
    iat = _number(claims.get("iat"))
    nbf = _number(claims.get("nbf")) if "nbf" in claims else None
    if exp is None or iat is None:
        return False
    if "nbf" in claims and nbf is None:
        return False
    if exp < now - CLOCK_SKEW or iat > now + CLOCK_SKEW:
        return False
    if nbf is not None and nbf > now + CLOCK_SKEW:
        return False
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not _same(token_nonce, nonce):
        return False
    return True


def _audiences(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _claim_groups(claims: Mapping[str, object]) -> set[str]:
    raw = claims.get("groups")
    values: list[str] = []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, int) and not isinstance(item, bool):
                values.append(str(item))
    return {item.strip() for item in values if item.strip()}


def _select_keys(keys: list, kid: str) -> list:
    chosen = []
    for key in keys:
        if not isinstance(key, dict) or key.get("kty") != "RSA":
            continue
        if key.get("use") not in (None, "sig"):
            continue
        if key.get("alg") not in (None, "RS256"):
            continue
        chosen.append(key)
    if kid:
        return [key for key in chosen if key.get("kid") == kid]
    return chosen[:8]


def _verify_hs256(signing_input: bytes, signature: bytes, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if len(expected) != len(signature):
        return False
    return hmac.compare_digest(expected, signature)


def _verify_rs256(signing_input: bytes, signature: bytes, jwk: Mapping[str, object]) -> bool:
    try:
        n_bytes = _b64url_decode(str(jwk.get("n") or ""))
        e_bytes = _b64url_decode(str(jwk.get("e") or ""))
    except (ValueError, TypeError):
        return False
    if not n_bytes or not e_bytes or len(e_bytes) > 8:
        return False
    modulus = int.from_bytes(n_bytes, "big")
    exponent = int.from_bytes(e_bytes, "big")
    bits = modulus.bit_length()
    if bits < 512 or bits > 8192 or exponent < 3 or exponent % 2 == 0:
        return False
    length = (bits + 7) // 8
    if len(signature) != length:
        return False
    signature_int = int.from_bytes(signature, "big")
    if signature_int <= 0 or signature_int >= modulus:
        return False
    encoded = pow(signature_int, exponent, modulus).to_bytes(length, "big")
    digest_info = _SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    if len(encoded) < len(digest_info) + 11 or encoded[0] != 0 or encoded[1] != 1:
        return False
    separator = encoded.find(b"\x00", 2)
    if separator < 10:
        return False
    padding = encoded[2:separator]
    if len(padding) < 8 or any(byte != 0xFF for byte in padding):
        return False
    return hmac.compare_digest(encoded[separator + 1 :], digest_info)


def _txn_serializer() -> URLSafeTimedSerializer:
    material = b"georisk-okta-txn-v1:" + issuer().encode("utf-8") + b"\0" + client_id().encode("utf-8")
    secret = client_secret().encode("utf-8")
    material += b"\0" + (secret or _PROCESS_KEY)
    key = hashlib.sha256(material).hexdigest()
    return URLSafeTimedSerializer(key, salt="georisk-okta-txn")


def _set_txn(response: Response, payload: dict[str, str]) -> None:
    response.set_cookie(
        TXN_COOKIE,
        _txn_serializer().dumps(payload),
        max_age=TXN_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=preview_lock._cookie_secure(),
        path="/",
    )


def _clear_txn(response: Response) -> None:
    response.delete_cookie(
        TXN_COOKIE,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=preview_lock._cookie_secure(),
    )


def _basic_auth(ident: str, secret: str) -> str:
    user = urllib.parse.quote(ident, safe="")
    password = urllib.parse.quote(secret, safe="")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return "Basic " + token


def _read_limited(resp: object, limit: int = 65536) -> bytes:
    data = resp.read(limit + 1)  # type: ignore[attr-defined]
    if not isinstance(data, (bytes, bytearray)) or len(data) > limit:
        raise TokenExchangeError("size")
    return bytes(data)


def _b64url_decode(data: str) -> bytes:
    text = (data or "").strip()
    if not text or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in text):
        raise ValueError("alphabet")
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _clean_code(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 2048 or any(ord(ch) < 33 or ord(ch) > 126 for ch in text):
        return ""
    return text


def _same(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_b = left.encode("utf-8")
    right_b = right.encode("utf-8")
    if len(left_b) != len(right_b):
        return False
    return hmac.compare_digest(left_b, right_b)


def _same_host(url: str, issuer_url: str) -> bool:
    left = urllib.parse.urlsplit(url).hostname
    right = urllib.parse.urlsplit(issuer_url).hostname
    return bool(left) and left == right


def _origin_of(uri: str) -> str:
    parts = urllib.parse.urlsplit((uri or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return ""
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _configured_origin() -> str:
    for key in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        origin = _origin_of(os.environ.get(key) or "")
        if origin:
            return origin
    railway = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").split(",")[0].strip()
    if railway:
        return _origin_of(railway if "://" in railway else "https://" + railway)
    return ""


def _request_origin() -> str:
    try:
        from flask import has_request_context, request as flask_request
    except ImportError:
        return ""
    if not has_request_context():
        return ""
    proto = (flask_request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto not in {"http", "https"}:
        proto = "https" if flask_request.is_secure else "http"
    host = (flask_request.host or "").strip()
    if not host or any(ch in host for ch in " /\r\n"):
        return ""
    return f"{proto}://{host}"


def _redirect(url: str) -> Response:
    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _html(body: str, status: int) -> Response:
    response = Response(body, status=status, mimetype="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _message_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <meta name="robots" content="noindex, nofollow">
  <title>Okta sign-in — GeoRiskGlobal</title>
  <style>
    body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#060A13; color:#E2E8F0; font:15px/1.45 "Segoe UI", system-ui, sans-serif; padding:24px; }}
    main {{ width:min(460px, 100%); background:#0D1424; border:1px solid #C5A059; border-radius:14px; padding:28px 24px; }}
    h1 {{ margin:0 0 8px; font-size:24px; }}
    p {{ margin:0 0 14px; color:#94A3B8; }}
    a {{ color:#D4AF37; }}
  </style>
</head>
<body>
  <main>
    <h1>Okta sign-in</h1>
    <p>{_esc(message)}</p>
    <p><a href="/auth/okta/login">Try again</a> · <a href="/sso-setup">Okta setup sheet</a></p>
  </main>
</body>
</html>
"""


_SETUP_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/static/favicon.ico?v=5" sizes="any">
  <link rel="icon" type="image/png" href="/static/favicon.png?v=5">
  <meta name="robots" content="noindex, nofollow">
  <title>Okta SSO Setup — GeoRiskGlobal</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100vh; background: #060A13; color: #E2E8F0; font: 15px/1.45 "Segoe UI", system-ui, sans-serif; }
    #live-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid #C5A059; background: #060A13; }
    .brand-lockup { display: flex; align-items: center; gap: 8px; color: #D4AF37; font-weight: 800; letter-spacing: .04em; text-decoration: none; }
    .risk-pill { color: #D4AF37; border: 1px solid #C5A059; border-radius: 999px; padding: 2px 8px; font-size: 10px; letter-spacing: .08em; font-weight: 700; }
    #page-nav { display: flex; gap: 6px; max-width: 100%; flex-wrap: wrap; align-items: center; }
    #page-nav a { color: #94A3B8; text-decoration: none; border: 1px solid #1E293B; border-radius: 8px; padding: 6px 10px; font-size: 12px; font-weight: 700; white-space: nowrap; }
    #page-nav a[aria-current="page"] { color: #D4AF37; background: #102A63; border-color: #C5A059; }
    details.nav-more { position: relative; }
    details.nav-more summary { list-style: none; cursor: pointer; color: #D4AF37; background: #102A63; border: 1px solid #C5A059; border-radius: 8px; padding: 6px 10px; font-size: 12px; font-weight: 700; }
    details.nav-more summary::-webkit-details-marker { display: none; }
    .nav-menu { position: absolute; z-index: 20; min-width: 210px; margin-top: 6px; background: #0D1424; border: 1px solid #C5A059; border-radius: 10px; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
    .nav-menu a { display: block; }
    a.nav-cta { margin-left: auto; color: #060A13; background: #D4AF37; border-radius: 8px; padding: 7px 12px; font-size: 12px; font-weight: 800; text-decoration: none; white-space: nowrap; }
    .section-banner { padding: 18px 16px 8px; max-width: 1180px; margin: 0 auto; }
    .kicker { margin: 0 0 4px; color: #D4AF37; font-size: 12px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
    .section-banner h1 { margin: 0 0 6px; font-size: clamp(26px, 4vw, 36px); line-height: 1.15; }
    .section-banner p, .note { color: #94A3B8; }
    main { max-width: 1180px; margin: 0 auto; padding: 8px 16px 48px; }
    .panel { background: #0D1424; border: 1px solid #1E293B; border-radius: 12px; padding: 14px; margin: 12px 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 16px; }
    .grid p { margin: 0; min-width: 0; overflow-wrap: anywhere; }
    label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #94A3B8; font-weight: 700; margin: 10px 0; }
    input { width: 100%; background: #060A13; color: #E2E8F0; border: 1px solid #1E293B; border-radius: 8px; padding: 8px 10px; font: inherit; }
    button { color: #D4AF37; background: #102A63; border: 1px solid #C5A059; border-radius: 8px; padding: 8px 12px; font: inherit; font-weight: 700; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #1E293B; vertical-align: top; }
    th { color: #D4AF37; }
    a { color: #D4AF37; }
    code { color: #E2E8F0; }
    footer { padding: 8px 12px; background: #0D1424; border-top: 1px solid #6E5825; color: #94A3B8; font-size: 11px; }
    .status-pill { display: inline-block; border-radius: 999px; padding: 4px 10px; font-weight: 800; letter-spacing: .04em; }
    .status-pill.ready { color: #94A3B8; border: 1px solid #1E293B; }
    .status-pill.live { color: #D4AF37; background: #102A63; border: 1px solid #C5A059; }
    .uri-row { display: flex; gap: 8px; align-items: end; }
    .uri-row button { white-space: nowrap; }
    input[readonly] { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }
    ol.guide { margin: 0; padding-left: 1.2rem; }
    ol.guide li { margin: 10px 0; }
    .spec-table td { word-break: break-all; }
    @media (max-width: 720px) { .uri-row { flex-direction: column; align-items: stretch; } a.nav-cta { margin-left: 0; } }
    @media print {
      body { background: #fff; color: #111; }
      #live-bar, .no-print, footer, button { display: none !important; }
      .panel { border-color: #ccc; background: #fff; }
      a, th, .kicker { color: #102A63; }
    }
  </style>
</head>
<body>
  <header id="live-bar">
    <a class="brand-lockup" href="/"><span>GeoRiskGlobal</span><span class="risk-pill">REGIONAL RISK</span></a>
    <nav id="page-nav" aria-label="Sections">
      <a href="/map">Live Map</a>
      <a href="/risk-assessments">Risk Assessments</a>
      <a href="/route-risk">Route risk</a>
      <a href="/check-in">Check-in</a>
      <a href="/pricing">Pricing</a>
      <details class="nav-more">
        <summary>Tools</summary>
        <div class="nav-menu">
          <a href="/reports">Reports</a>
          <a href="/sentinel-zones">Sentinel zones</a>
          <a href="/slack-alerts">Slack alerts</a>
          <a href="/white-label">White-label</a>
          <a href="/grok-ops">Grok Ops</a>
          <a href="/sso-setup" aria-current="page">Okta SSO</a>
          <a href="/briefing">Daily briefing</a>
          <a href="/world-risk">World risk</a>
          <a href="/countries">Country guides</a>
          <a href="/enterprise">Enterprise</a>
        </div>
      </details>
    </nav>
    <a class="nav-cta" href="/enterprise">Book a Pilot / Subscribe</a>
  </header>
  <div class="section-banner">
    <p class="kicker">Enterprise admin</p>
    <h1>Okta SSO and dashboard tile</h1>
    <p>Add GeoRiskGlobal to an Okta dashboard, then sign in with OpenID Connect. Authorization Code and PKCE (S256). The client secret stays in the host environment and is never printed on this page.</p>
  </div>
  <main>
    <section class="panel">
      <h2>Live Okta connection status</h2>
      <p><strong class="status-pill @@STATUS_CLASS@@" id="okta-connection-status">@@STATUS@@</strong></p>
      <p class="note">Configured means <code>OKTA_ISSUER</code> and <code>OKTA_CLIENT_ID</code> are set to a usable https issuer and client id. Ready to Configure means sign-in still follows the existing preview password or open preview.</p>
      <div class="grid">
        <p><strong>Issuer</strong><br><span id="okta-issuer">@@ISSUER@@</span></p>
        <p><strong>Client ID</strong><br><span id="okta-client-id">@@CLIENT@@</span></p>
        <p><strong>Client secret</strong><br>@@SECRET@@</p>
        <p><strong>Allowed groups</strong><br>@@GROUPS@@</p>
      </div>
    </section>
    <section class="panel" id="okta-uris">
      <h2>Values to paste into the Okta Admin Console</h2>
      <p class="note">@@DERIVED@@</p>
      <label for="sign-in-redirect-uri">Sign-in redirect URI
        <span class="uri-row"><input id="sign-in-redirect-uri" readonly value="@@SIGN_IN@@" spellcheck="false"><button type="button" data-copy="sign-in-redirect-uri">Copy</button></span>
      </label>
      <label for="sign-out-redirect-uri">Sign-out redirect URI
        <span class="uri-row"><input id="sign-out-redirect-uri" readonly value="@@SIGN_OUT@@" spellcheck="false"><button type="button" data-copy="sign-out-redirect-uri">Copy</button></span>
      </label>
      <label for="initiate-login-uri">Initiate login URI
        <span class="uri-row"><input id="initiate-login-uri" readonly value="@@INITIATE@@" spellcheck="false"><button type="button" data-copy="initiate-login-uri">Copy</button></span>
      </label>
      <p class="note">The Initiate login URI is the Okta Dashboard tile target. It is also the Bookmark App URL and the OIDC Initiate Login URI. <code>GET /auth/okta/login</code> starts the same Authorization Code + PKCE flow as the Sign in with Okta button. Okta may append <code>?iss=</code> — that issuer must be this org.</p>
      <label for="oidc-scopes">Required OIDC scopes
        <span class="uri-row"><input id="oidc-scopes" readonly value="@@SCOPES@@" spellcheck="false"><button type="button" data-copy="oidc-scopes">Copy</button></span>
      </label>
    </section>
    <section class="panel">
      <h2>Five-minute Okta Dashboard tile setup</h2>
      <ol class="guide">
        <li>In the Okta Admin Console, open Applications → Applications → Create App Integration. Choose <strong>OIDC - OpenID Connect</strong> and <strong>Web Application</strong>.</li>
        <li>Grant type: <strong>Authorization Code</strong>. Keep PKCE available — GeoRiskGlobal always sends a S256 <code>code_challenge</code>. Set <strong>Login initiated by</strong> to <strong>Either Okta or App</strong> so the dashboard tile and the Sign in with Okta button both work. Client authentication: <strong>Client secret</strong>.</li>
        <li>Paste the Sign-in redirect URI, Sign-out redirect URI, and Initiate login URI from this page. Assign the people or groups who should see the GeoRiskGlobal tile.</li>
        <li>On the authorization server (usually <code>default</code>), confirm the scopes <code>openid</code>, <code>profile</code>, <code>email</code>, and <code>groups</code>. Add a claim named <code>groups</code> that is included in the <strong>ID token</strong>, value type Groups, so the optional allow-list can be checked. Save.</li>
        <li>Copy the issuer, Client ID, and Client secret into the host environment, then redeploy. Railway: service → Variables → add the names below → Deploy. Google Cloud: Cloud Run → Edit &amp; deploy new revision → Variables &amp; Secrets (or the Cloud Run / App Engine environment block) using the same names. Open the new Okta tile, or use Sign in with Okta on <a href="/login">/login</a>.</li>
      </ol>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Environment variable</th><th>Railway / Google Cloud</th><th>Required</th></tr></thead>
          <tbody>
            <tr><td><code>OKTA_ISSUER</code></td><td>Issuer from the authorization server, for example <code>https://your-org.okta.com/oauth2/default</code></td><td>Yes, to turn Okta on</td></tr>
            <tr><td><code>OKTA_CLIENT_ID</code></td><td>Client ID of the Web application</td><td>Yes, to turn Okta on</td></tr>
            <tr><td><code>OKTA_CLIENT_SECRET</code></td><td>Client secret. Store it as a variable or secret. Do not commit it.</td><td>Yes for a confidential Web app</td></tr>
            <tr><td><code>OKTA_REDIRECT_URI</code></td><td>Optional override of the Sign-in redirect URI</td><td>No</td></tr>
            <tr><td><code>OKTA_ALLOWED_GROUPS</code></td><td>Optional comma-separated groups. The ID token must include at least one.</td><td>No</td></tr>
          </tbody>
        </table>
      </div>
      <p class="note">Leave <code>OKTA_ISSUER</code> and <code>OKTA_CLIENT_ID</code> empty and the site keeps today's behavior: <code>PREVIEW_PASSWORD</code> if it is set, otherwise an open preview. When both Okta and <code>PREVIEW_PASSWORD</code> are set, the login page offers Okta and the preview password.</p>
    </section>
    <section class="panel" id="okta-spec-sheet">
      <h2>Okta App Integration spec sheet</h2>
      <p class="no-print"><button type="button" id="print-spec">Print spec sheet</button></p>
      <table class="spec-table">
        <tbody>
          <tr><th>Application</th><td>GeoRiskGlobal</td></tr>
          <tr><th>Sign-in method</th><td>OIDC - OpenID Connect</td></tr>
          <tr><th>Application type</th><td>Web Application</td></tr>
          <tr><th>Grant type</th><td>Authorization Code + PKCE (S256)</td></tr>
          <tr><th>Login initiated by</th><td>Either Okta or App (dashboard tile and in-app button)</td></tr>
          <tr><th>Client authentication</th><td>Client secret — HTTP Basic and POST body — plus PKCE</td></tr>
          <tr><th>Sign-in redirect URI</th><td id="spec-sign-in">@@SIGN_IN@@</td></tr>
          <tr><th>Sign-out redirect URI</th><td id="spec-sign-out">@@SIGN_OUT@@</td></tr>
          <tr><th>Initiate login URI</th><td id="spec-initiate">@@INITIATE@@</td></tr>
          <tr><th>Scopes</th><td>@@SCOPES@@</td></tr>
          <tr><th>Authorize endpoint</th><td>@@AUTHORIZE@@</td></tr>
          <tr><th>Token endpoint</th><td>@@TOKEN@@</td></tr>
          <tr><th>Issuer</th><td>@@ISSUER@@</td></tr>
          <tr><th>Client ID</th><td>@@CLIENT@@</td></tr>
          <tr><th>Client secret</th><td>@@SECRET@@</td></tr>
          <tr><th>Group allow-list</th><td>@@GROUPS@@</td></tr>
          <tr><th>Session</th><td>HttpOnly cookie <code>georisk_preview</code>. Stores role <code>okta</code> and a SHA-256 of the subject. Email, name, and groups are not stored.</td></tr>
        </tbody>
      </table>
    </section>
  </main>
  <footer>GeoRiskGlobal · Okta SSO setup · client secret stays on the server</footer>
  <script>
    (function () {
      var buttons = document.querySelectorAll("[data-copy]");
      for (var i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener("click", function () {
          var button = this;
          var id = button.getAttribute("data-copy");
          var node = id ? document.getElementById(id) : null;
          var text = "";
          if (node) {
            text = node.value || node.textContent || "";
          }
          function mark(label) { button.textContent = label; }
          function selectField() {
            if (node && node.focus && node.select) {
              node.focus();
              node.select();
              mark("Selected");
              return;
            }
            mark("Select the field");
          }
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { mark("Copied"); }, selectField);
          } else {
            selectField();
          }
        });
      }
      var printButton = document.getElementById("print-spec");
      if (printButton) {
        printButton.addEventListener("click", function () { window.print(); });
      }
    })();
  </script>
</body>
</html>
"""
