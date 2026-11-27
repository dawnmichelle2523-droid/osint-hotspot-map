"""Area of Operations filters, geopolitical summaries, country templates, and pricing."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

import geo_split

ROOT = Path(__file__).resolve().parents[1]


def incident(title, country, region, severity="medium", category="Conflict", timestamp="2026-11-26T12:00:00Z"):
    return {"properties": {
        "title": title, "country": country, "region": region, "severity": severity,
        "category": category, "timestamp": timestamp,
    }}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            incident("Aleppo clash", "Syria", "Middle East", "high", "Conflict", "2026-11-26T12:00:00Z"),
            incident("Damascus outage", "Syria", "Middle East", "low", "Infrastructure", "2026-11-25T12:00:00Z"),
            incident("Paris protest", "France", "UK & Europe", "medium", "Security", "2026-11-26T08:00:00Z"),
        ]

    def test_country_summary_uses_baseline_and_live_counts(self):
        summary = geo_split.compose_summary(self.events, "Middle East", "Syria")
        self.assertEqual(summary["xai_credits"], 0)
        self.assertEqual(summary["scope"], "country")
        self.assertEqual(summary["country"], "Syria")
        self.assertIn("active conflict", summary["stability"].lower())
        self.assertEqual(summary["trend"]["total"], 2)
        self.assertEqual(summary["trend"]["high"], 1)
        self.assertEqual(summary["trend"]["headlines"][0], "Aleppo clash")
        self.assertIn("elevated", summary["trend"]["sentence"].lower())
        self.assertNotIn("Paris", " ".join(summary["trend"]["headlines"]))

    def test_region_summary_excludes_other_regions(self):
        europe = geo_split.compose_summary(self.events, "Europe", "")
        self.assertEqual(europe["scope"], "region")
        self.assertEqual(europe["trend"]["total"], 1)
        self.assertIn("Paris protest", europe["trend"]["headlines"])
        middle = geo_split.compose_summary(self.events, "Middle East", "")
        self.assertEqual(middle["trend"]["total"], 2)
        self.assertNotIn("Paris protest", middle["trend"]["headlines"])

    def test_empty_area_states_the_baseline(self):
        summary = geo_split.compose_summary([], "Africa", "Kenya")
        self.assertEqual(summary["trend"]["total"], 0)
        self.assertIn("No live OSINT events", summary["trend"]["sentence"])
        self.assertIn("Kenya", summary["stability"])

    def test_sudan_template_is_a_priority_country(self):
        template = geo_split.country_template("Sudan")
        self.assertEqual(template["xai_credits"], 0)
        self.assertEqual(template["country"], "Sudan")
        self.assertFalse(template["baseline"])
        self.assertIn("Khartoum", template["situation"])
        self.assertIn("Rapid Support Forces", template["actors"])
        self.assertEqual(len(template["hra_sections"]), 9)
        names = [row["category"] for row in template["hazards"]]
        self.assertEqual(names, list(geo_split.RISK_CATEGORIES))
        war = template["hazards"][0]
        self.assertEqual(war["category"], "Conflict/War")
        self.assertGreaterEqual(war["likelihood"] * war["impact"], 17)
        self.assertIn("Port Sudan", " ".join(item["city"] for item in template["facilities"]))
        self.assertIn("roster", template["local_only_fields"])
        self.assertEqual(template["branding"], "GeoRiskGlobal")

    def test_legacy_theatre_still_matches_a_country(self):
        self.assertTrue(geo_split.event_matches(self.events[2], "UK & Europe", "France"))
        self.assertFalse(geo_split.event_matches(self.events[0], "Europe", ""))
        self.assertTrue(geo_split.event_matches(self.events[0], "Middle East", "Syria"))


class PricingTests(unittest.TestCase):
    def test_country_region_global_and_pass_math(self):
        one = geo_split.quote_ao("country", 1, "none", "gbp")
        self.assertEqual(one["total"], 149)
        self.assertEqual(one["xai_credits"], 0)
        self.assertEqual(one["suggestion"], "")
        four = geo_split.quote_ao("country", 4, "none", "gbp")
        self.assertEqual(four["base"], 296)
        self.assertIn("Four or more", four["suggestion"])
        self.assertIn("£399", four["suggestion"])
        weekly = geo_split.quote_ao("country", 4, "weekly", "usd")
        self.assertEqual(weekly["base"], 375)
        self.assertEqual(weekly["briefing_total"], 300)
        self.assertEqual(weekly["total"], 675)
        region = geo_split.quote_ao("region", 2, "none", "gbp")
        self.assertEqual(region["total"], 598)
        wide = geo_split.quote_ao("region", 4, "none", "gbp")
        self.assertEqual(wide["total"], 996)
        self.assertIn("Global All-Access", wide["suggestion"])
        globe = geo_split.quote_ao("global", 9, "bundle", "gbp")
        self.assertEqual(globe["count"], 1)
        self.assertEqual(globe["total"], 1548)
        self.assertIn("Okta SSO", globe["includes"])
        self.assertIn("White-Label Studio", globe["summary"])
        day = geo_split.quote_ao("pass", 3, "daily", "gbp", "annual")
        self.assertEqual(day["total"], 59)
        self.assertIn("7-day", day["summary"])
        self.assertIn("not added", day["suggestion"])
        annual = geo_split.quote_ao("country", 1, "none", "gbp", "annual")
        self.assertEqual(annual["total"], 1490)
        self.assertTrue(annual["total_label"].endswith("/yr"))

    def test_quote_drops_personal_fields(self):
        quote = geo_split.quote_ao("country", "nope", "weekly", "usd")
        self.assertEqual(quote["count"], 1)
        self.assertNotIn("email", quote["summary"].lower())


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        for name in ("PREVIEW_PASSWORD", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
            os.environ.pop(name, None)
        import app as app_module

        cls.client = app_module.app.test_client()

    def test_map_countries_and_assessments_expose_the_area_and_pdf(self):
        home = self.client.get("/")
        countries = self.client.get("/countries")
        assessments = self.client.get("/risk-assessments")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(countries.status_code, 200)
        self.assertEqual(assessments.status_code, 200)
        home_html = home.get_data(as_text=True)
        country_html = countries.get_data(as_text=True)
        ra_html = assessments.get_data(as_text=True)
        for text in (home_html, country_html, ra_html):
            self.assertIn("Area of Operations", text)
            self.assertIn("/static/geo-split.js", text)
        self.assertIn('id="ao-pdf-link"', home_html)
        self.assertIn("auto=1", home_html)
        self.assertIn("£59 ($75)", home_html)
        self.assertIn("Subscribers: included", home_html)
        self.assertIn("Download ", country_html)
        self.assertIn("Risk Assessment (PDF)", country_html)
        self.assertIn("auto=1", country_html)
        self.assertIn("£59 ($75)", country_html)
        self.assertIn("Geopolitical summary", home_html)
        self.assertIn("Geopolitical summary", country_html)
        self.assertIn("2. Risk assessment", ra_html)
        self.assertIn("7. Travelling staff details", ra_html)
        self.assertIn('href="/privacy"', ra_html)
        self.assertIn('href="/check-in"', ra_html)
        self.assertIn("Sudan", ra_html)
        self.assertIn("id=\"country\"", ra_html)
        self.assertIn("Clear Local Browser Data", ra_html)
        self.assertIn("auto=1", ra_html)
        self.assertIn("/static/ra-hra.js", ra_html)
        home.close()
        countries.close()
        assessments.close()

    def test_summary_and_pricing_endpoints_do_not_use_xai(self):
        summary = self.client.get("/api/geo/summary?region=Africa&country=Sudan")
        self.assertEqual(summary.status_code, 200)
        body = summary.get_json()
        self.assertEqual(body["xai_credits"], 0)
        self.assertEqual(body["country"], "Sudan")
        self.assertIn("stability", body)
        self.assertIn("trend", body)
        template = self.client.get("/api/geo/template?country=Sudan")
        self.assertEqual(template.get_json()["country"], "Sudan")
        self.assertIn("Conflict/War", [row["category"] for row in template.get_json()["hazards"]])
        quote = self.client.get("/api/pricing/ao?mode=pass&currency=usd")
        self.assertEqual(quote.get_json()["total"], 75)
        self.assertEqual(quote.get_json()["xai_credits"], 0)
        catalog = self.client.get("/api/geo/catalog")
        self.assertIn("Sudan", catalog.get_json()["priority"])
        summary.close()
        template.close()
        quote.close()
        catalog.close()

    def test_pricing_page_has_the_calculator(self):
        page = self.client.get("/pricing")
        html = page.get_data(as_text=True)
        self.assertIn('id="ao-calculator"', html)
        self.assertIn('id="ao-mode"', html)
        self.assertIn('id="ao-pass"', html)
        self.assertIn("£149", html)
        self.assertIn("$189", html)
        self.assertIn("£899", html)
        self.assertIn("$1,149", html)
        self.assertIn("£59", html)
        self.assertIn("$75", html)
        self.assertIn('data-gbp-monthly="£29"', html)
        self.assertIn("/static/geo-split.js", html)
        page.close()

    def test_browser_matcher_and_quote_match_the_server(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r"""
