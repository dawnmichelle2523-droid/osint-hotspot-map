import json
import os
import unittest

import feed_quality as fq
import global_feeds as gf
import ppv


class FeedCatalogTests(unittest.TestCase):
    def test_required_live_feeds_are_wired(self):
        ids = {feed["id"] for feed in gf._feeds()}
        for required in (
            "eonet", "eonet-wildfires", "eonet-severestorms", "gdacs-rss", "ifrc",
            "cisa-kev", "vic-emergency", "qld-bushfire", "uk-floods", "emsc",
            "met-norway", "dabanga", "tamazuj", "sachet", "gdelt", "wire-bbc",
            "tsunami-ntwc", "tsunami-ptwc", "tsunami-ntwc-cap", "tsunami-ptwc-cap",
            "nws-tsunami", "nhc", "nhc-ep", "nhc-cp", "jtwc", "wfigs", "inciweb", "gvp",
        ):
            self.assertIn(required, ids)
        urls = " ".join(feed.get("url") or "" for feed in gf._feeds())
        self.assertNotIn("events4app", urls)
        self.assertNotIn("incident_FCAD.json", urls)
        self.assertNotIn("cybersecurity-advisories/alerts.xml", urls)
        self.assertNotIn("effis.jrc.ec.europa.eu", urls)

    def test_cap_tsunami_warning_is_geolocated_and_tagged(self):
        cap = """<?xml version="1.0"?>
        <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
          <identifier>PAAQ-1</identifier>
          <sent>2026-11-27T10:00:00-00:00</sent>
          <info>
            <event>Tsunami Warning</event>
            <headline>Tsunami warning for the Aleutians</headline>
            <description>A tsunami warning is in effect.</description>
            <area><areaDesc>Aleutian Islands</areaDesc><circle>52.0,-176.0 0</circle></area>
          </info>
        </alert>"""
        feed = {"id": "tsunami-ntwc-cap", "name": "NTWC", "region": "United States", "category": "Tsunami", "url": "https://example.test/cap"}
        events = gf.parse_cap(feed, cap.encode())
        self.assertEqual(len(events), 1)
        self.assertIn("Tsunami", events[0]["title"])
        self.assertEqual(events[0]["feed_category"], "Tsunami")
        self.assertAlmostEqual(events[0]["lat"], 52.0, delta=0.2)
        self.assertEqual(events[0]["severity"], "high")

    def test_quiet_cap_is_not_pinned(self):
        cap = b"<alert><info><event>Tsunami Information</event><description>There is no tsunami threat.</description></info></alert>"
        feed = {"id": "tsunami-ptwc-cap", "name": "PTWC", "region": "United States", "category": "Tsunami", "url": "https://example.test/cap"}
        self.assertEqual(gf.parse_cap(feed, cap), [])

    def test_wildfire_name_is_read_from_geojson(self):
        payload = {"features": [{"geometry": {"type": "Point", "coordinates": [-121.5, 44.1]}, "properties": {"IncidentName": "Bear Wallow", "POOState": "US-OR"}}]}
        feed = {"id": "wfigs", "name": "NIFC", "region": "United States", "category": "Natural Disaster", "url": "https://example.test/wfigs", "default_country": "United States"}
        events = gf.parse_geojson(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["title"], "Bear Wallow")
        self.assertEqual(events[0]["country"], "United States")
        self.assertEqual(events[0]["feed_category"], "Natural Disaster")

    def test_security_wire_keeps_only_incident_headlines(self):
        rss = """<?xml version="1.0"?><rss><channel>
          <item><title>Market protest in Nairobi after clashes</title><description>Security forces fired tear gas.</description><link>https://example.test/nairobi</link></item>
          <item><title>Gallery opens a new wing</title><description>The museum added three rooms.</description><link>https://example.test/art</link></item>
        </channel></rss>"""
        feed = {"id": "wire-bbc", "name": "BBC", "region": "Global", "category": "Security", "url": "https://example.test/bbc", "limit": 8}
        events = gf.parse_security_rss(feed, rss.encode())
        self.assertEqual(len(events), 1)
        self.assertIn("Nairobi", events[0]["title"])
        self.assertEqual(events[0]["country"], "Kenya")

    def test_prepare_events_translates_before_the_map_sees_them(self):
        def fake(text, live=True, ingest=False):
            self.assertTrue(ingest)
            if "inondation" in text.lower() or "à" in text:
                return "Flood alert for Lyon"
            return text

        original = fq.translate_text
        fq.translate_text = fake
        try:
            events = [{
                "id": "lyon",
                "title": "Alerte inondation à Lyon",
                "summary": "Vigilance rouge pour le Rhône",
                "lat": 45.76,
                "lng": 4.84,
                "country": "France",
                "region": "UK & Europe",
                "theatre": "UK & Europe",
                "feed_category": "Weather",
                "severity": "high",
                "timestamp": "2026-11-27T10:00:00Z",
                "source": "https://example.test/lyon",
                "source_name": "MeteoAlarm France",
            }]
            prepared = gf.prepare_events(events)
        finally:
            fq.translate_text = original
        self.assertEqual(prepared[0]["title"], "Flood alert for Lyon")
        self.assertNotIn("inondation", prepared[0]["summary"].lower())

    def test_uk_flood_uses_the_county_gazetteer(self):
        payload = {"items": [{"severity": "Flood Warning", "severityLevel": 2, "message": "Flooding is expected.", "eaAreaName": "East", "floodArea": {"county": "Norfolk"}, "floodAreaID": "nf1"}]}
        feed = {"id": "uk-floods", "name": "UK Flood Warnings", "region": "UK & Europe", "category": "Weather", "url": "https://example.test/floods", "default_country": "United Kingdom"}
        events = gf.parse_uk_floods(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["country"], "United Kingdom")
        self.assertAlmostEqual(events[0]["lat"], 52.61, delta=0.4)


