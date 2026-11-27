"""Headers, rate limits, disclosure files, and the trust and accessibility pages."""

import os
import unittest
from pathlib import Path

from flask import Flask, Response, session

import preview_lock
import security_compliance

ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = (
    "TRUST_PROXY",
    "K_SERVICE",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "SECURITY_RATE_LIMIT",
    "PREVIEW_PASSWORD",
    "PREVIEW_USER",
    "BASIC_AUTH_PASSWORD",
    "BASIC_AUTH_USERNAME",
    "PUBLIC_BASE_URL",
    "SECURITY_CONTACT",
    "ACCESSIBILITY_CONTACT",
    "ALLOW_INDEXING",
    "SITE_PRIVATE_MODE",
    "PRIVACY_CONTACT",
    "PRIVACY_ERASE_TOKEN",
    "PSEUDONYMOUS_DB",
)


def _api_app(*, testing: bool = False, force_limit: bool = False) -> Flask:
    app = Flask(__name__)
    app.testing = testing
    app.config["TESTING"] = testing
    if force_limit:
        app.config["SECURITY_RATE_LIMIT_FORCE"] = True
    app.secret_key = "test-only-session-key"
    security_compliance.install(app)

    @app.route("/api/ping", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def ping() -> Response:
        return Response("ok", mimetype="text/plain")

    @app.get("/page")
    def page() -> Response:
        return Response("<!DOCTYPE html><html><head><title>Page</title></head><body>page</body></html>", mimetype="text/html")

    @app.get("/embed/map")
    def embed() -> Response:
        return Response("embed", mimetype="text/html")

    @app.get("/embed/map/<path:rest>")
    def embed_rest(rest: str) -> Response:
        return Response(rest, mimetype="text/html")

    @app.get("/embedding")
    def embedding() -> Response:
        return Response("not-embed", mimetype="text/html")

    @app.get("/set-cookie")
    def set_cookie() -> Response:
        response = Response("ok")
        response.set_cookie("georisk_preview", "abc")
        response.set_cookie("theme", "dark")
        return response

    @app.get("/session")
    def session_route() -> Response:
        session["analyst"] = "desk"
        return Response("session")

    return app


class _EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ENV_KEYS}
        for name in ENV_KEYS:
            os.environ.pop(name, None)
        self._clock = security_compliance.clock
        os.environ.setdefault("DISABLE_BACKGROUND", "1")
        os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

    def tearDown(self) -> None:
        security_compliance.clock = self._clock
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _assert_common(case: unittest.TestCase, response, *, https: bool, embed: bool) -> None:
    case.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
    case.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
    case.assertEqual(response.headers["Permissions-Policy"], security_compliance.PERMISSIONS_POLICY)
    case.assertIn("geolocation=(self)", response.headers["Permissions-Policy"])
    policy = response.headers["Content-Security-Policy"]
    for token in (
        "https://unpkg.com",
        "arcgisonline.com",
        "openstreetmap.org",
        "cartocdn.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "'unsafe-inline'",
        "worker-src 'self'",
    ):
        case.assertIn(token, policy)
    case.assertNotIn("unsafe-eval", policy)
    if embed:
        case.assertNotIn("X-Frame-Options", response.headers)
        case.assertIn("frame-ancestors *", policy)
        case.assertNotIn("frame-ancestors 'self'", policy)
    else:
        case.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        case.assertIn("frame-ancestors 'self'", policy)
        case.assertNotIn("frame-ancestors *", policy)
    if https:
        case.assertEqual(response.headers["Strict-Transport-Security"], security_compliance.HSTS_VALUE)
    else:
        case.assertNotIn("Strict-Transport-Security", response.headers)


