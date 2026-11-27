"""Shared navigation stays on ops pages and off field and embed surfaces."""

import html
import os
import unittest

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
for _name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
    os.environ.pop(_name, None)

import app as app_module

OPS_PAGES = (
    "/",
    "/map",
    "/methodology",
    "/contact",
    "/risk-assessments",
    "/route-risk",
    "/sentinel-zones",
    "/check-in",
    "/white-label",
    "/reports",
    "/slack-alerts",
    "/pricing",
    "/grok-ops",
    "/briefing",
    "/world-risk",
    "/countries",
    "/enterprise",
)


class SiteNavTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_ops_pages_share_one_navigation(self):
        for path in OPS_PAGES:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            self.assertIn("/static/site-nav.css", html, path)
            self.assertIn('id="page-nav"', html, path)
            self.assertNotIn('class="nav-more"', html, path)
            self.assertNotIn(">Tools</summary>", html, path)
            self.assertIn('class="brand-lockup" href="/"', html, path)
            self.assertIn('href="/methodology"', html, path)
            self.assertIn('href="/contact"', html, path)
            self.assertIn("Powered by GeoRiskGlobal", html, path)
            self.assertIn('src="/static/logo.png', html, path)
            self.assertIn("Book a Pilot / Subscribe", html, path)
            self.assertIn("page-lead", html, path)
            page.close()

    def test_field_checkin_and_embed_stay_minimal(self):
        widget = self.client.get("/embed/widget")
        self.assertEqual(widget.status_code, 200)
        body = widget.get_data(as_text=True)
        self.assertNotIn('id="page-nav"', body)
        self.assertNotIn("site-nav.js", body)
        self.assertNotIn("<script", body.lower())
        widget.close()

        import checkin_beacon

        created = self.client.post("/api/checkins", json={
            "callsign": "CREW-BEIRUT-1",
            "sector": "media",
            "cadence_minutes": 120,
            "adult_staff_only": True,
        })
        self.assertEqual(created.status_code, 200, created.get_json())
        mobile = self.client.get(created.get_json()["beacon"]["mobile_path"])
        self.assertEqual(mobile.status_code, 200)
        field = mobile.get_data(as_text=True)
        self.assertNotIn("site-nav.js", field)
        self.assertNotIn('class="nav-more"', field)
        self.assertIn("TAP TO CONFIRM SAFE", field)
        self.assertIn(html.escape(checkin_beacon.SCHOOLS_NOTICE), field)
        mobile.close()

    def test_pricing_figures_are_untouched(self):
        page = self.client.get("/pricing")
        html = page.get_data(as_text=True)
        self.assertIn('aria-current="page"', html)
        self.assertIn('data-gbp-monthly="£29"', html)
        self.assertIn('data-usd-monthly="$39"', html)
        self.assertIn("1 Live Slack Ops-Room channel included", html)
        page.close()


if __name__ == "__main__":
    unittest.main()
