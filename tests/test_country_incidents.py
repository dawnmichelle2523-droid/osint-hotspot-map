import unittest
from datetime import datetime, timezone

from country_incidents import build_report


class CountryIncidentTests(unittest.TestCase):
    def test_counts_and_orders_the_last_seven_days(self):
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        features = [
            {"properties": {"title": "Aleppo clash", "country": "Syria", "area": "Aleppo", "severity": "high", "category": "Conflict", "timestamp": "2026-11-26T12:00:00Z", "source_name": "Wire", "source": "https://example.test/a", "lat": 36.2, "lng": 37.1}},
            {"properties": {"title": "Old Damascus note", "country": "Syria", "severity": "low", "category": "Security", "timestamp": "2026-10-01T00:00:00Z", "source": "https://example.test/old"}},
            {"properties": {"title": "Baghdad blast", "country": "Iraq", "severity": "high", "timestamp": "2026-11-26T00:00:00Z"}},
            {"properties": {"title": "https://almaalomah.me/123", "country": "Syria", "severity": "high", "timestamp": "2026-11-26T00:00:00Z"}},
        ]
        report = build_report(features, "Syria", days=7, now=now)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["by_severity"]["high"], 1)
        self.assertEqual(report["incidents"][0]["area"], "Aleppo")
        self.assertEqual(report["incidents"][0]["source"], "https://example.test/a")
        self.assertEqual(report["note"], "")

    def test_empty_country_has_the_baseline_note(self):
        report = build_report([], "Canada", days=7, now=datetime(2026, 11, 27, tzinfo=timezone.utc))
        self.assertEqual(report["total"], 0)
        self.assertIn("No verified OSINT incidents logged for Canada in the last 7 days", report["note"])
        self.assertIn("baseline advisory & regional monitoring active", report["note"])

    def test_area_filter_keeps_the_named_city(self):
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        features = [
            {"properties": {"title": "Market incident", "country": "Syria", "area": "Damascus", "severity": "medium", "timestamp": "2026-11-25T00:00:00Z"}},
            {"properties": {"title": "Airport closure", "country": "Syria", "area": "Aleppo", "severity": "high", "timestamp": "2026-11-25T00:00:00Z"}},
        ]
        report = build_report(features, "Syria", area="Aleppo", days=7, now=now)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["incidents"][0]["title"], "Airport closure")

    def test_america_alias_matches_united_states(self):
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        features = [{"properties": {"title": "Storm warning", "country": "United States", "area": "Florida", "severity": "medium", "timestamp": "2026-11-27T00:00:00Z", "feed_category": "Weather"}}]
        report = build_report(features, "America", days=7, now=now)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["by_category"]["Weather"], 1)


if __name__ == "__main__":
    unittest.main()
