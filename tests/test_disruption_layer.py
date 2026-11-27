import os
import unittest
from unittest import mock

import requests

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

import disruption_layer


AIR_KEYS = {
    "polygon", "center", "fir", "region", "severity", "advisory_summary",
    "source", "updated_at",
}
GPS_KEYS = {"center", "radius_km", "severity", "operational_impact", "source"}
SEA_KEYS = {"coords", "chokepoint", "threat_level", "live_incident_count", "summary", "source"}


def offline_get(*args, **kwargs):
    raise requests.Timeout("timed out")


class DisruptionLayerTests(unittest.TestCase):
    def setUp(self):
        disruption_layer.clear_cache()

    def test_api_schema_and_corridor_geometry(self):
        payload = disruption_layer.build_payload(feeds=[], incidents=[])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cache_ttl_seconds"], 15 * 60)
        self.assertFalse(payload["cached"])
        self.assertTrue(payload["disclaimer"])
        for item in payload["airspace"]:
            self.assertTrue(AIR_KEYS <= set(item))
            self.assertGreaterEqual(len(item["polygon"]), 3)
            self.assertEqual(len(item["center"]), 2)
            self.assertIn(item["severity"], {"high", "medium", "low"})
            self.assertTrue(item["advisory_summary"])
            self.assertTrue(item["source"])
            self.assertTrue(item["updated_at"])
        for item in payload["gps_jamming"]:
            self.assertTrue(GPS_KEYS <= set(item))
            self.assertEqual(len(item["center"]), 2)
            self.assertGreater(item["radius_km"], 0)
            self.assertTrue(item["operational_impact"])
            self.assertTrue(item["source"])
        for item in payload["maritime"]:
            self.assertTrue(SEA_KEYS <= set(item))
            self.assertEqual(len(item["coords"]), 2)
            self.assertIn(item["threat_level"], {"high", "medium", "low"})
            self.assertIsInstance(item["live_incident_count"], int)
            self.assertTrue(item["summary"])
            self.assertTrue(item["source"])
        air = {item["id"]: item for item in payload["airspace"]}
        sea = {item["id"]: item for item in payload["maritime"]}
        self.assertTrue(disruption_layer.point_in_polygon(50.45, 30.52, air["ukraine"]["polygon"]))
        self.assertTrue(disruption_layer.point_in_polygon(26.62, 56.45, sea["hormuz"]["polygon"]))
        self.assertTrue(disruption_layer.point_in_polygon(12.6, 43.35, sea["bab-el-mandeb"]["polygon"]))

    def test_cache_serves_the_same_payload_until_ttl(self):
        calls = {"n": 0}

        def counting_timeout(*args, **kwargs):
            calls["n"] += 1
            raise requests.Timeout("timed out")

        with mock.patch("disruption_layer.requests.get", side_effect=counting_timeout):
            first = disruption_layer.get_disruptions(now=1_000)
            second = disruption_layer.get_disruptions(now=1_100)
            third = disruption_layer.get_disruptions(now=1_000 + disruption_layer.CACHE_TTL_SECONDS + 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["generated_at"], second["generated_at"])
        self.assertFalse(third["cached"])
        self.assertEqual(calls["n"], len(disruption_layer.FEEDS) * 2)
        self.assertTrue(all(feed["ok"] is False for feed in second["feeds"]))

    def test_timeout_falls_back_to_baseline_and_counts_maritime_osint(self):
        with mock.patch("disruption_layer.requests.get", side_effect=offline_get):
            payload = disruption_layer.get_disruptions(now=50)
        self.assertTrue(payload["airspace"])
        self.assertTrue(payload["gps_jamming"])
        self.assertTrue(payload["maritime"])
        self.assertTrue(payload["feeds"])
        self.assertTrue(all(feed["ok"] is False and feed["error"] == "Timeout" for feed in payload["feeds"]))
        self.assertTrue(all("baseline" in item["source"].lower() or item["source"] for item in payload["airspace"]))
        hormuz = next(item for item in payload["maritime"] if item["id"] == "hormuz")
        self.assertGreaterEqual(hormuz["live_incident_count"], 1)
        self.assertIn("Hormuz", hormuz["chokepoint"])

        synthetic = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [101.3, 2.6]},
            "properties": {
                "title": "Robbery reported from a vessel in the Singapore Strait",
                "summary": "Maritime shipping incident near Malacca.",
                "timestamp": "2026-11-26T00:00:00Z",
                "source": "https://www.recaap.org/",
            },
        }]
        built = disruption_layer.build_payload(feeds=[{
            "id": "easa-czib",
            "name": "EASA CZIB",
            "url": "https://example.test/czib",
            "ok": False,
            "error": "Timeout",
            "entries": [],
        }], incidents=synthetic)
        malacca = next(item for item in built["maritime"] if item["id"] == "malacca")
        self.assertGreaterEqual(malacca["live_incident_count"], 1)
        ukraine = next(item for item in built["airspace"] if item["id"] == "ukraine")
        self.assertEqual(ukraine["origin"], "baseline")

    def test_live_feed_hit_updates_the_matching_advisory(self):
        feeds = [{
            "id": "easa-czib",
            "name": "EASA CZIB",
            "url": "https://www.easa.europa.eu/en/domains/air-operations/czibs/feed.xml",
            "ok": True,
            "error": "",
            "entries": [{
                "title": "Airspace of Ukraine",
                "summary": "CZIB remains active.",
                "url": "https://www.easa.europa.eu/en/domains/air-operations/czibs",
                "updated_at": "2026-11-20T12:00:00Z",
                "kind": "airspace",
            }],
        }]
        payload = disruption_layer.build_payload(feeds=feeds, incidents=[])
        ukraine = next(item for item in payload["airspace"] if item["id"] == "ukraine")
        self.assertEqual(ukraine["origin"], "live")
        self.assertIn("Airspace of Ukraine", ukraine["advisory_summary"])
        self.assertEqual(ukraine["updated_at"], "2026-11-20T12:00:00Z")
        self.assertIn("EASA CZIB", ukraine["source"])

    def test_route_schema_and_script_are_served(self):
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)
        from flask import Flask

        mini = Flask(__name__)
        mini.register_blueprint(disruption_layer.disruption_bp)
        mini.testing = True
        with mock.patch("disruption_layer.requests.get", side_effect=offline_get):
            disruption_layer.clear_cache()
            response = mini.test_client().get("/api/disruptions")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("airspace", body)
        self.assertIn("gps_jamming", body)
        self.assertIn("maritime", body)
        self.assertEqual(body["airspace"][0]["severity"] in {"high", "medium", "low"}, True)

        import app as app_module

        app_module.app.testing = True
        client = app_module.app.test_client()
        script = client.get("/static/disruption-layer.js")
        try:
            self.assertEqual(script.status_code, 200)
            self.assertIn(b"Airspace &amp; Maritime", script.data)
            self.assertIn(b"/api/disruptions", script.data)
        finally:
            script.close()
        page = client.get("/")
        try:
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"/static/disruption-layer.js", page.data)
        finally:
            page.close()
        with mock.patch("disruption_layer.requests.get", side_effect=offline_get):
            disruption_layer.clear_cache()
            live = client.get("/api/disruptions")
        self.assertEqual(live.status_code, 200)
        self.assertIn(b'"gps_jamming"', live.data)
        live.close()


if __name__ == "__main__":
    unittest.main()
