"""Corridor geometry, scoring, OSRM fallback, and the route-risk HTTP routes."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import route_risk

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def _disabled(waypoints, timeout):
    return {"ok": False, "reason": "disabled"}


def _route(buffer=25, sector="", country="", stops=None):
    return {
        "waypoints": stops or [
            {"name": "A", "lat": 0, "lng": 0},
            {"name": "B", "lat": 0, "lng": 1},
        ],
        "buffer_km": buffer,
        "sector": sector,
        "country": country,
    }


def _incident(**kwargs):
    item = {
        "title": "Security incident on the corridor",
        "severity": "high",
        "category": "Security",
        "confidence": "official",
        "confidence_label": "Official Advisory",
        "lat": 0,
        "lng": 0.5,
        "timestamp": "2026-11-27T08:00:00Z",
        "source": "https://example.test/incident",
        "region": "Test",
        "country": "Testland",
    }
    item.update(kwargs)
    return item


class PointToSegmentTests(unittest.TestCase):
    def test_point_on_the_segment_is_essentially_zero(self):
        distance = route_risk.point_to_segment_km(0, 0.5, 0, 0, 0, 1)
        self.assertLess(distance, 0.05)

    def test_cross_track_matches_the_distance_to_the_foot(self):
        distance = route_risk.point_to_segment_km(1, 0.5, 0, 0, 0, 1)
        expected = route_risk.haversine_km(1, 0.5, 0, 0.5)
        self.assertAlmostEqual(distance, expected, delta=0.25)

    def test_point_past_the_end_uses_the_endpoint(self):
        distance = route_risk.point_to_segment_km(0, 2, 0, 0, 0, 1)
        self.assertAlmostEqual(distance, route_risk.haversine_km(0, 2, 0, 1), delta=0.05)

    def test_point_before_the_start_uses_the_start(self):
        distance = route_risk.point_to_segment_km(0, -1, 0, 0, 0, 1)
        self.assertAlmostEqual(distance, route_risk.haversine_km(0, -1, 0, 0), delta=0.05)

    def test_degenerate_segment_uses_the_point(self):
        distance = route_risk.point_to_segment_km(0, 1, 0, 0, 0, 0)
        self.assertAlmostEqual(distance, route_risk.haversine_km(0, 1, 0, 0), delta=0.05)

    def test_segment_across_the_antimeridian(self):
        distance = route_risk.point_to_segment_km(0, 180, 0, 179, 0, -179)
        self.assertLess(distance, 0.5)

    def test_corridor_ring_closes(self):
        ring = route_risk.corridor_polygon([(0, 0), (0, 1)], 10)
        self.assertGreaterEqual(len(ring), 4)
        self.assertEqual(ring[0], ring[-1])


class CorridorScoreTests(unittest.TestCase):
    def test_tier_boundaries(self):
        self.assertEqual(route_risk.tier_for(0), "LOW")
        self.assertEqual(route_risk.tier_for(19), "LOW")
        self.assertEqual(route_risk.tier_for(20), "MODERATE")
        self.assertEqual(route_risk.tier_for(39), "MODERATE")
        self.assertEqual(route_risk.tier_for(40), "ELEVATED")
        self.assertEqual(route_risk.tier_for(59), "ELEVATED")
        self.assertEqual(route_risk.tier_for(60), "HIGH")
        self.assertEqual(route_risk.tier_for(79), "HIGH")
        self.assertEqual(route_risk.tier_for(80), "CRITICAL")
        self.assertEqual(route_risk.tier_for(100), "CRITICAL")

    def test_score_curve_anchors(self):
        self.assertEqual(route_risk.score_from_points([]), 6)
        self.assertEqual(route_risk.score_from_points([24]), 35)
        self.assertEqual(route_risk.score_from_points([36, 36, 36, 36]), 93)

    def test_empty_corridor_is_the_low_baseline(self):
        result = route_risk.analyze_route(_route(), incidents=[], osrm_fetch=_disabled, now=NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["tier"], "LOW")
        self.assertEqual(result["incident_count"], 0)
        self.assertEqual(result["xai_credits"], 0)
        self.assertIn("Baseline monitoring", result["summary"])

    def test_incident_on_the_line_scores_moderate(self):
        result = route_risk.analyze_route(_route(), incidents=[_incident()], osrm_fetch=_disabled, now=NOW)
        self.assertEqual(result["incident_count"], 1)
        self.assertLess(result["incidents"][0]["distance_km"], 0.2)
        self.assertEqual(result["incidents"][0]["confidence"], "Official Advisory")
        self.assertEqual(result["score"], 35)
        self.assertEqual(result["tier"], "MODERATE")

    def test_incident_outside_the_buffer_is_excluded(self):
        near = _incident(lat=0.05, lng=0.5, title="Close to the line")
        far = _incident(lat=0.6, lng=0.5, title="Far from the line")
        result = route_risk.analyze_route(_route(buffer=25), incidents=[near, far], osrm_fetch=_disabled, now=NOW)
        titles = [item["title"] for item in result["incidents"]]
        self.assertIn("Close to the line", titles)
        self.assertNotIn("Far from the line", titles)
        tight = route_risk.analyze_route(_route(buffer=5), incidents=[near], osrm_fetch=_disabled, now=NOW)
        self.assertEqual(tight["incident_count"], 0)

    def test_old_incidents_drop_out_and_recent_ones_count_less(self):
        fresh = route_risk.analyze_route(_route(), incidents=[_incident()], osrm_fetch=_disabled, now=NOW)
        aged = route_risk.analyze_route(
            _route(),
            incidents=[_incident(timestamp=(NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))],
            osrm_fetch=_disabled,
            now=NOW,
        )
        ancient = route_risk.analyze_route(
            _route(),
            incidents=[_incident(timestamp="2026-10-01T00:00:00Z")],
            osrm_fetch=_disabled,
            now=NOW,
        )
        self.assertGreater(fresh["score"], aged["score"])
        self.assertEqual(aged["incident_count"], 1)
        self.assertEqual(ancient["incident_count"], 0)
        self.assertEqual(ancient["score"], 6)

    def test_matching_sector_raises_the_score(self):
        incident = _incident(title="Student gathering near the campus", category="Civil unrest")
        plain = route_risk.analyze_route(_route(sector="media"), incidents=[incident], osrm_fetch=_disabled, now=NOW)
        school = route_risk.analyze_route(_route(sector="schools"), incidents=[incident], osrm_fetch=_disabled, now=NOW)
        self.assertGreater(school["score"], plain["score"])

    def test_repeated_critical_incidents_reach_critical(self):
        incidents = [
            _incident(title=f"Critical strike {index}", severity="critical", lat=0, lng=0.2 * index)
            for index in range(1, 5)
        ]
        result = route_risk.analyze_route(_route(buffer=50), incidents=incidents, osrm_fetch=_disabled, now=NOW)
        self.assertEqual(result["incident_count"], 4)
        self.assertGreaterEqual(result["score"], 80)
        self.assertEqual(result["tier"], "CRITICAL")

    def test_buffer_is_clamped_and_contacts_are_stripped(self):
        result = route_risk.analyze_route(
            _route(
                buffer=1,
                stops=[
                    {"name": "Desk jane@example.com", "lat": 0, "lng": 0, "phone": "+44 20 7946 0123"},
                    {"name": "Office +44 20 7946 0123", "lat": 0, "lng": 1},
                ],
            ),
            incidents=[],
            osrm_fetch=_disabled,
            now=NOW,
        )
        self.assertEqual(result["buffer_km"], 5)
        blob = json.dumps(result)
        self.assertNotIn("example.com", blob)
        self.assertNotIn("7946", blob)
        self.assertNotIn("phone", result["waypoints"][0])
        high = route_risk.analyze_route(_route(buffer=250), incidents=[], osrm_fetch=_disabled, now=NOW)
        self.assertEqual(high["buffer_km"], 100)
        missing = route_risk.analyze_route({"waypoints": [{"lat": 0, "lng": 0}]}, incidents=[], osrm_fetch=_disabled)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["xai_credits"], 0)
        bad = route_risk.analyze_route(
            {"waypoints": [{"lat": 95, "lng": 0}, {"lat": 0, "lng": 1}], "buffer_km": "nope"},
            incidents=[],
            osrm_fetch=_disabled,
        )
        self.assertFalse(bad["ok"])

    def test_beirut_corridor_lists_trauma_care_and_masnaa(self):
        preset = next(item for item in route_risk.PRESETS if item["id"] == "beirut-airport-downtown")
        result = route_risk.analyze_route(
            {
                "waypoints": [dict(stop) for stop in preset["waypoints"]],
                "buffer_km": preset["buffer_km"],
                "sector": preset["sector"],
                "country": preset["country"],
            },
            incidents=[],
            osrm_fetch=_disabled,
            now=NOW,
        )
        medical = " ".join(item["name"] for item in result["medical"])
        borders = " ".join(item["name"] for item in result["borders"])
        self.assertIn("American University of Beirut Medical Center", medical)
        self.assertIn("Masnaa", borders)
        self.assertGreater(result["distance_km"], 0)
        self.assertGreaterEqual(len(result["corridor"]), 4)


class OsrmFallbackTests(unittest.TestCase):
    def test_timeout_falls_back_to_the_great_circle(self):
        def fetch(waypoints, timeout):
            self.assertEqual(timeout, route_risk.OSRM_TIMEOUT_SECONDS)
            return {"ok": False, "reason": "timeout"}

        result = route_risk.analyze_route(_route(), incidents=[_incident()], osrm_fetch=fetch, now=NOW)
        self.assertEqual(result["routing"], "geodesic")
        self.assertEqual(result["routing_reason"], "timeout")
        self.assertIn("4 seconds", result["routing_note"])
        self.assertEqual(result["score"], 35)

    def test_raised_timeout_also_falls_back(self):
        def fetch(waypoints, timeout):
            raise TimeoutError("timed out")

        result = route_risk.analyze_route(_route(), incidents=[], osrm_fetch=fetch, now=NOW)
        self.assertEqual(result["routing"], "geodesic")
        self.assertEqual(result["routing_reason"], "timeout")

    def test_public_request_uses_a_four_second_timeout_and_no_names(self):
        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen["url"] = url
            seen["timeout"] = timeout
            raise requests.Timeout("slow")

        with mock.patch("route_risk.requests.get", fake_get):
            result = route_risk.fetch_osrm([(33.8209, 35.4884), (33.8969, 35.4786)])
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(seen["timeout"], 4)
        self.assertNotIn("Beirut", seen["url"])
        self.assertIn("35.488400,33.820900", seen["url"])
        self.assertTrue(seen["url"].startswith("https://router.project-osrm.org/"))

    def test_open_water_route_falls_back(self):
        def fetch(waypoints, timeout):
            return {
                "ok": True,
                "distance_km": 340,
                "points": [(51.53, -0.13), (50.2, 1.0), (48.88, 2.35)],
            }

        result = route_risk.analyze_route(
            _route(stops=[
                {"name": "London St Pancras", "lat": 51.5305, "lng": -0.1257},
                {"name": "Paris Gare du Nord", "lat": 48.8809, "lng": 2.3553},
            ]),
            incidents=[],
            osrm_fetch=fetch,
            now=NOW,
        )
        self.assertEqual(result["routing"], "geodesic")
        self.assertEqual(result["routing_reason"], "sea_or_border")
        self.assertIn("English Channel", result["routing_note"])

    def test_border_detour_falls_back(self):
        def fetch(waypoints, timeout):
            return {"ok": True, "distance_km": 400, "points": [(0, 0), (0, 2), (0, 1)]}

        result = route_risk.analyze_route(_route(), incidents=[], osrm_fetch=fetch, now=NOW)
        self.assertEqual(result["routing"], "geodesic")
        self.assertEqual(result["routing_reason"], "sea_or_border")

    def test_direct_road_is_kept(self):
        def fetch(waypoints, timeout):
            return {
                "ok": True,
                "distance_km": 12,
                "points": [(33.8209, 35.4884), (33.85, 35.484), (33.8969, 35.4786)],
            }

        result = route_risk.analyze_route(
            _route(stops=[
                {"name": "Beirut Rafic Hariri Airport", "lat": 33.8209, "lng": 35.4884},
                {"name": "Hamra", "lat": 33.8969, "lng": 35.4786},
            ]),
            incidents=[],
            osrm_fetch=fetch,
            now=NOW,
        )
        self.assertEqual(result["routing"], "osrm")
        self.assertEqual(result["distance_km"], 12)

    def test_module_does_not_call_a_model(self):
        source = (ROOT / "route_risk.py").read_text(encoding="utf-8")
        self.assertNotIn("openai", source)
        self.assertNotIn("api.x.ai", source)
        self.assertIn("xai_credits", source)


class RouteRiskHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME", "ALLOW_INDEXING", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        os.environ["ROUTE_RISK_OSRM"] = "0"
        import serve_map

        serve_map.start_background = lambda: None
        import app as app_module

        app_module.app.testing = True
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("ROUTE_RISK_OSRM", None)

    def test_page_presets_and_analyze_return_200(self):
        page = self.client.get("/route-risk")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("Corridor Risk Gauge", body)
        self.assertIn("Copy into Risk Assessment", body)
        self.assertIn('id="buffer"', body)
        self.assertIn('min="5"', body)
        self.assertIn('max="100"', body)
        self.assertIn("georisk.corridor.v1", body)
        page.close()

        presets = self.client.get("/api/route-risk/presets")
        self.assertEqual(presets.status_code, 200)
        catalog = presets.get_json()
        names = " ".join(item["name"] for item in catalog["presets"])
        for label in ("Beirut", "Kyiv", "Nairobi", "Amman", "Aqaba", "London", "Paris", "Damascus"):
            self.assertIn(label, names)
        self.assertEqual(catalog["xai_credits"], 0)

        analyzed = self.client.post("/api/route-risk/analyze", json={
            "waypoints": [
                {"name": "Beirut Rafic Hariri Airport", "lat": 33.8209, "lng": 35.4884},
                {"name": "Hamra", "lat": 33.8969, "lng": 35.4786},
            ],
            "buffer_km": 20,
            "sector": "media",
            "country": "Lebanon",
        })
        self.assertEqual(analyzed.status_code, 200)
        payload = analyzed.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["routing"], "geodesic")
        self.assertEqual(payload["xai_credits"], 0)
        self.assertIn(payload["tier"], {"LOW", "MODERATE", "ELEVATED", "HIGH", "CRITICAL"})
        self.assertGreaterEqual(payload["score"], 0)
        self.assertLessEqual(payload["score"], 100)
        self.assertGreater(payload["distance_km"], 0)
        self.assertGreaterEqual(len(payload["route"]), 2)
        self.assertIn("CORRIDOR RISK BRIEF", payload["summary"])

        rejected = self.client.post("/api/route-risk/analyze", data="nope", content_type="text/plain")
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(rejected.get_json()["ok"])

    def test_grok_ops_links_to_the_corridor_page(self):
        ops = (ROOT / "grok-ops.html").read_text(encoding="utf-8")
        assessment = (ROOT / "risk-assessments.html").read_text(encoding="utf-8")
        self.assertIn('href="/route-risk"', ops)
        self.assertIn("georisk.corridor.v1", assessment)
        self.assertIn("Paste into assessment", assessment)
        page = self.client.get("/grok-ops")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'href="/route-risk"', page.data)
        page.close()


if __name__ == "__main__":
    unittest.main()