class HeaderTests(_EnvCase):
    def test_baseline_headers_on_http(self) -> None:
        client = _api_app().test_client()
        response = client.get("/page")
        self.assertEqual(response.status_code, 200)
        _assert_common(self, response, https=False, embed=False)

    def test_hsts_when_forwarded_proto_is_https(self) -> None:
        client = _api_app().test_client()
        response = client.get("/page", headers={"X-Forwarded-Proto": "https"})
        _assert_common(self, response, https=True, embed=False)
        forwarded = client.get("/page", headers={"X-Forwarded-Proto": "https, http"})
        self.assertEqual(forwarded.headers["Strict-Transport-Security"], security_compliance.HSTS_VALUE)

    def test_hsts_follows_the_request_scheme(self) -> None:
        client = _api_app().test_client()
        secure = client.get("/page", base_url="https://georiskintel.test")
        self.assertIn("Strict-Transport-Security", secure.headers)
        downgraded = client.get("/page", base_url="https://georiskintel.test", headers={"X-Forwarded-Proto": "http"})
        self.assertNotIn("Strict-Transport-Security", downgraded.headers)

    def test_embed_paths_can_be_framed_and_lookalikes_cannot(self) -> None:
        client = _api_app().test_client()
        embed = client.get("/embed/map")
        _assert_common(self, embed, https=False, embed=True)
        nested = client.get("/embed/map/uk")
        _assert_common(self, nested, https=False, embed=True)
        lookalike = client.get("/embedding")
        _assert_common(self, lookalike, https=False, embed=False)

    def test_auth_cookie_gains_httponly_and_samesite_and_secure_on_https(self) -> None:
        client = _api_app().test_client()
        plain = client.get("/set-cookie")
        preview = _cookie(plain, "georisk_preview")
        theme = _cookie(plain, "theme")
        self.assertIn("HttpOnly", preview)
        self.assertIn("SameSite=Lax", preview)
        self.assertNotIn("Secure", preview)
        self.assertEqual(preview.lower().count("httponly"), 1)
        self.assertNotIn("HttpOnly", theme)
        secure = client.get("/set-cookie", headers={"X-Forwarded-Proto": "https"})
        hardened = _cookie(secure, "georisk_preview")
        self.assertIn("Secure", hardened)
        self.assertEqual(hardened.lower().count("secure"), 1)
        self.assertEqual(hardened.lower().count("httponly"), 1)

    def test_session_cookie_is_httponly_samesite_and_secure_on_https(self) -> None:
        client = _api_app().test_client()
        plain = client.get("/session")
        cookie = _cookie(plain, "session")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        secure = client.get("/session", base_url="https://georiskintel.test")
        https_cookie = _cookie(secure, "session")
        self.assertIn("Secure", https_cookie)
        self.assertIn("HttpOnly", https_cookie)

    def test_cookie_helper_is_idempotent(self) -> None:
        once = security_compliance.harden_cookie_header(
            "georisk_pass=abc; Path=/",
            secure=True,
        )
        twice = security_compliance.harden_cookie_header(once, secure=True)
        self.assertEqual(once, twice)
        self.assertIn("HttpOnly", once)
        self.assertIn("SameSite=Lax", once)
        self.assertIn("Secure", once)
        strict = security_compliance.harden_cookie_header(
            "session=abc; SameSite=Strict",
            secure=False,
        )
        self.assertIn("SameSite=Strict", strict)
        self.assertNotIn("Secure", strict)


