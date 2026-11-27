"""Installable PWA: manifest, icons, service worker, and page tags."""

import json
import os
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
for _name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
    os.environ.pop(_name, None)

import app as app_module

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("/", "/risk-assessments", "/sentinel-zones", "/grok-ops")
ICONS = {
    "/static/icons/icon-192.png": (192, 192),
    "/static/icons/icon-512.png": (512, 512),
    "/static/icons/apple-touch-icon.png": (180, 180),
}


class PwaTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
            os.environ.pop(name, None)
        self.client = app_module.app.test_client()

    def test_manifest_is_valid_and_icons_exist(self) -> None:
        response = self.client.get("/static/manifest.webmanifest")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/manifest+json", response.content_type)
        payload = json.loads(response.data)
        self.assertEqual(payload["name"], "GeoRiskGlobal — Live OSINT & Travel Risk")
        self.assertEqual(payload["short_name"], "GeoRiskGlobal")
        self.assertEqual(payload["start_url"], "/")
        self.assertEqual(payload["display"], "standalone")
        self.assertEqual(payload["background_color"], "#0b1120")
        self.assertEqual(payload["theme_color"], "#0b1120")
        shortcuts = {item["url"] for item in payload["shortcuts"]}
        self.assertEqual(shortcuts, {"/risk-assessments", "/sentinel-zones", "/reports", "/grok-ops"})
        self.assertGreaterEqual(len(payload["icons"]), 2)
        for icon in payload["icons"]:
            self.assertIn(icon["src"], ICONS)
            fetched = self.client.get(icon["src"])
            try:
                self.assertEqual(fetched.status_code, 200, icon["src"])
                self.assertTrue(fetched.data.startswith(b"\x89PNG"), icon["src"])
                image = Image.open(BytesIO(fetched.data))
                self.assertEqual(image.size, ICONS[icon["src"]], icon["src"])
            finally:
                fetched.close()

    def test_icon_files_are_served(self) -> None:
        for path in ICONS:
            response = self.client.get(path)
            try:
                self.assertEqual(response.status_code, 200, path)
                self.assertIn("image/png", response.content_type)
                self.assertTrue((ROOT / path.lstrip("/")).is_file())
            finally:
                response.close()

    def test_service_worker_headers_and_strategy(self) -> None:
        response = self.client.get("/sw.js")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/javascript", response.content_type)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertNotIn("no-store", response.headers["Cache-Control"])
        source = response.get_data(as_text=True)
        self.assertIn("request.method !== \"GET\"", source)
        self.assertIn("url.pathname.startsWith(\"/api/\")", source)
        self.assertIn("networkFirst", source)
        self.assertIn("cacheFirst", source)
        self.assertIn("isSensitiveResponse", source)
        self.assertIn("www-authenticate", source)
        self.assertNotIn("cache.put(request, copy)", source)
        disk = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertEqual(source, disk)

    def test_pages_link_the_manifest_and_register_script(self) -> None:
        for path in PAGES:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            self.assertIn('rel="manifest"', html, path)
            self.assertIn('href="/static/manifest.webmanifest"', html, path)
            self.assertIn('name="theme-color" content="#0b1120"', html, path)
            self.assertIn('name="apple-mobile-web-app-capable" content="yes"', html, path)
            self.assertIn('href="/static/icons/apple-touch-icon.png"', html, path)
            self.assertIn('src="/static/pwa-register.js"', html, path)
            page.close()

    def test_brand_mark_favicon_and_home_screen_icon(self) -> None:
        with Image.open(ROOT / "static" / "logo.png") as logo:
            self.assertEqual(logo.mode, "RGBA")
            self.assertGreater(logo.width, logo.height * 2)
            self.assertEqual(logo.getchannel("A").getextrema()[0], 0)
        with Image.open(ROOT / "static" / "favicon.png") as favicon:
            self.assertEqual(favicon.size, (64, 64))
        with Image.open(ROOT / "static" / "icons" / "apple-touch-icon.png") as touch:
            self.assertEqual(touch.size, (180, 180))
        ico = ROOT / "static" / "favicon.ico"
        self.assertTrue(ico.is_file())
        self.assertGreater(ico.stat().st_size, 100)
        page = self.client.get("/")
        try:
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
        finally:
            page.close()
        self.assertIn('href="/static/favicon.ico?v=5"', html)
        self.assertIn('href="/static/favicon.png?v=5"', html)
        self.assertIn("GeoRiskGlobal", html)
        self.assertNotIn("GeoRiskIntel", html)

    def test_pricing_and_preview_lock_are_unchanged_by_the_pwa(self) -> None:
        pricing = (ROOT / "pricing.html").read_text(encoding="utf-8")
        self.assertNotIn("pwa-register.js", pricing)
        self.assertNotIn("/static/manifest.webmanifest", pricing)
        preview = (ROOT / "preview_lock.py").read_text(encoding="utf-8")
        self.assertNotIn("pwa-register", preview)
        self.assertNotIn("manifest.webmanifest", preview)
