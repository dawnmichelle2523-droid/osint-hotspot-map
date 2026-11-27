import os
import unittest

from flask import Flask, Response

import preview_lock


def _app() -> Flask:
    app = Flask(__name__)
    preview_lock.install(app)

    @app.get("/")
    def home() -> Response:
        return Response("<!DOCTYPE html><html><head><title>Map</title></head><body>map</body></html>", mimetype="text/html")

    @app.get("/api/data")
    def data() -> Response:
        return Response('{"ok":true}', mimetype="application/json")

    return app


class PreviewLockTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)

    def test_open_site_when_password_is_unset(self) -> None:
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        client = _app().test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"map", page.data)
        self.assertEqual(page.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertIn(b'name="robots"', page.data)
        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertEqual(robots.data.decode(), "User-agent: *\nDisallow: /\n")
        self.assertTrue(robots.mimetype.startswith("text/plain"))

    def test_password_gate_redirects_pages_and_blocks_api(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        client = _app().test_client()
        page = client.get("/risk-assessments")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].startswith("/login"))
        api = client.get("/api/data")
        self.assertEqual(api.status_code, 401)
        self.assertIn("Basic", api.headers["WWW-Authenticate"])
        health = client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        robots = client.get("/robots.txt")
        self.assertIn("Disallow: /", robots.data.decode())

    def test_login_sets_an_httponly_cookie_and_unlocks(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        client = _app().test_client()
        denied = client.post("/login", data={"password": "wrong", "next": "/"})
        self.assertEqual(denied.status_code, 200)
        self.assertNotIn(b"correct-horse", denied.data)
        accepted = client.post("/login", data={"password": "correct-horse", "next": "/"})
        self.assertEqual(accepted.status_code, 302)
        cookie = accepted.headers.getlist("Set-Cookie")
        self.assertTrue(any("georisk_preview=" in item and "HttpOnly" in item and "SameSite=Lax" in item for item in cookie))
        self.assertNotIn("correct-horse", "".join(cookie))
        unlocked = client.get("/")
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn(b"map", unlocked.data)

    def test_basic_auth_uses_the_environment_password(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        client = _app().test_client()
        response = client.get("/api/data", headers={"Authorization": "Basic Z2Vvcmlza2ludGVsOmNvcnJlY3QtaG9yc2U="})
        self.assertEqual(response.status_code, 200)

    def test_indexing_can_be_turned_on(self) -> None:
        os.environ["ALLOW_INDEXING"] = "true"
        client = _app().test_client()
        robots = client.get("/robots.txt")
        self.assertIn("Allow: /", robots.data.decode())
        self.assertNotIn("X-Robots-Tag", robots.headers)
        os.environ["SITE_PRIVATE_MODE"] = "true"
        blocked = client.get("/robots.txt")
        self.assertIn("Disallow: /", blocked.data.decode())
        self.assertEqual(blocked.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")

    def test_source_does_not_contain_a_password_literal(self) -> None:
        with open(preview_lock.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('os.environ.get("PREVIEW_PASSWORD")', text)
        self.assertNotIn("correct-horse", text)

    def test_basic_auth_password_can_lock_when_preview_password_is_empty(self) -> None:
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ["BASIC_AUTH_PASSWORD"] = "legacy-secret"
        self.assertTrue(preview_lock.lock_enabled())
        self.assertTrue(preview_lock.passwords_match("legacy-secret"))
        self.assertFalse(preview_lock.passwords_match("other"))


if __name__ == "__main__":
    unittest.main()
