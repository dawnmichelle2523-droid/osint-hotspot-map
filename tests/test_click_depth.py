"""Every key public destination is at most three clicks from the homepage."""

import os
import re
import unittest

os.environ["DISABLE_BACKGROUND"] = "1"
os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
for _name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
    os.environ.pop(_name, None)

import app as app_module

HREF = re.compile(r"<a\b[^>]*\bhref=\"([^\"]+)\"", re.I)
MAX_CLICKS = 3

# Route, and the flat path a reader should be able to follow.
KEY_ROUTES = {
    "/map": "Home → Risk map tile",
    "/map?country=Syria": "Home → Syria on the Risk Map",
    "/briefing": "Home → Daily Briefings",
    "/briefing#export-pdf": "Home → Briefing PDF",
    "/pricing": "Home → Pricing",
    "/risk-assessments": "Home → Country risk tile",
    "/methodology": "Home → Methodology",
    "/contact": "Home → Contact",
}

SHELL_PAGES = (
    "/",
    "/map",
    "/briefing",
    "/pricing",
    "/methodology",
    "/contact",
    "/risk-assessments",
    "/world-risk",
    "/countries",
    "/enterprise",
)


def _normalise(href: str, page: str) -> str:
    if href.startswith(("mailto:", "tel:", "javascript:")):
        return href
    if href.startswith("#"):
        return page.split("#", 1)[0].split("?", 1)[0] + href
    if href.startswith("http://") or href.startswith("https://"):
        return ""
    path, _, fragment = href.partition("#")
    if not path.startswith("/"):
        return ""
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path + (("#" + fragment) if fragment else "")


class ClickDepthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.testing = True
        cls.client = app_module.app.test_client()
        cls.depth, cls.pages = cls._crawl()

    @classmethod
    def _crawl(cls):
        depth = {"/": 0}
        pages = {}
        frontier = ["/"]
        while frontier:
            page = frontier.pop(0)
            here = depth[page]
            if here >= MAX_CLICKS:
                continue
            target = page.split("#", 1)[0]
            if target in pages:
                html = pages[target]
            else:
                if target.startswith(("/api", "/static", "/embed", "/auth", "/login")):
                    continue
                response = cls.client.get(target)
                if response.status_code != 200 or "html" not in (response.content_type or ""):
                    response.close()
                    continue
                html = response.get_data(as_text=True)
                response.close()
                pages[target] = html
            for raw in HREF.findall(html):
                href = _normalise(raw, target)
                if not href or href in depth:
                    continue
                depth[href] = here + 1
                if href.split("#", 1)[0].startswith("/") and here + 1 < MAX_CLICKS:
                    frontier.append(href)
        return depth, pages

    def test_key_routes_are_within_three_clicks(self):
        for route, path in KEY_ROUTES.items():
            self.assertIn(route, self.depth, path)
            self.assertLessEqual(self.depth[route], MAX_CLICKS, f"{path} took {self.depth.get(route)} clicks")
        enquiry = [item for item in self.depth if item.startswith("mailto:contact@georiskglobal.com")]
        self.assertTrue(enquiry, "risk assessment enquiry")
        self.assertLessEqual(min(self.depth[item] for item in enquiry), MAX_CLICKS)

    def test_briefing_page_holds_the_country_and_the_pdf(self):
        html = self.pages["/briefing"]
        self.assertIn('id="ao-country"', html)
        self.assertIn('value="syria"', html)
        self.assertIn('id="export-pdf"', html)
        self.assertLessEqual(self.depth["/briefing"] + 1, MAX_CLICKS)

    def test_every_shell_page_links_home_in_one_click(self):
        for path in SHELL_PAGES:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            page.close()
            self.assertIn('class="brand-lockup" href="/"', html, path)
            self.assertNotIn('class="nav-more"', html, path)
            self.assertIn("Powered by GeoRiskGlobal", html, path)

    def test_homepage_tiles_are_the_first_click(self):
        home = self.pages["/"]
        tiles = home.split('id="browse-tiles"', 1)[1].split('class="country-jump"', 1)[0]
        for route in ("/map", "/briefing", "/risk-assessments", "/pricing", "/methodology", "/contact"):
            self.assertIn(f'href="{route}"', tiles, route)
            self.assertEqual(self.depth[route], 1, route)


if __name__ == "__main__":
    unittest.main()
