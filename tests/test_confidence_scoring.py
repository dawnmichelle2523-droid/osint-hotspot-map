"""Confidence tiers for official advisories, corroborated clusters, and single sources."""

import json
import os
import unittest

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")

import confidence_scoring as scoring


STAMP = "2026-11-26T12:00:00Z"
LATER = "2026-11-26T18:00:00Z"


def incident(**kwargs):
    row = {
        "title": "Oil terminal drone strike",
        "summary": "A drone strike hit the oil terminal",
        "country": "Yemen",
        "category": "Conflict",
        "timestamp": STAMP,
        "lat": 15.35,
        "lng": 44.20,
        "source_name": "Reuters",
        "source": "https://example.test/reuters",
    }
    row.update(kwargs)
    return row


class OfficialSourceTests(unittest.TestCase):
    def test_named_authorities_are_official(self):
        samples = [
            ("USGS Earthquakes M4.5+", "https://earthquake.usgs.gov/earthquakes/eventpage/us1", "USGS"),
            ("NASA EONET Wildfires", "https://eonet.gsfc.nasa.gov/api/v3/events", "NASA EONET"),
            ("GDACS 24h Disasters", "https://www.gdacs.org/xml/rss_24h.xml", "GDACS"),
            ("NIFC Current Wildfires", "https://services3.arcgis.com/nifc", "NIFC"),
            ("InciWeb Wildfires", "https://inciweb.wildfire.gov/incidents/rss.xml", "InciWeb"),
            ("NTWC Pacific Tsunami Atom", "https://www.tsunami.gov/ntwc", "NTWC"),
            ("PTWC Pacific Tsunami Atom", "https://www.tsunami.gov/ptwc", "PTWC"),
            ("NWS Tsunami Warnings", "https://api.weather.gov/alerts", "NWS"),
            ("JTWC Tropical Cyclones", "https://www.metoc.navy.mil/jtwc", "JTWC"),
            ("MeteoAlarm France", "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-france", "MeteoAlarm"),
            ("UK Flood Warnings", "https://environment.data.gov.uk/flood-monitoring/id/floods", "UK Environment Agency"),
            ("ReliefWeb", "https://reliefweb.int/updates/rss.xml", "ReliefWeb"),
            ("UN OCHA", "https://www.unocha.org/advisory", "OCHA"),
            ("International Crisis Group", "https://www.crisisgroup.org/feed", "Crisis Group"),
            ("UKMTO", "https://www.ukmto.org/advisory", "UKMTO"),
        ]
        for name, url, label in samples:
            with self.subTest(name=name):
                scored = scoring.score_incidents([incident(source_name=name, source=url, title="Advisory", summary="Advisory")])
                self.assertEqual(scored[0]["confidence_tier"], "official")
                self.assertEqual(scored[0]["confidence_label"], "Official Advisory")
                self.assertEqual(scored[0]["confidence_score"], 92)
                self.assertEqual(scored[0]["corroboration_count"], 1)
                self.assertEqual(scored[0]["corroborating_sources"], [label])

    def test_feed_ids_and_exact_official_tier(self):
        quake = incident(source_name="Quake feed", feed_id="usgs-quakes", source="https://example.test/q")
        floods = incident(source_name="Floods", feed_id="uk-floods", source="https://example.test/f")
        desk = incident(source_name="Ministry of Foreign Affairs", source_tier="Official", source="https://example.test/mfa")
        for row, label in ((quake, "USGS"), (floods, "UK Environment Agency"), (desk, "Ministry of Foreign Affairs")):
            scoring.score_incidents([row])
            self.assertEqual(row["confidence_tier"], "official", label)
            self.assertIn(label, row["corroborating_sources"])

    def test_wires_and_blanket_primary_tier_stay_unverified(self):
        for name in ("BBC World", "Al Jazeera", "Reuters", "DeepStateMap"):
            row = incident(
                source_name=name,
                source_tier="Official / Primary",
                title="Market clash in Aden",
                summary="Witnesses reported a market clash",
            )
            scoring.score_incidents([row])
            self.assertEqual(row["confidence_tier"], "unverified", name)
            self.assertEqual(row["confidence_label"], "Single-Source Unverified")
            self.assertEqual(row["confidence_score"], 36)
            self.assertEqual(row["corroboration_count"], 1)


