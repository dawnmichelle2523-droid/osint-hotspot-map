import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
for _name in (
    "PREVIEW_PASSWORD",
    "PREVIEW_USER",
    "BASIC_AUTH_PASSWORD",
    "BASIC_AUTH_USERNAME",
    "SITE_PRIVATE_MODE",
):
    os.environ.pop(_name, None)

import analytics_store
import ppv
import ra_diff
from app import app
from ra_diff import public_id


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
SINCE = "2026-11-25T12:00:00Z"


def feature(title, severity, timestamp, country="Syria", category="Conflict", source="https://example.test/live", confidence="confirmed"):
    return {
        "type": "Feature",
        "properties": {
            "title": title,
            "severity": severity,
            "timestamp": timestamp,
            "country": country,
            "region": "Middle East",
            "category": category,
            "source": source,
            "source_name": "Wire",
            "confidence": confidence,
        },
    }


class RaDiffTests(unittest.TestCase):
    def setUp(self):
        for name in ("PREVIEW_PASSWORD", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME", "SITE_PRIVATE_MODE"):
            os.environ.pop(name, None)
        ppv.set_current(None)
        app.testing = True
        self.client = app.test_client()
        self.time_patch = patch("ra_diff.current_time", return_value=NOW)
        self.time_patch.start()

    def tearDown(self):
        self.time_patch.stop()
        ppv.set_current(None)

    def post(self, payload, features=None, rows=None):
        live = patch("ra_diff.load_live_features", return_value=[] if features is None else features)
        archive = patch("ra_diff.load_archive_rows", return_value=[] if rows is None else rows)
        with live, archive:
            return self.client.post("/api/ra/diff", json=payload)

    def baseline(self, **overrides):
        payload = {
            "country": "Syria",
            "region": "Middle East",
            "since_iso": SINCE,
            "original_incident_ids": [],
            "original_risk_level": "LOW",
        }
        payload.update(overrides)
        return payload

    def test_new_high_incident_escalates_even_if_tier_cannot_rise(self):
        response = self.post(
            self.baseline(original_risk_level="EXTREME"),
            features=[feature("Aleppo airstrike hits an industrial district", "high", "2026-11-26T09:00:00Z")],
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["new_high_count"], 1)
        self.assertEqual(body["new_incidents_count"], 1)
        self.assertTrue(body["escalated"])
        self.assertEqual(body["new_incidents"][0]["severity"], "HIGH")
        self.assertEqual(body["new_incidents"][0]["confidence_tier"], "Corroborated")
        self.assertIn("Armed conflict", body["recommended_control_upgrades"][0])
        self.assertIn("Security control", body["recommended_control_upgrades"][0])
        self.assertEqual(body["checked_at_utc"], "2026-11-27T12:00:00Z")

    def test_three_medium_incidents_escalate(self):
        features = [
            feature(f"Damascus protest number {index} blocks a main road", "medium", f"2026-11-26T0{index}:00:00Z", category="Civil unrest", source=f"https://example.test/med-{index}", confidence="unverified")
            for index in range(1, 4)
        ]
        response = self.post(self.baseline(original_risk_level="EXTREME"), features=features)
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["new_med_count"], 3)
        self.assertEqual(body["new_high_count"], 0)
        self.assertTrue(body["escalated"])
        self.assertTrue(any("Civil unrest" in line for line in body["recommended_control_upgrades"]))

    def test_two_medium_do_not_escalate_when_the_tier_stays_high(self):
        features = [
            feature("Homs robbery reported near the market", "medium", "2026-11-26T08:00:00Z", category="Violent crime", source="https://example.test/crime-1"),
            feature("Second Homs robbery reported overnight", "medium", "2026-11-26T18:00:00Z", category="Violent crime", source="https://example.test/crime-2"),
        ]
        response = self.post(self.baseline(original_risk_level="HIGH"), features=features)
        body = response.get_json()
        self.assertEqual(body["new_med_count"], 2)
        self.assertEqual(body["new_high_count"], 0)
        self.assertFalse(body["escalated"])
        self.assertEqual(body["recommended_control_upgrades"], [])

    def test_risk_tier_increase_escalates_without_a_high_or_three_medium(self):
        response = self.post(
            self.baseline(original_risk_level="Low"),
            features=[feature("Latakia checkpoint delays traffic", "medium", "2026-11-26T10:00:00Z", category="Security force activity", source="https://example.test/sfa")],
        )
        body = response.get_json()
        self.assertEqual(body["new_high_count"], 0)
        self.assertEqual(body["new_med_count"], 1)
        self.assertTrue(body["escalated"])
        self.assertTrue(any("Security-force" in line for line in body["recommended_control_upgrades"]))

    def test_known_ids_and_older_incidents_are_not_new(self):
        title = "Known Damascus market incident"
        stamp = "2026-11-26T11:00:00Z"
        known = public_id(title, stamp)
        features = [
            feature(title, "high", stamp, source="https://example.test/known"),
            feature("Old Aleppo note from before the baseline", "high", "2026-11-24T11:00:00Z", source="https://example.test/old"),
            feature("Baghdad blast stays outside Syria", "high", "2026-11-26T11:00:00Z", country="Iraq", source="https://example.test/iraq"),
        ]
        response = self.post(self.baseline(original_incident_ids=[known, "jane@example.com"]), features=features)
        body = response.get_json()
        self.assertEqual(body["new_incidents_count"], 0)
        self.assertFalse(body["escalated"])
        self.assertNotIn("jane@example.com", response.get_data(as_text=True))

    def test_live_and_archive_rows_collapse_to_one_incident(self):
        title = "Shared Idlib clash report"
        stamp = "2026-11-26T15:00:00Z"
        row = {
            "title": title,
            "severity": "HIGH",
            "timestamp": stamp,
            "country": "Syria",
            "region": "Middle East",
            "category": "ACT",
            "source_url": "https://example.test/shared",
            "source_name": "Archive",
        }
        response = self.post(
            self.baseline(),
            features=[feature(title, "high", stamp, source="https://example.test/shared")],
            rows=[row],
        )
        body = response.get_json()
        self.assertEqual(body["new_incidents_count"], 1)
        self.assertEqual(body["new_incidents"][0]["id"], public_id(title, stamp))

    def test_sqlite_history_is_read_and_personal_fields_are_not_stored(self):
        tmp = tempfile.TemporaryDirectory()
        previous = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmp.name
        analytics_store.reset_store()
        try:
            analytics_store.get_store().upsert_incident({
                "title": "Archive airstrike reported in Aleppo",
                "country": "Syria",
                "category": "ACT",
                "severity": "HIGH",
                "timestamp": "2026-11-26T09:30:00Z",
                "source_url": "https://example.test/archive-aleppo",
                "source_name": "Archive",
            }, now=NOW)
            secret = {
                "names": "Jane Traveller",
                "medical": "penicillin allergy",
                "passport": "X1234567",
                "reviewer_name": "Alex Analyst",
                "email": "alex@example.com",
                "qualification": "CPP",
                "phone": "+1-202-555-0100",
            }
            with patch("ra_diff.load_live_features", return_value=[]):
                response = self.client.post("/api/ra/diff", json=self.baseline(**secret))
            body = response.get_json()
            text = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["new_high_count"], 1)
            self.assertTrue(body["escalated"])
            self.assertEqual(body["new_incidents"][0]["confidence_tier"], "Historical Archive")
            for value in secret.values():
                self.assertNotIn(value, text)
            database = Path(tmp.name) / "incidents.db"
            blob = database.read_bytes()
            for value in secret.values():
                self.assertNotIn(value.encode(), blob)
            source = (ROOT / "ra_diff.py").read_text(encoding="utf-8")
            self.assertNotIn("INSERT", source)
            self.assertNotIn("open(", source)
        finally:
            analytics_store.reset_store()
            if previous is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = previous
            tmp.cleanup()

    def test_missing_scope_and_bad_timestamp_are_rejected(self):
        missing = self.post({"since_iso": SINCE})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("country or region", missing.get_json()["error"])
        bad = self.post(self.baseline(since_iso="next tuesday"))
        self.assertEqual(bad.status_code, 400)
        self.assertIn("since_iso", bad.get_json()["error"])
        self.assertNotIn("next tuesday", bad.get_data(as_text=True))

    def test_single_area_pass_does_not_return_another_country(self):
        ppv.set_current({"scope_type": "country", "scope": "Iraq", "viewer": "pass", "exp": 9999999999})
        try:
            result = ra_diff.build_diff(
                self.baseline(),
                now=NOW,
                live_features=[feature("Aleppo airstrike hits an industrial district", "high", "2026-11-26T09:00:00Z")],
                archive_rows=[],
            )
        finally:
            ppv.set_current(None)
        self.assertEqual(result["new_incidents_count"], 0)
        self.assertFalse(result["escalated"])

    def test_page_and_script_are_served(self):
        page = self.client.get("/risk-assessments")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/static/ra-diff-audit.js", page.data)
        self.assertIn(b"Check What Changed", page.data)
        self.assertIn(b"Approved with Conditions", page.data)
        self.assertIn(b"Clear Local Browser Data", page.data)
        self.assertIn(b"never sent to GeoRiskIntel servers", page.data)
        self.assertIn(b"ISO 31030", page.data)
        self.assertIn(b"BS 8848", page.data)
        script = self.client.get("/static/ra-diff-audit.js")
        self.assertEqual(script.status_code, 200)
        self.assertGreater(script.content_length or len(script.data), 500)
        self.assertIn(b"/api/ra/diff", script.data)
        self.assertIn(b"localStorage", script.data)
        self.assertIn(b"AI-Assisted Draft \xe2\x80\x94 Qualified Human Verification Required", script.data)
        self.assertIn(b"Human Reviewer Signed Off", script.data)
        self.assertIn(b"georisk.ra.baselines.v1", script.data)
        self.assertIn(b"georisk.corridor.v1", script.data)
        self.assertIn(b"Clear GeoRiskIntel baselines", script.data)
        source = script.get_data(as_text=True)
        body = source[source.index("function diffRequestBody"):source.index("async function postDiff")]
        for key in ("country:", "region:", "since_iso:", "original_incident_ids:", "original_risk_level:"):
            self.assertIn(key, body)
        for forbidden in ("reviewer", "passport", "medical", "email", "names"):
            self.assertNotIn(forbidden, body)
        page.close()
        script.close()
        pricing = self.client.get("/pricing")
        self.assertEqual(pricing.status_code, 200)
        pricing.close()


if __name__ == "__main__":
    unittest.main()
