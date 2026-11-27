"""Sector-tailored risk assessment templates stay on the page and out of local-only fields."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTORS = [
    "Media & Journalism",
    "Governments & Public Sector",
    "Travel Risk Agencies & TMCs",
    "Insurance & Compliance",
    "NGOs & Humanitarian",
    "Private Security Companies (PSCs) & Ops Rooms",
    "Schools & Universities (BS 8848)",
    "Private Individuals & Expats",
    "C-Suite Executives & Family Offices",
    "Medical Risk & Assistance",
]


def _normalise(text: str) -> str:
    return text.replace("&amp;", "&")


class RiskAssessmentSectorTests(unittest.TestCase):
    def test_page_and_taxonomy_serve_all_ten_sectors(self) -> None:
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        import serve_map

        serve_map.start_background = lambda: None
        import app as app_module

        client = app_module.app.test_client()
        page = client.get("/risk-assessments")
        script = client.get("/static/ra-sectors.js")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", (script.content_type or "").lower())
        page_text = _normalise(page.get_data(as_text=True))
        script_text = script.get_data(as_text=True)
        page.close()
        script.close()
        self.assertIn("/static/ra-sectors.js", page_text)
        self.assertIn('id="sector"', page_text)
        self.assertEqual(len(SECTORS), 10)
        for label in SECTORS:
            self.assertIn(label, page_text, label)
            self.assertIn(label, script_text, label)

    def test_taxonomy_module_covers_every_dropdown(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r"""
const sectors = require("./static/ra-sectors.js");
const labels = new Set(sectors.map((item) => item.label));
const expected = %s;
if (sectors.length !== 10) process.exit(2);
expected.forEach((label) => { if (!labels.has(label)) process.exit(3); });
const need = ["templates", "operations", "threats", "controls", "transport", "accommodation", "comms", "owners", "context"];
sectors.forEach((sector) => {
  need.forEach((key) => {
    if (!Array.isArray(sector[key]) || !sector[key].length) process.exit(4);
  });
  ["heading", "names", "contacts", "medical", "passport"].forEach((key) => {
    if (!sector.localLabels || !sector.localLabels[key]) process.exit(5);
  });
  if (!sector.templates.some((item) => item.title === "ISO 31000 + ISO 31030")) process.exit(6);
  if (!sector.templates.some((item) => item.pdr)) process.exit(7);
  sector.controls.forEach((control) => {
    if (!control.id || !control.label || !control.text) process.exit(8);
    if (!(control.residualL >= 1 && control.residualI >= 1)) process.exit(9);
  });
});
const byId = Object.fromEntries(sectors.map((item) => [item.id, item]));
function has(id, key, value) {
  if (!byId[id][key].includes(value)) process.exit(10);
}
has("media", "operations", "Frontline / Conflict Reporting");
has("media", "threats", "Detention/Arrest");
has("media", "comms", "Encrypted Signal");
has("schools", "operations", "Cultural City Visit");
has("schools", "threats", "Lost Student");
has("schools", "transport", "School coach");
has("medical", "operations", "Outbreak Response");
has("medical", "threats", "Cold-Chain Failure");
has("csuite", "threats", "Executive Doxxing");
has("psc", "transport", "B6 armoured");
has("ngo", "transport", "Soft-skin convoy");
if (byId.schools.localLabels.heading !== "Pupil Roster, Allergies & Parent Emergency Contacts (Local-Only)") process.exit(11);
if (byId.medical.localLabels.heading !== "Clinical/Trauma Capabilities, Blood Type & Medevac Policy (Local-Only)") process.exit(12);
if (byId.psc.localLabels.names !== "Principal & Close-Protection Callsigns (Local-Only)") process.exit(13);
if (byId.csuite.localLabels.names !== "Principal & Close-Protection Callsigns (Local-Only)") process.exit(14);
if (!byId.media.templates.some((item) => item.title === "Hostile Environment / Media Safety")) process.exit(15);
if (!byId.schools.templates.some((item) => item.title === "BS 8848 Educational Visits")) process.exit(16);
if (!byId.ngo.templates.some((item) => item.title === "Humanitarian Access & Duty of Care")) process.exit(17);
if (!byId.psc.templates.some((item) => item.title === "Close Protection SOP")) process.exit(18);
if (!byId.medical.templates.some((item) => item.title === "Clinical/Outbreak & Medevac Standard")) process.exit(19);
if (!byId.tmc.templates.some((item) => item.title === "Corporate Duty of Care")) process.exit(20);
""" % SECTORS
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_risk_draft_prompt_includes_sector_and_drops_local_pii(self) -> None:
        import platform_phase1

        captured = {}

        def fake(system, user, fallback, meta):
            captured["system"] = system
            captured["user"] = user
            captured["fallback"] = fallback
            captured["meta"] = meta
            yield "data: {}\n\n"

        original = platform_phase1._stream_model
        platform_phase1._stream_model = fake
        try:
            list(platform_phase1.stream_risk_draft({
                "sector": "Schools & Universities (BS 8848)",
                "compliance_standard": "BS 8848 · UK DfE / OEAP · Educational Visits",
                "template": "BS 8848 Educational Visits",
                "operation": "Cultural City Visit",
                "country": "Jordan",
                "city": "Amman",
                "transport": "School coach",
                "names": "Secret Pupil Name",
                "contacts": "Parent 555",
                "medical": "peanut allergy",
                "passport": "AB1234567",
                "advisor": "Advisor Name",
                "callsign": "EAGLE",
                "roster": "Form 4B",
                "hazards": [{"threat": "Lost Student", "likelihood": 2, "impact": 4}],
            }))
        finally:
            platform_phase1._stream_model = original
        user = captured["user"]
        self.assertIn("Schools & Universities (BS 8848)", user)
        self.assertIn("BS 8848", user)
        self.assertIn("Cultural City Visit", user)
        self.assertIn("School coach", user)
        self.assertIn("Lost Student", user)
        self.assertIn("Schools & Universities (BS 8848)", captured["fallback"])
        self.assertEqual(captured["meta"]["sector"], "Schools & Universities (BS 8848)")
        for secret in ("Secret Pupil Name", "Parent 555", "peanut allergy", "AB1234567", "Advisor Name", "EAGLE", "Form 4B"):
            self.assertNotIn(secret, user)
            self.assertNotIn(secret, captured["system"])
            self.assertNotIn(secret, captured["fallback"])


if __name__ == "__main__":
    unittest.main()
