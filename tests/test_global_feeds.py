import json
import unittest

import global_feeds as gf


class GlobalFeedParserTests(unittest.TestCase):
    def test_usgs_places_a_japan_quake_in_apac(self):
        payload = {
            "features": [{
                "id": "us1000",
                "geometry": {"type": "Point", "coordinates": [142.1, 38.2, 10]},
                "properties": {"mag": 6.2, "place": "20 km E of Sendai, Japan", "time": 1700000000000, "title": "M 6.2 - Sendai", "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us1000"},
            }]
        }
        feed = {"id": "usgs-quakes", "name": "USGS", "region": "Global", "category": "Seismic", "url": "https://example.test/usgs"}
        events = gf.parse_usgs(feed, json.dumps(payload).encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["theatre"], "APAC")
        self.assertEqual(events[0]["severity"], "high")
        self.assertAlmostEqual(events[0]["lat"], 38.2)

    def test_us_advisory_keeps_level_3_and_4_only(self):
        payload = [
            {"Title": "Syria Travel Advisory", "Category": ["Level 4: Do Not Travel"], "Summary": "Do not travel", "Link": "https://travel.state.gov/syria"},
            {"Title": "Spain Travel Advisory", "Category": ["Level 2: Exercise Increased Caution"], "Summary": "Increased caution", "Link": "https://travel.state.gov/spain"},
        ]
        feed = {"id": "us-advisories", "name": "US", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/us"}
        events = gf.parse_us_advisory(feed, json.dumps(payload).encode())
        self.assertEqual([item["country"] for item in events], ["Syria"])
        self.assertEqual(events[0]["severity"], "high")
        self.assertEqual(events[0]["theatre"], "Middle East")

    def test_canada_numeric_advisory_state(self):
        payload = {"data": {"AF": {"country-eng": "Afghanistan", "advisory-state": 3, "date-published": {"date": "2026-11-25"}}}}
        feed = {"id": "canada-advisories", "name": "Canada", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/ca"}
        events = gf.parse_canada_advisory(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["severity"], "high")
        self.assertEqual(events[0]["theatre"], "Afghanistan")

    def test_volcano_uses_known_coordinates_when_the_feed_has_no_lat(self):
        payload = [{"volcano_name": "Great Sitkin", "alert_level": "WATCH", "obs_abbr": "avo", "vnum": "311120"}]
        feed = {"id": "usgs-volcanoes", "name": "USGS Volcanoes", "region": "United States", "category": "Volcano", "url": "https://example.test/volcano"}
        events = gf.parse_volcano(feed, json.dumps(payload).encode())
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["lat"], 52.076, delta=0.8)
        self.assertEqual(events[0]["theatre"], "United States")

    def test_canada_avoid_all_travel(self):
        payload = {"data": [{"country-eng": "Afghanistan", "advisory-text": "Avoid all travel", "url": "https://travel.gc.ca/destinations/afghanistan"}]}
        feed = {"id": "canada-advisories", "name": "Canada", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/ca"}
        events = gf.parse_canada_advisory(feed, json.dumps(payload).encode())
        self.assertEqual(events[0]["theatre"], "Afghanistan")
        self.assertEqual(events[0]["severity"], "high")

    def test_uk_page_level_reads_alert_status(self):
        self.assertEqual(gf.uk_page_level(["avoid_all_travel_to_whole_country"]), 4)
        self.assertEqual(gf.uk_page_level(["avoid_all_but_essential_travel_to_parts"]), 2)
        self.assertEqual(gf.uk_page_level([]), 1)

    def test_keyed_source_stays_idle_without_a_secret(self):
        feed = {"id": "acled", "name": "ACLED", "region": "Global", "category": "Conflict", "parser": "keyed", "env": ("ACLED_API_KEY", "ACLED_EMAIL"), "needs_all": True}
        result = gf._keyed_status(feed)
        self.assertEqual(result["status"], "standby")
        self.assertEqual(result["events"], [])
        self.assertNotIn("ACLED", json.dumps(result["events"]))

    def test_one_bad_feed_does_not_drop_the_other(self):
        feeds = {
            "usgs-quakes": {"name": "USGS", "region": "Global", "category": "Seismic", "status": "active", "events": [
                {"id": "q1", "title": "Quake", "summary": "Quake", "lat": 10, "lng": 20, "country": "Japan", "region": "APAC", "theatre": "APAC", "feed_category": "Seismic", "severity": "high", "timestamp": "2026-01-01T00:00:00Z", "source": "https://example.test/q", "source_name": "USGS"}
            ]},
            "deepstate": {"name": "DeepState", "region": "Russia & Ukraine", "category": "Conflict", "status": "offline", "error": "rate limited", "events": []},
        }
        with gf._lock:
            gf._memory["feeds"] = feeds
            gf._memory["at"] = gf.time.time()
        rows = gf.features()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["properties"]["theatre"], "APAC")
        status = {item["source_name"]: item["status"] for item in gf.status_rows()}
        self.assertEqual(status["DeepStateMap Snapshot"], "offline")
        self.assertIn(status["USGS Earthquakes M4.5+"], {"active", "unchecked"})

    def test_7sur7_stays_disabled_because_it_has_no_rss(self):
        feed = next(item for item in gf._feeds() if item["id"] == "7sur7")
        self.assertTrue(feed["disabled"])
        self.assertEqual(feed["reason"], "no RSS available")
        self.assertEqual(feed["source_tier"], "National outlet")
        self.assertEqual(feed["language"], "French")
        self.assertEqual(feed["default_country"], "Democratic Republic of the Congo")
        self.assertEqual(feed["parser"], "")
        result = gf._fetch_feed(feed, [{"id": "old"}])
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["error"], "no RSS available")
        self.assertEqual(result["events"], [])
        status = {item["source_name"]: item for item in gf.status_rows()}
        self.assertEqual(status["7sur7.cd"]["status"], "disabled")
        self.assertEqual(status["7sur7.cd"]["last_error"], "no RSS available")

    def test_ashara_keeps_place_and_drops_personal_names(self):
        feed = next(item for item in gf._feeds() if item["id"] == "tg-ashara")
        self.assertEqual(feed["language"], "am")
        self.assertEqual(feed["source_tier"], "partisan (pro-Fano) / Social-unverified")
        self.assertTrue(feed["low_yield"])
        self.assertIn("Amharic", feed["yield_note"])
        self.assertNotIn("ENABLE_X", feed["url"])
        page = """
        <div class="tgme_widget_message" data-post="ashara_media/42">
          <div class="tgme_widget_message_text">Defectors: Abebe Bekele, Chaltu Tesfaye. Fighting in Amhara.</div>
          <time datetime="2026-11-27T12:00:00+00:00"></time>
        </div>
        <div class="tgme_widget_message" data-post="ashara_media/43">
          <div class="tgme_widget_message_text">አበበ በቀለ እና ጫልቱ ተስፋዬ ከአፋር</div>
          <time datetime="2026-11-27T13:00:00+00:00"></time>
        </div>
        """
        events = gf.parse_ashara(feed, page.encode())
        self.assertEqual(len(events), 2)
        blob = json.dumps(events)
        self.assertNotIn("Abebe", blob)
        self.assertNotIn("Bekele", blob)
        self.assertNotIn("Chaltu", blob)
        self.assertNotIn("Tesfaye", blob)
        self.assertNotIn("አበበ", blob)
        self.assertNotIn("በቀለ", blob)
        self.assertEqual(events[0]["location_name"], "Amhara")
        self.assertEqual(events[0]["country"], "Ethiopia")
        self.assertEqual(events[0]["source_tier"], "partisan (pro-Fano) / Social-unverified")
        self.assertEqual(events[0]["original_text"] if "original_text" in events[0] else "", "")
        self.assertIn("Amhara", events[0]["summary"])
        self.assertEqual(events[1]["location_name"], "Afar")
        self.assertNotIn("አበበ", events[1]["summary"])

    def test_ahram_is_kept_for_a_quiet_block_and_imb_is_public(self):
        feeds = {item["id"]: item for item in gf._feeds()}
        ahram = feeds["ahram"]
        self.assertEqual(ahram["source_tier"], "State Media")
        self.assertEqual(ahram["default_country"], "Egypt")
        self.assertIn(403, ahram["quiet_http"])
        self.assertIn("Rss.aspx", ahram["url"])
        imb = feeds["imb-prc"]
        self.assertEqual(imb["url"], "https://icc-ccs.org/feed/")
        self.assertEqual(imb["source_tier"], "Official")
        rss = """<?xml version="1.0"?><rss><channel>
          <item><title>Eight sailors kidnapped off Somalia</title>
          <description>Pirates seized the crew in the Gulf of Aden.</description>
          <link>https://icc-ccs.org/example</link></item>
        </channel></rss>"""
        events = gf.parse_security_rss(imb, rss.encode())
        self.assertEqual(len(events), 1)
        self.assertIn("Somalia", events[0]["title"])
        self.assertEqual(events[0]["source_tier"], "Official")
        arab = [item for item in gf._feeds() if item["id"].startswith("alarabiya-")]
        self.assertEqual(len(arab), 2)
        self.assertTrue(all(403 in item["quiet_http"] for item in arab))
        self.assertTrue(all(item["source_tier"] == "Regional broadcaster / Saudi state-aligned" for item in arab))

    def test_baidoa_sitemap_keeps_recent_security_urls_only(self):
        feed = next(item for item in gf._feeds() if item["id"] == "baidoa-sitemap")
        self.assertEqual(feed["source_tier"], "Local outlet")
        self.assertEqual(feed["default_country"], "Somalia")
        recent = "2026-11-27T08:00:00.000Z"
        old = "2026-06-27T09:25:46.674Z"
        xml = f"""<?xml version="1.0"?><urlset>
          <url><loc>https://www.baidoaonline.com/about</loc><lastmod>{recent}</lastmod></url>
          <url><loc>https://www.baidoaonline.com/news/clan-fighting-erupts-in-galmudug</loc><lastmod>{recent}</lastmod></url>
          <url><loc>https://www.baidoaonline.com/news/armed-fighters-attack-a-base</loc><lastmod>{old}</lastmod></url>
        </urlset>"""
        events = gf.parse_sitemap_news(feed, xml.encode())
        self.assertEqual(len(events), 1)
        self.assertIn("Galmudug", events[0]["title"])
        self.assertEqual(events[0]["country"], "Somalia")
        self.assertEqual(events[0]["source_tier"], "Local outlet")

    def test_nasha_niva_sections_are_rss_and_tagged_belarus(self):
        feeds = {item["id"]: item for item in gf._feeds()}
        for section_id in ("611", "584", "615", "706"):
            feed = feeds["nashaniva-" + section_id]
            self.assertEqual(feed["source_tier"], "National outlet")
            self.assertEqual(feed["default_country"], "Belarus")
            self.assertEqual(feed["language"], "be")
            self.assertTrue(feed["url"].endswith("/rss-ca/" + section_id + "/"))
        empty = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
          <title>Nasha Niva</title><link>https://nashaniva.com/</link><language>be</language>
        </channel></rss>"""
        self.assertEqual(gf.parse_placed_rss(feeds["nashaniva-706"], empty), [])
        item = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
          <item><title>Explosion in Minsk</title><description>A blast was reported.</description>
          <link>https://nashaniva.com/example</link></item>
        </channel></rss>"""
        events = gf.parse_placed_rss(feeds["nashaniva-611"], item)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["country"], "Belarus")
        self.assertEqual(events[0]["source_tier"], "National outlet")

    def test_meduza_keeps_both_languages_once(self):
        meduza = [item for item in gf._feeds() if str(item.get("url") or "").startswith("https://meduza.io/rss/")]
        urls = sorted(item["url"] for item in meduza)
        self.assertEqual(urls, ["https://meduza.io/rss/all", "https://meduza.io/rss/en/all"])
        self.assertTrue(all(item["source_tier"] == "National outlet" for item in meduza))
        self.assertTrue(all(item["region"] == "Russia, Central Asia and the Caucasus" for item in meduza))
        self.assertEqual(len(meduza), 2)

    def test_air_alerts_keep_one_explosion_per_oblast(self):
        feeds = {item["id"]: item for item in gf._feeds()}
        self.assertNotEqual(feeds["tg-air-alert-ua"]["channel"], feeds["tg-povitryana"]["channel"])
        self.assertEqual(feeds["tg-povitryana"]["source_tier"], "Ukraine air-alert aggregator / Social-unverified")
        page = """
        <div class="tgme_widget_message" data-post="povitryanatrivogaaa/1">
          <div class="tgme_widget_message_text">✈️ Харківщина: →Лозова/р-н.</div>
          <time datetime="2026-11-27T23:40:00+00:00"></time>
        </div>
        <div class="tgme_widget_message" data-post="povitryanatrivogaaa/2">
          <div class="tgme_widget_message_text">💥 Лозова (Харківщина) - вибухи</div>
          <time datetime="2026-11-27T23:43:00+00:00"></time>
        </div>
        <div class="tgme_widget_message" data-post="povitryanatrivogaaa/3">
          <div class="tgme_widget_message_text">💥 Харків - вибухи</div>
          <time datetime="2026-11-27T23:44:00+00:00"></time>
        </div>
        """
        events = gf.parse_ukraine_air(feeds["tg-povitryana"], page.encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["location_name"], "Kharkiv oblast")
        self.assertNotIn("Лозова", events[0]["summary"])
        other = page.replace("povitryanatrivogaaa", "air_alert_ua")
        again = gf.parse_ukraine_air(feeds["tg-air-alert-ua"], other.encode())
        self.assertEqual(again[0]["id"], events[0]["id"])

    def test_efremov_sources_and_operative_english_feed(self):
        feeds = {item["id"]: item for item in gf._feeds()}
        exile = feeds["tg-exilenova-plus"]
        amk = feeds["tg-amk-mapping"]
        self.assertEqual(exile["url"], "https://t.me/s/exilenova_plus")
        self.assertEqual(exile["source_tier"], "Social-unverified / partisan (pro-UA)")
        self.assertEqual(exile["language"], "uk")
        self.assertEqual(amk["url"], "https://t.me/s/AMK_Mapping")
        self.assertEqual(amk["source_tier"], "Social-unverified / partisan (pro-UA)")
        operative = feeds["operative-en"]
        self.assertEqual(operative["url"], "https://operativmm.az/en/rss")
        self.assertEqual(operative["source_tier"], "National outlet")
        self.assertEqual(operative["default_country"], "Azerbaijan")
        self.assertIn("Caucasus", operative["region"])
        page = """
        <div class="tgme_widget_message" data-post="exilenova_plus/35393">
          <div class="tgme_widget_message_text">Drone strike on the Efremov synthetic rubber plant in Tula Oblast.</div>
          <time datetime="2026-11-26T23:18:00+00:00"></time>
        </div>
        """
        events = gf.parse_telegram_preview(exile, page.encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["country"], "Russia")
        self.assertEqual(events[0]["location_name"], "Efremov")
        self.assertEqual(events[0]["source_tier"], "Social-unverified / partisan (pro-UA)")

    def test_turkey_ukraine_israel_and_lebanon_catalogue(self):
        feeds = {item["id"]: item for item in gf._feeds()}
        self.assertEqual(feeds["wire-anadolu"]["url"], "https://www.aa.com.tr/en/rss/default?cat=guncel")
        self.assertEqual(feeds["wire-anadolu-tr"]["url"], "https://www.aa.com.tr/tr/rss/default?cat=guncel")
        self.assertEqual(feeds["bbc-turkce"]["source_tier"], "Established national")
        self.assertEqual(feeds["bianet-tr"]["language"], "tr")
        self.assertEqual(feeds["bianet-en"]["language"], "en")
        self.assertIn(403, feeds["haberdairesi"]["quiet_http"])
        self.assertEqual(feeds["haberdairesi"]["source_tier"], "Local outlet")
        self.assertNotIn("dha", feeds)
        self.assertNotIn("iha", feeds)
        self.assertTrue(feeds["korrespondent-all"]["url"].endswith("/all_news2.0.xml"))
        self.assertEqual(feeds["korrespondent-kyiv"]["source_tier"], "National outlet")
        self.assertNotIn("korrespondent-zaporozhye", feeds)
        self.assertNotIn("inform-zp-ru", feeds)
        inform = feeds["inform-zp"]
        self.assertEqual(inform["url"], "https://www.inform.zp.ua/feed/")
        self.assertEqual(inform["source_tier"], "Local outlet")
        self.assertTrue(inform["tag_transport"])
        rss = """<?xml version="1.0"?><rss><channel><item>
          <title>У Запоріжжі перекрили дорогу біля ДніпроГЕС</title>
          <description>The DniproHES dam road is closed.</description>
          <link>https://www.inform.zp.ua/example</link></item></channel></rss>"""
        events = gf.parse_security_rss(inform, rss.encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["feed_category"], "Travel / transport disruption")
        self.assertEqual(events[0]["country"], "Ukraine")
        self.assertEqual(feeds["ywn"]["url"], "https://www.theyeshivaworld.com/feed")
        self.assertEqual(feeds["ywn"]["region"], "Israel and the United States")
        self.assertTrue(feeds["transparency-news"]["disabled"])
        self.assertEqual(feeds["transparency-news"]["reason"], "no RSS available")
        self.assertNotIn("nna-leb", feeds)
        self.assertFalse(any("lorient" in item["id"] for item in gf._feeds()))
        airport = """<?xml version="1.0"?><rss><channel><item>
          <title>Protest at Ben Gurion Airport</title>
          <description>Haredi protesters blocked a road.</description>
          <link>https://www.theyeshivaworld.com/example</link></item></channel></rss>"""
        pinned = gf.parse_security_rss(feeds["ywn"], airport.encode())
        self.assertEqual(pinned[0]["location_name"], "Ben Gurion Airport")
        self.assertEqual(pinned[0]["country"], "Israel")

    def test_daily_qudrat_keeps_protest_and_drops_lifestyle(self):
        feed = next(item for item in gf._feeds() if item["id"] == "daily-qudrat")
        self.assertEqual(feed["url"], "https://dailyqudrat.pk/feed/")
        self.assertEqual(feed["language"], "ur")
        self.assertEqual(feed["source_tier"], "National/regional outlet")
        rss = """<?xml version="1.0"?><rss><channel>
          <item><title>سرگودھا میں احتجاج</title><description>سڑک بند</description>
          <link>https://dailyqudrat.pk/protest</link></item>
          <item><title>فلمی تقریب</title><description>مشہور اداکارہ</description>
          <link>https://dailyqudrat.pk/film</link></item>
        </channel></rss>"""
        events = gf.parse_security_rss(feed, rss.encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["country"], "Pakistan")
        self.assertNotIn("اداکارہ", events[0]["title"])


if __name__ == "__main__":
    unittest.main()
