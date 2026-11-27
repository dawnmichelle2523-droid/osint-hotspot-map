import os
import tempfile
import unittest
from pathlib import Path

from flask import Flask, Response, send_from_directory

import preview_lock

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "/": "home.html",
    "/map": "index.html",
    "/pricing": "pricing.html",
    "/enterprise": "enterprise.html",
    "/risk-assessments": "risk-assessments.html",
    "/grok-ops": "grok-ops.html",
}


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


def _passthrough_app() -> Flask:
    """Serve the real pages the way app.py does: send_from_directory.

    Those responses stay in Werkzeug direct-passthrough mode, which is what
    crashed / and /pricing when the robots tag was injected.
    """
    app = Flask(__name__, static_folder=str(ROOT / "static"))
    app.testing = True
    preview_lock.install(app)

    def page(filename: str):
        def view() -> Response:
            return send_from_directory(ROOT, filename)

        view.__name__ = "page_" + filename.replace(".", "_").replace("-", "_")
        return view

    for path, filename in PAGES.items():
        app.get(path)(page(filename))

    @app.get("/api/data")
    def data() -> Response:
        return Response('{"ok":true}', mimetype="application/json")

    return app


class PassthroughRobotsTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)

    def test_open_pages_inject_robots_without_passthrough_error(self) -> None:
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        os.environ.pop("ALLOW_INDEXING", None)
        client = _passthrough_app().test_client()
        for path in ("/", "/map", "/pricing", "/enterprise", "/risk-assessments", "/grok-ops"):
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertEqual(page.headers["X-Robots-Tag"], "noindex, nofollow, noarchive", path)
            self.assertIn(b'<meta name="robots" content="noindex, nofollow">', page.data, path)
            self.assertIn(b"</html>", page.data, path)
            self.assertEqual(page.content_length, len(page.data), path)
            page.close()
        login = client.get("/login")
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], "/")
        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Disallow: /", robots.data.decode())
        self.assertEqual(robots.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")
        api = client.get("/api/data")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertNotIn(b"name=\"robots\"", api.data)
        stylesheet = client.get("/static/platform.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"{", stylesheet.data)
        stylesheet.close()
        icon = client.get("/static/favicon.png")
        self.assertEqual(icon.status_code, 200)
        self.assertTrue(icon.data.startswith(b"\x89PNG"))
        icon.close()

    def test_password_locks_pages_and_unlocks_passthrough_home(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        os.environ.pop("ALLOW_INDEXING", None)
        client = _passthrough_app().test_client()
        for path in ("/", "/map", "/pricing", "/enterprise", "/risk-assessments", "/grok-ops", "/static/platform.css"):
            blocked = client.get(path)
            self.assertEqual(blocked.status_code, 302, path)
            self.assertTrue(blocked.headers["Location"].startswith("/login"), path)
        login = client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Private Preview Access", login.data)
        self.assertEqual(login.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")
        api = client.get("/api/data")
        self.assertEqual(api.status_code, 401)
        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        accepted = client.post("/login", data={"password": "correct-horse", "next": "/"})
        self.assertEqual(accepted.status_code, 302)
        unlocked = client.get("/")
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn(b'<meta name="robots" content="noindex, nofollow">', unlocked.data)
        self.assertIn(b"</html>", unlocked.data)
        pricing = client.get("/pricing")
        self.assertEqual(pricing.status_code, 200)
        self.assertIn(b"Pricing", pricing.data)

    def test_broken_html_stream_does_not_raise(self) -> None:
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("ALLOW_INDEXING", None)
        app = _passthrough_app()

        def broken():
            raise OSError("stream closed")
            yield b"<html><head></head></html>"

        with app.test_request_context("/"):
            response = Response(broken(), mimetype="text/html", direct_passthrough=True)
            result = app.process_response(response)
        self.assertEqual(result.headers["X-Robots-Tag"], "noindex, nofollow, noarchive")


class ProductionRouteTests(unittest.TestCase):
    """Hit the Flask app Railway serves, with background ingest left off."""

    @classmethod
    def setUpClass(cls) -> None:
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)
        cls._db_dir = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(cls._db_dir.name) / "events.db")
        import serve_map

        serve_map.start_background = lambda: None
        import app as production

        production.app.testing = True
        cls.app = production.app
        cls.serve_map = serve_map

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("DB_PATH", None)
        cls._db_dir.cleanup()

    def tearDown(self) -> None:
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)

    def test_open_routes(self) -> None:
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        os.environ.pop("ALLOW_INDEXING", None)
        original = self.serve_map.data_response
        self.serve_map.data_response = lambda: {"ok": True, "events": []}
        client = self.app.test_client()
        try:
            for path in ("/", "/map", "/pricing", "/enterprise", "/risk-assessments", "/grok-ops"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200, path)
                self.assertEqual(page.headers["X-Robots-Tag"], "noindex, nofollow, noarchive", path)
                self.assertIn(b'<meta name="robots" content="noindex, nofollow">', page.data, path)
                self.assertEqual(page.content_length, len(page.data), path)
                page.close()
            login = client.get("/login")
            self.assertEqual(login.status_code, 302)
            self.assertEqual(login.headers["Location"], "/")
            robots = client.get("/robots.txt")
            self.assertEqual(robots.status_code, 200)
            self.assertIn("Disallow: /", robots.data.decode())
            api = client.get("/api/data")
            self.assertEqual(api.status_code, 200)
            self.assertIn(b'"ok": true', api.data)
            stylesheet = client.get("/static/platform.css")
            self.assertEqual(stylesheet.status_code, 200)
            stylesheet.close()
        finally:
            self.serve_map.data_response = original

    def test_locked_routes_and_unlock(self) -> None:
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        os.environ.pop("ALLOW_INDEXING", None)
        client = self.app.test_client()
        for path in ("/", "/map", "/pricing", "/enterprise", "/risk-assessments", "/grok-ops"):
            blocked = client.get(path)
            self.assertEqual(blocked.status_code, 302, path)
            self.assertTrue(blocked.headers["Location"].startswith("/login"), path)
        login = client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Private Preview Access", login.data)
        api = client.get("/api/data")
        self.assertEqual(api.status_code, 401)
        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        accepted = client.post("/login", data={"password": "correct-horse", "next": "/"})
        self.assertEqual(accepted.status_code, 302)
        unlocked = client.get("/")
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn(b'<meta name="robots" content="noindex, nofollow">', unlocked.data)
        self.assertEqual(unlocked.content_length, len(unlocked.data))
        unlocked.close()


if __name__ == "__main__":
    unittest.main()
