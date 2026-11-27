"""Daily Area of Operations publisher. No live Claude or xAI call."""

import errno
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("GeoRiskIntel", None)
os.environ.pop("GEORISKINTEL", None)
for _name in (
    "PREVIEW_PASSWORD",
    "PREVIEW_USER",
    "BASIC_AUTH_PASSWORD",
    "BASIC_AUTH_USERNAME",
    "SITE_PRIVATE_MODE",
    "CRON_SECRET",
    "PUBLISH_SECRET",
):
    os.environ.pop(_name, None)

import claude_client
import daily_ao_publisher
from analytics_store import resolve_data_dir
from app import app

ROOT = Path(__file__).resolve().parents[1]
EPHEMERAL = Path(tempfile.gettempdir()) / "georiskintel-data"
US_SPELLINGS = (
    "organization",
    "organizations",
    "center",
    "centers",
    "color",
    "colors",
    "armored",
    "mobilization",
    "defense",
    "traveling",
    "labeled",
    "program",
)
UK_SPELLINGS = ("organisation", "programme", "centre", "colour", "armoured", "mobilisation")


def _incident(**overrides):
    item = {
        "title": "Armed clash reported on the outskirts of Aleppo",
        "summary": "Open-source reporting described shelling near Aleppo. No personal data.",
        "severity": "high",
        "category": "Armed Conflict",
        "country": "Syria",
        "region": "Middle East",
        "area": "Aleppo",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_name": "Public wire",
    }
    item.update(overrides)
    return item


def _claude_text():
    sections = []
    for _number, title in daily_ao_publisher.SECTION_SPECS:
        sections.append({
            "title": title,
            "body": (
                f"{title}. The duty organisation should hold the movement programme at the coordination "
                "centre, keep armoured options ready, and watch for mobilisation. UK English colour code applies."
            ),
        })
    return json.dumps({"threat_level": "SEVERE", "sections": sections})


class PublisherStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "DATA_DIR": self.tmp.name,
                "GEORISKINTEL_DATA_DIR": self.tmp.name,
                "ANTHROPIC_API_KEY": "",
                "DISABLE_BACKGROUND": "1",
            },
            clear=False,
        )
        self.env.start()
        os.environ.pop("CRON_SECRET", None)
        os.environ.pop("PUBLISH_SECRET", None)
        os.environ.pop("GeoRiskIntel", None)
        self.client = app.test_client()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_publish_once_is_idempotent_until_forced(self):
        first = daily_ao_publisher.publish_ao("syria", incidents=[_incident()])
        self.assertTrue(first["published"])
        self.assertFalse(first["cached"])
        self.assertEqual(first["generator"], "deterministic")
        self.assertEqual(first["cost_usd"], 0)
        path = daily_ao_publisher.edition_file(first["ao_slug"], first["date"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, first["date"])
        self.assertTrue(str(path).endswith("/syria.json"))
        stored = path.read_text(encoding="utf-8")

        with patch.object(daily_ao_publisher, "build_deterministic", side_effect=AssertionError("regenerated")):
            with patch.object(daily_ao_publisher, "_try_claude", side_effect=AssertionError("claude")):
                second = daily_ao_publisher.publish_ao("syria", use_claude=True, incidents=[_incident(title="Different")])

        self.assertTrue(second["cached"])
        self.assertEqual(second["edition_id"], first["edition_id"])
        self.assertEqual(second["published_at"], first["published_at"])
        self.assertEqual(path.read_text(encoding="utf-8"), stored)
        self.assertIn("Aleppo", json.dumps(second))

        refreshed = daily_ao_publisher.publish_ao(
            "syria",
            force=True,
            incidents=[_incident(title="Refreshed clash reported in Homs", area="Homs")],
        )
        self.assertFalse(refreshed["cached"])
        self.assertNotEqual(refreshed["edition_id"], first["edition_id"])
        self.assertIn("Homs", json.dumps(refreshed))

    def test_batch_publishes_country_and_region(self):
        payload, status = daily_ao_publisher.publish_request({
            "ao_slugs": ["syria", "middle-east", "sahel"],
            "use_claude": False,
        }, incidents=[
            _incident(),
            _incident(title="Banditry reported near Bamako", country="Mali", region="Sahel", area="Bamako", severity="medium"),
            _incident(title="Tanker attack reported in the Strait of Hormuz", country="", region="Maritime", area="Strait of Hormuz", severity="high"),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["mode"], "deterministic")
        self.assertEqual(payload["cost_usd"], 0)
        kinds = {item["ao_slug"]: item["ao_type"] for item in payload["published"]}
        self.assertEqual(kinds["syria"], "country")
        self.assertEqual(kinds["middle-east"], "region")
        self.assertEqual(kinds["sahel"], "region")
        self.assertIn("Aleppo", json.dumps(next(item for item in payload["published"] if item["ao_slug"] == "syria")))
        self.assertIn("Bamako", json.dumps(next(item for item in payload["published"] if item["ao_slug"] == "sahel")))

        subscribed, subscribed_status = daily_ao_publisher.publish_request({"all_subscribed": True})
        self.assertEqual(subscribed_status, 200)
        slugs = [item["ao_slug"] for item in subscribed["published"]]
        self.assertEqual(slugs, list(daily_ao_publisher.DEFAULT_SUBSCRIBED))
        cached = daily_ao_publisher.publish_ao("middle-east")
        self.assertTrue(cached["cached"])

    def test_deterministic_mode_and_get_never_call_claude(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("Claude or xAI was called")

        with patch("claude_client.requests.post", side_effect=explode), patch("requests.post", side_effect=explode):
            created = self.client.post("/api/briefings/publish", json={"ao_slug": "ukraine", "use_claude": False})
            self.assertEqual(created.status_code, 200, created.get_json())
            body = created.get_json()
            edition = body["published"][0]
            self.assertEqual(edition["generator"], "deterministic")
            self.assertEqual(edition["cost_usd"], 0)
            self.assertEqual(edition["api_calls"], 0)
            index = self.client.get("/api/briefings/published")
            self.assertEqual(index.status_code, 200)
            listed = index.get_json()
            self.assertGreaterEqual(listed["today_count"], 1)
            row = next(item for item in listed["briefings"] if item["ao_slug"] == "ukraine")
            self.assertEqual(row["ao_type"], "country")
            self.assertEqual(row["generator"], "deterministic")
            self.assertTrue(row["cached_for_today"])
            self.assertIn("published_at_gmt", row)
            self.assertTrue(row["published_at_gmt"].endswith("GMT"))
            fetched = self.client.get("/api/briefings/published/ukraine")
            self.assertEqual(fetched.status_code, 200)
            self.assertTrue(fetched.get_json()["published"])
            self.assertEqual(claude_client.anthropic_calls(), 0)
            preview = self.client.get("/api/briefings/published/lebanon")
            self.assertEqual(preview.status_code, 200)
            self.assertFalse(preview.get_json()["published"])
            self.assertEqual(preview.get_json()["generator"], "deterministic")
            self.assertFalse(daily_ao_publisher.edition_file("lebanon", daily_ao_publisher.day_string()).exists())

    def test_use_claude_is_mocked_and_falls_back(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-publisher"
        os.environ.pop("GeoRiskIntel", None)
        calls = {"n": 0}

        def fake_call(*_args, **_kwargs):
            calls["n"] += 1
            return _claude_text(), {"input_tokens": 180, "output_tokens": 90}, ""

        with patch("claude_client._call_anthropic", side_effect=fake_call):
            created = self.client.post("/api/briefings/publish", json={
                "ao_slug": "yemen",
                "use_claude": True,
                "force": True,
            })
        self.assertEqual(created.status_code, 200, created.get_json())
        edition = created.get_json()["published"][0]
        self.assertEqual(calls["n"], 1)
        self.assertEqual(edition["generator"], "claude-haiku-5-5")
        self.assertGreater(edition["cost_usd"], 0)
        self.assertEqual(len(edition["sections"]), 6)

        with patch("claude_client._call_anthropic", side_effect=AssertionError("GET must not call Claude")):
            again = self.client.get("/api/briefings/published/yemen")
            listed = self.client.get("/api/briefings/published")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["generator"], "claude-haiku-5-5")
        self.assertTrue(listed.get_json()["ok"])

        def fail_call(*_args, **_kwargs):
            calls["n"] += 1
            return "", {}, "error"

        with patch("claude_client._call_anthropic", side_effect=fail_call):
            failed = self.client.post("/api/briefings/publish", json={
                "ao_slug": "yemen",
                "use_claude": True,
                "force": True,
            })
        self.assertEqual(failed.get_json()["published"][0]["generator"], "deterministic_fallback")
        self.assertEqual(failed.get_json()["published"][0]["cost_usd"], 0)

        os.environ["ANTHROPIC_API_KEY"] = ""
        with patch("claude_client._call_anthropic", side_effect=AssertionError("unconfigured must not call")):
            quiet = self.client.post("/api/briefings/publish", json={
                "ao_slug": "nigeria",
                "use_claude": True,
            })
        self.assertEqual(quiet.get_json()["published"][0]["generator"], "deterministic_fallback")
        self.assertEqual(quiet.get_json()["published"][0]["api_calls"], 0)

    def test_cron_secret_rejects_a_missing_bearer(self):
        os.environ["CRON_SECRET"] = "morning-secret"
        os.environ.pop("PUBLISH_SECRET", None)
        denied = self.client.post("/api/briefings/publish", json={"ao_slug": "syria"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["error"], "Unauthorised.")
        wrong = self.client.post(
            "/api/briefings/publish",
            json={"ao_slug": "syria"},
            headers={"Authorization": "Bearer other-secret"},
        )
        self.assertEqual(wrong.status_code, 401)
        allowed = self.client.post(
            "/api/briefings/publish",
            json={"ao_slug": "syria"},
            headers={"Authorization": "Bearer morning-secret"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        self.assertTrue(allowed.get_json()["published"][0]["published"])

        os.environ.pop("CRON_SECRET", None)
        os.environ["PUBLISH_SECRET"] = "ui-secret"
        via_publish = self.client.post(
            "/api/briefings/publish",
            json={"ao_slug": "sudan"},
            headers={"Authorization": "Bearer ui-secret"},
        )
        self.assertEqual(via_publish.status_code, 200, via_publish.get_json())
        os.environ.pop("PUBLISH_SECRET", None)
        open_post = self.client.post("/api/briefings/publish", json={"ao_slug": "lebanon"})
        self.assertEqual(open_post.status_code, 200, open_post.get_json())

    def test_vercel_readonly_filesystem_uses_resolve_data_dir(self):
        blocked = Path(self.tmp.name) / "var-task"
        real_mkdir = Path.mkdir

        def guarded(target, *args, **kwargs):
            path = Path(target)
            if path == blocked or blocked in path.parents:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_mkdir(path, *args, **kwargs)

        env = {
            "VERCEL": "1",
            "GEORISKINTEL_DATA_DIR": str(blocked),
            "DATA_DIR": str(blocked),
        }
        self.env.stop()
        try:
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("DB_PATH", None)
                with patch.object(Path, "mkdir", guarded):
                    edition = daily_ao_publisher.publish_ao("syria", incidents=[_incident()], force=True)
                    resolved = resolve_data_dir()
                self.assertEqual(resolved, EPHEMERAL)
                stored = EPHEMERAL / "daily_briefings" / edition["date"] / "syria.json"
                self.assertTrue(stored.is_file(), stored)
                self.assertNotIn(str(ROOT), str(stored))
                self.assertFalse((blocked / "daily_briefings").exists())
                self.assertIn("resolve_data_dir", daily_ao_publisher.data_root.__doc__ or "")
                payload = json.loads(stored.read_text(encoding="utf-8"))
                self.assertEqual(payload["ao_slug"], "syria")
        finally:
            leftover = EPHEMERAL / "daily_briefings"
            if leftover.exists():
                for path in leftover.rglob("syria.json"):
                    path.unlink()
            self.env.start()

    def test_uk_english_six_sections_drop_personal_data(self):
        edition = daily_ao_publisher.publish_ao("israel-gaza", incidents=[
            _incident(
                title="Rocket fire reported near Gaza. Staff Jane Doe email jane.doe@news.example phone +44 7700 900123",
                summary="Contact jane.doe@news.example for the roster.",
                country="Palestine",
                region="Levant",
                area="Gaza",
                severity="high",
            ),
            _incident(title="Sirens sounded in northern Israel", country="Israel", region="Levant", area="Northern Israel", severity="high"),
            _incident(title="Crossing closed on the Lebanese border", country="Israel", region="Levant", area="Northern Israel", severity="medium"),
        ])
        self.assertEqual(edition["ao_name"], "Israel/Gaza")
        self.assertEqual(edition["ao_type"], "country")
        self.assertEqual(edition["language"], "en-GB")
        self.assertFalse(edition["pii"])
        self.assertEqual(len(edition["sections"]), 6)
        titles = [section["title"] for section in edition["sections"]]
        for number, (_section_id, heading) in enumerate(daily_ao_publisher.SECTION_SPECS, start=1):
            self.assertEqual(titles[number - 1], f"{number}. {heading}")
        blob = json.dumps(edition)
        for word in UK_SPELLINGS:
            self.assertIn(word, blob, word)
        for word in US_SPELLINGS:
            self.assertNotRegex(blob, rf"\b{word}\b", word)
        self.assertNotIn("jane.doe@news.example", blob)
        self.assertNotIn("+44 7700 900123", blob)
        self.assertNotIn("Jane Doe", blob)
        self.assertIn("published movement programme", blob)
        self.assertNotIn("[removed] movement", blob)
        self.assertIn("Gaza", blob)
        self.assertIn(edition["threat_level"], daily_ao_publisher.LEVELS)
        self.assertIn(edition["threat_level"], edition["sections"][0]["body"])
        self.assertIn("GMT", edition["published_at_gmt"])

    def test_index_date_filter_and_unknown_area(self):
        daily_ao_publisher.publish_ao("sudan", incidents=[_incident(title="Clash reported in Khartoum", country="Sudan", area="Khartoum")])
        today = daily_ao_publisher.day_string()
        listed = self.client.get("/api/briefings/published", query_string={"date": today})
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(item["ao_slug"] == "sudan" for item in listed.get_json()["briefings"]))
        empty = self.client.get("/api/briefings/published", query_string={"date": "2001-01-01"})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json()["briefings"], [])
        bad = self.client.get("/api/briefings/published", query_string={"date": "27-11-2026"})
        self.assertEqual(bad.status_code, 400)
        missing = self.client.get("/api/briefings/published/not-a-real-ao")
        self.assertEqual(missing.status_code, 404)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        absent = self.client.get("/api/briefings/published/sudan", query_string={"date": yesterday})
        self.assertEqual(absent.status_code, 404)
        regions = {area["ao_slug"] for area in listed.get_json()["areas"] if area["ao_type"] == "region"}
        for slug in ("middle-east", "east-africa-horn", "sahel", "eastern-europe", "indo-pacific", "global"):
            self.assertIn(slug, regions, slug)


class BriefingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()

    def test_briefing_page_has_publisher_and_branded_print_sheet(self):
        page = self.client.get("/briefing")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('id="ao-region"', html)
        self.assertIn('id="ao-country"', html)
        self.assertIn("East Africa / Horn of Africa", html)
        self.assertIn("Israel/Gaza", html)
        self.assertIn('id="publisher-controls"', html)
        self.assertIn("Today's published AO count", html)
        self.assertIn("Publish Today's Briefing ($0 OSINT)", html)
        self.assertIn("Publish with Claude (Haiku 5.5)", html)
        self.assertIn("Estimated cost", html)
        self.assertIn('data-claude-status', html)
        self.assertIn("Live $0 Preview", html)
        self.assertIn(">Published<", html)
        self.assertIn("window.print()", html)
        self.assertIn("@media print", html)
        self.assertIn("size: A4", html)
        self.assertIn("/static/logo.png", html)
        self.assertIn("Powered by GeoRiskIntel", html)
        self.assertIn("print-header", html)
        self.assertIn("Executive Threat Assessment", html)
        self.assertIn("72-Hour Watchlist", html)
        self.assertIn("Draft with Claude", html)
        self.assertIn("/api/briefings/ai-draft", html)
        page.close()

        reports = self.client.get("/reports")
        self.assertEqual(reports.status_code, 200)
        reports_html = reports.get_data(as_text=True)
        self.assertIn('href="/briefing"', reports_html)
        self.assertIn("Daily AO geopolitical publisher", reports_html)
        reports.close()

        workflow = (ROOT / ".github" / "workflows" / "daily-ao-briefings.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "0 6 * * *"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("GEORISKINTEL_BASE_URL", workflow)
        self.assertIn('"use_claude":false', workflow)
        self.assertIn("No AI credits spent", workflow)


if __name__ == "__main__":
    unittest.main()
