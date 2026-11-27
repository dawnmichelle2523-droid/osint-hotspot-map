import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import analytics_store
import report_builder
from analytics_store import Store, classify_text, query_analytics, resolve_data_dir, risk_index, risk_level


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {}
        for name in (
            "DATA_DIR", "DB_PATH", "XAI_API_KEY", "ENABLE_XAI_INGEST", "SMTP_HOST",
            "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_PASSWORD", "SMTP_FROM",
            "SMTP_TO", "REPORT_RECIPIENTS", "PREVIEW_PASSWORD", "BASIC_AUTH_PASSWORD",
            "DISABLE_BACKGROUND", "DISABLE_HISTORICAL_SYNC",
        ):
            self.saved[name] = os.environ.get(name)
            os.environ.pop(name, None)
        os.environ["DATA_DIR"] = self.tmp.name
        analytics_store.reset_store()

    def tearDown(self):
        analytics_store.reset_store()
        self.tmp.cleanup()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_data_dir_priority(self):
        root = Path(self.tmp.name)
        mount = root / "mount"
        mount.mkdir()
        local = root / "local"
        chosen = root / "chosen"
        env_dir = resolve_data_dir({"DATA_DIR": str(chosen)}, mount=mount, local=local)
        self.assertEqual(env_dir, chosen)
        self.assertTrue(chosen.is_dir())
        mounted = resolve_data_dir({}, mount=mount, local=local)
        self.assertEqual(mounted, mount)
        db_dir = root / "dbdir"
        db_dir.mkdir()
        from_db = resolve_data_dir({"DB_PATH": str(db_dir / "osint_events.db")}, mount=root / "missing", local=local)
        self.assertEqual(from_db, db_dir)
        fallback = resolve_data_dir({}, mount=root / "missing", local=local)
        self.assertEqual(fallback, local)
        self.assertTrue(local.is_dir())

    def test_historical_rows_are_not_pruned(self):
        path = Path(self.tmp.name) / "incidents.db"
        store = Store(path)
        old = NOW - timedelta(days=400)
        store.upsert_incident({
            "title": "Old Aleppo clash recorded for the archive",
            "country": "Syria",
            "subregion": "Aleppo",
            "category": "ACT",
            "severity": "HIGH",
            "timestamp": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_url": "https://example.test/old-aleppo",
        }, now=NOW)
        store.close()
        reopened = Store(path)
        self.assertEqual(reopened.incident_count(), 1)
        recent = query_analytics("Syria", "weekly", now=NOW, store=reopened)
        self.assertEqual(recent["events"], 0)
        year = query_analytics("Syria", days=365, now=NOW, store=reopened)
        self.assertEqual(year["events"], 0)
        self.assertEqual(reopened.incident_count(), 1)
        indexes = reopened.index_columns()
        for name, column in (
            ("idx_incidents_timestamp", "timestamp"),
            ("idx_incidents_country", "country"),
            ("idx_incidents_region", "region"),
            ("idx_incidents_subregion", "subregion"),
            ("idx_incidents_category", "category"),
            ("idx_incidents_severity", "severity"),
        ):
            self.assertIn(column, indexes[name])
        source = Path(analytics_store.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM incidents", source)
        self.assertNotIn("api.x.ai", source)
        self.assertFalse(analytics_store.ingest_uses_model())
        reopened.close()

    def test_classification_and_casualties(self):
        strike = classify_text("Airstrike kills 12 in Aleppo")
        self.assertEqual(strike["category"], "ACT")
        self.assertEqual(strike["severity"], "HIGH")
        self.assertEqual(strike["fatalities"], 12)
        arrest = classify_text("Security forces arrested 4 at a checkpoint in Damascus")
        self.assertEqual(arrest["category"], "SFA")
        self.assertEqual(arrest["severity"], "LOW")
        self.assertIsNone(arrest["fatalities"])
        robbery = classify_text("Armed robbery leaves 2 injured in London")
        self.assertEqual(robbery["category"], "VC")
        self.assertEqual(robbery["severity"], "MED")
        self.assertEqual(robbery["injuries"], 2)
        protest = classify_text("Thousands protest in Khartoum")
        self.assertEqual(protest["category"], "CU")
        self.assertEqual(protest["severity"], "LOW")
        quake = classify_text("Earthquake magnitude 6.4 strikes western Turkey")
        self.assertEqual(quake["category"], "HS")
        self.assertEqual(quake["severity"], "MED")
        closure = classify_text("Airport closure in Beirut disrupts flights")
        self.assertEqual(closure["category"], "OD")
        self.assertEqual(closure["severity"], "INFO")
        none = classify_text("Clashes continued and no fatalities were reported")
        self.assertEqual(none["fatalities"], 0)
        self.assertEqual(none["category"], "ACT")
        self.assertEqual(classify_text("One soldier was killed near the road")["fatalities"], 1)

    def _incident(self, **kwargs):
        raw = {
            "title": kwargs.get("title", "Logged security incident in the named area"),
            "country": kwargs.get("country", "Syria"),
            "subregion": kwargs.get("subregion", "Damascus"),
            "category": kwargs.get("category", "SFA"),
            "severity": kwargs.get("severity", "MED"),
            "timestamp": kwargs["timestamp"],
            "source_url": kwargs["source_url"],
        }
        for field in ("lat", "lng", "fatalities", "injuries", "summary"):
            if field in kwargs:
                raw[field] = kwargs[field]
        analytics_store.store_incident(raw, now=NOW)

    def _seed_baseline(self):
        for week in range(1, 9):
            stamp = (NOW - timedelta(days=7 * week + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for index in range(4):
                self._incident(
                    timestamp=stamp,
                    source_url=f"https://example.test/prior-{week}-{index}",
                    subregion="Damascus",
                    category="SFA",
                    severity="MED",
                )
        current = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        specs = [
            ("Aleppo", "ACT", "HIGH", 12, 0),
            ("Aleppo", "ACT", "HIGH", 0, 0),
            ("Aleppo", "ACT", "HIGH", 0, 0),
            ("Aleppo", "SFA", "MED", 0, 3),
            ("Damascus", "VC", "MED", 0, 0),
            ("Damascus", "SFA", "LOW", 0, 0),
            ("Damascus", "SFA", "LOW", 0, 0),
            ("Homs", "HS", "INFO", 0, 0),
        ]
        for index, (area, category, severity, fatalities, injuries) in enumerate(specs):
            self._incident(
                title=f"{severity} {category} incident {index} in {area}",
                timestamp=current,
                source_url=f"https://example.test/current-{index}",
                subregion=area,
                category=category,
                severity=severity,
                fatalities=fatalities,
                injuries=injuries,
                lat=36.2 if area == "Aleppo" else 33.5,
                lng=37.1 if area == "Aleppo" else 36.3,
            )
        uk_stamp = current
        for index in range(3):
            self._incident(
                title=f"London robbery report {index}",
                country="UK",
                subregion="London",
                category="VC",
                severity="MED",
                timestamp=uk_stamp,
                source_url=f"https://example.test/uk-{index}",
            )
        for index in range(2):
            self._incident(
                title=f"Baghdad checkpoint report {index}",
                country="Iraq",
                subregion="Baghdad",
                category="SFA",
                severity="LOW",
                timestamp=uk_stamp,
                source_url=f"https://example.test/iq-{index}",
            )
        self._incident(
            title="UKMTO advisory for the Bab el-Mandeb",
            country="",
            subregion="Bab el-Mandeb",
            category="OD",
            severity="INFO",
            timestamp=uk_stamp,
            source_url="https://example.test/maritime",
        )

    def test_country_and_region_baselines(self):
        self._seed_baseline()
        syria = query_analytics("Syria", "weekly", now=NOW)
        self.assertEqual(syria["scope_type"], "country")
        self.assertEqual(syria["events"], 8)
        self.assertEqual(syria["totals"]["baseline"], 4.0)
        self.assertEqual(syria["totals"]["baseline_label"], "8-week average")
        self.assertEqual(syria["totals"]["percent_delta"], 100.0)
        self.assertEqual(syria["totals"]["arrow"], "↑")
        self.assertEqual(syria["severity"], {"HIGH": 3, "MED": 2, "LOW": 2, "INFO": 1})
        self.assertEqual(syria["significant"]["current"], 5)
        self.assertEqual(syria["significant"]["baseline"], 4.0)
        self.assertEqual(syria["significant"]["percent_delta"], 25.0)
        self.assertEqual(syria["significant"]["arrow"], "↑")
        self.assertEqual(syria["risk"]["index"], risk_index(syria["severity"], 7))
        self.assertEqual(syria["risk"]["level"], risk_level(syria["risk"]["index"]))
        self.assertEqual(syria["casualties"]["fatalities"], 12)
        self.assertEqual(syria["casualties"]["injuries"], 3)
        self.assertIn("open-source", syria["casualties"]["disclaimer"])
        self.assertEqual(len(syria["series"]), 9)
        self.assertEqual(syria["series"][-1]["count"], 8)
        by_name = {row["name"]: row for row in syria["breakdown"]}
        self.assertEqual(by_name["Aleppo"]["ACT"], 3)
        self.assertEqual(by_name["Aleppo"]["total"], 4)
        self.assertEqual(by_name["Aleppo"]["arrow"], "↑")
        self.assertEqual(by_name["Damascus"]["total"], 3)
        self.assertEqual(by_name["Damascus"]["prior_total"], 4)
        self.assertEqual(by_name["Damascus"]["arrow"], "↓")
        self.assertEqual(syria["breakdown_axis"], "subregion")
        self.assertTrue(syria["points"])

        region = query_analytics("Middle East", period="weekly", now=NOW)
        self.assertEqual(region["scope_type"], "region")
        self.assertEqual(region["events"], 10)
        self.assertEqual(region["totals"]["baseline"], 4.0)
        self.assertEqual(region["totals"]["percent_delta"], 150.0)
        self.assertEqual(region["totals"]["arrow"], "↑")
        self.assertEqual(region["breakdown_axis"], "country")
        countries = {row["name"]: row["total"] for row in region["breakdown"]}
        self.assertEqual(countries["Syria"], 8)
        self.assertEqual(countries["Iraq"], 2)
        self.assertNotIn("United Kingdom", countries)

        europe = query_analytics("UK & Europe", now=NOW)
        self.assertEqual(europe["events"], 3)
        alias = query_analytics("Europe", now=NOW)
        self.assertEqual(alias["scope"], "UK & Europe")
        self.assertEqual(alias["events"], 3)
        united = query_analytics("United Kingdom", now=NOW)
        self.assertEqual(united["events"], 3)
        maritime = query_analytics("Maritime", now=NOW)
        self.assertEqual(maritime["events"], 1)
        world = query_analytics("Global", "daily", now=NOW)
        self.assertEqual(world["scope_type"], "global")
        self.assertEqual(world["events"], 14)
        self.assertEqual(world["baseline_label"], "30-day average")

        armed = query_analytics("Syria", "weekly", category="ACT", now=NOW)
        self.assertEqual(armed["events"], 3)
        self.assertIsNone(armed["totals"]["percent_delta"])
        self.assertEqual(armed["totals"]["arrow"], "↑")

        monthly = query_analytics("Syria", days=30, now=NOW)
        self.assertEqual(monthly["days"], 30)
        self.assertEqual(monthly["baseline_label"], "3-month average")
        self.assertGreaterEqual(monthly["events"], 8)
        custom = query_analytics("Syria", days=90, now=NOW)
        self.assertEqual(custom["period"], "custom")
        self.assertEqual(custom["baseline_slices"], 3)
        with self.assertRaises(ValueError):
            query_analytics("Syria", category="NOPE", now=NOW)
        self.assertEqual(risk_level(0), "LOW")
        self.assertEqual(risk_level(2.5), "MEDIUM")
        self.assertEqual(risk_level(5), "HIGH")
        self.assertEqual(risk_level(7.5), "EXTREME")
        self.assertEqual(risk_index({"HIGH": 0, "MED": 0, "LOW": 0, "INFO": 0}, 7), 0.0)

    def test_sync_sqlite_does_not_call_xai(self):
        os.environ["ENABLE_XAI_INGEST"] = "true"
        os.environ["XAI_API_KEY"] = "test-key"
        source = Path(self.tmp.name) / "live.db"
        conn = sqlite3.connect(source)
        conn.execute(
            "CREATE TABLE items (title TEXT, summary TEXT, country TEXT, region TEXT, "
            "published_at TEXT, source_url TEXT, severity TEXT, category TEXT, lat REAL, lng REAL)"
        )
        conn.execute(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Shelling kills 6 in Idlib", "Artillery hit the town", "Syria", "Syria",
                "2026-11-26T08:00:00Z", "https://example.test/idlib", "high", "Conflict", 35.9, 36.6,
            ),
        )
        conn.commit()
        conn.close()
        with patch("requests.post") as post:
            added = analytics_store.sync_sqlite(source)
        post.assert_not_called()
        self.assertEqual(added, 1)
        rows = analytics_store.get_store().rows_between(NOW - timedelta(days=7), NOW + timedelta(days=1), "Syria")
        self.assertEqual(rows[0]["category"], "ACT")
        self.assertEqual(rows[0]["severity"], "HIGH")
        self.assertEqual(rows[0]["fatalities"], 6)
        self.assertEqual(rows[0]["subregion"], "Idlib")

    def test_report_fallback_html_and_email(self):
        self._seed_baseline()
        with patch("report_builder.requests.post") as post:
            document = report_builder.generate("Syria", "weekly", now=NOW, use_grok=True)
        post.assert_not_called()
        self.assertEqual(document["narrative_source"], "fallback")
        html = document["html"]
        self.assertIn("GeoRiskIntel | Syria", html)
        self.assertIn("SECURITY ASSESSMENT · WEEKLY", html)
        self.assertIn("What changed, why it matters, and what to watch next.", html)
        self.assertIn("For clients only, not for copy or distribution.", html)
        self.assertIn("page-break-after", html)
        self.assertIn(">ACT<", html)
        self.assertIn("Implications.", html)
        self.assertIn("What to watch.", html)
        self.assertIn("open-source", html)
        self.assertIn("reported fatalities", html.lower())
        self.assertIn("leaflet", html)
        message = report_builder.build_message(document, ["ops@example.com"])
        self.assertIn("Syria", message["Subject"])
        self.assertEqual(message["To"], "ops@example.com")
        html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
        self.assertIn("For clients only", html_part.get_payload(decode=True).decode())
        os.environ.pop("SMTP_HOST", None)
        with patch("report_builder.smtplib.SMTP") as smtp:
            result = report_builder.smtp_send(message)
        smtp.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("SMTP", result["error"])

    def test_report_uses_grok_and_falls_back(self):
        self._incident(
            title="Damascus incident for the narrative",
            timestamp=(NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_url="https://example.test/grok-row",
            category="ACT",
            severity="HIGH",
        )
        payload = {
            "executive_summary": "Grok summary for Syria.",
            "outlook": "Grok outlook for the next week.",
            "key_developments": [{
                "title": "Aleppo strikes",
                "badge": "↑ RISING",
                "summary": "Strikes increased.",
                "implications": "Movement risk is higher.",
                "what_to_watch": "Further strikes this week.",
            }],
        }

        class Ok:
            status_code = 200
            text = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})

            def json(self):
                return json.loads(self.text)

        os.environ["XAI_API_KEY"] = "test-key"
        with patch("report_builder.requests.post", return_value=Ok()) as post:
            document = report_builder.generate("Syria", "weekly", now=NOW)
        self.assertTrue(post.called)
        self.assertEqual(document["narrative_source"], "grok")
        self.assertIn("Grok summary for Syria.", document["html"])
        self.assertIn("↑ RISING", document["html"])
        self.assertIn("Implications.", document["html"])

        class Denied:
            status_code = 403
            text = "forbidden"

            def json(self):
                return {}

        with patch("report_builder.requests.post", return_value=Denied()):
            denied = report_builder.generate("Syria", "daily", now=NOW)
        self.assertEqual(denied["narrative_source"], "fallback")
        self.assertIn("GeoRiskIntel logged", denied["executive_summary"])

        class Quota:
            status_code = 200
            text = '{"error":{"message":"You exceeded your current quota"}}'

            def json(self):
                return {"error": {"message": "You exceeded your current quota"}}

        with patch("report_builder.requests.post", return_value=Quota()):
            limited = report_builder.generate("Middle East", "monthly", now=NOW)
        self.assertEqual(limited["narrative_source"], "fallback")
        self.assertIn("SECURITY ASSESSMENT · MONTHLY", limited["html"])

    def test_subscriptions_schedule_and_test_send(self):
        self._incident(
            title="Weekly Syria incident for delivery",
            timestamp=(NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_url="https://example.test/delivery",
        )
        db = analytics_store.get_store()
        first = db.save_schedule({
            "client_label": "Acme Desk",
            "recipients": "a@acme.example, b@acme.example",
            "scope": "Syria",
            "cadence": "weekly",
            "enabled": True,
        }, now=NOW)
        second = db.save_schedule({
            "client_label": "North Africa desk",
            "recipients": ["sudan@example.com"],
            "scope": "Sudan",
            "cadence": "daily",
            "enabled": True,
        }, now=NOW)
        paused = db.save_schedule({
            "client_label": "Paused client",
            "recipients": "paused@example.com",
            "scope": "Global",
            "cadence": "monthly",
            "enabled": False,
        }, now=NOW)
        reopened = Store(db.path)
        labels = {row["client_label"] for row in reopened.list_schedules()}
        self.assertEqual(labels, {"Acme Desk", "North Africa desk", "Paused client"})
        sent = []

        def sender(message):
            sent.append(message)
            return {"ok": True}

        early = datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(report_builder.process_due_schedules(now=early, sender=sender, gate_hour=7, db=reopened), [])
        test = report_builder.deliver_schedule(reopened.get_schedule(first["id"]), now=NOW, sender=sender, test=True, db=reopened)
        self.assertTrue(test["ok"])
        self.assertEqual(test["test"], True)
        self.assertIn("Acme Desk", sent[0]["Subject"])
        refreshed = reopened.get_schedule(first["id"])
        self.assertEqual(refreshed["last_sent_at"], "")
        self.assertTrue(refreshed["last_test_at"])
        due = report_builder.process_due_schedules(now=NOW, sender=sender, gate_hour=7, db=reopened)
        self.assertEqual(len(due), 2)
        self.assertEqual(len(sent), 3)
        again = report_builder.process_due_schedules(now=NOW, sender=sender, gate_hour=7, db=reopened)
        self.assertEqual(again, [])
        self.assertEqual(len(sent), 3)
        self.assertTrue(reopened.get_schedule(second["id"])["last_sent_at"])
        self.assertEqual(reopened.get_schedule(paused["id"])["last_sent_at"], "")
        os.environ["SMTP_PASS"] = "test-pass"
        os.environ["SMTP_HOST"] = "smtp.example"
        os.environ["SMTP_PORT"] = "2525"
        self.assertEqual(report_builder.smtp_settings()[3], "test-pass")
        os.environ.pop("SMTP_PASS")
        os.environ["SMTP_PASSWORD"] = "other-pass"
        self.assertEqual(report_builder.smtp_settings()[3], "other-pass")
        reopened.close()

    def test_http_dispatch_and_pages(self):
        self._incident(
            title="Dispatch incident in Aleppo",
            timestamp=(NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_url="https://example.test/dispatch",
            category="ACT",
            severity="HIGH",
        )
        analytics = report_builder.dispatch("GET", "/api/analytics", {"country": "Syria", "period": "weekly"}, {})
        self.assertEqual(analytics["status"], 200)
        self.assertEqual(analytics["body"]["events"], 1)
        import serve_map
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        self.assertFalse(str(serve_map.FEEDS_DB).startswith(self.tmp.name))
        import app as app_module

        client = app_module.app.test_client()
        page = client.get("/reports")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Scheduled delivery", page.data)
        script = client.get("/static/reports.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"Send Test Now", script.data)
        pricing = client.get("/pricing")
        self.assertEqual(pricing.status_code, 200)
        self.assertIn(b"Automated Intelligence Reports (Optional Add-On)", pricing.data)
        self.assertIn(b"Request / Add to Plan", pricing.data)
        self.assertIn(b"dailyM: 149", pricing.data)
        self.assertIn(b"dailyM: 189", pricing.data)
        created = client.post("/api/reports/schedules", json={
            "client_label": "Route client",
            "recipients": "route@example.com",
            "scope": "Ukraine",
            "cadence": "monthly",
            "enabled": True,
        })
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.get_json()["ok"])
        generated = client.post("/api/reports/generate", json={"scope": "Syria", "period": "daily", "use_grok": False})
        body = generated.get_json()
        self.assertEqual(generated.status_code, 200)
        self.assertIn("SECURITY ASSESSMENT · DAILY", body["html"])
        self.assertEqual(body["narrative_source"], "fallback")
        downloaded = client.get(f"/api/reports/{body['id']}/html?download=1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        missing = client.post("/api/reports/schedules", json={"client_label": "", "recipients": "nope", "scope": "Syria", "cadence": "weekly"})
        self.assertEqual(missing.status_code, 400)


if __name__ == "__main__":
    unittest.main()
