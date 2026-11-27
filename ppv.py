"""Single-region and single-country live-map passes.

Tokens are HMAC-signed. The key is PPV_SECRET, then Flask SECRET_KEY.
Nothing in this file is a password or a signing key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextvars import ContextVar
from urllib.parse import parse_qs, quote

COOKIE = "georisk_pass"
_grant: ContextVar[dict | None] = ContextVar("ppv_grant", default=None)
_route: ContextVar[str] = ContextVar("ppv_route", default="")

REGIONS = {
    "Global": {"Global"},
    "Middle East": {
        "Middle East", "Syria", "Iraq", "Iran", "Lebanon", "Israel", "Palestine",
        "Jordan", "Yemen", "Saudi Arabia", "Sudan", "Chad", "UAE", "Qatar",
        "Bahrain", "Kuwait", "Oman", "Gulf States",
    },
    "United States": {"United States", "USA"},
    "Canada": {"Canada"},
    "South America": {
        "South America", "Brazil", "Colombia", "Venezuela", "Peru", "Chile",
        "Argentina", "Ecuador", "Bolivia", "Guyana", "Suriname", "Paraguay", "Uruguay",
    },
    "APAC": {
        "APAC", "Japan", "Taiwan", "China", "Philippines", "South Korea", "North Korea",
        "Indonesia", "Vietnam", "Thailand", "Malaysia", "Singapore",
    },
    "Australia": {"Australia", "New Zealand"},
    "Afghanistan": {"Afghanistan"},
    "Russia & Ukraine": {"Russia & Ukraine", "Russia", "Ukraine", "Belarus"},
    "UK & Europe": {
        "UK & Europe", "Europe", "United Kingdom", "UK", "Ireland", "France", "Germany",
        "Italy", "Spain", "Poland", "Greece", "Norway", "Sweden", "Finland", "Denmark",
        "Netherlands", "Belgium", "Portugal", "Austria", "Switzerland", "Czechia",
        "Hungary", "Romania", "Turkey",
    },
    "Africa": {
        "Africa", "Nigeria", "Kenya", "Ethiopia", "Somalia", "South Africa", "Egypt",
        "Libya", "Tunisia", "Algeria", "Mali", "Ghana", "Tanzania", "Uganda",
        "Mozambique", "Angola", "Cameroon", "Niger", "Democratic Republic of the Congo",
        "Sudan", "Chad", "Senegal", "Morocco",
    },
    "South Asia": {
        "South Asia", "India", "Pakistan", "Bangladesh", "Nepal", "Sri Lanka",
        "Maldives", "Bhutan",
    },
    "Maritime": {"Maritime"},
}


def signing_secret() -> str:
    return (os.environ.get("PPV_SECRET") or os.environ.get("SECRET_KEY") or "").strip()


def set_current(grant: dict | None) -> None:
    _grant.set(grant)


def current() -> dict | None:
    return _grant.get()


def set_route(path: str) -> None:
    _route.set(path or "")


def request_route() -> str:
    return _route.get() or ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue(scope_type: str, scope: str, viewer: str, days: int = 7) -> str:
    key = signing_secret()
    if not key:
        raise RuntimeError("PPV_SECRET is not set")
    if scope_type not in {"region", "country"}:
        raise ValueError("scope_type must be region or country")
    clean_scope = " ".join(str(scope or "").split())[:80]
    clean_viewer = " ".join(str(viewer or "").split())[:120]
    if not clean_scope or not clean_viewer:
        raise ValueError("scope and viewer are required")
    span = max(1, min(int(days or 7), 30))
    payload = {
        "exp": int(time.time()) + span * 86400,
        "scope": clean_scope,
        "scope_type": scope_type,
        "viewer": clean_viewer,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64(raw) + "." + _b64(signature)


def verify(token: str) -> dict | None:
    key = signing_secret()
    if not key or not token or "." not in token:
        return None
    encoded, encoded_sig = token.split(".", 1)
    try:
        raw = _b64_decode(encoded)
        signature = _b64_decode(encoded_sig)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(key.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("scope_type") not in {"region", "country"}:
        return None
    if not payload.get("scope") or not payload.get("viewer"):
        return None
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if expires < int(time.time()):
        return None
    return {
        "scope_type": payload["scope_type"],
        "scope": str(payload["scope"]),
        "viewer": str(payload["viewer"]),
        "exp": expires,
    }


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def matches(item: dict, grant: dict | None = None) -> bool:
    grant = current() if grant is None else grant
    if not grant or not isinstance(item, dict):
        return False
    props = item.get("properties") if isinstance(item.get("properties"), dict) else item
    if not isinstance(props, dict):
        return False
    country = str(props.get("country") or props.get("name") or "")
    region = str(props.get("region") or "")
    theatre = str(props.get("theatre") or "")
    scope = grant["scope"]
    if grant["scope_type"] == "country":
        return _norm(scope) in {_norm(country), _norm(region), _norm(theatre), _norm(props.get("area") or "")}
    if _norm(scope) in {_norm(country), _norm(region), _norm(theatre)}:
        return True
    members = {_norm(name) for name in REGIONS.get(scope, set())}
    return bool(members & {_norm(country), _norm(region), _norm(theatre)})


def public_grant(grant: dict | None = None) -> dict | None:
    grant = current() if grant is None else grant
    if not grant:
        return None
    return {
        "active": True,
        "scope_type": grant["scope_type"],
        "scope": grant["scope"],
        "viewer": grant["viewer"],
        "exp": grant["exp"],
    }


def is_sensitive(path: str) -> bool:
    route = (path or "").split("?", 1)[0]
    prefixes = (
        "/api/data",
        "/api/news",
        "/api/travel-advisories",
        "/api/risk-assessments",
        "/api/country-assets",
        "/api/export.csv",
        "/api/export.geojson",
        "/api/daily-chart",
    )
    return any(route == prefix or route.startswith(prefix + "/") or route.startswith(prefix) for prefix in prefixes)


def export_blocked(query: str) -> bool:
    grant = current()
    if not grant:
        return False
    params = parse_qs(query or "", keep_blank_values=False)
    region = (params.get("region") or ["all"])[0]
    country = (params.get("country") or ["all"])[0]
    if region in {"", "all"} and country in {"", "all"}:
        return True
    return not matches({"region": region, "country": country}, grant)


def restrict(payload: dict) -> dict:
    """Copy a payload down to the active pass. The full payload is left untouched."""
    grant = current()
    if not grant or not isinstance(payload, dict):
        return payload
    out = dict(payload)
    out["ppv"] = public_grant(grant)
    for key in ("features", "events", "items"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [item for item in rows if isinstance(item, dict) and matches(item, grant)]
    if isinstance(out.get("countries"), list):
        out["countries"] = [
            item for item in out["countries"]
            if isinstance(item, dict) and matches({"country": item.get("name") or item.get("country"), "name": item.get("name")}, grant)
        ]
    if "incidents" in out and "country" in out:
        if matches({"country": out.get("country")}, grant):
            kept = []
            for item in out.get("incidents") or []:
                if not isinstance(item, dict):
                    continue
                stamped = dict(item)
                stamped.setdefault("country", out.get("country"))
                if matches(stamped, grant):
                    kept.append(item)
            out["incidents"] = kept
        else:
            out["incidents"] = []
            out["total"] = 0
            out["by_severity"] = {"high": 0, "medium": 0, "low": 0}
            out["by_category"] = {}
            out["note"] = "This pass does not include " + str(out.get("country") or "that area") + "."
            out["advisories"] = {}
    if isinstance(out.get("counts_by_region"), dict):
        counts: dict[str, int] = {}
        for item in out.get("events") or []:
            props = item.get("properties") if isinstance(item, dict) and isinstance(item.get("properties"), dict) else item
            if not isinstance(props, dict):
                continue
            name = str(props.get("region") or props.get("country") or grant["scope"])
            counts[name] = counts.get(name, 0) + 1
        out["counts_by_region"] = counts or {grant["scope"]: len(out.get("features") or [])}
    if isinstance(out.get("summaries"), dict):
        summaries = dict(out["summaries"])
        regions = summaries.get("regions")
        if isinstance(regions, dict):
            summaries["regions"] = {
                name: text for name, text in regions.items()
                if matches({"region": name, "country": name}, grant)
            }
        out["summaries"] = summaries
    if isinstance(out.get("summary"), dict):
        summary = dict(out["summary"])
        notes = summary.get("regional_notes")
        if isinstance(notes, list):
            summary["regional_notes"] = [
                note for note in notes
                if isinstance(note, dict) and matches({"region": note.get("region"), "country": note.get("region")}, grant)
            ]
        out["summary"] = summary
    if isinstance(out.get("region_trends"), dict):
        out["region_trends"] = {
            name: value for name, value in out["region_trends"].items()
            if matches({"region": name}, grant)
        }
    visible = len(out.get("features") or [])
    if "mapped_count" in out:
        out["mapped_count"] = visible
    if "total_count" in out:
        out["total_count"] = visible
    return out


def _secure_cookie() -> bool:
    try:
        from flask import request
    except Exception:
        return False
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return bool(request.is_secure or forwarded == "https")


def _preview_admin() -> bool:
    try:
        import preview_lock
        from flask import request
    except Exception:
        return False
    if not preview_lock.lock_enabled():
        return False
    if preview_lock.token_is_valid(request.cookies.get(preview_lock.COOKIE, "")):
        return True
    return preview_lock.basic_auth_ok(request.headers.get("Authorization", ""))


def _next_url() -> str:
    from flask import request

    pairs = [(key, value) for key, value in request.args.items() if key != "pass"]
    if not pairs:
        return request.path
    return request.path + "?" + "&".join(f"{quote(key)}={quote(value)}" for key, value in pairs)


def install(app) -> None:
    """Read pass tokens and offer an admin issuer on the preview session."""
    from flask import Response, jsonify, redirect, request

    @app.before_request
    def _load_pass():
        set_route(request.path)
        # Client-portal embeds must not read or write a pass cookie.
        if (request.path or "").startswith("/embed/widget"):
            set_current(None)
            return None
        explicit = (request.args.get("pass") or "").strip()
        header = (request.headers.get("X-GeoRisk-Pass") or "").strip()
        cookie = (request.cookies.get(COOKIE) or "").strip()
        if explicit == "clear":
            set_current(None)
            if request.method == "GET" and not request.path.startswith("/api/"):
                response = redirect(_next_url())
                response.delete_cookie(COOKIE, path="/")
                return response
            request.environ["ppv_clear"] = "1"
            return None
        token = header or cookie
        if explicit and explicit != "clear":
            token = explicit
        grant = verify(token) if token else None
        set_current(grant)
        if explicit and explicit != "clear" and grant and request.method == "GET" and not request.path.startswith("/api/"):
            response = redirect(_next_url())
            max_age = max(60, int(grant["exp"]) - int(time.time()))
            response.set_cookie(
                COOKIE,
                explicit,
                max_age=max_age,
                httponly=True,
                samesite="Lax",
                secure=_secure_cookie(),
                path="/",
            )
            return response
        return None

    @app.after_request
    def _store_pass(response: Response) -> Response:
        if (request.path or "").startswith("/embed/widget"):
            return response
        if request.environ.get("ppv_clear"):
            response.delete_cookie(COOKIE, path="/")
            return response
        header = (request.headers.get("X-GeoRisk-Pass") or "").strip()
        explicit = (request.args.get("pass") or "").strip()
        grant = current()
        token = header or explicit
        if grant and token and request.path.startswith("/api/"):
            max_age = max(60, int(grant["exp"]) - int(time.time()))
            response.set_cookie(
                COOKIE,
                token,
                max_age=max_age,
                httponly=True,
                samesite="Lax",
                secure=_secure_cookie(),
                path="/",
            )
        return response

    @app.post("/api/admin/ppv-pass")
    def issue_pass():
        if not _preview_admin():
            return jsonify({"ok": False, "message": "Preview sign-in is required to issue a pass."}), 403
        if not signing_secret():
            return jsonify({"ok": False, "message": "Set PPV_SECRET on the server before issuing a pass."}), 503
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        try:
            token = issue(
                str(body.get("scope_type") or "region"),
                str(body.get("scope") or ""),
                str(body.get("viewer") or ""),
                int(body.get("days") or 7),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        grant = verify(token) or {}
        link = request.host_url.rstrip("/") + "/?pass=" + quote(token, safe="")
        return jsonify({
            "ok": True,
            "token": token,
            "url": link,
            "scope_type": grant.get("scope_type"),
            "scope": grant.get("scope"),
            "viewer": grant.get("viewer"),
            "exp": grant.get("exp"),
            "days": 7,
        })
