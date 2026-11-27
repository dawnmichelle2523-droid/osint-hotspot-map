"""5×5 risk matrix, six-column mitigation table, and UK English band names."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

import geo_split
import ra_diff

ROOT = Path(__file__).resolve().parents[1]

COLUMNS = [
    "Threat Category",
    "Inherent Rating",
    "Country / Operational Context",
    "Mandatory Mitigation Controls",
    "Residual Rating",
    "Action Owner",
]
CATEGORIES = [
    "Conflict/Armed Clashes",
    "Terrorism/Kidnap",
    "Crime/Road Safety",
    "Civil Unrest/Detention",
    "Medical/Evacuation",
    "Digital/Comms/OPSEC",
    "Environmental/ERW",
]


class ScoreBandTests(unittest.TestCase):
    def test_boundaries_match_the_five_by_five_scale(self):
        expected = {
            1: "LOW",
            4: "LOW",
            5: "MODERATE",
            9: "MODERATE",
            10: "HIGH",
            14: "HIGH",
            15: "VERY HIGH",
            19: "VERY HIGH",
            20: "SEVERE",
            25: "SEVERE",
        }
        for score, name in expected.items():
            self.assertEqual(geo_split.score_band(score), name, score)
        self.assertEqual(geo_split.overall_rating([
            {"likelihood": 5, "impact": 5},
            {"likelihood": 2, "impact": 2},
        ])["band"], "SEVERE")
        moderate = geo_split.overall_rating([{"likelihood": 2, "impact": 3}])
        self.assertEqual(moderate["band"], "MODERATE")
        self.assertEqual(moderate["score"], 6)

    def test_diff_levels_keep_the_new_band_names(self):
        self.assertEqual(ra_diff.normalize_level("Moderate"), "MEDIUM")
        self.assertEqual(ra_diff.normalize_level("Very High"), "VERY HIGH")
        self.assertEqual(ra_diff.normalize_level("Severe"), "EXTREME")
        self.assertEqual(ra_diff.normalize_level("Low"), "LOW")
        for name in ("LOW", "MEDIUM", "HIGH", "VERY HIGH", "EXTREME"):
            self.assertIn(name, ra_diff.TIER_RANK)


class MatrixPageTests(unittest.TestCase):
    def test_page_css_and_script_define_the_matrix_and_table(self):
        page = (ROOT / "risk-assessments.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "platform.css").read_text(encoding="utf-8")
        self.assertIn("/static/risk-matrix.js", page)
        self.assertIn('id="risk-grid"', page)
        self.assertIn("Powered by GeoRiskGlobal", page)
        self.assertIn("Go / Conditional-Go / No-Go", page)
        for column in COLUMNS:
            self.assertIn(column, page, column)
        for category in CATEGORIES:
            self.assertIn(category, (ROOT / "static" / "risk-matrix.js").read_text(encoding="utf-8"), category)
        self.assertIn(".risk-cell", css)
        self.assertIn("print-color-adjust: exact", css)
        self.assertIn(".risk-cell.is-inherent", css)
        self.assertIn(".risk-cell.is-residual", css)
        for band in (".band.Low", ".band.Moderate", ".band.High", ".band.VeryHigh", ".band.Severe"):
            self.assertIn(band, css, band)
        self.assertNotIn("High 10–16", page)
        self.assertNotIn("Extreme 17–25", page)

    def test_script_builds_a_highlighted_five_by_five_and_seven_rows(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r"""
const matrix = require("./static/risk-matrix.js");
const cells = matrix.cells().flat();
if (cells.length !== 25) process.exit(2);
const seen = new Set(cells.map((cell) => cell.likelihood + "x" + cell.impact));
if (seen.size !== 25) process.exit(3);
function expect(score, name) {
  if (matrix.band(score).name !== name) process.exit(4);
}
expect(1, "Low");
expect(4, "Low");
expect(5, "Moderate");
expect(9, "Moderate");
expect(10, "High");
expect(14, "High");
expect(15, "Very High");
expect(19, "Very High");
expect(20, "Severe");
expect(25, "Severe");
if (matrix.LIKELIHOOD[0].label !== "Rare" || matrix.LIKELIHOOD[4].label !== "Almost Certain") process.exit(5);
if (matrix.IMPACT[0].label !== "Negligible" || matrix.IMPACT[4].label !== "Catastrophic") process.exit(6);
if (matrix.COLUMNS.length !== 6) process.exit(7);
["Threat Category", "Inherent Rating", "Country / Operational Context", "Mandatory Mitigation Controls", "Residual Rating", "Action Owner"].forEach((name) => {
  if (!matrix.COLUMNS.includes(name)) process.exit(8);
});
if (matrix.CATEGORIES.length !== 7) process.exit(9);
const rows = [
  { likelihood: 5, impact: 5, residualL: 2, residualI: 2 },
  { likelihood: 3, impact: 3, residualL: 4, residualI: 4 }
];
const html = matrix.renderGrid(rows);
if (!html.includes('data-likelihood="5" data-impact="5"') || !html.includes("is-inherent")) process.exit(10);
if (!html.includes('data-likelihood="4" data-impact="4"') || !html.includes("is-residual")) process.exit(11);
if (!html.includes("Almost Certain") || !html.includes("Catastrophic") || !html.includes("Rare")) process.exit(12);
if (!html.includes("Overall inherent Severe") || !html.includes("Overall residual Very High")) process.exit(13);
const built = matrix.rowsFromHazards([
  { category: "Conflict/War", threat: "Conflict/War", likelihood: 5, impact: 5, mitigation: "Stay off the front.", residual_likelihood: 3, residual_impact: 4 },
  { category: "Terrorism", threat: "Terrorism", likelihood: 3, impact: 5, mitigation: "Vary the route.", residual_likelihood: 2, residual_impact: 3 },
  { category: "Road/Airspace", threat: "Road/Airspace", likelihood: 4, impact: 4, mitigation: "Read the NOTAM.", residual_likelihood: 2, residual_impact: 2 }
], {
  country: "Syria",
  region: "Middle East",
  situation: "Unexploded-ordnance remains on former fighting ground.",
  incidents: [{ title: "Clash near Aleppo", category: "Conflict", area: "Aleppo" }]
});
if (built.length !== 7) process.exit(14);
if (built[0].threat !== "Conflict/Armed Clashes" || built[0].likelihood !== 5 || built[0].impact !== 5) process.exit(15);
if (!built[0].context.includes("Syria") || !built[0].context.includes("Aleppo")) process.exit(16);
if (built[0].owner !== "Security Advisor") process.exit(17);
if (!built[0].control.includes("stand-off") || !built[0].control.includes("+30")) process.exit(18);
const crime = built.find((row) => row.threat === "Crime/Road Safety");
if (!crime || crime.likelihood !== 4 || !crime.context.includes("NOTAM")) process.exit(19);
const environment = built.find((row) => row.threat === "Environmental/ERW");
if (!environment || environment.likelihood !== 3 || environment.impact !== 4) process.exit(20);
const owners = new Set(matrix.OWNERS);
["Security Advisor", "Team Leader", "Driver/Fixer", "Ops Desk"].forEach((name) => {
  if (!owners.has(name)) process.exit(21);
});
const text = JSON.stringify(matrix.CATEGORIES).toLowerCase();
["traveling", "organization", "defense", "prioritize", "center"].forEach((word) => {
  if (text.includes(word)) process.exit(22);
});
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
