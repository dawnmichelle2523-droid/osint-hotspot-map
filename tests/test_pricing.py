"""The public pricing ladder: five tiers, passes, add-ons, and GBP/USD toggles."""

import os
import re
import unittest

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

PRICE = re.compile(r"<[^>]*\sdata-price=\"([^\"]+)\"[^>]*>")
ATTRS = ("data-gbp-monthly", "data-gbp-annual", "data-usd-monthly", "data-usd-annual")

EXPECTED = {
    "individual": {"data-gbp-monthly": "£19", "data-gbp-annual": "£190", "data-usd-monthly": "$25", "data-usd-annual": "$249"},
    "professional": {"data-gbp-monthly": "£39", "data-gbp-annual": "£390", "data-usd-monthly": "$49", "data-usd-annual": "$490"},
    "teams-user": {"data-gbp-monthly": "£49", "data-gbp-annual": "£490", "data-usd-monthly": "$65", "data-usd-annual": "$650"},
    "teams-minimum": {
        "data-gbp-monthly": "Minimum 3 seats · £147/mo",
        "data-gbp-annual": "Minimum 3 seats · £1,470/yr",
        "data-usd-monthly": "Minimum 3 seats · $195/mo",
        "data-usd-annual": "Minimum 3 seats · $1,950/yr",
    },
    "teams-desk": {"data-gbp-monthly": "£199", "data-gbp-annual": "£1,990", "data-usd-monthly": "$259", "data-usd-annual": "$2,590"},
    "ops-room": {"data-gbp-monthly": "£599", "data-gbp-annual": "£5,990", "data-usd-monthly": "$785", "data-usd-annual": "$7,850"},
    "enterprise": {
        "data-gbp-monthly": "From £15,000",
        "data-gbp-annual": "From £15,000",
        "data-usd-monthly": "From $19,500",
        "data-usd-annual": "From $19,500",
    },
    "pass-7-country": {"data-gbp-monthly": "£7.99", "data-gbp-annual": "£7.99", "data-usd-monthly": "$9.99", "data-usd-annual": "$9.99"},
    "pass-7-region": {"data-gbp-monthly": "£14.99", "data-gbp-annual": "£14.99", "data-usd-monthly": "$19", "data-usd-annual": "$19"},
    "pass-30-country": {"data-gbp-monthly": "£19", "data-gbp-annual": "£19", "data-usd-monthly": "$25", "data-usd-annual": "$25"},
    "pass-30-region": {"data-gbp-monthly": "£39", "data-gbp-annual": "£39", "data-usd-monthly": "$49", "data-usd-annual": "$49"},
    "iso-pdf": {"data-gbp-monthly": "£29", "data-gbp-annual": "£29", "data-usd-monthly": "$39", "data-usd-annual": "$39"},
    "medical": {"data-gbp-monthly": "£49–£79", "data-gbp-annual": "£49–£79", "data-usd-monthly": "$65–$99", "data-usd-annual": "$65–$99"},
    "school-trip": {"data-gbp-monthly": "£99–£199", "data-gbp-annual": "£99–£199", "data-usd-monthly": "$129–$259", "data-usd-annual": "$129–$259"},
    "education-desk": {
        "data-gbp-monthly": "Education Desk £499–£990/yr",
        "data-gbp-annual": "Education Desk £499–£990/yr",
        "data-usd-monthly": "Education Desk $650–$1,300/yr",
        "data-usd-annual": "Education Desk $650–$1,300/yr",
    },
    "srm": {"data-gbp-monthly": "£400–£1,500", "data-gbp-annual": "£400–£1,500", "data-usd-monthly": "$525–$1,950", "data-usd-annual": "$525–$1,950"},
    "slack": {"data-gbp-monthly": "£29", "data-gbp-annual": "£290", "data-usd-monthly": "$39", "data-usd-annual": "$390"},
    "report-monthly": {"data-gbp-monthly": "£19", "data-gbp-annual": "£190", "data-usd-monthly": "$25", "data-usd-annual": "$250"},
    "report-weekly": {"data-gbp-monthly": "£39", "data-gbp-annual": "£390", "data-usd-monthly": "$49", "data-usd-annual": "$490"},
    "report-daily": {"data-gbp-monthly": "£79", "data-gbp-annual": "£790", "data-usd-monthly": "$99", "data-usd-annual": "$990"},
    "report-region": {"data-gbp-monthly": "£149", "data-gbp-annual": "£1,490", "data-usd-monthly": "$195", "data-usd-annual": "$1,950"},
    "white-label-build": {
        "data-gbp-monthly": "£10,000–£35,000",
        "data-gbp-annual": "£10,000–£35,000",
        "data-usd-monthly": "$13,000–$46,000",
        "data-usd-annual": "$13,000–$46,000",
    },
    "white-label-fee": {
        "data-gbp-monthly": "£1,500–£5,000",
        "data-gbp-annual": "£15,000–£50,000",
        "data-usd-monthly": "$1,950–$6,550",
        "data-usd-annual": "$19,500–$65,500",
    },
}