class TranslationTests(unittest.TestCase):
    def test_ingest_translation_is_not_blocked_by_the_interactive_budget(self):
        fq._live_budget["n"] = 50
        calls = {"n": 0}

        def fake(text):
            calls["n"] += 1
            return "Flood alert"

        original = (fq._google_translate, fq._grok_translate, fq._mymemory_translate, fq._cache_get, fq._cache_put)
        fq._google_translate = fake
        fq._grok_translate = lambda text: ""
        fq._mymemory_translate = lambda text: ""
        fq._cache_get = lambda digest: ""
        fq._cache_put = lambda digest, text: None
        try:
            self.assertIn("flom", "Farevarsel flom og jordskjelv")
            self.assertTrue(fq.needs_translation("Farevarsel flom og jordskjelv"))
            self.assertTrue(fq.needs_translation("बाढ़ की चेतावनी"))
            turned = fq.translate_text("Alerte inondation à Lyon", live=True, ingest=True)
        finally:
            fq._google_translate, fq._grok_translate, fq._mymemory_translate, fq._cache_get, fq._cache_put = original
            fq._live_budget["n"] = 0
        self.assertEqual(turned, "Flood alert")
        self.assertEqual(calls["n"], 1)


class PassTests(unittest.TestCase):
    def setUp(self):
        os.environ["PPV_SECRET"] = "test-secret"
        ppv.set_current(None)

    def tearDown(self):
        os.environ.pop("PPV_SECRET", None)
        ppv.set_current(None)

    def test_token_round_trip_and_region_filter(self):
        token = ppv.issue("region", "UK & Europe", "desk@example.com", days=7)
        grant = ppv.verify(token)
        self.assertEqual(grant["scope"], "UK & Europe")
        self.assertNotIn("test-secret", token)
        ppv.set_current(grant)
        payload = ppv.restrict({
            "features": [
                {"properties": {"title": "Lyon flood", "country": "France", "region": "UK & Europe"}},
                {"properties": {"title": "Nairobi clash", "country": "Kenya", "region": "Africa"}},
            ],
            "events": [],
            "total_count": 2,
        })
        titles = [item["properties"]["title"] for item in payload["features"]]
        self.assertEqual(titles, ["Lyon flood"])
        self.assertEqual(payload["total_count"], 1)
        self.assertTrue(payload["ppv"]["active"])
        self.assertIsNone(ppv.verify(token + "x"))


if __name__ == "__main__":
    unittest.main()
