"""Okta OIDC Authorization Code + PKCE, dashboard launch, and the setup sheet."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from flask import Flask, Response

import okta_sso
import preview_lock

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://example.okta.com/oauth2/default"
CLIENT = "0oaGeoriskIntelApp"
SECRET = "test-client-secret-value"
REDIRECT = "https://georisk.example.com/auth/okta/callback"
ENV_KEYS = (
    "OKTA_ISSUER",
    "OKTA_CLIENT_ID",
    "OKTA_CLIENT_SECRET",
    "OKTA_REDIRECT_URI",
    "OKTA_ALLOWED_GROUPS",
    "PUBLIC_BASE_URL",
    "RAILWAY_PUBLIC_DOMAIN",
    "RENDER_EXTERNAL_URL",
    "PREVIEW_PASSWORD",
    "PREVIEW_USER",
    "BASIC_AUTH_PASSWORD",
)

# RFC 7515 appendix A.2 RSASSA-PKCS1-v1_5 SHA-256 vector.
RFC_SIGNING_INPUT = (
    "eyJhbGciOiJSUzI1NiJ9."
    "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
)
RFC_N = """
ofgWCuLjybRlzo0tZWJjNiuSfb4p4fAkd_wWJcyQoTbji9k0l8W26mPddx
HmfHQp-Vaw-4qPCJrcS2mJPMEzP1Pt0Bm4d4QlL-yRT-SFd2lZS-pCgNMs
D1W_YpRPEwOWvG6b32690r2jZ47soMZo9wGzjb_7OMg0LOL-bSf63kpaSH
SXndS5z5rexMdbBYUsLA9e-KXBdQOS-UTo7WTBEMa2R2CapHg665xsmtdV
MTBQY4uDZlxvb3qCo5ZwKh9kG4LT6_I5IhlJH7aGhyxXFvUK-DWNmoudF8
NAco9_h9iaGNj8q2ethFkMLs91kzk2PAcDTW9gb54h4FRWyuXpoQ
"""
RFC_D = """
Eq5xpGnNCivDflJsRQBXHx1hdR1k6Ulwe2JZD50LpXyWPEAeP88vLNO97I
jlA7_GQ5sLKMgvfTeXZx9SE-7YwVol2NXOoAJe46sui395IW_GO-pWJ1O0
BkTGoVEn2bKVRUCgu-GjBVaYLU6f3l9kJfFNS3E0QbVdxzubSu3Mkqzjkn
439X0M_V51gfpRLI9JYanrC4D4qAdGcopV_0ZHHzQlBjudU2QvXt4ehNYT
CBr6XCLQUShb1juUO1ZdiYoFaFQT5Tw8bGUl_x_jTj3ccPDVZFD9pIuhLh
BOneufuBiB4cS98l2SR_RQyGWSeWjnczT0QU91p1DhOVRuOopznQ
"""
RFC_SIG = """
cC4hiUPoj9Eetdgtv3hF80EGrhuB__dzERat0XF9g2VtQgr9PJbu3XOiZj5RZmh7
AAuHIm4Bh-0Qc_lF5YKt_O8W2Fp5jujGbds9uJdbF9CUAr7t1dnZcAcQjbKBYNX4
BAynRFdiuB--f_nZLgrnbyTyWzO75vRK5h6xBArLIARNPvkSjtQBMHlb1L07Qe7K
0GarZRmB_eSN9383LcOLn6_dO--xi12jzDwusC-eOkHWEsqtFZESc6BfI7noOPqv
hJ1phCnvWh6IeYI2w9QOYEUipUTI8np6LbgGY9Fs98rqVt5AXLIhWkWywlVmtVrB
p0igcN_IoypGlUPQGe77Rw
"""


def _compact(value: str) -> str:
    return "".join(value.split())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_int(value: str) -> int:
    return int.from_bytes(okta_sso._b64url_decode(_compact(value)), "big")


def _jwt(payload: dict, secret: str, header: dict | None = None) -> str:
    head = header or {"alg": "HS256", "typ": "JWT"}
    signing = _b64url(json.dumps(head, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    if head.get("alg") == "none":
        return signing + "."
    signature = hmac.new(secret.encode("utf-8"), signing.encode("ascii"), hashlib.sha256).digest()
    return signing + "." + _b64url(signature)


def _rs256_sign(message: bytes, modulus: int, private: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    info = prefix + digest
    length = (modulus.bit_length() + 7) // 8
    padding = b"\xff" * (length - len(info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + info
    return pow(int.from_bytes(encoded, "big"), private, modulus).to_bytes(length, "big")


def _jwt_rs256(payload: dict, modulus: int, private: int) -> str:
    head = {"alg": "RS256", "typ": "JWT", "kid": "rfc7515"}
    signing = _b64url(json.dumps(head, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    return signing + "." + _b64url(_rs256_sign(signing.encode("ascii"), modulus, private))


def _app() -> Flask:
    app = Flask(__name__)
    app.testing = True
    preview_lock.install(app)
    okta_sso.install(app)

    @app.get("/")
    def home() -> Response:
        return Response("map", mimetype="text/html")

    @app.get("/api/data")
    def data() -> Response:
        return Response('{"ok":true}', mimetype="application/json")

    return app


def _cookie(client, name: str) -> str:
    found = client.get_cookie(name)
    if found is None:
        return ""
    value = getattr(found, "value", "")
    return value or ""


class _EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        okta_sso._jwks_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        okta_sso._jwks_cache.clear()


class OktaDisabledTests(_EnvCase):
    def test_open_preview_and_existing_login_are_unchanged(self) -> None:
        client = _app().test_client()
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"map", home.data)
        login = client.get("/login")
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], "/")
        self.assertEqual(client.get("/auth/okta/login").status_code, 404)
        self.assertEqual(client.get("/auth/okta/callback").status_code, 404)
        self.assertFalse(preview_lock.lock_enabled())

    def test_password_gate_does_not_offer_okta(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        client = _app().test_client()
        blocked = client.get("/")
        self.assertEqual(blocked.status_code, 302)
        self.assertTrue(blocked.headers["Location"].startswith("/login"))
        login = client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Private Preview Access", login.data)
        self.assertIn(b"Enter the preview password", login.data)
        self.assertNotIn(b"Sign in with Okta", login.data)
        accepted = client.post("/login", data={"password": "correct-horse", "next": "/"})
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/data").status_code, 200)
        self.assertNotIn(b"correct-horse", accepted.data)

    def test_sso_setup_is_ready_to_configure_with_exact_uris(self) -> None:
        client = _app().test_client()
        page = client.get("/sso-setup", base_url="https://desk.example.com")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('id="okta-connection-status">Ready to Configure<', body)
        self.assertIn("https://desk.example.com/auth/okta/callback", body)
        self.assertIn("https://desk.example.com/auth/okta/logout", body)
        self.assertIn("https://desk.example.com/auth/okta/login", body)
        self.assertIn("openid profile email groups", body)
        self.assertIn("Sign-in redirect URI", body)
        self.assertIn("Sign-out redirect URI", body)
        self.assertIn("Initiate login URI", body)
        self.assertIn("Railway", body)
        self.assertIn("Google Cloud", body)


class OktaEnabledTests(_EnvCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["OKTA_ISSUER"] = ISSUER
        os.environ["OKTA_CLIENT_ID"] = CLIENT
        os.environ["OKTA_CLIENT_SECRET"] = SECRET
        os.environ["OKTA_REDIRECT_URI"] = REDIRECT
        self.client = _app().test_client()
        self.modulus = _b64url_int(RFC_N)
        self.private = _b64url_int(RFC_D)

    def test_login_renders_sign_in_with_okta(self) -> None:
        self.assertTrue(preview_lock.lock_enabled())
        blocked = self.client.get("/")
        self.assertEqual(blocked.status_code, 302)
        self.assertTrue(blocked.headers["Location"].startswith("/login"))
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Sign in with Okta", page.data)
        self.assertNotIn(SECRET.encode(), page.data)
        self.assertNotIn(b'name="password"', page.data)
        api = self.client.get("/api/data")
        self.assertEqual(api.status_code, 401)

    def test_password_fallback_sits_beside_okta(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Sign in with Okta", page.data)
        self.assertIn(b"Private Preview Access", page.data)
        self.assertIn(b'name="password"', page.data)
        accepted = self.client.post("/login", data={"password": "correct-horse", "next": "/"})
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertNotIn(SECRET.encode(), b"".join(item.encode() for item in accepted.headers.getlist("Set-Cookie")))

    def test_authorize_redirect_uses_pkce_and_hides_the_secret(self) -> None:
        start = self.client.get("/auth/okta/login", query_string={"next": "/reports"})
        self.assertEqual(start.status_code, 302)
        location = start.headers["Location"]
        self.assertIn("/v1/authorize?", location)
        self.assertTrue(location.startswith(ISSUER + "/v1/authorize?"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        txn = okta_sso.read_transaction(_cookie(self.client, okta_sso.TXN_COOKIE))
        self.assertIsNotNone(txn)
        assert txn is not None
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [okta_sso.code_challenge(txn["verifier"])])
        self.assertEqual(query["state"], [txn["state"]])
        self.assertEqual(query["nonce"], [txn["nonce"]])
        self.assertEqual(query["scope"], [okta_sso.SCOPES])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["client_id"], [CLIENT])
        self.assertEqual(txn["next"], "/reports")
        self.assertNotIn(SECRET, location)
        self.assertNotIn(txn["verifier"], location)
        self.assertNotIn("code_verifier", location)
        cookie = "\n".join(start.headers.getlist("Set-Cookie"))
        self.assertIn("georisk_okta_txn=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_dashboard_tile_accepts_matching_iss_and_rejects_a_foreign_one(self) -> None:
        org = "https://example.okta.com"
        allowed = self.client.get("/auth/okta/login", query_string={"iss": org})
        self.assertEqual(allowed.status_code, 302)
        self.assertIn(ISSUER, allowed.headers["Location"])
        foreign = self.client.get("/auth/okta/login", query_string={"iss": "https://evil.example/oauth2/default"})
        self.assertEqual(foreign.status_code, 403)
        self.assertNotIn("evil.example", foreign.headers.get("Location", ""))

    def test_open_redirect_is_dropped(self) -> None:
        self.client.get("/auth/okta/login", query_string={"next": "https://evil.example/phish"})
        txn = okta_sso.read_transaction(_cookie(self.client, okta_sso.TXN_COOKIE))
        self.assertIsNotNone(txn)
        assert txn is not None
        self.assertEqual(txn["next"], "/")

    def test_callback_grants_the_preview_session(self) -> None:
        response, seen, txn = self._callback()
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertEqual(response.headers["Location"], "/")
        self.assertTrue(seen["url"].endswith("/v1/token"))
        self.assertEqual(seen["form"]["grant_type"], "authorization_code")
        self.assertEqual(seen["form"]["code"], "auth-code")
        self.assertEqual(seen["form"]["code_verifier"], txn["verifier"])
        self.assertEqual(seen["form"]["redirect_uri"], REDIRECT)
        self.assertEqual(seen["form"]["client_secret"], SECRET)
        issued = "\n".join(response.headers.getlist("Set-Cookie"))
        self.assertIn("georisk_preview=", issued)
        self.assertIn("HttpOnly", issued)
        self.assertNotIn("ada@example.com", issued)
        self.assertNotIn("opaque-access", issued)
        self.assertNotIn(SECRET, issued)
        session = preview_lock._serializer().loads(_cookie(self.client, preview_lock.COOKIE))
        self.assertEqual(session["role"], "okta")
        self.assertTrue(session["ok"])
        self.assertEqual(session["uh"], hashlib.sha256(b"00uabc").hexdigest())
        self.assertNotIn("email", session)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/data").status_code, 200)

    def test_callback_honors_the_safe_next_path(self) -> None:
        response, _seen, _txn = self._callback(next_path="/reports")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/reports")

    def test_bad_state_is_rejected(self) -> None:
        response, _seen, _txn = self._callback(state="not-the-state")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"did not match", response.data)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertNotIn("georisk_preview=", "\n".join(response.headers.getlist("Set-Cookie")))

    def test_bad_group_is_rejected_and_a_listed_group_is_accepted(self) -> None:
        os.environ["OKTA_ALLOWED_GROUPS"] = " Analysts , Desk "
        denied, _seen, _txn = self._callback(groups=["Interns"])
        self.assertEqual(denied.status_code, 403)
        self.assertIn(b"allowed group", denied.data)
        self.assertEqual(self.client.get("/").status_code, 302)
        allowed, _seen, _txn = self._callback(groups=["Desk"])
        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(self.client.get("/api/data").status_code, 200)

    def test_bad_nonce_audience_issuer_and_expiry_are_rejected(self) -> None:
        for kwargs in (
            {"nonce": "wrong-nonce"},
            {"aud": "someone-else"},
            {"iss": "https://evil.example/oauth2/default"},
            {"exp": int(time.time()) - 1000},
            {"header": {"alg": "none", "typ": "JWT"}},
            {"secret": "different-secret"},
        ):
            with self.subTest(kwargs=kwargs):
                response, _seen, _txn = self._callback(**kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.client.get("/").status_code, 302)

    def test_rs256_callback_verifies_with_jwks(self) -> None:
        jwks = {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "rfc7515", "n": _compact(RFC_N), "e": "AQAB"}]}

        def fake_fetch(url: str) -> dict:
            self.assertTrue(url.endswith("/v1/keys"), url)
            return jwks

        with mock.patch.object(okta_sso, "fetch_json", side_effect=fake_fetch):
            response, seen, _txn = self._callback(alg="RS256")
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertTrue(seen["url"].endswith("/v1/token"))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_logout_clears_the_session(self) -> None:
        granted, _seen, _txn = self._callback()
        self.assertEqual(granted.status_code, 302)
        self.assertEqual(self.client.get("/api/data").status_code, 200)
        gone = self.client.get("/auth/okta/logout")
        self.assertEqual(gone.status_code, 302)
        self.assertTrue(gone.headers["Location"].endswith("/login"))
        cleared = "\n".join(gone.headers.getlist("Set-Cookie")).lower()
        self.assertIn("georisk_preview=", cleared)
        self.assertTrue("max-age=0" in cleared or "expires=" in cleared)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/data").status_code, 401)

    def test_sso_setup_returns_exact_uris(self) -> None:
        page = self.client.get("/sso-setup", base_url="https://other.example")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('id="okta-connection-status">Configured<', body)
        self.assertIn("https://georisk.example.com/auth/okta/callback", body)
        self.assertIn("https://georisk.example.com/auth/okta/logout", body)
        self.assertIn("https://georisk.example.com/auth/okta/login", body)
        self.assertIn("openid profile email groups", body)
        self.assertIn("Sign-in redirect URI", body)
        self.assertIn("Sign-out redirect URI", body)
        self.assertIn("Initiate login URI", body)
        self.assertIn("OKTA_ISSUER", body)
        self.assertIn("OKTA_CLIENT_ID", body)
        self.assertIn("OKTA_CLIENT_SECRET", body)
        self.assertIn("OKTA_REDIRECT_URI", body)
        self.assertIn("OKTA_ALLOWED_GROUPS", body)
        self.assertIn("Railway", body)
        self.assertIn("Google Cloud", body)
        self.assertIn("Five-minute Okta Dashboard tile setup", body)
        self.assertIn("okta-spec-sheet", body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("ada@example.com", body)

    def test_forwarded_proto_builds_the_redirect_uri(self) -> None:
        os.environ.pop("OKTA_REDIRECT_URI", None)
        start = self.client.get(
            "/auth/okta/login",
            base_url="http://map.example.com",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(start.status_code, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.headers["Location"]).query)
        self.assertEqual(query["redirect_uri"], ["https://map.example.com/auth/okta/callback"])
        page = self.client.get("/sso-setup", base_url="http://map.example.com", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("https://map.example.com/auth/okta/callback", page.get_data(as_text=True))
        self.assertIn("https://map.example.com/auth/okta/login", page.get_data(as_text=True))

    def test_okta_login_is_exempt_from_the_password_gate(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        start = self.client.get("/auth/okta/login")
        self.assertEqual(start.status_code, 302)
        self.assertIn("/v1/authorize", start.headers["Location"])
        self.assertNotIn("/login", start.headers["Location"])

    def test_tools_menu_links_to_the_setup_sheet(self) -> None:
        ops = (ROOT / "grok-ops.html").read_text(encoding="utf-8")
        pricing = (ROOT / "pricing.html").read_text(encoding="utf-8")
        live = (ROOT / "index.html").read_text(encoding="utf-8")
        for text in (ops, pricing, live):
            self.assertIn(">Tools</summary>", text)
            self.assertIn('href="/sso-setup">Okta SSO</a>', text)
            self.assertIn('href="/check-in"', text)
            self.assertIn('href="/white-label"', text)
        self.assertNotIn("/auth/okta/", pricing)
        self.assertIn('data-gbp-monthly="£29"', pricing)

    def _callback(self, **overrides):
        start = self.client.get("/auth/okta/login", query_string={"next": overrides.get("next_path", "/")})
        self.assertEqual(start.status_code, 302, start.get_data(as_text=True))
        txn = okta_sso.read_transaction(_cookie(self.client, okta_sso.TXN_COOKIE))
        self.assertIsNotNone(txn)
        assert txn is not None
        now = int(time.time())
        payload = {
            "iss": overrides.get("iss", ISSUER),
            "aud": overrides.get("aud", CLIENT),
            "exp": overrides.get("exp", now + 600),
            "iat": overrides.get("iat", now),
            "nonce": overrides.get("nonce", txn["nonce"]),
            "sub": overrides.get("sub", "00uabc"),
            "email": "ada@example.com",
            "name": "Ada Lovelace",
        }
        if "groups" in overrides:
            payload["groups"] = overrides["groups"]
        if "azp" in overrides:
            payload["azp"] = overrides["azp"]
        if overrides.get("alg") == "RS256":
            token = _jwt_rs256(payload, self.modulus, self.private)
        else:
            token = _jwt(payload, overrides.get("secret", SECRET), overrides.get("header"))
        seen: dict = {}

        def fake_post(url, form):
            seen["url"] = url
            seen["form"] = dict(form)
            return {"id_token": token, "token_type": "Bearer", "access_token": "opaque-access"}

        state = overrides.get("state", txn["state"])
        with mock.patch.object(okta_sso, "post_form", side_effect=fake_post):
            response = self.client.get("/auth/okta/callback", query_string={"code": "auth-code", "state": state})
        return response, seen, txn


class OktaTokenTests(_EnvCase):
    def test_pkce_s256_matches_the_rfc_vector(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(okta_sso.code_challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_org_and_custom_authorization_servers(self) -> None:
        self.assertEqual(okta_sso.token_endpoint("https://acme.okta.com"), "https://acme.okta.com/oauth2/v1/token")
        self.assertEqual(
            okta_sso.authorize_endpoint("https://acme.okta.com/oauth2/default"),
            "https://acme.okta.com/oauth2/default/v1/authorize",
        )
        self.assertEqual(
            okta_sso.jwks_endpoint("https://acme.okta.com/oauth2/aus123"),
            "https://acme.okta.com/oauth2/aus123/v1/keys",
        )

    def test_rfc7515_rs256_vector_and_a_tampered_signature(self) -> None:
        signature = okta_sso._b64url_decode(_compact(RFC_SIG))
        jwk = {"kty": "RSA", "n": _compact(RFC_N), "e": "AQAB"}
        self.assertTrue(okta_sso._verify_rs256(RFC_SIGNING_INPUT.encode("ascii"), signature, jwk))
        tampered = bytearray(signature)
        tampered[0] ^= 0x01
        self.assertFalse(okta_sso._verify_rs256(RFC_SIGNING_INPUT.encode("ascii"), bytes(tampered), jwk))
        self.assertTrue(
            okta_sso._verify_rs256(
                b"signed-by-the-same-key",
                _rs256_sign(b"signed-by-the-same-key", _b64url_int(RFC_N), _b64url_int(RFC_D)),
                jwk,
            )
        )

    def test_multiple_audiences_require_azp(self) -> None:
        os.environ["OKTA_ISSUER"] = ISSUER
        os.environ["OKTA_CLIENT_ID"] = CLIENT
        os.environ["OKTA_CLIENT_SECRET"] = SECRET
        now = int(time.time())
        nonce = "nonce-value"
        base = {"iss": ISSUER, "exp": now + 300, "iat": now, "nonce": nonce, "sub": "00uabc"}
        good = dict(base, aud=[CLIENT, "api://other"], azp=CLIENT)
        self.assertIsNotNone(okta_sso.verify_id_token(_jwt(good, SECRET), nonce=nonce))
        missing = dict(base, aud=[CLIENT, "api://other"])
        self.assertIsNone(okta_sso.verify_id_token(_jwt(missing, SECRET), nonce=nonce))
        single = dict(base, aud=CLIENT)
        self.assertIsNotNone(okta_sso.verify_id_token(_jwt(single, SECRET), nonce=nonce))

    def test_http_issuer_does_not_enable_the_gate(self) -> None:
        os.environ["OKTA_ISSUER"] = "http://example.okta.com/oauth2/default"
        os.environ["OKTA_CLIENT_ID"] = CLIENT
        self.assertFalse(okta_sso.configured())
        self.assertFalse(preview_lock.lock_enabled())


if __name__ == "__main__":
    unittest.main()
