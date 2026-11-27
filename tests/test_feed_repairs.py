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
