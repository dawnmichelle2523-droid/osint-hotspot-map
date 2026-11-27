import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests
from flask import Flask

import preview_lock
import slack_alerts

WEBHOOK = "https://hooks.slack.com/services/T0123ABCD/B99XYZ/abcdEFGH1234"
SECRET = "abcdEFGH1234"
WHEN = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def _button(payload):
    for block in payload["attachments"][0]["blocks"]:
        if block["type"] == "actions":
            return block["elements"][0]
    raise AssertionError("missing View on Live Map button")


def _section(payload):
    for block in payload["attachments"][0]["blocks"]:
        if block["type"] == "section":
            return block["text"]["text"]
    raise AssertionError("missing section")


def _header(payload):
    for block in payload["attachments"][0]["blocks"]:
        if block["type"] == "header":
            return block["text"]["text"]
    raise AssertionError("missing header")


class SlackAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "slack_alerts.db")
        self.items = os.path.join(self.tmp.name, "items.db")
        os.environ["SLACK_ALERTS_DB"] = self.db
        os.environ["SLACK_INCIDENTS_DB"] = self.items
        os.environ["SLACK_DISABLE_FEEDS"] = "1"
        for key in (
            "PUBLIC_BASE_URL",
            "RENDER_EXTERNAL_URL",
            "RAILWAY_PUBLIC_DOMAIN",
            "PREVIEW_PASSWORD",
            "BASIC_AUTH_PASSWORD",
            "PREVIEW_USER",
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        for key in (
            "SLACK_ALERTS_DB",
            "SLACK_INCIDENTS_DB",
            "SLACK_DISABLE_FEEDS",
            "PUBLIC_BASE_URL",
            "RENDER_EXTERNAL_URL",
            "RAILWAY_PUBLIC_DOMAIN",
            "PREVIEW_PASSWORD",
            "BASIC_AUTH_PASSWORD",
            "PREVIEW_USER",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _channel(self, **overrides):
        payload = {
            "label": "#security-ops",
            "webhook_url": WEBHOOK,
            "regions": ["ALL"],
            "countries": [],
            "categories": ["ALL"],
            "min_severity": "ALL",
            "rate_limit_per_hour": 20,
            "digest_minutes": 0,
            "enabled": True,
        }
        payload.update(overrides)
        return slack_alerts.save_channel(payload)

    def _incident(self, **overrides):
        payload = {
            "event_key": "evt-1",
            "title": "Indirect fire reported near Aleppo",
            "summary": "Impacts were reported on the northern edge of the city.",
            "country": "Syria",
            "city": "Aleppo",
            "region": "Syria",
            "category": "ACT",
            "severity": "HIGH",
            "source_name": "Field wire",
            "timestamp": "2026-11-27T12:00:00Z",
        }
        payload.update(overrides)
        return payload

    def _poster(self):
        calls = []

        def poster(url, payload, timeout=6):
            calls.append((url, payload, timeout))
            return 200, "ok"

        return calls, poster

    def test_webhook_validation(self):
        self.assertEqual(slack_alerts.validate_webhook(WEBHOOK), WEBHOOK)
        for bad in (
            "",
            "http://hooks.slack.com/services/T0123ABCD/B99XYZ/abcdEFGH1234",
            "https://hooks.slack.com.evil/services/T0123ABCD/B99XYZ/abcdEFGH1234",
            "https://example.com/services/T0123ABCD/B99XYZ/abcdEFGH1234",
            WEBHOOK + "/extra",
            WEBHOOK + "?x=1",
            "https://hooks.slack.com/services/T.../B.../••••1234",
        ):
            with self.assertRaises(slack_alerts.SlackConfigError):
                slack_alerts.validate_webhook(bad)

    def test_mask_webhook_hides_the_secret(self):
        masked = slack_alerts.mask_webhook(WEBHOOK)
        self.assertEqual(masked, "https://hooks.slack.com/services/T.../B.../••••1234")
        self.assertNotIn(SECRET, masked)
        self.assertNotIn("abcdEFGH", masked)

    def test_db_path_uses_writable_data_or_local_file(self):
        data = Path(self.tmp.name) / "data"
        data.mkdir()
        fallback = Path(self.tmp.name) / "slack_alerts.db"
        self.assertEqual(
            slack_alerts.resolve_db_path("", data, fallback),
            data / "slack_alerts.db",
        )
        missing = Path(self.tmp.name) / "missing"
        self.assertEqual(slack_alerts.resolve_db_path("", missing, fallback), fallback)
        custom = Path(self.tmp.name) / "custom.db"
        self.assertEqual(slack_alerts.resolve_db_path(str(custom), data, fallback), custom)

    def test_schema_has_channel_and_delivery_columns(self):
        self._channel()
        conn = sqlite3.connect(self.db)
        try:
            channels = {row[1] for row in conn.execute("PRAGMA table_info(slack_channels)")}
            deliveries = {row[1] for row in conn.execute("PRAGMA table_info(slack_deliveries)")}
        finally:
            conn.close()
        self.assertTrue(
            {
                "id",
                "label",
                "webhook_url",
                "regions",
                "countries",
                "categories",
                "min_severity",
                "rate_limit_per_hour",
                "digest_minutes",
                "enabled",
                "created_at",
                "last_sent_at",
            }
            <= channels
        )
        self.assertTrue({"channel_id", "event_key", "sent_at"} <= deliveries)

    def test_block_kit_colours_summary_and_map_link(self):
        high = slack_alerts.build_block_kit(self._incident(), "https://ops.example")
        med = slack_alerts.build_block_kit(self._incident(severity="Medium", category="CU", event_key="med"))
        low = slack_alerts.build_block_kit(self._incident(severity="low", category="OD", event_key="low"))
        info = slack_alerts.build_block_kit(self._incident(severity="info", category="HS", event_key="info"))
        tsunami = slack_alerts.build_block_kit(
            self._incident(severity="medium", category="Tsunami", country="Japan", city="Sendai", event_key="wave"),
            "https://ops.example",
        )
        self.assertEqual(high["attachments"][0]["color"], "#dc2626")
        self.assertEqual(med["attachments"][0]["color"], "#f59e0b")
        self.assertEqual(low["attachments"][0]["color"], "#3b82f6")
        self.assertEqual(info["attachments"][0]["color"], "#3b82f6")
        self.assertEqual(tsunami["attachments"][0]["color"], "#dc2626")
        self.assertIn("HIGH", _header(high))
        self.assertIn("Syria", _header(high))
        self.assertIn("Aleppo", _header(high))
        self.assertIn("ACT", _header(high))
        self.assertIn("TSUNAMI", _header(tsunami))
        self.assertIn("*Indirect fire reported near Aleppo*", _section(high))
        long_summary = "Reported impacts continued along the airport road. " * 6
        long_card = slack_alerts.build_block_kit(self._incident(summary=long_summary))
        self.assertIn("\n", _section(long_card))
        self.assertLessEqual(_section(long_card).count("\n"), 2)
        button = _button(high)
        self.assertEqual(button["text"]["text"], "View on Live Map")
        self.assertEqual(button["url"], "https://ops.example/?country=Syria")
        saudi = slack_alerts.build_block_kit(self._incident(country="Saudi Arabia"), "https://ops.example")
        self.assertEqual(_button(saudi)["url"], "https://ops.example/?country=Saudi%20Arabia")
        context = next(block for block in high["attachments"][0]["blocks"] if block["type"] == "context")
        self.assertIn("Field wire", context["elements"][0]["text"])
        self.assertIn("UTC", context["elements"][0]["text"])
        os.environ["PUBLIC_BASE_URL"] = "https://desk.example/"
        from_env = slack_alerts.build_block_kit(self._incident())
        self.assertEqual(_button(from_env)["url"], "https://desk.example/?country=Syria")

    def test_matching_region_country_category_and_severity(self):
        strict = {
            "regions": ["Middle East"],
            "countries": ["Syria"],
            "categories": ["ACT"],
            "min_severity": "HIGH",
            "enabled": True,
        }
        self.assertTrue(
            slack_alerts.channel_matches(
                strict,
                {"country": "Syria", "region": "Syria", "category": "Kinetic/Military", "severity": "High", "title": "Strike"},
            )
        )
        self.assertFalse(
            slack_alerts.channel_matches(strict, {"country": "Iraq", "region": "Middle East", "category": "ACT", "severity": "HIGH"})
        )
        self.assertFalse(
            slack_alerts.channel_matches(strict, {"country": "Syria", "region": "Middle East", "category": "CU", "severity": "HIGH"})
        )
        self.assertFalse(
            slack_alerts.channel_matches(strict, {"country": "Syria", "region": "Middle East", "category": "ACT", "severity": "Medium"})
        )
        self.assertTrue(
            slack_alerts.channel_matches(
                {"regions": ["APAC"], "countries": [], "categories": ["ALL"], "min_severity": "LOW", "enabled": True},
                {"country": "Japan", "severity": "low", "category": "Seismic", "title": "Quake"},
            )
        )
        self.assertTrue(
            slack_alerts.channel_matches(
                {"regions": ["ALL"], "countries": ["Gulf States"], "categories": ["ALL"], "min_severity": "ALL", "enabled": True},
                {"country": "Qatar", "severity": "LOW", "category": "OD"},
            )
        )
        self.assertTrue(
            slack_alerts.channel_matches(
                {"regions": ["ALL"], "countries": ["USA"], "categories": ["HS"], "min_severity": "MED", "enabled": True},
                {"country": "United States", "severity": "HIGH", "feed_category": "Travel Advisory", "title": "Advisory"},
            )
        )
        self.assertTrue(
            slack_alerts.channel_matches(
                {"regions": ["ALL"], "countries": [], "categories": ["TSUNAMI"], "min_severity": "HIGH", "enabled": True},
                {"country": "Japan", "severity": "medium", "feed_category": "Tsunami", "title": "Coastal warning"},
            )
        )
        self.assertFalse(
            slack_alerts.channel_matches(
                {"regions": ["ALL"], "countries": [], "categories": ["ACT"], "min_severity": "ALL", "enabled": True},
                {"country": "Japan", "severity": "high", "category": "Tsunami", "title": "Wave"},
            )
        )
        self.assertFalse(
            slack_alerts.channel_matches(
                {"regions": ["ALL"], "countries": [], "categories": ["ALL"], "min_severity": "ALL", "enabled": False},
                {"country": "Syria", "severity": "HIGH", "category": "ACT"},
            )
        )
        self.assertEqual(slack_alerts.category_code({"threat_code": 9, "title": "Crowd"}), "CU")

    def test_dedup_posts_an_incident_once(self):
        self._channel()
        calls, poster = self._poster()
        incident = self._incident()
        first = slack_alerts.dispatch_incidents([incident], now=WHEN, poster=poster)
        second = slack_alerts.dispatch_incidents([incident], now=WHEN, poster=poster)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], WEBHOOK)

    def test_hourly_rate_limit_and_later_retry(self):
        self._channel(rate_limit_per_hour=2)
        calls, poster = self._poster()
        incidents = [self._incident(event_key=f"evt-{index}", title=f"Strike {index}") for index in range(3)]
        stats = slack_alerts.dispatch_incidents(incidents, now=WHEN, poster=poster)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(len(calls), 2)
        slack_alerts.dispatch_incidents([], now=WHEN + timedelta(minutes=61), poster=poster)
        self.assertEqual(len(calls), 3)

    def test_digest_holds_med_and_sends_high_immediately(self):
        self._channel(digest_minutes=15)
        calls, poster = self._poster()
        stats = slack_alerts.dispatch_incidents(
            [
                self._incident(event_key="med", severity="MED", category="CU"),
                self._incident(event_key="high", severity="HIGH", category="ACT"),
            ],
            now=WHEN,
            poster=poster,
        )
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("HIGH", _header(calls[0][1]))
        slack_alerts.dispatch_incidents([], now=WHEN + timedelta(minutes=16), poster=poster)
        self.assertEqual(len(calls), 2)
        self.assertIn("MED", _header(calls[1][1]))

    def test_tsunami_bypasses_digest(self):
        self._channel(digest_minutes=30, categories=["TSUNAMI"])
        calls, poster = self._poster()
        stats = slack_alerts.dispatch_incidents(
            [self._incident(event_key="wave", severity="medium", category="Tsunami", country="Japan", city="Sendai")],
            now=WHEN,
            poster=poster,
            base_url="https://ops.example",
        )
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(calls[0][1]["attachments"][0]["color"], "#dc2626")
        self.assertEqual(_button(calls[0][1])["url"], "https://ops.example/?country=Japan")

    def test_disabled_channel_is_silent(self):
        self._channel(enabled=False)
        calls, poster = self._poster()
        stats = slack_alerts.dispatch_incidents([self._incident()], now=WHEN, poster=poster)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(calls, [])

    def test_timeout_does_not_raise_or_mark_sent(self):
        self._channel()

        def poster(url, payload, timeout=6):
            raise requests.Timeout("slow")

        stats = slack_alerts.dispatch_incidents([self._incident()], now=WHEN, poster=poster)
        self.assertEqual(stats["sent"], 0)
        self.assertGreaterEqual(stats["errors"], 1)
        deliveries = slack_alerts.list_deliveries()
        self.assertFalse(any(row["status"] == "sent" for row in deliveries))

    def test_post_webhook_mocks_http_and_rejects_other_hosts(self):
        with mock.patch("slack_alerts.requests.post") as posted:
            posted.return_value.status_code = 200
            posted.return_value.text = "ok"
            status, body = slack_alerts.post_webhook(WEBHOOK, {"text": "hi"})
            self.assertEqual((status, body), (200, "ok"))
            self.assertFalse(posted.call_args.kwargs["allow_redirects"])
            self.assertEqual(posted.call_args.kwargs["timeout"], slack_alerts.WEBHOOK_TIMEOUT)
            posted.side_effect = requests.Timeout("slow")
            status, body = slack_alerts.post_webhook(WEBHOOK, {"text": "hi"})
            self.assertEqual((status, body), (0, "timeout"))
            posted.reset_mock()
            status, body = slack_alerts.post_webhook("https://example.com/hook", {"text": "hi"})
            self.assertEqual(status, 0)
            posted.assert_not_called()

    def test_new_items_after_bootstrap_are_dispatched(self):
        self._channel()
        self._insert_item(1, title="Already stored")
        calls, poster = self._poster()
        first = slack_alerts.run_cycle(now=WHEN, poster=poster)
        self.assertTrue(first["bootstrapped"])
        self.assertEqual(calls, [])
        self._insert_item(2, title="Fresh strike")
        second = slack_alerts.run_cycle(now=WHEN, poster=poster)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Fresh strike", calls[0][1]["text"])

    def test_cycle_survives_a_broken_reader(self):
        original = slack_alerts.read_new_items

        def boom(after_id):
            raise RuntimeError("locked")

        slack_alerts.read_new_items = boom
        try:
            first = slack_alerts.run_cycle(now=WHEN, poster=lambda *args, **kwargs: (200, "ok"))
            self.assertTrue(first["bootstrapped"])
            second = slack_alerts.run_cycle(now=WHEN, poster=lambda *args, **kwargs: (200, "ok"))
        finally:
            slack_alerts.read_new_items = original
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "RuntimeError")

    def test_notify_after_ingest_does_not_raise(self):
        slack_alerts.notify_after_ingest()

    def _insert_item(self, row_id, title):
        conn = sqlite3.connect(self.items)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY,
                title TEXT,
                summary TEXT,
                country TEXT,
                region TEXT,
                severity TEXT,
                category TEXT,
                source_url TEXT,
                source_name TEXT,
                location_name TEXT,
                published_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO items (
                id, title, summary, country, region, severity, category, source_url, source_name, location_name, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                title,
                "A short English summary of the incident.",
                "Syria",
                "Syria",
                "High",
                "ACT",
                f"https://example.test/{row_id}",
                "Field wire",
                "Aleppo",
                "2026-11-27T12:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

    def _client(self):
        app = Flask(__name__)
        app.register_blueprint(slack_alerts.slack_bp)
        return app.test_client()

    def test_api_crud_masks_url_and_sends_a_test(self):
        client = self._client()
        created = client.post(
            "/api/slack/channels",
            json={
                "label": "#security-ops",
                "webhook_url": WEBHOOK,
                "regions": ["Middle East"],
                "countries": ["Syria"],
                "categories": ["ACT", "TSUNAMI"],
                "min_severity": "MED",
                "rate_limit_per_hour": 8,
                "digest_minutes": 10,
            },
        )
        self.assertEqual(created.status_code, 200)
        body = created.get_json()
        self.assertNotIn(SECRET, created.get_data(as_text=True))
        self.assertEqual(body["channel"]["webhook_url_masked"], "https://hooks.slack.com/services/T.../B.../••••1234")
        self.assertNotIn("webhook_url", body["channel"])
        channel_id = body["channel"]["id"]
        updated = client.post(
            "/api/slack/channels",
            json={"id": channel_id, "label": "#ops-floor", "webhook_url": body["channel"]["webhook_url_masked"]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["channel"]["label"], "#ops-floor")
        with mock.patch("slack_alerts.post_webhook", return_value=(200, "ok")) as posted:
            tested = client.post(f"/api/slack/channels/{channel_id}/test")
        self.assertEqual(tested.status_code, 200)
        self.assertTrue(tested.get_json()["ok"])
        self.assertEqual(tested.get_json()["status"], 200)
        self.assertEqual(tested.get_json()["slack"], "ok")
        self.assertEqual(posted.call_args[0][0], WEBHOOK)
        self.assertEqual(posted.call_args[0][1]["attachments"][0]["blocks"][-1]["elements"][0]["text"]["text"], "View on Live Map")
        deliveries = client.get("/api/slack/deliveries")
        self.assertEqual(deliveries.status_code, 200)
        rows = deliveries.get_json()["deliveries"]
        self.assertEqual(rows[0]["status"], "sent")
        self.assertTrue(rows[0]["event_key"].startswith("test:"))
        self.assertNotIn(SECRET, deliveries.get_data(as_text=True))
        removed = client.delete(f"/api/slack/channels/{channel_id}")
        self.assertEqual(removed.status_code, 200)
        listing = client.get("/api/slack/channels")
        self.assertEqual(listing.get_json()["channels"], [])
        missing = client.delete(f"/api/slack/channels/{channel_id}")
        self.assertEqual(missing.status_code, 404)

    def test_api_rejects_a_bad_webhook(self):
        client = self._client()
        response = client.post(
            "/api/slack/channels",
            json={"label": "#security-ops", "webhook_url": "https://example.com/hook"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("hooks.slack.com", response.get_json()["error"])

    def test_preview_uses_the_request_host(self):
        client = self._client()
        response = client.post("/api/slack/preview", json={"country": "Iraq", "severity": "LOW", "category": "OD"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["payload"]
        self.assertEqual(payload["attachments"][0]["color"], "#3b82f6")
        self.assertTrue(_button(payload)["url"].startswith("http://localhost/?country=Iraq"))

    def test_routes_beat_a_later_catch_all(self):
        app = Flask(__name__)

        @app.route("/api/<path:endpoint>", methods=["GET", "POST"])
        def forward(endpoint):
            return {"forwarded": endpoint}, 200

        @app.get("/<path:filename>")
        def static_file(filename):
            return "catch " + filename, 200

        app.register_blueprint(slack_alerts.slack_bp)
        client = app.test_client()
        page = client.get("/slack-alerts")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Send Test Alert", page.data)
        created = client.post("/api/slack/channels", json={"label": "#security-ops", "webhook_url": WEBHOOK})
        self.assertEqual(created.status_code, 200)
        self.assertNotIn("forwarded", created.get_json())
        missing = client.delete("/api/slack/channels/404")
        self.assertEqual(missing.status_code, 404)

    def test_preview_auth_protects_slack_routes(self):
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        app = Flask(__name__)
        preview_lock.install(app)
        app.register_blueprint(slack_alerts.slack_bp)
        client = app.test_client()
        api = client.get("/api/slack/channels")
        self.assertEqual(api.status_code, 401)
        page = client.get("/slack-alerts")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].startswith("/login"))

    def test_pages_link_the_add_on_without_touching_the_preview_lock(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn("slack_alerts.slack_bp", app_source)
        self.assertIn("notify_after_ingest", app_source)
        self.assertNotIn("slack", (root / "preview_lock.py").read_text(encoding="utf-8").lower())
        self.assertIn('href="/slack-alerts"', (root / "grok-ops.html").read_text(encoding="utf-8"))
        pricing = (root / "pricing.html").read_text(encoding="utf-8")
        self.assertIn("Live Slack Ops-Room Alerts", pricing)
        self.assertIn('id="addon-slack"', pricing)
        self.assertIn('data-price="slack"', pricing)
        self.assertIn('data-gbp-monthly="£29"', pricing)
        self.assertIn('data-usd-monthly="$39"', pricing)
        self.assertIn("1 Live Slack Ops-Room channel included", pricing)


if __name__ == "__main__":
    unittest.main()