const geo = require("./static/geo-split.js");
const events = [
  {properties:{title:"Aleppo clash", country:"Syria", region:"Middle East", severity:"high", category:"Conflict", timestamp:"2026-11-26T12:00:00Z"}},
  {properties:{title:"Paris protest", country:"France", region:"UK & Europe", severity:"medium", category:"Security", timestamp:"2026-11-26T08:00:00Z"}}
];
if (!geo.eventMatches(events[0], "Middle East", "Syria")) process.exit(2);
if (geo.eventMatches(events[0], "Europe", "")) process.exit(3);
if (!geo.eventMatches(events[1], "UK & Europe", "France")) process.exit(4);
const four = geo.quoteAo("country", 4, "none", "gbp");
if (four.total !== 296 || four.suggestion.indexOf("Four or more") < 0) process.exit(5);
const pass = geo.quoteAo("pass", 1, "none", "usd");
if (pass.total !== 75) process.exit(6);
const pinned = { get:{}, setItem:function(k,v){ this.get[k]=v; }, getItem:function(k){ return this.get[k] || null; }, removeItem:function(k){ delete this.get[k]; } };
const saved = geo.writeAo(pinned, {region:"Africa", country:"Sudan", email:"secret@example.com", name:"A Person"});
if (saved.country !== "Sudan" || saved.region !== "Africa") process.exit(7);
if (JSON.stringify(saved).indexOf("secret") >= 0) process.exit(8);
const syria = geo.countryTemplate("Syria");
if (!syria.hazards || syria.hazards.length < 8) process.exit(9);
if (syria.local_only_fields.indexOf("passport") < 0) process.exit(10);
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