class CorroborationTests(unittest.TestCase):
    def test_two_sources_within_75km_and_24h_are_corroborated(self):
        reuters = incident(source_name="Reuters", lat=15.35, lng=44.20, timestamp=STAMP)
        afp = incident(source_name="AFP", lat=15.55, lng=44.30, timestamp=LATER, source="https://example.test/afp")
        scoring.score_incidents([reuters, afp])
        self.assertEqual(reuters["confidence_tier"], "corroborated")
        self.assertEqual(afp["confidence_label"], "Multi-Source Corroborated")
        self.assertEqual(reuters["corroboration_count"], 2)
        self.assertEqual(reuters["confidence_score"], 62)
        self.assertEqual(reuters["corroborating_sources"], ["AFP", "Reuters"])
        self.assertEqual(afp["corroborating_sources"], ["AFP", "Reuters"])

    def test_exact_24h_matches_and_the_next_minute_does_not(self):
        left = incident(source_name="Reuters", timestamp=STAMP)
        on_time = incident(source_name="AFP", timestamp="2026-11-27T12:00:00Z", source="https://example.test/afp")
        scoring.score_incidents([left, on_time])
        self.assertEqual(on_time["confidence_tier"], "corroborated")
        late = incident(source_name="AP", timestamp="2026-11-27T12:01:00Z", source="https://example.test/ap")
        scoring.score_incidents([incident(source_name="Reuters", timestamp=STAMP), late])
        self.assertEqual(late["confidence_tier"], "unverified")

    def test_coordinates_farther_than_75km_do_not_cluster(self):
        north = incident(source_name="Reuters", lat=15.35, lng=44.20)
        south = incident(source_name="AFP", lat=16.60, lng=44.20, source="https://example.test/afp")
        scoring.score_incidents([north, south])
        self.assertEqual(north["confidence_tier"], "unverified")
        self.assertEqual(south["corroboration_count"], 1)
        self.assertEqual(south["corroborating_sources"], ["AFP"])

    def test_same_country_without_coordinates_can_corroborate(self):
        left = incident(source_name="Reuters", lat=None, lng=None)
        right = incident(source_name="AFP", lat=None, lng=None, source="https://example.test/afp")
        scoring.score_incidents([left, right])
        self.assertEqual(left["confidence_tier"], "corroborated")
        self.assertEqual(left["corroborating_sources"], ["AFP", "Reuters"])

    def test_same_source_is_not_multi_source(self):
        first = incident(source_name="Reuters", source="https://example.test/a")
        second = incident(source_name="Reuters", source="https://example.test/b", timestamp=LATER)
        scoring.score_incidents([first, second])
        self.assertEqual(first["confidence_tier"], "unverified")
        self.assertEqual(second["corroboration_count"], 1)

    def test_category_overlap_clusters_without_shared_words(self):
        usgs_wire = incident(
            source_name="Local desk",
            title="Shaking reported",
            summary="Residents felt a long tremor",
            category="Seismic",
            country="Japan",
            lat=38.20,
            lng=142.10,
        )
        other = incident(
            source_name="Regional desk",
            title="Coastal alert issued",
            summary="Sirens sounded along the waterfront",
            category="Seismic",
            country="Japan",
            lat=38.40,
            lng=142.20,
            timestamp=LATER,
            source="https://example.test/desk",
        )
        scoring.score_incidents([usgs_wire, other])
        self.assertEqual(usgs_wire["confidence_tier"], "corroborated")
        self.assertEqual(other["corroboration_count"], 2)

    def test_no_keyword_or_category_overlap_stays_single_source(self):
        clash = incident(title="Market clash", summary="Gunfire in the market", category="Security")
        flood = incident(
            source_name="AFP",
            title="River rises",
            summary="The river rose over the road",
            category="Weather",
            source="https://example.test/afp",
        )
        scoring.score_incidents([clash, flood])
        self.assertEqual(clash["confidence_tier"], "unverified")
        self.assertEqual(flood["confidence_tier"], "unverified")

    def test_clustering_is_not_transitive_past_75km(self):
        alpha = incident(source_name="Alpha Wire", lat=12.00, lng=45.00, source="https://example.test/a")
        bravo = incident(source_name="Bravo Wire", lat=12.50, lng=45.00, source="https://example.test/b")
        charlie = incident(source_name="Charlie Wire", lat=13.00, lng=45.00, source="https://example.test/c")
        scoring.score_incidents([alpha, bravo, charlie])
        self.assertNotIn("Charlie Wire", alpha["corroborating_sources"])
        self.assertEqual(bravo["corroboration_count"], 3)
        self.assertEqual(bravo["confidence_score"], 70)
        self.assertEqual(alpha["corroboration_count"], 2)

    def test_official_advisory_keeps_its_tier_when_a_wire_agrees(self):
        usgs = incident(
            source_name="USGS",
            source="https://earthquake.usgs.gov/q",
            title="M 5.2 earthquake near Aden",
            summary="Magnitude 5.2 earthquake",
            category="Seismic",
        )
        reuters = incident(
            source_name="Reuters",
            title="Quake rattles Aden",
            summary="A magnitude earthquake shook Aden",
            category="Seismic",
            timestamp=LATER,
        )
        scoring.score_incidents([usgs, reuters])
        self.assertEqual(usgs["confidence_tier"], "official")
        self.assertEqual(usgs["confidence_score"], 94)
        self.assertEqual(usgs["corroborating_sources"], ["Reuters", "USGS"])
        self.assertEqual(reuters["confidence_tier"], "corroborated")
        self.assertEqual(reuters["corroboration_count"], 2)

    def test_missing_time_or_place_does_not_invent_a_cluster(self):
        orphan = incident(timestamp="", lat=None, lng=None, country="")
        other = incident(source_name="AFP", timestamp="", lat=None, lng=None, country="", source="https://example.test/afp")
        scoring.score_incidents([orphan, other])
        self.assertEqual(orphan["confidence_tier"], "unverified")
        self.assertEqual(orphan["confidence_score"], 24)
        self.assertEqual(other["corroboration_count"], 1)

    def test_geojson_features_are_scored_on_properties(self):
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.2, 15.35]},
            "properties": {
                "title": "Oil terminal drone strike",
                "summary": "A drone strike hit the oil terminal",
                "country": "Yemen",
                "category": "Conflict",
                "timestamp": STAMP,
                "source_name": "Reuters",
            },
        }
        scoring.score_incidents([feature])
        self.assertNotIn("confidence_tier", feature)
        self.assertEqual(feature["properties"]["confidence_tier"], "unverified")
        self.assertEqual(feature["properties"]["confidence_score"], 36)

    def test_al_arabiya_does_not_corroborate_al_hadath(self):
        scored = scoring.score_incidents([
            incident(
                source_name="Al Arabiya",
                source="https://english.alarabiya.net/story",
                source_tier="Regional broadcaster / Saudi state-aligned",
                title="Airstrike near the port",
                summary="Airstrike near the port",
                country="Yemen",
                lat=12.8,
                lng=45.0,
            ),
            incident(
                source_name="Alhadath_Brk",
                source="https://x.com/Alhadath_Brk/status/9",
                source_tier="Established national / breaking news",
                title="Airstrike near the port",
                summary="Airstrike near the port",
                country="Yemen",
                lat=12.8,
                lng=45.0,
                timestamp=LATER,
            ),
        ])
        self.assertTrue(all(item["confidence_tier"] == "unverified" for item in scored))
        self.assertTrue(all(item["corroboration_count"] == 1 for item in scored))


