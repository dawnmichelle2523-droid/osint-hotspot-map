import json
import unittest

import global_feeds as gf


class GlobalFeedParserTests(unittest.TestCase):
    def test_usgs_places_a_japan_quake_in_apac(self):
        payload = {
            "features": [{
                "id": "us1000",
                "geometry": {"type": "Point", "coordinates": [142.1, 38.2, 10]},
                "properties": {"mag": 6.2, "place": "20 km E of Sendai, Japan", "time": 1700000000000, "title": "M 6.2 - Sendai", "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us1000"},
            }]
        }
        feed = {"id": "usgs-quakes", "name": "USGS", "region": "Global", "category": "Seismic", "url": "https://example.test/usgs"}
        events = gf.parse_usgs(feed, json.dumps(payload).encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["theatre"], "APAC")
        self.assertEqual(events[0]["severity"], "high")
        self.assertAlmostEqual(events[0]["lat"], 38.2)

    def test_us_advisory_keeps_level_3_and_4_only(self):
        payload = [
            {"Title": "Syria Travel Advisory", "Category": ["Level 4: Do Not Travel"], "Summary": "Do not travel", "Link": "https://travel.state.gov/syria"},
            {"Title": "Spain Travel Advisory", "Category": ["Level 2: Exercise Increased Caution"], "Summary": "Increased caution", "Link": "https://travel.state.gov/spain"},
        ]
        feed = {"id": "us-advisories", "name": "US", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/us"}
        events = gf.parse_us_advisory(feed, json.dumps(payload).encode())
        self.assertEqual([item["country"] for item in events], ["Syria"])
        self.assertEqual(events[0]["severity"], "high")
        self.assertEqual(events[0]["theatre"], "Middle East")

    def test_canada_numeric_advisory_state(self):
        payload = {"data": {"AF": {"country-eng": "Afghanistan", "advisory-state": 3, "date-published": {"date": "2026-11-25"}}}}
        feed = {"id": "canada-advisories", "name": "Canada", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/ca"}
        events = gf.parse_canada_advisory(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["severity"], "high")
        self.assertEqual(events[0]["theatre"], "Afghanistan")

    def test_volcano_uses_known_coordinates_when_the_feed_has_no_lat(self):
        payload = [{"volcano_name": "Great Sitkin", "alert_level": "WATCH", "obs_abbr": "avo", "vnum": "311120"}]
        feed = {"id": "usgs-volcanoes", "name": "USGS Volcanoes", "region": "United States", "category": "Volcano", "url": "https://example.test/volcano"}
        events = gf.parse_volcano(feed, json.dumps(payload).encode())
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["lat"], 52.076, delta=0.8)
        self.assertEqual(events[0]["theatre"], "United States")

    def test_canada_avoid_all_travel(self):
        payload = {"data": [{"country-eng": "Afghanistan", "advisory-text": "Avoid all travel", "url": "https://travel.gc.ca/destinations/afghanistan"}]}
        feed = {"id": "canada-advisories", "name": "Canada", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/ca"}
        events = gf.parse_canada_advisory(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["theatre"], "Afghanistan")
        self.assertEqual(events[0]["severity"], "high")

    def test_uk_page_level_reads_alert_status(self):
        self.assertEqual(gf.uk_page_level(["avoid_all_travel_to_whole_country"]), 4)
        self.assertEqual(gf.uk_page_level(["avoid_all_but_essential_travel_to_parts"]), 2)
        self.assertEqual(gf.uk_page_level([]), 1)

    def test_keyed_source_stays_idle_without_a_secret(self):
        feed = {"id": "acled", "name": "ACLED", "region": "Global", "category": "Conflict", "parser": "keyed", "env": ("ACLED_API_KEY", "ACLED_EMAIL"), "needs_all": True}
        result = gf._keyed_status(feed)
        self.assertEqual(result["status"], "standby")
        self.assertEqual(result["events"], [])
        self.assertNotIn("ACLED", json.dumps(result["events"]))

    def test_one_bad_feed_does_not_drop_the_other(self):
        feeds = {
            "usgs-quakes": {"name": "USGS", "region": "Global", "category": "Seismic", "status": "active", "events": [
                {"id": "q1", "title": "Quake", "summary": "Quake", "lat": 10, "lng": 20, "country": "Japan", "region": "APAC", "theatre": "APAC", "feed_category": "Seismic", "severity": "high", "timestamp": "2026-01-01T00:00:00Z", "source": "https://example.test/q", "source_name": "USGS"}
            ]},
            "deepstate": {"name": "DeepState", "region": "Russia & Ukraine", "category": "Conflict", "status": "offline", "error": "rate limited", "events": []},
        }
        with gf._lock:
            gf._memory["feeds"] = feeds
            gf._memory["at"] = gf.time.time()
        rows = gf.features()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["properties"]["theatre"], "APAC")
        status = {item["source_name"]: item["status"] for item in gf.status_rows()}
        self.assertEqual(status["DeepStateMap Snapshot"], "offline")
        self.assertIn(status["USGS Earthquakes M4.5+"], {"active", "unchecked"})


if __name__ == "__main__":
    unittest.main()
