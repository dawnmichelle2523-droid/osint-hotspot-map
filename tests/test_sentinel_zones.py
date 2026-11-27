import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from flask import Flask

import preview_lock
import sentinel_zones

WEBHOOK = "https://hooks.slack.com/services/T0123ABCD/B99XYZ/abcdEFGH1234"
SECRET = "abcdEFGH1234"
ROOT = Path(__file__).resolve().parents[1]


def _app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(sentinel_zones.sentinel_bp)
    return app


class SentinelZoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "sentinel_zones.db")
        self._saved = {
            "SENTINEL_DB_PATH": os.environ.get("SENTINEL_DB_PATH"),
            "SENTINEL_SLACK_SYNC": os.environ.get("SENTINEL_SLACK_SYNC"),
            "SENTINEL_EVAL_SYNC": os.environ.get("SENTINEL_EVAL_SYNC"),
            "PREVIEW_PASSWORD": os.environ.get("PREVIEW_PASSWORD"),
            "BASIC_AUTH_PASSWORD": os.environ.get("BASIC_AUTH_PASSWORD"),
        }
        os.environ["SENTINEL_DB_PATH"] = self.db
        os.environ.pop("SENTINEL_SLACK_SYNC", None)
        os.environ.pop("SENTINEL_EVAL_SYNC", None)
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _zone(self, **overrides):
        payload = {
            "name": "Beirut Bureau",
            "sector": "Media",
            "lat": 33.8938,
            "lng": 35.5018,
            "radius_km": 25,
            "min_severity": "LOW",
            "categories": "ALL",
            "alert_email": "desk@example.com",
            "active": True,
        }
        payload.update(overrides)
        return sentinel_zones.create_zone(payload)

    def _incident(self, lat, lng, **overrides):
        payload = {
            "id": "inc-beirut",
            "title": "Blast reported near the port",
            "severity": "HIGH",
            "category": "ACT",
            "lat": lat,
            "lng": lng,
            "timestamp": "2026-11-27T12:00:00Z",
        }
        payload.update(overrides)
        return payload

    def test_db_path_prefers_writable_data_then_local_file(self):
        mount = Path(self.tmp.name) / "data"
        mount.mkdir()
        fallback = Path(self.tmp.name) / "sentinel_zones.db"
        chosen = sentinel_zones.resolve_db_path("", data_dir=mount, fallback=fallback)
        self.assertEqual(chosen, mount / "sentinel_zones.db")
        missing = Path(self.tmp.name) / "missing"
        local = sentinel_zones.resolve_db_path("", data_dir=missing, fallback=fallback)
        self.assertEqual(local, fallback)
        default = sentinel_zones.resolve_db_path("", data_dir=missing)
        self.assertEqual(default.name, "sentinel_zones.db")
        override = Path(self.tmp.name) / "custom.db"
        self.assertEqual(
            sentinel_zones.resolve_db_path(str(override), data_dir=mount),
            override,
        )

    def test_haversine_distance_and_cardinal_bearing(self):
        equator = sentinel_zones.distance_km(0, 0, 0, 1)
        self.assertAlmostEqual(equator, (2 * math.pi * sentinel_zones.EARTH_RADIUS_KM) / 360, places=6)
        self.assertAlmostEqual(sentinel_zones.distance_km(0, 0, 1, 0), equator, places=6)
        self.assertAlmostEqual(sentinel_zones.distance_km(10, 20, 33, 44), sentinel_zones.distance_km(33, 44, 10, 20), places=6)
        short = sentinel_zones.distance_km(0, 179.5, 0, -179.5)
        self.assertAlmostEqual(short, equator, delta=0.2)
        self.assertLess(short, 200)
        self.assertAlmostEqual(sentinel_zones.bearing_deg(0, 0, 1, 0), 0, places=5)
        self.assertAlmostEqual(sentinel_zones.bearing_deg(0, 0, 0, 1), 90, places=5)
        self.assertAlmostEqual(sentinel_zones.bearing_deg(0, 0, -1, 0), 180, places=5)
        self.assertAlmostEqual(sentinel_zones.bearing_deg(0, 0, 0, -1), 270, places=5)
        self.assertAlmostEqual(sentinel_zones.bearing_deg(0, 179.5, 0, -179.5), 90, places=3)
        northeast = sentinel_zones.bearing_deg(0, 0, 1, 1)
        self.assertAlmostEqual(northeast, 45, delta=0.2)
        points = (
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        )
        for index, label in enumerate(points):
            self.assertEqual(sentinel_zones.compass_point(index * 22.5), label)
        self.assertEqual(sentinel_zones.compass_point(11.24), "N")
        self.assertEqual(sentinel_zones.compass_point(11.25), "NNE")
        self.assertEqual(sentinel_zones.compass_point(348.75), "N")
        phrase = sentinel_zones.cardinal_dir(0, 0, 0.015, 0.015, "Beirut Bureau")
        self.assertRegex(phrase, r"^\d+\.\d km NE of Beirut Bureau$")
        northwest = sentinel_zones.cardinal_dir(0, 0, 0.036, -0.0144, "Beirut Bureau")
        self.assertIn("NNW of Beirut Bureau", northwest)
        self.assertRegex(northwest, r"^\d+\.\d km NNW of Beirut Bureau$")

    def test_inside_outside_severity_category_and_dedup(self):
        zone = self._zone(radius_km=10, min_severity="MED", categories=["ACT"])
        inside = self._incident(33.94, 35.5018, id="near")
        outside = self._incident(34.9, 35.5018, id="far")
        quiet = self._incident(33.94, 35.5018, id="quiet", severity="LOW")
        other = self._incident(33.94, 35.5018, id="other", category="CU", severity="HIGH")
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [35.52, 33.90]},
            "properties": {
                "id": "geo-1",
                "title": "Clashes east of the bureau",
                "severity": "High",
                "category": "Kinetic/Military",
                "source": "https://example.test/geo-1",
            },
        }
        swapped = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [33.90, 35.52]},
            "properties": {"id": "swapped", "title": "Wrong axis", "severity": "HIGH", "category": "ACT"},
        }
        os.environ["SENTINEL_SLACK_SYNC"] = "1"
        with mock.patch("sentinel_zones.requests.post") as post:
            first = sentinel_zones.evaluate_incidents_for_zones(
                [inside, outside, quiet, other, feature, swapped, None, "skip"]
            )
            second = sentinel_zones.evaluate_incidents_for_zones([inside, feature])
        self.assertEqual(first["new_hits"], 2)
        self.assertEqual(first["matches"], 2)
        self.assertEqual(second["matches"], 2)
        self.assertEqual(second["new_hits"], 0)
        self.assertEqual(len(sentinel_zones.list_hits()), 2)
        phrases = {hit["phrase"] for hit in sentinel_zones.list_hits()}
        self.assertTrue(any(phrase.endswith("of Beirut Bureau") for phrase in phrases))
        self.assertTrue(any("N" in phrase or "E" in phrase for phrase in phrases))
        near = sentinel_zones.distance_km(33.8938, 35.5018, 33.94, 35.5018)
        self.assertLess(near, 10)
        self.assertGreater(sentinel_zones.distance_km(33.8938, 35.5018, 34.9, 35.5018), 10)
        self.assertEqual(post.call_count, 0)
        paused = sentinel_zones.update_zone(zone["id"], {"active": False})
        self.assertFalse(paused["active"])
        again = sentinel_zones.evaluate_incidents_for_zones([self._incident(33.94, 35.5018, id="while-paused")])
        self.assertEqual(again["new_hits"], 0)
        self.assertEqual(again["zones"], 0)

    def test_slack_block_kit_is_non_blocking_safe_and_deduped(self):
        zone = self._zone(slack_webhook_url=WEBHOOK, radius_km=50)
        incident = self._incident(33.91, 35.49, id="slack-1", title="Strike 4 km from the bureau")
        os.environ["SENTINEL_SLACK_SYNC"] = "1"
        with mock.patch("sentinel_zones.requests.post") as post:
            post.side_effect = requests.Timeout("slow")
            result = sentinel_zones.evaluate_incidents_for_zones([incident])
            self.assertTrue(result["ok"])
            self.assertEqual(result["new_hits"], 1)
            self.assertEqual(result["alerts"], 1)
            self.assertEqual(post.call_count, 1)
            args, kwargs = post.call_args
            self.assertEqual(args[0], WEBHOOK)
            self.assertEqual(kwargs["timeout"], 4)
            payload = kwargs["json"]
            self.assertIn("blocks", payload)
            self.assertEqual(payload["blocks"][0]["text"]["text"], "Sentinel Zone breach")
            rendered = str(payload)
            self.assertIn("Beirut Bureau", rendered)
            self.assertIn("km", rendered)
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(WEBHOOK, rendered)
            post.reset_mock()
            post.side_effect = None
            again = sentinel_zones.evaluate_incidents_for_zones([incident])
            self.assertEqual(again["new_hits"], 0)
            post.assert_not_called()
        self.assertTrue(zone["slack_webhook_set"])
        self.assertNotIn(SECRET, zone["slack_webhook_url"])
        self.assertIn("••••", zone["slack_webhook_url"])

    def test_masked_webhook_survives_get_and_update(self):
        created = self._zone(slack_webhook_url=WEBHOOK)
        self.assertNotEqual(created["slack_webhook_url"], WEBHOOK)
        self.assertNotIn("abcdEFGH", created["slack_webhook_url"])
        self.assertTrue(created["slack_webhook_url"].endswith("1234"))
        client = _app().test_client()
        listed = client.get("/api/sentinel-zones")
        self.assertEqual(listed.status_code, 200)
        body = listed.get_json()
        self.assertEqual(body["zones"][0]["id"], created["id"])
        encoded = listed.get_data(as_text=True)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(WEBHOOK, encoded)
        self.assertIn("••••", body["zones"][0]["slack_webhook_url"])
        updated = client.put(
            f"/api/sentinel-zones/{created['id']}",
            json={"name": "Beirut Desk", "slack_webhook_url": created["slack_webhook_url"]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["zone"]["name"], "Beirut Desk")
        self.assertNotIn(SECRET, updated.get_data(as_text=True))
        stored = sqlite3.connect(self.db).execute("SELECT slack_webhook_url FROM sentinel_zones").fetchone()
        self.assertEqual(stored[0], WEBHOOK)
        cleared = client.put(f"/api/sentinel-zones/{created['id']}", json={"slack_webhook_url": ""})
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.get_json()["zone"]["slack_webhook_set"])
        stored = sqlite3.connect(self.db).execute("SELECT slack_webhook_url FROM sentinel_zones").fetchone()
        self.assertEqual(stored[0], "")

    def test_crud_routes_hits_and_live_scan(self):
        client = _app().test_client()
        rejected = client.post("/api/sentinel-zones", json={"name": " ", "lat": 1, "lng": 2, "radius_km": 5})
        self.assertEqual(rejected.status_code, 400)
        too_wide = client.post("/api/sentinel-zones", json={
            "name": "Too wide", "lat": 1, "lng": 2, "radius_km": 501,
        })
        self.assertEqual(too_wide.status_code, 400)
        bad_hook = client.post("/api/sentinel-zones", json={
            "name": "Open hook", "lat": 1, "lng": 2, "radius_km": 5,
            "slack_webhook_url": "https://example.com/hook",
        })
        self.assertEqual(bad_hook.status_code, 400)
        self.assertNotIn("example.com", bad_hook.get_data(as_text=True))
        created = client.post("/api/sentinel-zones", json={
            "name": "Amman Hotel",
            "sector": "Hospitality",
            "lat": "31.9539",
            "lng": "35.9106",
            "radius_km": 40,
            "min_severity": "medium",
            "categories": ["HS", "ACT"],
            "alert_email": "stay@example.com",
            "slack_webhook_url": WEBHOOK,
            "active": True,
        })
        self.assertEqual(created.status_code, 200)
        zone = created.get_json()["zone"]
        self.assertEqual(zone["min_severity"], "MED")
        self.assertEqual(zone["categories"], ["HS", "ACT"])
        self.assertEqual(zone["alert_email"], "stay@example.com")
        self.assertNotIn(SECRET, created.get_data(as_text=True))
        paused = client.put(f"/api/sentinel-zones/{zone['id']}", json={"active": False})
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(paused.get_json()["zone"]["active"])
        resumed = client.put(f"/api/sentinel-zones/{zone['id']}", json={"active": True, "radius_km": 15})
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["zone"]["radius_km"], 15)
        missing = client.put("/api/sentinel-zones/999", json={"name": "Nope"})
        self.assertEqual(missing.status_code, 404)
        incident = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [35.95, 31.98]},
            "properties": {
                "title": "Road closure near the hotel",
                "severity": "HIGH",
                "category": "HS",
                "source_url": "https://example.test/amman",
            },
        }
        outside = self._incident(34.5, 36.5, id="outside-amman", severity="HIGH", category="HS")
        with mock.patch("sentinel_zones.load_live_incidents", return_value=[incident, outside]):
            scan = client.post(f"/api/sentinel-zones/{zone['id']}/check")
        self.assertEqual(scan.status_code, 200)
        body = scan.get_json()
        self.assertEqual(body["scanned"], 2)
        self.assertEqual(len(body["matches"]), 1)
        match = body["matches"][0]
        self.assertEqual(match["title"], "Road closure near the hotel")
        self.assertIn("of Amman Hotel", match["phrase"])
        self.assertIn(match["cardinal"], sentinel_zones.COMPASS_POINTS)
        self.assertGreater(match["distance_km"], 0)
        self.assertEqual(sentinel_zones.list_hits(), [])
        recorded = sentinel_zones.evaluate_incidents_for_zones([incident, outside])
        self.assertEqual(recorded["new_hits"], 1)
        hits = client.get("/api/sentinel-zones/hits")
        self.assertEqual(hits.status_code, 200)
        listed = hits.get_json()["hits"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["phrase"], match["phrase"])
        self.assertEqual(listed[0]["zone_name"], "Amman Hotel")
        self.assertNotIn(SECRET, hits.get_data(as_text=True))
        empty = client.post("/api/sentinel-zones/404/check")
        self.assertEqual(empty.status_code, 404)
        removed = client.delete(f"/api/sentinel-zones/{zone['id']}")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(client.get("/api/sentinel-zones").get_json()["zones"], [])
        self.assertEqual(client.delete(f"/api/sentinel-zones/{zone['id']}").status_code, 404)

    def test_page_and_routes_beat_the_api_catch_all(self):
        app = Flask(__name__)

        @app.route("/api/<path:endpoint>", methods=["GET", "POST"])
        def forward(endpoint):
            return {"forwarded": endpoint}, 418

        @app.get("/<path:filename>")
        def static_file(filename):
            return "catch " + filename, 418

        app.register_blueprint(sentinel_zones.sentinel_bp)
        client = app.test_client()
        page = client.get("/sentinel-zones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Sentinel Zones", page.data)
        self.assertIn(b"leaflet", page.data.lower())
        self.assertIn(b'id="radius"', page.data)
        self.assertIn(b'max="250"', page.data)
        self.assertIn(b"Beirut", page.data)
        self.assertIn(b"Scan now", page.data)
        listed = client.get("/api/sentinel-zones")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(b"forwarded", listed.data)
        hits = client.get("/api/sentinel-zones/hits")
        self.assertEqual(hits.status_code, 200)
        self.assertEqual(hits.get_json()["hits"], [])

    def test_preview_lock_still_covers_sentinel_routes(self):
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        app = Flask(__name__)
        preview_lock.install(app)
        app.register_blueprint(sentinel_zones.sentinel_bp)
        client = app.test_client()
        api = client.get("/api/sentinel-zones")
        self.assertEqual(api.status_code, 401)
        page = client.get("/sentinel-zones")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].startswith("/login"))

    def test_app_wires_evaluation_after_ingest_without_touching_pricing(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("sentinel_zones.sentinel_bp", source)
        self.assertIn("evaluate_incidents_for_zones", source)
        self.assertIn("Sentinel zone evaluation failed", source)
        self.assertIn("Sentinel zone evaluation was not scheduled", source)
        self.assertIn('href="/sentinel-zones"', (ROOT / "grok-ops.html").read_text(encoding="utf-8"))
        self.assertNotIn("sentinel", (ROOT / "pricing.html").read_text(encoding="utf-8").lower())
        self.assertNotIn("sentinel", (ROOT / "preview_lock.py").read_text(encoding="utf-8").lower())
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        os.environ["SENTINEL_EVAL_SYNC"] = "1"
        import app as app_module

        with mock.patch("sentinel_zones.load_live_incidents", side_effect=RuntimeError("down")):
            app_module._schedule_sentinel_evaluation()
        seen = {}

        def _fake(incidents):
            seen["incidents"] = incidents
            return {"ok": True, "new_hits": 0}

        with mock.patch("sentinel_zones.load_live_incidents", return_value=[{"id": "live-1"}]):
            with mock.patch("sentinel_zones.evaluate_incidents_for_zones", side_effect=_fake):
                app_module._schedule_sentinel_evaluation()
        self.assertEqual(seen["incidents"], [{"id": "live-1"}])
        self.assertIn("sentinel_zones", app_module.app.blueprints)
        client = app_module.app.test_client()
        page = client.get("/sentinel-zones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"radius", page.data)


if __name__ == "__main__":
    unittest.main()