class ApiOutputTests(unittest.TestCase):
    def setUp(self):
        for name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
            os.environ.pop(name, None)
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"

    def test_api_data_scores_features_without_mutating_the_snapshot(self):
        import serve_map
        import app as app_module

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.2, 15.35]},
            "properties": {
                "title": "M 5.2 earthquake near Aden",
                "summary": "Magnitude 5.2 earthquake",
                "country": "Yemen",
                "category": "Seismic",
                "timestamp": STAMP,
                "source_name": "USGS",
                "source": "https://earthquake.usgs.gov/q",
                "lat": 15.35,
                "lng": 44.2,
            },
        }
        wire = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.25, 15.40]},
            "properties": {
                "title": "Quake rattles Aden",
                "summary": "A magnitude earthquake shook Aden",
                "country": "Yemen",
                "category": "Seismic",
                "timestamp": LATER,
                "source_name": "Reuters",
                "source": "https://example.test/reuters",
                "lat": 15.40,
                "lng": 44.25,
            },
        }
        snapshot = {
            "ok": True,
            "features": [feature, wire],
            "events": [{
                "title": "M 5.2 earthquake near Aden",
                "summary": "Magnitude 5.2 earthquake",
                "country": "Yemen",
                "category": "Seismic",
                "timestamp": STAMP,
                "region": "Middle East",
            }],
        }
        original = serve_map.data_response
        serve_map.data_response = lambda: snapshot
        try:
            response = app_module.app.test_client().get("/api/data")
        finally:
            serve_map.data_response = original
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        props = body["features"][0]["properties"]
        self.assertEqual(props["confidence_tier"], "official")
        self.assertEqual(props["confidence_label"], "Official Advisory")
        self.assertEqual(props["confidence_score"], 94)
        self.assertEqual(props["corroborating_sources"], ["Reuters", "USGS"])
        self.assertEqual(body["features"][1]["properties"]["confidence_tier"], "corroborated")
        self.assertEqual(body["events"][0]["confidence_tier"], "official")
        self.assertNotIn("confidence_tier", snapshot["features"][0]["properties"])
        self.assertNotIn("confidence_tier", snapshot["events"][0])

    def test_news_stream_and_country_routes_score_flat_incidents(self):
        import serve_map
        import app as app_module

        payloads = {
            "/api/news-stream": {
                "ok": True,
                "items": [
                    incident(source_name="Reuters"),
                    incident(source_name="AFP", timestamp=LATER, source="https://example.test/afp"),
                ],
            },
            "/api/risk-assessments/country-incidents": {
                "ok": True,
                "country": "Yemen",
                "incidents": [
                    incident(source_name="ReliefWeb", source="https://reliefweb.int/a"),
                ],
            },
        }
        original = serve_map.handle_api

        def fake(method, target, raw):
            path = target.split("?", 1)[0]
            return 200, "application/json; charset=utf-8", json.dumps(payloads[path]).encode()

        serve_map.handle_api = fake
        try:
            client = app_module.app.test_client()
            news = client.get("/api/news-stream").get_json()
            country = client.get("/api/risk-assessments/country-incidents?country=Yemen").get_json()
        finally:
            serve_map.handle_api = original
        self.assertEqual(news["items"][0]["confidence_tier"], "corroborated")
        self.assertEqual(news["items"][0]["corroboration_count"], 2)
        self.assertEqual(country["incidents"][0]["confidence_tier"], "official")
        self.assertEqual(country["incidents"][0]["confidence_label"], "Official Advisory")
        self.assertNotIn("confidence_tier", payloads["/api/news-stream"]["items"][0])

    def test_map_page_exposes_the_confidence_filter_and_badge(self):
        import app as app_module

        client = app_module.app.test_client()
        page = client.get("/map")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        page.close()
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        home_html = home.get_data(as_text=True)
        home.close()
        self.assertIn("Global Geopolitical Risk Intelligence", home_html)
        self.assertNotIn("confidenceMarkup", home_html)
        self.assertIn("All Confidence", html)
        self.assertIn("Corroborated+", html)
        self.assertIn("confidenceMarkup", html)
        self.assertIn("confidence-official", html)
        script = client.get("/static/news-stream.js")
        self.assertIn(b"confidence-badge", script.data)
        script.close()

    def test_slack_card_includes_the_badge_only_when_present(self):
        import slack_alerts

        plain = slack_alerts.build_block_kit(incident())
        context = next(block for block in plain["attachments"][0]["blocks"] if block["type"] == "context")
        self.assertEqual(len(context["elements"]), 1)
        self.assertNotIn("Confidence:", context["elements"][0]["text"])

        scored = scoring.score_incidents([
            incident(source_name="USGS", source="https://earthquake.usgs.gov/q", category="Seismic", title="Quake", summary="Quake"),
            incident(source_name="Reuters", category="Seismic", title="Quake felt", summary="Quake felt", timestamp=LATER),
        ])
        card = slack_alerts.build_block_kit(scored[1])
        context = next(block for block in card["attachments"][0]["blocks"] if block["type"] == "context")
        badge = context["elements"][1]["text"]
        self.assertIn("Confidence: Multi-Source Corroborated", badge)
        self.assertIn("62", badge)
        self.assertIn("Reuters", badge)
        self.assertIn("USGS", badge)
        digest = slack_alerts.build_digest_payload(scored)
        blob = json.dumps(digest)
        self.assertIn("Confidence: Official Advisory", blob)
        self.assertIn("Confidence: Multi-Source Corroborated", blob)

    def test_partisan_accounts_do_not_corroborate_each_other(self):
        scored = scoring.score_incidents([
            incident(
                source_name="Ashara Media",
                source="https://t.me/ashara_media/42",
                source_tier="partisan (pro-Fano) / Social-unverified",
                title="Security incident in Amhara",
                summary="Security incident in Amhara",
                country="Ethiopia",
                lat=11.6,
                lng=37.4,
            ),
            incident(
                source_name="AAA_Amhara",
                source="https://x.com/AAA_Amhara/status/1",
                source_tier="Social-unverified / partisan",
                title="Security incident in Amhara",
                summary="Security incident in Amhara",
                country="Ethiopia",
                lat=11.6,
                lng=37.4,
                timestamp=LATER,
            ),
        ])
        self.assertTrue(all(item["confidence_tier"] == "unverified" for item in scored))
        self.assertTrue(all(item["corroboration_count"] == 1 for item in scored))

    def test_pro_ua_accounts_do_not_corroborate_each_other(self):
        scored = scoring.score_incidents([
            incident(
                source_name="Exilenova+",
                source="https://t.me/exilenova_plus/35393",
                source_tier="Social-unverified / partisan (pro-UA)",
                title="Drone strike on the Efremov plant",
                summary="A drone strike hit the synthetic rubber plant in Tula",
                country="Russia",
                lat=53.15,
                lng=38.08,
            ),
            incident(
                source_name="AMK Mapping",
                source="https://t.me/AMK_Mapping/41266",
                source_tier="Social-unverified / partisan (pro-UA)",
                title="Drone strike on the Efremov plant",
                summary="A drone strike hit the synthetic rubber plant in Tula",
                country="Russia",
                lat=53.15,
                lng=38.08,
                timestamp=LATER,
            ),
        ])
        self.assertTrue(all(item["confidence_tier"] == "unverified" for item in scored))

    def test_same_video_repost_is_not_an_independent_source(self):
        clip = "https://t.me/exilenova_plus/35393"
        scored = scoring.score_incidents([
            incident(
                source_name="Exilenova+",
                source=clip,
                source_tier="Social-unverified / partisan (pro-UA)",
                title="Drone strike on the Efremov plant",
                summary="Video of a strike on the Efremov synthetic rubber plant",
                country="Russia",
                lat=53.15,
                lng=38.08,
            ),
            incident(
                source_name="Reuters",
                source="https://www.reuters.com/world/efremov",
                title="Video shows a strike on a rubber plant",
                summary="The clip was first posted at " + clip,
                country="Russia",
                lat=53.15,
                lng=38.08,
                timestamp=LATER,
            ),
        ])
        reuters = next(item for item in scored if item["source_name"] == "Reuters")
        self.assertEqual(reuters["confidence_tier"], "unverified")
        self.assertEqual(reuters["corroboration_count"], 1)

    def test_an_outlet_that_does_not_repost_the_clip_still_corroborates(self):
        scored = scoring.score_incidents([
            incident(
                source_name="Exilenova+",
                source="https://t.me/exilenova_plus/35393",
                source_tier="Social-unverified / partisan (pro-UA)",
                title="Drone strike on the Efremov plant",
                summary="A drone strike hit the synthetic rubber plant in Efremov",
                country="Russia",
                lat=53.15,
                lng=38.08,
            ),
            incident(
                source_name="Operative Information Center",
                source="https://operativmm.az/en/story",
                source_tier="National outlet",
                title="Drone strike on the Efremov plant",
                summary="A drone strike hit the synthetic rubber plant in Efremov",
                country="Russia",
                lat=53.15,
                lng=38.08,
                timestamp=LATER,
            ),
        ])
        outlet = next(item for item in scored if item["source_name"] == "Operative Information Center")
        self.assertEqual(outlet["confidence_tier"], "corroborated")
        self.assertGreaterEqual(outlet["corroboration_count"], 2)

    def test_kenya_times_scores_below_the_standard(self):
        standard = scoring.score_incidents([
            incident(source_name="The Standard", source_tier="National outlet", country="Kenya", lat=-1.29, lng=36.82),
        ])[0]
        newer = scoring.score_incidents([
            incident(source_name="The Kenya Times", source_tier="Secondary national outlet", country="Kenya", lat=-1.29, lng=36.82),
        ])[0]
        self.assertLess(newer["confidence_score"], standard["confidence_score"])

    def test_yaffa_repost_of_quds_is_not_a_second_source(self):
        scored = scoring.score_incidents([
            incident(
                source_name="qudsn",
                source="https://qudsn.co/post/1",
                source_tier="partisan (Hamas-leaning)",
                title="Drone strike on al-Shati camp",
                summary="A drone strike killed 3. Cited source: al-Shifa Medical Complex.",
                country="Palestine",
                lat=31.531,
                lng=34.445,
                timestamp="2026-11-26T01:00:00Z",
            ),
            incident(
                source_name="yaffa_ps",
                source="https://x.com/yaffa_ps/status/1",
                source_tier="Social-unverified/partisan",
                title="Drone strike on al-Shati camp",
                summary="A drone strike killed 3. Cited source: al-Shifa Medical Complex.",
                country="Palestine",
                lat=31.531,
                lng=34.445,
                timestamp="2026-11-26T01:00:30Z",
            ),
        ])
        self.assertTrue(all(item["confidence_tier"] == "unverified" for item in scored))
        self.assertTrue(all(item["corroboration_count"] == 1 for item in scored))


if __name__ == "__main__":
    unittest.main()