def _attr(tag: str, name: str) -> str:
    match = re.search(r"\s" + name + r'="([^"]*)"', tag)
    if not match:
        raise AssertionError(name + " missing from " + tag)
    return match.group(1)


class PricingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        for name in ("PREVIEW_PASSWORD", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
            os.environ.pop(name, None)
        import app as app_module

        cls.client = app_module.app.test_client()

    def setUp(self):
        for name in ("PREVIEW_PASSWORD", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
            os.environ.pop(name, None)

    def test_pricing_renders_the_value_ladder(self):
        response = self.client.get("/pricing")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)
        html = response.get_data(as_text=True)

        self.assertIn('aria-current="page"', html)
        self.assertIn("Book a Pilot / Subscribe", html)
        self.assertIn('href="/reports"', html)
        self.assertIn('href="/enterprise"', html)
        self.assertIn("Talk to us", html)
        self.assertIn("Request / Add to Plan", html)
        self.assertIn('id="gbp" data-currency="gbp"', html)
        self.assertIn('id="usd" data-currency="usd"', html)
        self.assertIn('id="monthly" data-billing="monthly"', html)
        self.assertIn('id="annual" data-billing="annual"', html)
        self.assertIn("Annual (Save 2 months — 10× monthly)", html)
        self.assertIn('id="pricing-root" data-currency="gbp" data-billing="monthly"', html)
        self.assertIn('data-role="recommended-anchor"', html)
        self.assertIn("Recommended · Most Popular", html)

        for tier in ("Individual", "Professional", "Teams &amp; Desks", "Ops Room", "Enterprise &amp; Government"):
            self.assertIn(tier, html)
        for sector in (
            "Governments",
            "Media &amp; Journalism",
            "Travel Risk &amp; TMCs",
            "Insurance &amp; Compliance",
            "NGOs &amp; Humanitarian",
            "Private Security &amp; Ops Rooms",
            "Schools &amp; Universities",
            "Private Individuals &amp; Expats",
            "C-Suite &amp; Family Offices",
            "Medical Risk &amp; Assistance",
        ):
            self.assertIn(sector, html)
        self.assertIn("30% off the Teams plan", html)
        self.assertIn("complements, rather than replaces, medical and security evacuation cover", html)
        self.assertIn("Watermarked. Locked to 1 country.", html)
        self.assertIn("Watermarked. Locked to 1 theatre.", html)
        self.assertIn("1 Live Slack Ops-Room channel included", html)

        found = {price_id: tag for price_id, tag in ((match.group(1), match.group(0)) for match in PRICE.finditer(html))}
        self.assertIn("individual-aside", found)
        for price_id, expected in EXPECTED.items():
            self.assertIn(price_id, found, price_id)
            tag = found[price_id]
            for name in ATTRS:
                self.assertEqual(_attr(tag, name), expected[name], price_id + " " + name)
                self.assertTrue(_attr(tag, name), price_id + " " + name)
            self.assertIn("data-suffix-monthly", tag)
            self.assertIn("data-suffix-annual", tag)


if __name__ == "__main__":
    unittest.main()
