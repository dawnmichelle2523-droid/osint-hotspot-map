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
os.environ.pop("GeoRiskGlobal", None)
os.environ.pop("GeoRiskIntel", None)
os.environ.pop("GEORISKGLOBAL", None)
os.environ.pop("GEORISKINTEL", None)
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
PRIMARY_KEY = "sk-ant-primary-test-value"
FALLBACK_KEY = "sk-ant-georiskglobal-test-value"
LEGACY_KEY = "sk-ant-georiskintel-legacy-test"


def _clear_anthropic_keys():
    os.environ["ANTHROPIC_API_KEY"] = ""
    for name in ("GeoRiskGlobal", "GeoRiskIntel", "GEORISKGLOBAL", "GEORISKINTEL"):
        os.environ.pop(name, None)
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
        _clear_anthropic_keys()
        os.environ.pop("CLAUDE_RA_MODEL", None)
        os.environ.pop("CLAUDE_BRIEF_MODEL", None)
        claude_client.reset_call_count()
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        _clear_anthropic_keys()
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

    def test_prompts_and_fallbacks_use_uk_english(self):
        context, _ = claude_client.public_context(
            {"country": "Syria", "region": "Middle East", "sector": "Media", "cadence": "weekly"},
            task="briefing",
        )
        risk_context, _ = claude_client.public_context(
            {
                "country": "Syria",
                "sector": "Media",
                "compliance_standard": "ISO 31030",
                "hazards": [{"threat": "Indirect fire", "likelihood": 4, "impact": 3}],
            },
            task="risk",
        )
        briefing_system, _ = claude_client._prompt("briefing", context)
        risk_system, risk_user = claude_client._prompt("risk", risk_context)
        briefing = claude_client.local_briefing_narrative(context)
        risk = claude_client.local_risk_narrative(risk_context)
        for text in (briefing_system, risk_system, briefing, risk, risk_user):
            self.assertIn("UK English", text)
            lowered = text.lower()
            for american in ("traveling", "organization", "defense", "license ", "labeled", "toward ", "analyze", "behavior", "prioritize", "center"):
                self.assertNotIn(american, lowered, american)
        self.assertIn("travelling", briefing_system.lower())
        risk_lower = risk_system.lower()
        self.assertIn("organisation", risk_lower)
        self.assertIn("prioritise", risk_lower)
        self.assertIn("centre", risk_lower)
        self.assertIn("traveller", risk_lower)
        self.assertIn("mitigation", risk_lower)
        self.assertIn("stand-off", risk_lower)
        self.assertIn("medevac", risk_lower)
        self.assertIn("hostile environment", risk_lower)
        for standard in ("ISO 31000", "ISO 31030", "ISO 45001", "BS 8848", "ISO 18788", "ISO 22361"):
            self.assertIn(standard, risk_system, standard)
            self.assertIn(standard, risk, standard)
        for phrase in ("NOTAM", "EASA", "golden hour", "+30", "+60", "+120", "hibernation", "Conditional-Go", "No-Go", "5×5"):
            self.assertIn(phrase, risk_system, phrase)
        for heading in (
            "BLUF",
            "Verified Security Incidents",
            "Sub-Regional/Governorate Status",
            "Aviation/Routes/Infrastructure",
            "24h Operational Advice",
            "24–72h Outlook",
        ):
            self.assertIn(heading, briefing_system, heading)
            self.assertIn(heading, briefing, heading)
        self.assertIn("travel advice", briefing)
        self.assertIn("travel advice", risk)
        self.assertIn("Indirect fire", risk)

    def test_prices_follow_the_published_list(self):
        self.assertEqual(claude_client.estimate_usd("claude-sonnet-5-5", 1_000_000, 0), 2.0)
        self.assertEqual(claude_client.estimate_usd("claude-sonnet-5-5", 0, 1_000_000), 10.0)
        self.assertEqual(claude_client.estimate_usd("claude-haiku-5-5", 100_000, 0), 0.01)
        self.assertEqual(claude_client.estimate_usd("claude-haiku-5-5", 100_001, 0), round(100_001 / 1_000_000 * 0.5, 6))
        self.assertIsNone(claude_client.estimate_usd("claude-unknown-model", 10, 10))

    def _capture_draft(self, calls):
        def capture(url, json=None, headers=None, timeout=None):
            calls.append({"json": json, "headers": headers})
            return FakeResponse(200, _ok_payload())

        return capture

    def test_get_anthropic_api_key_supports_primary_global_and_legacy(self):
        os.environ["ANTHROPIC_API_KEY"] = PRIMARY_KEY
        os.environ["GeoRiskGlobal"] = FALLBACK_KEY
        os.environ["GeoRiskIntel"] = LEGACY_KEY
        self.assertEqual(claude_client.get_anthropic_api_key(), PRIMARY_KEY)

        os.environ["ANTHROPIC_API_KEY"] = " \n"
        self.assertEqual(claude_client.get_anthropic_api_key(), FALLBACK_KEY)

        os.environ["GeoRiskGlobal"] = "  "
        self.assertEqual(claude_client.get_anthropic_api_key(), LEGACY_KEY)

        os.environ.pop("GeoRiskIntel", None)
        os.environ["GEORISKGLOBAL"] = FALLBACK_KEY
        os.environ["GEORISKINTEL"] = LEGACY_KEY
        self.assertEqual(claude_client.get_anthropic_api_key(), "")

        os.environ.pop("GEORISKGLOBAL", None)
        os.environ.pop("GEORISKINTEL", None)
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["GeoRiskIntel"] = f"  {LEGACY_KEY}  "
        seen = []
        with patch("claude_client.requests.post", side_effect=self._capture_draft(seen)):
            risk = self.client.post("/api/risk-assessments/ai-draft", json={"country": "Iraq", "sector": "Media"})
        self.assertEqual(claude_client.get_anthropic_api_key(), LEGACY_KEY)
        self.assertEqual(seen[0]["headers"]["x-api-key"], LEGACY_KEY)
        self.assertEqual(risk.get_json()["source"], "claude")
        published = json.dumps(risk.get_json())
        self.assertNotIn(LEGACY_KEY, published)
        self.assertNotIn("GeoRiskIntel", published)
        self.assertNotIn("GeoRiskGlobal", published)

    def test_anthropic_api_key_takes_precedence_over_georiskglobal(self):
        os.environ["ANTHROPIC_API_KEY"] = PRIMARY_KEY
        os.environ["GeoRiskGlobal"] = FALLBACK_KEY
        os.environ["GeoRiskIntel"] = LEGACY_KEY
        seen = []
        with patch("claude_client.requests.post", side_effect=self._capture_draft(seen)):
            risk = self.client.post("/api/risk-assessments/ai-draft", json={"country": "Iraq", "sector": "Media"})
            brief = claude_client.draft_briefing({"country": "Iraq", "cadence": "daily"})
            status = self.client.get("/api/ai/claude-status")
        self.assertEqual(claude_client.get_anthropic_api_key(), PRIMARY_KEY)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["headers"]["x-api-key"], PRIMARY_KEY)
        self.assertEqual(seen[1]["headers"]["x-api-key"], PRIMARY_KEY)
        self.assertTrue(status.get_json()["configured"])
        published = json.dumps(risk.get_json()) + json.dumps(brief) + status.get_data(as_text=True)
        self.assertNotIn(PRIMARY_KEY, published)
        self.assertNotIn(FALLBACK_KEY, published)
        self.assertNotIn(LEGACY_KEY, published)
        self.assertNotIn("GeoRiskGlobal", published)
        self.assertNotIn("GeoRiskIntel", published)

    def test_georiskglobal_fallback_configures_claude_and_drafts(self):
        blanks = (None, "", "   ", "\n\t")
        for blank in blanks:
            claude_client.reset_call_count()
            if blank is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = blank
            os.environ["GeoRiskGlobal"] = f"  {FALLBACK_KEY}  "
            seen = []
            with patch("claude_client.requests.post", side_effect=self._capture_draft(seen)):
                status = self.client.get("/api/ai/claude-status")
                routed = serve_map.handle_api("GET", "/api/ai/claude-status")
                risk = self.client.post(
                    "/api/risk-assessments/ai-draft",
                    json={"country": "Iraq", "sector": "Media", "operation": f"Desk note {FALLBACK_KEY}"},
                )
                brief = serve_map.handle_api(
                    "POST",
                    "/api/briefings/ai-draft",
                    json.dumps({"country": "Yemen", "cadence": "weekly", "summary": FALLBACK_KEY}).encode("utf-8"),
                )
            self.assertEqual(claude_client.get_anthropic_api_key(), FALLBACK_KEY, repr(blank))
            self.assertEqual(status.get_json()["configured"], True, repr(blank))
            routed_body = json.loads(routed[2].decode("utf-8"))
            self.assertEqual(routed_body["configured"], True)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0]["headers"]["x-api-key"], FALLBACK_KEY)
            self.assertEqual(seen[1]["headers"]["x-api-key"], FALLBACK_KEY)
            risk_body = risk.get_json()
            brief_body = json.loads(brief[2].decode("utf-8"))
            self.assertEqual(risk_body["source"], "claude")
            self.assertEqual(brief_body["source"], "claude")
            published = (
                json.dumps(risk_body)
                + json.dumps(brief_body)
                + json.dumps(routed_body)
                + status.get_data(as_text=True)
            )
            self.assertNotIn(FALLBACK_KEY, published)
            prompt = json.dumps([call["json"] for call in seen])
            self.assertNotIn(FALLBACK_KEY, prompt)
            self.assertIn("Iraq", prompt)

        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("GeoRiskGlobal", None)
        os.environ.pop("GeoRiskIntel", None)
        os.environ["GEORISKGLOBAL"] = FALLBACK_KEY
        os.environ["GEORISKINTEL"] = LEGACY_KEY
        with patch("claude_client.requests.post", side_effect=AssertionError("wrong-case name must not call Anthropic")):
            wrong_case = self.client.get("/api/ai/claude-status").get_json()
        self.assertEqual(claude_client.get_anthropic_api_key(), "")
        self.assertEqual(wrong_case["configured"], False)
        self.assertNotIn(FALLBACK_KEY, json.dumps(wrong_case))

    def test_status_unconfigured_when_both_keys_are_blank(self):
        cases = (
            {"ANTHROPIC_API_KEY": None, "GeoRiskGlobal": None, "GeoRiskIntel": None},
            {"ANTHROPIC_API_KEY": "", "GeoRiskGlobal": "", "GeoRiskIntel": ""},
            {"ANTHROPIC_API_KEY": " \t\n", "GeoRiskGlobal": "   ", "GeoRiskIntel": " \n"},
            {"ANTHROPIC_API_KEY": None, "GeoRiskGlobal": "  ", "GeoRiskIntel": None},
            {"ANTHROPIC_API_KEY": "", "GeoRiskGlobal": None, "GeoRiskIntel": "  "},
        )

        def explode(*args, **kwargs):
            raise AssertionError("a blank key must not call Anthropic")

        for case in cases:
            for name, value in case.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            with patch("claude_client.requests.post", side_effect=explode):
                payload = claude_client.status_payload()
                response = self.client.post("/api/ai/claude-status")
                draft = self.client.post("/api/briefings/ai-draft", json={"country": "Chad", "cadence": "daily"})
            self.assertEqual(claude_client.get_anthropic_api_key(), "")
            self.assertEqual(payload["configured"], False)
            self.assertEqual(response.get_json()["configured"], False)
            self.assertEqual(draft.get_json()["source"], "local")
            self.assertEqual(draft.get_json()["usage"]["estimated_usd"], 0)
            self.assertEqual(claude_client.anthropic_calls(), 0)
            self.assertNotIn(FALLBACK_KEY, response.get_data(as_text=True))
            self.assertNotIn(PRIMARY_KEY, response.get_data(as_text=True))

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
