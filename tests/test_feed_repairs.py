"""Repairs from the 27 Nov 2026 source check, including the off-by-default X gate."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

import fetch_x_osint as ox
import serve_map


class XGateTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ENABLE_X_FEEDS", None)
        os.environ.pop("RELIEFWEB_APPNAME", None)

    def test_x_fetch_makes_no_call_unless_enabled(self):
        os.environ.pop("ENABLE_X_FEEDS", None)
        self.assertFalse(ox.x_feeds_enabled())
        with patch.object(ox, "exit_if_no_key", side_effect=AssertionError("paid API")):
            ox.main()
        with patch("serve_map.requests.get", side_effect=AssertionError("mirror")):
            self.assertEqual(serve_map.fetch_x_handle("Alhadath_Brk"), [])
            self.assertEqual(serve_map.fetch_x_handle("AmharaWarUpdate"), [])
            self.assertEqual(serve_map.fetch_x_handle("AfriMEOSINT"), [])
        self.assertEqual(serve_map.X_FEED_URLS, [])
        self.assertTrue(serve_map.DISABLED_X_MIRRORS)

    def test_new_x_accounts_are_listed_and_tagged(self):
        handles = {item.lower() for item in serve_map.X_HANDLES}
        for handle in ("alhadath_brk", "amharawarupdate", "aaa_amhara", "afrimeosint"):
            self.assertIn(handle, handles)
        self.assertEqual(
            serve_map.source_tier_for("x", "Alhadath_Brk", ""),
            "Established national / breaking news",
        )
        self.assertEqual(
            serve_map.source_tier_for("x", "AAA_Amhara", "https://x.com/AAA_Amhara/status/1"),
            "Social-unverified / partisan",
        )
        self.assertEqual(
            serve_map.source_tier_for("x", "AfriMEOSINT", ""),
            "OSINT analyst / Social-unverified",
        )
        self.assertEqual(
            serve_map.source_tier_for("x", "AlArabiya", ""),
            "Regional broadcaster / Saudi state-aligned",
        )
        self.assertEqual(
            serve_map.source_tier_for("x", "AlArabiya_Eng", "https://english.alarabiya.net/feed/rss2/en.xml"),
            "Regional broadcaster / Saudi state-aligned",
        )
        self.assertEqual(
            serve_map.source_tier_for("x", "Alhadath_Brk", ""),
            "Established national / breaking news",
        )
        self.assertIn("alarabiya", handles)
        self.assertIn("alarabiya_eng", handles)
        self.assertIn("baidoaonline", handles)
        self.assertEqual(serve_map.source_tier_for("x", "BaidoaOnline", ""), "Local outlet")
        self.assertTrue(serve_map.keep_x_post("BaidoaOnline", "Hirshabelle election commission meets in Mogadishu"))
        self.assertFalse(serve_map.keep_x_post("BaidoaOnline", "A gallery opens in Paris"))
        self.assertIn("nhmpofficial", handles)
        self.assertEqual(serve_map.source_tier_for("x", "NHMPofficial", ""), "Official Source")
        self.assertTrue(serve_map.keep_x_post("NHMPofficial", "M-2 بند ہے"))
        self.assertIn("exilenova_plus", handles)
        self.assertIn("amk_mapping_", handles)
        self.assertEqual(
            serve_map.source_tier_for("x", "Exilenova_plus", ""),
            "Social-unverified / partisan (pro-UA)",
        )
        self.assertEqual(
            serve_map.source_tier_for("telegram", "AMK_Mapping", "https://t.me/s/AMK_Mapping"),
            "Social-unverified / partisan (pro-UA)",
        )
        self.assertTrue(serve_map.keep_x_post("Exilenova_plus", "Drone strike on the Efremov plant in Tula"))
        self.assertTrue(serve_map.keep_x_post("AMK_Mapping_", "Explosion reported in Kharkiv"))
        self.assertFalse(serve_map.keep_x_post("Exilenova_plus", "A gallery opens in Paris"))
        self.assertNotIn("theosintbr1", handles)
        self.assertNotIn("worldsource24", handles)
        self.assertEqual(ox.assign_tier("Exilenova_plus", "", False), "Social-unverified / partisan (pro-UA)")
        self.assertEqual(ox.assign_tier("AMK_Mapping_", "", False), "Social-unverified / partisan (pro-UA)")
        self.assertIn("ywnreporter", handles)
        self.assertEqual(ox.OUTLET_LABELS["ywnreporter"], "YWNReporter")
        self.assertEqual(serve_map.source_tier_for("x", "YWNReporter", ""), "Journalist / Social-unverified")
        self.assertTrue(serve_map.keep_x_post("YWNReporter", "Haredi anti-draft protest blocks a highway at Ben Gurion"))
        self.assertTrue(serve_map.keep_x_post("YWNReporter", "Protest in Brooklyn"))
        self.assertFalse(serve_map.keep_x_post("YWNReporter", "A gallery opens in Paris"))
        self.assertIn("rudaw_arabic", handles)
        self.assertEqual(
            serve_map.source_tier_for("x", "rudaw_arabic", ""),
            "Regional outlet / Kurdish-aligned",
        )
        self.assertTrue(serve_map.keep_x_post("rudaw_arabic", "Iraqi border forces closed the al-Walid crossing with Syria"))
        self.assertFalse(serve_map.keep_x_post("rudaw_arabic", "A gallery opens in Paris"))
        rudaw = next(row for row in serve_map.DISABLED_RSS if row["name"] == "Rudaw English")
        self.assertIn("HTML", rudaw["reason"])
        self.assertIn("dailyqudrat", handles)
        self.assertIn("sargodhapolice", handles)
        self.assertNotIn("officialshehr", handles)
        self.assertNotIn("sherazi_silmian", handles)
        self.assertEqual(serve_map.source_tier_for("x", "DailyQudrat", ""), "National/regional outlet")
        self.assertEqual(serve_map.source_tier_for("x", "sargodhapolice", ""), "Official Source")
        self.assertEqual(ox.assign_tier("sargodhapolice", "", False), "Official Source")
        self.assertTrue(serve_map.keep_x_post("DailyQudrat", "احتجاج کے باعث سڑک بند"))
        self.assertFalse(serve_map.keep_x_post("DailyQudrat", "A gallery opens in Paris"))
        placed = {}
        lat, _lng, precision = serve_map.placed_coordinates(
            {
                "source_name": "sargodhapolice",
                "source_kind": "x",
                "title": "Containers closed the Sargodha–Mandi Bahauddin road at Dhori",
                "summary": "",
                "original_text": "",
            },
            placed,
        )
        self.assertEqual(precision, "Regional / Country")
        self.assertIn("Dhori", placed["location_name"])
        self.assertAlmostEqual(lat, 32.33, places=2)
        self.assertIn("thekenyatimes", handles)
        self.assertEqual(serve_map.source_tier_for("x", "thekenyatimes", ""), "Secondary national outlet")
        self.assertEqual(
            serve_map.source_tier_for("rss", "The Standard", "https://www.standardmedia.co.ke/rss/kenya.php"),
            "National outlet",
        )
        self.assertTrue(serve_map.keep_x_post("thekenyatimes", "Protest in Nairobi"))
        self.assertFalse(serve_map.keep_x_post("thekenyatimes", "A gallery opens in Paris"))

    def test_pakistan_motorways_stay_regional(self):
        cases = {
            "M-1 closed for fog": ("M-1 Islamabad–Peshawar", 33.85),
            "Protest on M-2": ("M-2 Lahore–Islamabad", 32.60),
            "M-4 security incident": ("M-4 Faisalabad–Multan", 30.85),
            "M-11 closure": ("M-11 Lahore–Sialkot", 32.05),
        }
        for title, (label, lat_expected) in cases.items():
            placed = {}
            lat, _lng, precision = serve_map.placed_coordinates(
                {"source_name": "NHMPofficial", "source_kind": "x", "title": title, "summary": "", "original_text": ""},
                placed,
            )
            self.assertEqual(precision, "Regional / Country", title)
            self.assertEqual(placed["location_name"], label)
            self.assertEqual(placed["coord_origin"], "region")
            self.assertAlmostEqual(lat, lat_expected, places=2)
        eleven = {}
        serve_map.placed_coordinates(
            {"source_name": "NHMPofficial", "source_kind": "x", "title": "M-11", "summary": "", "original_text": ""},
            eleven,
        )
        self.assertNotIn("Peshawar", eleven["location_name"])
        self.assertTrue(serve_map.keep_x_post("AfriMEOSINT", "GPS jamming reported over Ethiopia"))
        self.assertTrue(serve_map.keep_x_post("AmharaWarUpdate", "Clashes in Amhara"))
        self.assertFalse(serve_map.keep_x_post("AmharaWarUpdate", "Market prices in Paris"))

    def test_reliefweb_v2_is_skipped_without_an_appname(self):
        os.environ.pop("RELIEFWEB_APPNAME", None)
        serve_map._reliefweb_noted = False
        self.assertEqual(serve_map.reliefweb_reports_url(), "")
        urls = " ".join(url for _name, url in serve_map.active_rss_feeds())
        self.assertNotIn("api.reliefweb.int/v1", urls)
        os.environ["RELIEFWEB_APPNAME"] = "georisk-test"
        url = serve_map.reliefweb_reports_url()
        self.assertIn("https://api.reliefweb.int/v2/reports", url)
        self.assertIn("appname=georisk-test", url)
        self.assertNotIn("/v1/", url)

    def test_dead_feeds_are_disabled_with_reasons(self):
        active = {url for _name, url in serve_map.RSS_FEEDS}
        for row in serve_map.DISABLED_RSS:
            self.assertTrue(row["reason"])
            self.assertNotIn(row["url"], active)
        disabled_channels = {row["channel"].lower() for row in serve_map.TELEGRAM_DISABLED}
        self.assertEqual(len(serve_map.TELEGRAM_DISABLED), 27)
        self.assertTrue(disabled_channels.isdisjoint({item.lower() for item in serve_map.TELEGRAM_CHANNELS}))
        self.assertEqual(len(serve_map.TELEGRAM_STALE), 11)


if __name__ == "__main__":
    unittest.main()