class RateLimitTests(_EnvCase):
    def test_testing_mode_does_not_limit_unless_forced(self) -> None:
        client = _api_app(testing=True).test_client()
        for _ in range(40):
            response = client.post("/api/ping")
            self.assertNotEqual(response.status_code, 429)
        forced = _api_app(testing=True, force_limit=True).test_client()
        for _ in range(security_compliance.MUTATION_LIMIT):
            self.assertEqual(forced.post("/api/ping").status_code, 200)
        blocked = forced.post("/api/ping")
        self.assertEqual(blocked.status_code, 429)

    def test_env_switch_disables_the_limiter(self) -> None:
        os.environ["SECURITY_RATE_LIMIT"] = "0"
        client = _api_app().test_client()
        for _ in range(security_compliance.MUTATION_LIMIT + 5):
            self.assertEqual(client.post("/api/ping").status_code, 200)

    def test_writes_return_429_with_retry_after(self) -> None:
        now = {"t": 1000.0}
        security_compliance.clock = lambda: now["t"]
        client = _api_app().test_client()
        for _ in range(security_compliance.MUTATION_LIMIT):
            self.assertEqual(client.post("/api/ping").status_code, 200)
        blocked = client.post("/api/ping")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "60")
        self.assertEqual(blocked.headers["X-Content-Type-Options"], "nosniff")
        body = blocked.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "too_many_requests")
        self.assertNotIn("127.0.0.1", blocked.get_data(as_text=True))
        self.assertEqual(client.put("/api/ping").status_code, 429)
        self.assertEqual(client.patch("/api/ping").status_code, 429)
        self.assertEqual(client.delete("/api/ping").status_code, 429)
        self.assertEqual(client.get("/api/ping").status_code, 200)
        now["t"] = 1060.0
        self.assertEqual(client.post("/api/ping").status_code, 200)

    def test_general_api_limit_and_page_exemption(self) -> None:
        client = _api_app().test_client()
        for _ in range(security_compliance.GENERAL_LIMIT):
            self.assertEqual(client.get("/api/ping").status_code, 200)
        blocked = client.get("/api/ping")
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
        self.assertLessEqual(int(blocked.headers["Retry-After"]), 60)
        self.assertEqual(client.get("/page").status_code, 200)
        self.assertEqual(client.get("/trust").status_code, 200)
        health = client.get("/api/health/security")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.get_json()["rate_limiting_enabled"])

    def test_forwarded_for_cannot_bypass_without_trust_proxy(self) -> None:
        client = _api_app().test_client()
        for _ in range(security_compliance.GENERAL_LIMIT):
            self.assertEqual(client.get("/api/ping").status_code, 200)
        spoofed = client.get("/api/ping", headers={"X-Forwarded-For": "203.0.113.50"})
        self.assertEqual(spoofed.status_code, 429)
        other = client.get("/api/ping", environ_overrides={"REMOTE_ADDR": "203.0.113.51"})
        self.assertEqual(other.status_code, 200)

    def test_trust_proxy_keys_the_forwarded_client(self) -> None:
        os.environ["TRUST_PROXY"] = "true"
        client = _api_app().test_client()
        for _ in range(security_compliance.GENERAL_LIMIT):
            response = client.get("/api/ping", headers={"X-Forwarded-For": "203.0.113.8"})
            self.assertEqual(response.status_code, 200)
        blocked = client.get("/api/ping", headers={"X-Forwarded-For": "203.0.113.8"})
        self.assertEqual(blocked.status_code, 429)
        neighbour = client.get("/api/ping", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(neighbour.status_code, 200)

    def test_install_is_idempotent(self) -> None:
        app = _api_app()
        security_compliance.install(app)
        client = app.test_client()
        for _ in range(70):
            self.assertEqual(client.get("/api/ping").status_code, 200)


class DisclosureTests(_EnvCase):
    def test_security_txt_is_rfc9116(self) -> None:
        os.environ["PUBLIC_BASE_URL"] = "https://georiskintel.example"
        os.environ["SECURITY_CONTACT"] = "security@georiskintel.example"
        client = _api_app().test_client()
        response = client.get("/.well-known/security.txt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/plain"))
        text = response.get_data(as_text=True)
        self.assertIn("Contact: mailto:security@georiskintel.example", text)
        self.assertIn(f"Expires: {security_compliance.SECURITY_TXT_EXPIRES}", text)
        self.assertIn("Canonical: https://georiskintel.example/.well-known/security.txt", text)
        self.assertIn("Policy: https://georiskintel.example/trust", text)
        self.assertIn("Preferred-Languages: en", text)
        self.assertNotIn("<html", text.lower())
        expires = security_compliance.SECURITY_TXT_EXPIRES
        self.assertTrue(expires.endswith("Z"))
        self.assertGreater(expires, "2026-11-27")

    def test_accessibility_statement_covers_wcag(self) -> None:
        client = _api_app().test_client()
        response = client.get("/accessibility")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/html"))
        text = response.get_data(as_text=True)
        for phrase in (
            "WCAG 2.2",
            "Public Sector Bodies",
            "ADA Title II",
            "Keyboard",
            "Contrast",
            "Screen readers",
            "Skip to content",
            "partially compliant",
            "security@georiskintel.com",
        ):
            self.assertIn(phrase, text)
        self.assertIn('href="#main"', text)
        self.assertIn('id="main"', text)
        _assert_common(self, response, https=False, embed=False)

    def test_trust_centre_states_controls_and_the_disclaimer(self) -> None:
        client = _api_app().test_client()
        response = client.get("/trust")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for phrase in (
            "Zero-PII",
            "Personal, medical, and pupil",
            "TLS 1.3",
            "HSTS",
            "Content-Security-Policy",
            "HttpOnly",
            "SameSite",
            "rate limit",
            "security.txt",
            "Cyber Essentials",
            "NCSC",
            "ISO 31000",
            "ISO 31030",
            "BS 8848",
            "EU AI Act",
            "human-in-the-loop",
            "WCAG 2.2",
            "ISO 27001",
            "SOC 2",
            "DORA",
            "europe-west2",
            "us-east4",
            "Okta",
            "not a medical device",
            "decision-support",
            "UK GDPR",
            "EU GDPR",
            "Data Controller",
            "Processor",
            "localStorage",
            "Article 4(5)",
            "Article 5(1)(e)",
            "Article 17",
            "30 days",
            "Children's Code",
            "72 hours",
            'href="/privacy"',
        ):
            self.assertIn(phrase, text)
        self.assertIn(">Privacy</a>", text)
        _assert_common(self, response, https=False, embed=False)

    def test_privacy_notice_covers_uk_and_eu_gdpr(self) -> None:
        client = _api_app().test_client()
        response = client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/html"))
        text = response.get_data(as_text=True)
        for phrase in (
            "Data Controller",
            "GeoRiskIntel is the Processor",
            "localStorage",
            "georisk.ra.v1",
            "passport",
            "medical",
            "sign-off",
            "Article 4(5)",
            "Article 5(1)(e)",
            "Article 17",
            "30 days",
            "one decimal",
            "opaque callsign",
            "HttpOnly",
            "SameSite=Lax",
            "Railway",
            "europe-west2",
            "xAI Grok",
            "OSRM",
            "/route-risk",
            "Slack",
            "Okta",
            "Children's Code",
            "Trip Leaders",
            "never for pupils or minors",
            "72 hours",
            "ICO",
            "DSAR",
            "privacy@georiskintel.com",
            'href="/trust"',
        ):
            self.assertIn(phrase, text)
        self.assertIn('href="/privacy"', text)
        _assert_common(self, response, https=False, embed=False)

    def test_security_health_is_booleans_only(self) -> None:
        client = _api_app().test_client()
        response = client.get("/api/health/security")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("application/json"))
        body = response.get_json()
        expected = {
            "ok",
            "hsts_enabled",
            "csp_enabled",
            "rate_limiting_enabled",
            "zero_pii_ra_mode",
            "preview_lock_active",
            "security_txt_present",
            "wcag_statement_present",
        }
        self.assertEqual(set(body), expected)
        for key in expected:
            self.assertIsInstance(body[key], bool)
        self.assertTrue(body["hsts_enabled"])
        self.assertTrue(body["csp_enabled"])
        self.assertTrue(body["rate_limiting_enabled"])
        self.assertTrue(body["zero_pii_ra_mode"])
        self.assertFalse(body["preview_lock_active"])
        self.assertTrue(body["security_txt_present"])
        self.assertTrue(body["wcag_statement_present"])
        self.assertNotIn("Strict-Transport-Security", response.headers)
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        locked_response = client.get("/api/health/security")
        locked = locked_response.get_json()
        self.assertTrue(locked["preview_lock_active"])
        self.assertNotIn("correct-horse", locked_response.get_data(as_text=True))

    def test_compliance_pages_stay_public_when_the_preview_is_locked(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        app = Flask(__name__)
        security_compliance.install(app)
        preview_lock.install(app)

        @app.get("/pricing")
        def pricing() -> Response:
            return Response("pricing-page", mimetype="text/html")

        client = app.test_client()
        for path in ("/trust", "/privacy", "/accessibility", "/.well-known/security.txt", "/api/health/security"):
            opened = client.get(path)
            self.assertEqual(opened.status_code, 200, path)
        blocked = client.get("/pricing")
        self.assertEqual(blocked.status_code, 302)
        self.assertTrue(blocked.headers["Location"].startswith("/login"))
        accepted = client.post("/login", data={"password": "correct-horse", "next": "/pricing"}, base_url="https://georiskintel.test")
        cookie = ",".join(accepted.headers.getlist("Set-Cookie"))
        self.assertEqual(cookie.lower().count("httponly"), 1)
        self.assertEqual(cookie.lower().count("samesite=lax"), 1)
        self.assertEqual(cookie.lower().count("secure"), 1)
        self.assertNotIn("correct-horse", cookie)

    def test_preview_lock_still_gates_when_compliance_is_installed_second(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        app = Flask(__name__)
        preview_lock.install(app)
        security_compliance.install(app)

        @app.get("/pricing")
        def pricing() -> Response:
            return Response("pricing-page", mimetype="text/html")

        client = app.test_client()
        self.assertEqual(client.get("/trust").status_code, 200)
        self.assertEqual(client.get("/privacy").status_code, 200)
        self.assertEqual(client.get("/pricing").status_code, 302)


class PseudonymousRetentionTests(_EnvCase):
    def test_check_in_is_coarse_and_refuses_personal_fields(self) -> None:
        import tempfile

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        os.environ["PSEUDONYMOUS_DB"] = str(Path(folder.name) / "taps.sqlite")
        client = _api_app().test_client()
        stored = client.post("/api/privacy/check-ins", json={"callsign": "eagle-4", "lat": 51.44, "lng": -0.14})
        self.assertEqual(stored.status_code, 200)
        body = stored.get_json()
        self.assertEqual(body["stored"], {"callsign": "EAGLE-4", "lat": 51.4, "lng": -0.1})
        self.assertEqual(body["retention_days"], 30)
        rejected = client.post(
            "/api/privacy/check-ins",
            json={"callsign": "EAGLE-4", "lat": 1, "lng": 2, "passport": "123456", "medical": "asthma"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("asthma", rejected.get_data(as_text=True))
        self.assertNotIn("123456", rejected.get_data(as_text=True))
        named = client.post("/api/privacy/check-ins", json={"callsign": "Ada Lovelace", "lat": 1, "lng": 2})
        self.assertEqual(named.status_code, 400)

    def test_records_purge_after_thirty_days_and_erase_is_immediate(self) -> None:
        import tempfile

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        os.environ["PSEUDONYMOUS_DB"] = str(Path(folder.name) / "taps.sqlite")
        os.environ["PRIVACY_ERASE_TOKEN"] = "erase-token"
        security_compliance.record_pseudonymous_tap({"callsign": "OLD-1", "lat": 10.0, "lng": 10.0}, now=0)
        self.assertEqual(security_compliance.purge_pseudonymous_taps(now=30 * 24 * 60 * 60), 1)
        security_compliance.record_pseudonymous_tap({"callsign": "NEW-1", "lat": 12.2, "lng": 3.0}, now=10_000)
        client = _api_app().test_client()
        denied = client.post("/api/privacy/erase", json={})
        self.assertEqual(denied.status_code, 403)
        erased = client.post(
            "/api/privacy/erase",
            json={"callsign": "new-1"},
            headers={"Authorization": "Bearer erase-token"},
        )
        self.assertEqual(erased.status_code, 200)
        self.assertEqual(erased.get_json()["deleted"], 1)
        self.assertEqual(security_compliance.erase_pseudonymous_taps(), 0)


class DependencyAuditTests(unittest.TestCase):
    def test_requirements_pin_patched_releases(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for pin in (
            "Flask==3.1.3",
            "Werkzeug==3.1.9",
            "requests==2.34.2",
            "python-dotenv==1.2.4",
            "feedparser==6.0.14",
            "Pillow==13.0.0",
            "gunicorn==27.0.1",
            "Flask-BasicAuth==0.2.0",
        ):
            self.assertIn(pin, text)
        self.assertNotIn("requests==2.32.3", text)
        self.assertNotIn("Flask==3.1.2", text)
        source = (ROOT / "security_compliance.py").read_text(encoding="utf-8")
        self.assertNotIn("correct-horse", source)
        self.assertNotIn("XAI_API_KEY=", source)


class ProductionWiringTests(_EnvCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        import app as app_module

        app_module.app.testing = True
        cls.app_module = app_module

    def test_real_app_serves_compliance_without_breaking_pricing(self) -> None:
        self.app_module.app.testing = True
        client = self.app_module.app.test_client()
        pricing = client.get("/pricing")
        self.assertEqual(pricing.status_code, 200)
        self.assertIn(b"Automated Intelligence Reports (Optional Add-On)", pricing.data)
        self.assertIn("Content-Security-Policy", pricing.headers)
        self.assertEqual(pricing.headers["X-Frame-Options"], "SAMEORIGIN")
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"leaflet", home.data.lower())
        trust = client.get("/trust")
        self.assertEqual(trust.status_code, 200)
        self.assertIn(b"Trust &amp; Cyber Compliance Center", trust.data)
        self.assertIn(b'href="/privacy"', trust.data)
        privacy = client.get("/privacy")
        self.assertEqual(privacy.status_code, 200)
        self.assertIn(b"UK GDPR and EU GDPR roles", privacy.data)
        accessibility = client.get("/accessibility")
        self.assertEqual(accessibility.status_code, 200)
        security_txt = client.get("/.well-known/security.txt")
        self.assertEqual(security_txt.status_code, 200)
        self.assertIn(b"Contact:", security_txt.data)
        health = client.get("/api/health/security")
        self.assertEqual(health.status_code, 200)
        body = health.get_json()
        self.assertTrue(body["csp_enabled"])
        self.assertTrue(body["zero_pii_ra_mode"])
        self.assertFalse(body["rate_limiting_enabled"])
        missing = client.get("/sentinel-zones")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Content-Security-Policy", missing.headers)

    def test_real_app_keeps_compliance_public_under_the_preview_lock(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        self.app_module.app.testing = True
        client = self.app_module.app.test_client()
        try:
            self.assertEqual(client.get("/trust").status_code, 200)
            self.assertEqual(client.get("/privacy").status_code, 200)
            self.assertEqual(client.get("/accessibility").status_code, 200)
            self.assertEqual(client.get("/.well-known/security.txt").status_code, 200)
            self.assertEqual(client.get("/api/health/security").status_code, 200)
            pricing = client.get("/pricing")
            self.assertEqual(pricing.status_code, 302)
            self.assertTrue(pricing.headers["Location"].startswith("/login"))
        finally:
            os.environ.pop("PREVIEW_PASSWORD", None)


def _cookie(response, name: str) -> str:
    matches = [item for item in response.headers.getlist("Set-Cookie") if item.startswith(name + "=")]
    if len(matches) != 1:
        raise AssertionError(response.headers.getlist("Set-Cookie"))
    return matches[0]


if __name__ == "__main__":
    unittest.main()
