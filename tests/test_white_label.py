import html
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"

import white_label
from app import app

ATTRIBUTION = "Powered by GeoRiskIntel"


class WhiteLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        cls.client = app.test_client()

    def test_studio_and_widget_return_200(self):
        studio = self.client.get("/white-label")
        self.assertEqual(studio.status_code, 200)
        page = studio.get_data(as_text=True)
        self.assertIn("White-Label Studio", page)
        self.assertIn(ATTRIBUTION, page)
        self.assertIn('class="powered-logo" src="/static/logo.png" alt="GeoRiskIntel"', page)
        self.assertIn('id="iframe-snippet"', page)
        self.assertIn('id="script-snippet"', page)
        self.assertEqual(studio.headers.get("X-Frame-Options"), "SAMEORIGIN")
        widget = self.client.get("/embed/widget")
        self.assertEqual(widget.status_code, 200)
        self.assertIn("text/html", widget.content_type)
        self.assertIn(ATTRIBUTION, widget.get_data(as_text=True))
        policy = widget.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors *", policy)
        self.assertIn("img-src 'self' https:", policy)
        self.assertIn("script-src 'none'", policy)
        self.assertIsNone(widget.headers.get("X-Frame-Options"))
        self.assertEqual(self.client.get("/pricing").status_code, 200)

    def test_embed_sets_no_tracking_cookies_or_third_parties(self):
        response = self.client.get("/embed/widget?country=Yemen&mode=map&org=Atlas")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        body = response.get_data(as_text=True).lower()
        for needle in (
            "gtag",
            "google-analytics",
            "googletagmanager",
            "facebook",
            "hotjar",
            "doubleclick",
            "segment.com",
            "unpkg.com",
            "fonts.googleapis",
            "jsdelivr",
            "cookie",
            "<script",
        ):
            self.assertNotIn(needle, body, needle)
        self.assertIn("powered by georiskintel", body)
        tainted = self.client.get("/embed/widget?country=Yemen&pass=not-a-tracker")
        self.assertEqual(tainted.status_code, 200)
        self.assertEqual(tainted.headers.getlist("Set-Cookie"), [])
        self.assertIn(ATTRIBUTION, tainted.get_data(as_text=True))

    def test_grok_ops_links_to_the_studio(self):
        page = self.client.get("/grok-ops")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/white-label"', page.get_data(as_text=True))

    def test_sanitizers_reject_scriptable_brand_values(self):
        self.assertNotIn("<", white_label.sanitize_org("<script>alert(1)</script>"))
        self.assertNotIn("javascript:", white_label.sanitize_org("javascript:alert(1)").lower())
        self.assertEqual(white_label.sanitize_logo("javascript:alert(1)"), "")
        self.assertEqual(white_label.sanitize_logo("data:text/html,<script>alert(1)</script>"), "")
        self.assertEqual(white_label.sanitize_logo("http://cdn.example.com/logo.png"), "")
        self.assertEqual(white_label.sanitize_logo("https://user:pass@cdn.example.com/logo.png"), "")
        self.assertEqual(white_label.sanitize_logo('https://cdn.example.com/a.png" onerror="alert(1)'), "")
        self.assertEqual(white_label.sanitize_logo("https://cdn.example.com/logo.png"), "https://cdn.example.com/logo.png")
        self.assertEqual(white_label.sanitize_accent("red;}</style><script>alert(1)</script>"), "#D4AF37")
        self.assertEqual(white_label.sanitize_accent("#1b4"), "#11BB44")
        self.assertEqual(white_label.sanitize_accent("#1b4f8a"), "#1B4F8A")

    def test_embed_strips_xss_from_org_logo_and_accent(self):
        response = self.client.get("/embed/widget", query_string={
            "org": '<script>alert(1)</script><img src=x onerror=alert(1)>',
            "logo": "javascript:alert(1)",
            "accent": "#fff;}</style><script>alert(1)</script>",
            "country": "Yemen",
            "theme": 'dark"><script>alert(1)</script>',
            "mode": 'badge"><script>alert(1)</script>',
        })
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("javascript:", body.lower())
        self.assertNotIn("onerror", body.lower())
        self.assertNotIn("data:text/html", body.lower())
        self.assertIn("#D4AF37", body)
        self.assertIn('data-mode="badge"', body)
        self.assertIn('data-theme="dark"', body)
        self.assertIn(ATTRIBUTION, body)
        data_logo = self.client.get("/embed/widget", query_string={
            "logo": "data:text/html,<svg/onload=alert(1)>",
            "country": "Yemen",
        })
        self.assertNotIn("data:text/html", data_logo.get_data(as_text=True).lower())
        hosted = self.client.get("/embed/widget", query_string={
            "org": "Atlas & Travel",
            "logo": "https://cdn.example.com/logo.png",
            "accent": "#1b4f8a",
            "country": "Yemen",
        })
        hosted_body = hosted.get_data(as_text=True)
        self.assertIn("Atlas &amp; Travel", hosted_body)
        self.assertIn("https://cdn.example.com/logo.png", hosted_body)
        self.assertIn("#1B4F8A", hosted_body)
        self.assertIn(ATTRIBUTION, hosted_body)

    def test_every_mode_keeps_the_attribution_bar(self):
        hides = (
            "",
            "attribution=0",
            "powered=0",
            "footer=0",
            "hide=1",
            "branding=off",
            "logo=0",
            "logo=",
            "logo=https://cdn.example.com/other-logo.png",
            "theme=light",
            "theme=dark",
            "accent=%23ffffff",
        )
        logo_tag = re.compile(
            r'<img class="powered-logo" src="([^"]+)" alt="GeoRiskIntel"'
        )
        for mode in ("badge", "ticker", "map"):
            for extra in hides:
                query = f"mode={mode}&country=Yemen&org=Atlas"
                if extra:
                    query += "&" + extra
                response = self.client.get("/embed/widget?" + query)
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200, query)
                self.assertIn(ATTRIBUTION, body, query)
                self.assertIn('data-attribution="required"', body, query)
                bar = re.search(
                    r'<a class="powered" data-attribution="required".*?</a>',
                    body,
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(bar, query)
                mark = logo_tag.search(bar.group(0))
                self.assertIsNotNone(mark, query)
                src = mark.group(1)
                self.assertTrue(src.endswith("/static/logo.png"), query + " " + src)
                self.assertNotIn("other-logo.png", src, query)
                self.assertNotIn("cdn.example.com", bar.group(0), query)
                self.assertLess(bar.group(0).find('class="powered-logo"'), bar.group(0).find(ATTRIBUTION), query)
                policy = response.headers.get("Content-Security-Policy", "")
                self.assertIn("frame-ancestors *", policy, query)
                self.assertIn("img-src 'self' https:", policy, query)
                if src.startswith(("http://", "https://")):
                    origin = src[: -len("/static/logo.png")]
                    self.assertIn(origin, policy, query)
                self.assertIn("height: 18px", body, query)
                self.assertIn("object-fit: contain", body, query)

    def test_live_country_data_binding(self):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        features = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [36.3, 33.5]},
            "properties": {
                "title": "Verified checkpoint closure near Damascus",
                "summary": "Official notice of a checkpoint closure.",
                "country": "Syria",
                "severity": "high",
                "timestamp": stamp,
                "verification_tier": "Official / Primary",
                "confidence": "confirmed",
                "source": "https://example.test/syria",
                "lat": 33.5,
                "lng": 36.3,
            },
        }]
        with patch("white_label.collect_live_features", return_value=features):
            facts = white_label.load_widget_facts("Syria", "Middle East")
            response = self.client.get("/embed/widget?country=Syria&region=Middle%20East&mode=badge&org=Atlas")
        self.assertEqual(facts["incident_count"], 1)
        self.assertEqual(facts["headline"], "Verified checkpoint closure near Damascus")
        self.assertEqual(facts["confidence"], "Verified")
        self.assertEqual(facts["headline_kind"], "Latest verified headline")
        self.assertIn(facts["tier"], {"LOW", "MEDIUM", "HIGH", "EXTREME"})
        body = response.get_data(as_text=True)
        self.assertIn(html.escape(facts["headline"], quote=True), body)
        self.assertIn(f'data-incident-count="{facts["incident_count"]}"', body)
        self.assertIn('data-confidence="Verified"', body)
        self.assertIn(f'data-tier="{facts["tier"]}"', body)
        self.assertIn("Syria", body)
        self.assertIn(ATTRIBUTION, body)
        self.assertIn('data-live="1"', body)

    def test_live_hotspots_bind_into_the_widget(self):
        facts = white_label.load_widget_facts("Yemen", "")
        response = self.client.get("/embed/widget?country=Yemen&mode=map")
        body = response.get_data(as_text=True)
        self.assertGreaterEqual(facts["incident_count"], 1)
        self.assertTrue(facts["headline"])
        self.assertIn(facts["tier"], {"LOW", "MEDIUM", "HIGH", "EXTREME"})
        self.assertIn(facts["confidence"], white_label.CONFIDENCE_LABELS)
        self.assertIn(html.escape(facts["headline"], quote=True), body)
        self.assertIn(f'data-incident-count="{facts["incident_count"]}"', body)
        self.assertIn(f'data-tier="{facts["tier"]}"', body)
        self.assertIn(f'data-confidence="{html.escape(facts["confidence"], quote=True)}"', body)
        self.assertIn(ATTRIBUTION, body)


if __name__ == "__main__":
    unittest.main()
