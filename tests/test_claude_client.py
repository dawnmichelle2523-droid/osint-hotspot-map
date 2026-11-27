"""On-demand Claude drafts. HTTP is mocked. No live Anthropic or xAI call."""

import inspect
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("CLAUDE_RA_MODEL", None)
os.environ.pop("CLAUDE_BRIEF_MODEL", None)
for _name in (
    "PREVIEW_PASSWORD",
    "PREVIEW_USER",
    "BASIC_AUTH_PASSWORD",
    "BASIC_AUTH_USERNAME",
    "SITE_PRIVATE_MODE",
):
    os.environ.pop(_name, None)

import requests

import claude_client
import report_builder
import serve_map
from app import app

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-ant-test-secret-value"
STAFF = "Jane Doe"
EMAIL = "jane.doe@news.example"
PHONE = "+44 7700 900123"
IMEI = "490154203237518"
HIDDEN_EMAIL = "hidden@secret.example"
MEDICAL = "allergic to penicillin"
TRAVELLER = "Amira Khalil"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.ok = 200 <= status_code < 300
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


def _ok_payload(text="Hold movement in the named area and re-check the advisory."):
    return {
        "content": [
            {"type": "thinking", "thinking": "THINKING-SECRET-BLOCK"},
            {"type": "text", "text": text},
        ],
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def _pii_body():
    return {
        "country": "Syria",
        "region": "Levant",
        "sector": "Media",
        "operation": f"News gathering. Staff: {STAFF}. Email {EMAIL}. Phone {PHONE}. IMEI {IMEI}.",
        "city": "12 Private Road",
        "transport": f"Driver: {STAFF}",
        "comms": PHONE,
        "names": TRAVELLER,
        "contacts": HIDDEN_EMAIL,
        "medical": MEDICAL,
        "passport": "P1234567",
        "reviewer": STAFF,
        "imei": "356938035643809",
        "compliance_standard": "ISO 31030",
        "hazards": [{
            "threat": "Indirect fire",
            "likelihood": 4,
            "impact": 4,
            "owner": STAFF,
        }],
        "incidents": [{
            "title": "Checkpoint near Damascus",
            "summary": f"Desk note {HIDDEN_EMAIL} or +963 11 123 4567. IMEI {IMEI}.",
            "severity": "high",
            "area": "Damascus",
            "source_name": "Wire",
            "author_email": HIDDEN_EMAIL,
        }],
    }


class ClaudeClientTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ.pop("CLAUDE_RA_MODEL", None)
        os.environ.pop("CLAUDE_BRIEF_MODEL", None)
        claude_client.reset_call_count()
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ.pop("CLAUDE_RA_MODEL", None)
        os.environ.pop("CLAUDE_BRIEF_MODEL", None)

    def test_status_reports_models_without_a_key_or_a_call(self):
        calls = []

        def explode(*args, **kwargs):
            calls.append(args)
            raise AssertionError("status must not call the network")

        with patch("claude_client.requests.post", side_effect=explode):
            payload = claude_client.status_payload()
            response = self.client.get("/api/ai/claude-status")
        self.assertEqual(calls, [])
        self.assertEqual(claude_client.anthropic_calls(), 0)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["ra_model"], "claude-sonnet-5-5")
        self.assertEqual(payload["brief_model"], "claude-haiku-5-5")
        self.assertNotIn("sk-ant", json.dumps(payload))
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["configured"], False)
        self.assertEqual(body["ra_model"], "claude-sonnet-5-5")
        self.assertEqual(body["brief_model"], "claude-haiku-5-5")
        self.assertIn("ANTHROPIC_API_KEY", body["status"])
        posted = self.client.post("/api/ai/claude-status")
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.get_json()["configured"], False)

    def test_unconfigured_drafts_use_the_local_template(self):
        def explode(*args, **kwargs):
            raise AssertionError("unconfigured draft must not call the network")

        with patch("claude_client.requests.post", side_effect=explode):
            risk = claude_client.draft_risk_assessment({
                "country": "Syria",
                "sector": "Media",
                "operation": "News gathering",
                "compliance_standard": "ISO 31030",
                "hazards": [{"threat": "Indirect fire", "likelihood": 4, "impact": 3}],
            })
            brief = self.client.post("/api/briefings/ai-draft", json={
                "country": "Yemen",
                "region": "Red Sea",
                "cadence": "weekly",
                "sector": "NGO",
            })
        self.assertEqual(claude_client.anthropic_calls(), 0)
        self.assertEqual(risk["source"], "local")
        self.assertFalse(risk["configured"])
        self.assertEqual(risk["usage"]["estimated_usd"], 0)
        self.assertIn("local template", risk["status"].lower())
        self.assertIn("Media", risk["narrative"])
        self.assertIn("Syria", risk["narrative"])
        self.assertIn("Indirect fire", risk["narrative"])
        self.assertIn("ISO 31030", risk["narrative"])
        self.assertNotIn("api.x.ai", risk["narrative"])
        body = brief.get_json()
        self.assertEqual(brief.status_code, 200)
        self.assertEqual(body["source"], "local")
        self.assertEqual(body["model"], None)
        self.assertIn("weekly", body["narrative"].lower())
        self.assertIn("Yemen", body["narrative"])
        self.assertIn("Red Sea", body["narrative"])
        self.assertIn("Nothing was sent", body["status"])
        self.assertEqual(body["usage"]["estimated_usd"], 0)
        self.assertFalse(body["estimate"]["sent"])

    def test_model_presets_and_overrides_are_on_demand(self):
        seen = []

        def capture(url, json=None, headers=None, timeout=None):
            seen.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return FakeResponse(200, _ok_payload())

        os.environ["ANTHROPIC_API_KEY"] = KEY
        with patch("claude_client.requests.post", side_effect=capture):
            risk = claude_client.draft_risk_assessment({"country": "Iraq", "sector": "Media"})
            brief = claude_client.draft_briefing({"country": "Iraq", "cadence": "monthly"})
        self.assertEqual(len(seen), 2)
        self.assertEqual(claude_client.anthropic_calls(), 2)
        self.assertEqual(seen[0]["url"], claude_client.API_URL)
        self.assertEqual(seen[1]["url"], claude_client.API_URL)
        self.assertEqual(seen[0]["json"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seen[1]["json"]["model"], "claude-haiku-5-5")
        self.assertEqual(seen[0]["json"]["output_config"]["effort"], "medium")
        self.assertEqual(seen[1]["json"]["output_config"]["effort"], "low")
        self.assertNotIn("temperature", seen[0]["json"])
        self.assertNotIn("Authorization", seen[0]["headers"])
        self.assertEqual(seen[0]["headers"]["x-api-key"], KEY)
        self.assertEqual(seen[0]["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(risk["source"], "claude")
        self.assertEqual(risk["model"], "claude-sonnet-5-5")
        self.assertIn("Hold movement", risk["narrative"])
        self.assertNotIn("THINKING-SECRET-BLOCK", risk["narrative"])
        self.assertEqual(risk["usage"]["input_tokens"], 120)
        self.assertEqual(risk["usage"]["output_tokens"], 40)
        self.assertEqual(risk["usage"]["estimated_usd"], 0.00064)
        self.assertEqual(brief["model"], "claude-haiku-5-5")
        self.assertEqual(brief["usage"]["estimated_usd"], 0.000032)
        self.assertNotIn(KEY, json.dumps(risk))
        self.assertNotIn(KEY, json.dumps(brief))

        seen.clear()
        os.environ["CLAUDE_RA_MODEL"] = "claude-sonnet-custom"
        os.environ["CLAUDE_BRIEF_MODEL"] = "claude-haiku-custom"
        with patch("claude_client.requests.post", side_effect=capture):
            claude_client.draft_risk_assessment({"country": "Iraq", "sector": "Media"})
            claude_client.draft_briefing({"country": "Iraq", "cadence": "daily"})
            status = self.client.get("/api/ai/claude-status").get_json()
        self.assertEqual(seen[0]["json"]["model"], "claude-sonnet-custom")
        self.assertEqual(seen[1]["json"]["model"], "claude-haiku-custom")
        self.assertEqual(status["configured"], True)
        self.assertEqual(status["ra_model"], "claude-sonnet-custom")
        self.assertEqual(status["brief_model"], "claude-haiku-custom")
        self.assertNotIn(KEY, json.dumps(status))

    def test_pii_is_stripped_before_the_prompt(self):
        seen = []

        def capture(url, json=None, headers=None, timeout=None):
            seen.append(json)
            return FakeResponse(200, _ok_payload("Public draft only."))

        os.environ["ANTHROPIC_API_KEY"] = KEY
        with patch("claude_client.requests.post", side_effect=capture):
            result = claude_client.draft_risk_assessment(_pii_body())
        blob = json.dumps(seen)
        for secret in (STAFF, EMAIL, PHONE, IMEI, HIDDEN_EMAIL, MEDICAL, TRAVELLER, "356938035643809", "+963 11 123 4567", "12 Private Road", KEY):
            self.assertNotIn(secret, blob, secret)
        self.assertIn("Syria", blob)
        self.assertIn("Levant", blob)
        self.assertIn("Media", blob)
        self.assertIn("Indirect fire", blob)
        self.assertIn("Checkpoint near Damascus", blob)
        self.assertIn("ISO 31030", blob)
        self.assertTrue(result["pii_withheld"])
        self.assertNotIn(STAFF, result["narrative"])
        self.assertNotIn(EMAIL, json.dumps(result))

        os.environ["ANTHROPIC_API_KEY"] = ""
        local = claude_client.draft_risk_assessment(_pii_body())
        local_blob = json.dumps(local)
        for secret in (STAFF, EMAIL, PHONE, IMEI, HIDDEN_EMAIL, MEDICAL, TRAVELLER):
            self.assertNotIn(secret, local_blob, secret)
        self.assertIn("Syria", local["narrative"])
        self.assertEqual(local["source"], "local")

    def test_errors_fall_back_without_retry_or_key_leak(self):
        os.environ["ANTHROPIC_API_KEY"] = KEY
        cases = [
            (FakeResponse(429, {"error": {"message": KEY}}), "rate limit"),
            (FakeResponse(401, {"error": {"message": KEY}}), "rejected the API key"),
            (FakeResponse(500, {"error": {"message": KEY}}), "did not return"),
            (FakeResponse(200, {"content": [{"type": "thinking", "thinking": KEY}]}), "did not return"),
        ]
        for response, expected in cases:
            claude_client.reset_call_count()
            with patch("claude_client.requests.post", return_value=response) as posted:
                result = claude_client.draft_briefing({"country": "Sudan", "cadence": "daily"})
            self.assertEqual(posted.call_count, 1)
            self.assertEqual(claude_client.anthropic_calls(), 1)
            self.assertEqual(result["source"], "local")
            self.assertIn("Sudan", result["narrative"])
            self.assertIn(expected.lower(), result["status"].lower())
            self.assertNotIn(KEY, json.dumps(result))
            self.assertEqual(result["usage"]["estimated_usd"], 0)

        claude_client.reset_call_count()
        with patch("claude_client.requests.post", side_effect=requests.RequestException(KEY)) as posted:
            result = claude_client.draft_risk_assessment({"country": "Lebanon", "sector": "NGO"})
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(result["source"], "local")
        self.assertIn("could not be reached", result["status"].lower())
        self.assertIn("Lebanon", result["narrative"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_background_paths_do_not_call_claude(self):
        watched = (
            serve_map.start_background,
            serve_map.start_poller,
            serve_map.poll_loop,
            serve_map.start_sweep_daemon,
            serve_map.start_summary_daemon,
            serve_map.start_briefing_daemon,
            serve_map.maybe_send_briefings,
            serve_map.start_translation_pass,
            report_builder.start_background,
            report_builder._loop,
            report_builder.generate,
            report_builder.synthesize,
        )
        for fn in watched:
            source = inspect.getsource(fn)
            self.assertNotIn("claude_client", source, fn.__name__)
            self.assertNotIn("api.anthropic.com", source, fn.__name__)
            self.assertNotIn("ANTHROPIC_API_KEY", source, fn.__name__)
        self.assertIn("claude_client", inspect.getsource(serve_map.handle_api))
        module = (ROOT / "claude_client.py").read_text(encoding="utf-8")
        self.assertNotIn("api.x.ai", module)
        self.assertNotIn("XAI_API_KEY", module)

        def explode(*args, **kwargs):
            raise AssertionError("health and status must not call Anthropic or xAI")

        with patch("claude_client.requests.post", side_effect=explode), patch("requests.post", side_effect=explode):
            self.client.get("/api/ai/claude-status")
            self.client.get("/api/health/security")
            routed = serve_map.handle_api("GET", "/api/ai/claude-status")
        self.assertIsNotNone(routed)
        self.assertEqual(routed[0], 200)
        self.assertIn(b'"configured"', routed[2])
        self.assertEqual(claude_client.anthropic_calls(), 0)

    def test_pages_offer_an_explicit_draft_button(self):
        pages = {
            "/risk-assessments": "/api/risk-assessments/ai-draft",
            "/briefing": "/api/briefings/ai-draft",
            "/reports": "/api/briefings/ai-draft",
        }
        for path, endpoint in pages.items():
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            self.assertIn("Draft with Claude", html, path)
            self.assertIn('data-claude-status', html, path)
            self.assertIn("/static/claude-draft.js", html, path)
            self.assertIn(endpoint, html, path)
            self.assertNotIn("api.anthropic.com", html, path)
            self.assertNotIn(KEY, html, path)
            page.close()
        script = (ROOT / "static" / "claude-draft.js").read_text(encoding="utf-8")
        self.assertIn("/api/ai/claude-status", script)
        self.assertNotIn("api.anthropic.com", script)
        self.assertNotIn("api.x.ai", script)
        reports = (ROOT / "static" / "reports.js").read_text(encoding="utf-8")
        handler = reports[reports.index('getElementById("claude-draft")'):reports.index('getElementById("print")')]
        self.assertIn("/api/briefings/ai-draft", handler)
        self.assertNotIn("send-recipients", handler)
        self.assertNotIn("schedule-recipients", handler)

    def test_prices_follow_the_published_list(self):
        self.assertEqual(claude_client.estimate_usd("claude-sonnet-5-5", 1_000_000, 0), 2.0)
        self.assertEqual(claude_client.estimate_usd("claude-sonnet-5-5", 0, 1_000_000), 10.0)
        self.assertEqual(claude_client.estimate_usd("claude-haiku-5-5", 100_000, 0), 0.01)
        self.assertEqual(claude_client.estimate_usd("claude-haiku-5-5", 100_001, 0), round(100_001 / 1_000_000 * 0.5, 6))
        self.assertIsNone(claude_client.estimate_usd("claude-unknown-model", 10, 10))

    def test_builtin_server_dispatch_matches_flask(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        raw = json.dumps({"country": "Chad", "cadence": "monthly", "sector": "NGO"}).encode("utf-8")
        with patch("claude_client.requests.post", side_effect=AssertionError("no network")):
            routed = serve_map.handle_api("POST", "/api/briefings/ai-draft", raw)
        self.assertEqual(routed[0], 200)
        payload = json.loads(routed[2].decode("utf-8"))
        self.assertEqual(payload["source"], "local")
        self.assertIn("monthly", payload["narrative"].lower())
        self.assertIn("Chad", payload["narrative"])


if __name__ == "__main__":
    unittest.main()
