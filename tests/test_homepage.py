"""Public GeoRiskGlobal homepage at / and the live OSINT map at /map."""

import os
import unittest

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
for _name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
    os.environ.pop(_name, None)

import app as app_module

NAV_PAGES = ("/", "/map", "/briefing", "/risk-assessments", "/world-risk", "/pricing")


class HomepageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.testing = True
        cls.client = app_module.app.test_client()

    def test_home_is_the_static_landing_page(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        page.close()
        self.assertIn("Global Geopolitical Risk Intelligence", html)
        self.assertIn("Bespoke Security Risk Assessments", html)
        self.assertIn("Explore Live Map", html)
        self.assertIn('href="/map"', html)
        self.assertIn('href="/pricing"', html)
        self.assertIn('href="/briefing"', html)
        self.assertIn('href="/risk-assessments"', html)
        self.assertIn("Live OSINT Hotspot Map", html)
        self.assertIn("Daily Geopolitical Briefings", html)
        self.assertIn("Bespoke Country Risk Assessments", html)
        self.assertIn("Transparent Subscription", html)
        self.assertIn("ISO 31000", html)
        self.assertIn("ISO 31030", html)
        self.assertIn("BS 8848", html)
        self.assertIn("ISO 18788", html)
        self.assertIn("UK GDPR and EU GDPR", html)
        self.assertIn("Zero-PII", html)
        self.assertIn("colour-coded", html)
        self.assertIn("organisations", html)
        self.assertIn("mailto:contact@georiskglobal.com", html)
        self.assertIn("Powered by GeoRiskGlobal", html)
        self.assertIn('class="brand-lockup" href="/"', html)
        self.assertIn('src="/static/logo.png', html)
        self.assertNotIn("<form", html.lower())
        self.assertNotIn("leaflet", html.lower())
        self.assertNotIn("claude", html.lower())
        self.assertNotIn("anthropic", html.lower())
        self.assertNotIn("/api/grok", html.lower())
        self.assertNotIn("testimonial", html.lower())
        self.assertNotIn("color-coded", html.lower())
        self.assertNotIn("organization", html.lower())

    def test_map_keeps_the_interactive_hotspot_page(self):
        page = self.client.get("/map")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        page.close()
        self.assertIn("leaflet", html.lower())
        self.assertIn('id="map"', html)
        self.assertIn('id="search-input"', html)
        self.assertIn("Area of Operations", html)
        self.assertIn('href="/map" aria-current="page"', html)
        self.assertIn('class="brand-lockup" href="/"', html)
        self.assertNotIn("Global Geopolitical Risk Intelligence &amp; Bespoke", html)

    def test_legacy_map_queries_leave_the_homepage(self):
        moved = self.client.get("/?country=Syria&region=Middle%20East")
        self.assertEqual(moved.status_code, 302)
        self.assertEqual(moved.headers["Location"], "/map?country=Syria&region=Middle%20East")
        pin = self.client.get("/?lat=33.5&lng=36.3&zoom=8")
        self.assertEqual(pin.status_code, 302)
        self.assertTrue(pin.headers["Location"].startswith("/map?"))
        bare = self.client.get("/map?country=Yemen")
        self.assertEqual(bare.status_code, 200)
        self.assertIn(b"leaflet", bare.data.lower())

    def test_navigation_logo_and_live_map_are_consistent(self):
        for path in NAV_PAGES:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            page.close()
            self.assertIn('class="brand-lockup" href="/"', html, path)
            self.assertIn('href="/map"', html, path)
            self.assertIn("Live Map", html, path)
            self.assertNotIn('href="/">Live Map', html, path)
            self.assertNotIn('href="/">Live map', html, path)

    def test_public_routes_and_claude_status_stay_available(self):
        for path in ("/", "/map", "/briefing", "/risk-assessments", "/world-risk", "/pricing"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            page.close()
        status = self.client.get("/api/ai/claude-status")
        self.assertEqual(status.status_code, 200)
        body = status.get_json()
        self.assertIn("configured", body)
        self.assertIn("on_demand", body)
        status.close()
        data = self.client.get("/api/data")
        self.assertEqual(data.status_code, 200)
        data.close()


if __name__ == "__main__":
    unittest.main()
