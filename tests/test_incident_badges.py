"""Corroboration and location-precision badges stay deterministic and visible on the map."""

import json
import os
import unittest

os.environ.setdefault("DISABLE_BACKGROUND", "1")
os.environ.setdefault("DISABLE_HISTORICAL_SYNC", "1")
for _name in ("PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME"):
    os.environ.pop(_name, None)

import feed_quality
import global_feeds as gf
import incident_badges as badges
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


class CorroborationTests(unittest.TestCase):
    def test_official_feeds_and_plain_official_tier(self):
        samples = [
            incident(source_name="USGS Earthquakes", source="https://earthquake.usgs.gov/q", feed_id="usgs-quakes", title="Quake", summary="Quake"),
            incident(source_name="GDACS", source="https://www.gdacs.org/xml/rss_24h.xml", feed_id="gdacs-rss", title="Flood", summary="Flood"),
            incident(source_name="ReliefWeb", source="https://reliefweb.int/updates", title="Advisory", summary="Advisory"),
            incident(source_name="UK FCDO", source="https://www.gov.uk/foreign-travel-advice/yemen", feed_id="uk-fcdo-atom", title="Yemen advice", summary="Advice"),
            incident(source_name="FAA", source="https://nasstatus.faa.gov/api/airport-events", feed_id="faa", title="Airport", summary="Ground stop"),
            incident(source_name="Ministry of Foreign Affairs", source_tier="Official", source="https://example.test/mfa", title="Advisory", summary="Advisory"),
        ]
        for row in samples:
            with self.subTest(source=row["source_name"]):
                badge = badges.classify_corroboration(row)
                self.assertEqual(badge["level"], "official")
                self.assertEqual(badge["label"], "Official Source")
                self.assertGreaterEqual(badge["source_count"], 1)
                self.assertIn("Official institutional feed", badge["rationale"])
                self.assertNotIn("color", badge["rationale"].lower())

    def test_official_primary_wires_stay_single_source(self):
        for name, feed in (("BBC World", "wire-bbc"), ("Al Jazeera", "wire-aj"), ("Reuters", "")):
            row = incident(source_name=name, source_tier="Official / Primary", feed_id=feed, title="Market clash", summary="One desk report")
            badge = badges.classify_corroboration(row)
            self.assertEqual(badge["level"], "single", name)
            self.assertEqual(badge["label"], "Single Source")
            self.assertEqual(badge["source_count"], 1)
            self.assertIn(name, badge["rationale"])
            self.assertIn("no independent corroboration", badge["rationale"])

    def test_clustered_independent_sources_are_multi_source(self):
        reuters = incident(source_name="Reuters")
        afp = incident(source_name="AFP", source="https://example.test/afp", timestamp=LATER, lat=15.4, lng=44.25)
        scoring.score_incidents([reuters, afp])
        badges.annotate_incident(reuters)
        badges.annotate_incident(afp)
        self.assertEqual(reuters["corroboration"]["level"], "corroborated")
        self.assertEqual(reuters["corroboration"]["label"], "Multi-Source")
        self.assertEqual(reuters["corroboration"]["source_count"], 2)
        self.assertEqual(reuters["corroboration"]["rationale"], "Corroborated across 2 independent sources.")
        self.assertEqual(afp["confidence_tier"], "corroborated")

    def test_same_outlet_is_not_treated_as_corroboration(self):
        first = incident(source_name="Reuters", source="https://example.test/a")
        second = incident(source_name="Reuters", source="https://example.test/b", timestamp=LATER)
        scoring.score_incidents([first, second])
        badge = badges.classify_corroboration(first)
        self.assertEqual(badge["level"], "single")
        self.assertEqual(badge["source_count"], 1)

    def test_stored_count_from_another_channel_is_kept(self):
        row = incident(corroboration_count=4, source_name="Reuters")
        badges.score_with_stored_count([row])
        self.assertEqual(row["confidence_tier"], "unverified")
        self.assertEqual(row["corroboration_count"], 1)
        badge = badges.classify_corroboration(row)
        self.assertEqual(badge["level"], "corroborated")
        self.assertEqual(badge["source_count"], 4)
        self.assertEqual(badge["rationale"], "Corroborated by 4 reports of the same incident.")
        badges.annotate_incident(row)
        self.assertNotIn("_stored_corroboration_count", row)

    def test_official_plus_an_independent_source_stays_official(self):
        usgs = incident(
            source_name="USGS",
            source="https://earthquake.usgs.gov/q",
            feed_id="usgs-quakes",
            category="Seismic",
            title="Quake felt near Aden",
            summary="Magnitude 5.2 earthquake",
        )
        wire = incident(
            source_name="Reuters",
            category="Seismic",
            title="Quake felt near Aden",
            summary="Magnitude 5.2 earthquake",
            timestamp=LATER,
            lat=15.4,
            lng=44.25,
        )
        scoring.score_incidents([usgs, wire])
        badge = badges.classify_corroboration(usgs)
        self.assertEqual(badge["level"], "official")
        self.assertGreaterEqual(badge["source_count"], 2)
        self.assertIn("USGS", badge["rationale"])
        self.assertIn("Corroborated across", badge["rationale"])
        self.assertEqual(badges.classify_corroboration(wire)["level"], "corroborated")

    def test_missing_and_odd_counts(self):
        empty = badges.classify_corroboration({})
        self.assertEqual(empty["level"], "single")
        self.assertEqual(empty["source_count"], 1)
        self.assertIn("Single source report", empty["rationale"])
        counted = badges.classify_corroboration(incident(corroboration_count="2", corroborating_sources=["Reuters", "AFP"]))
        self.assertEqual(counted["level"], "corroborated")
        self.assertEqual(counted["source_count"], 2)
        self.assertEqual(badges.classify_corroboration(incident(corroboration_count=0))["level"], "single")
        self.assertEqual(badges.classify_corroboration(incident(corroboration_count="nope"))["level"], "single")


class LocationPrecisionTests(unittest.TestCase):
    def test_structured_event_feeds_are_exact(self):
        samples = [
            ("usgs-quakes", "https://earthquake.usgs.gov/q", "USGS"),
            ("firms", "https://firms.modaps.eosdis.nasa.gov/a", "NASA FIRMS"),
            ("acled", "https://acleddata.com/event", "ACLED"),
            ("emsc", "https://www.seismicportal.eu/event", "EMSC"),
            ("faa", "https://nasstatus.faa.gov/api/airport-events", "FAA"),
            ("gdacs-rss", "https://www.gdacs.org/report", "GDACS"),
        ]
        for feed_id, source, name in samples:
            with self.subTest(feed=feed_id):
                badge = badges.classify_location_precision(incident(
                    feed_id=feed_id,
                    source=source,
                    source_name=name,
                    coord_origin="feed-point",
                    geo_precision="Exact",
                ))
                self.assertEqual(badge["level"], "exact")
                self.assertEqual(badge["label"], "Exact / Site")
                self.assertIn(name, badge["rationale"])
                self.assertIn("reported site", badge["rationale"])

    def test_pipeline_labels_keep_their_tier(self):
        city = badges.classify_location_precision(incident(geo_precision="City / Area", location_name="Aleppo", coord_origin="place"))
        self.assertEqual(city["level"], "city")
        self.assertEqual(city["label"], "City / District")
        self.assertIn("Aleppo", city["rationale"])
        self.assertIn("not an exact site", city["rationale"])
        governorate = badges.classify_location_precision(incident(geo_precision="City / Governorate", location_name="Homs"))
        self.assertEqual(governorate["level"], "city")
        country = badges.classify_location_precision(incident(geo_precision="Country", coord_origin="centroid", country="Canada", lat=56.1, lng=-96.0))
        self.assertEqual(country["level"], "regional")
        self.assertEqual(country["label"], "Regional / Country")
        self.assertIn("centroid", country["rationale"])
        sea = badges.classify_location_precision(incident(geo_precision="Country / Sea Fallback", location_name="Red Sea"))
        self.assertEqual(sea["level"], "regional")
        self.assertIn("sea", sea["rationale"].lower())
        damascus = badges.classify_location_precision(incident(geo_precision="Damascus Default Fallback"))
        self.assertEqual(damascus["level"], "regional")
        self.assertIn("Damascus", damascus["rationale"])

    def test_centroid_feed_is_not_promoted_when_coordinates_exist(self):
        advice = incident(
            feed_id="us-advisories",
            source="https://travel.state.gov/syria",
            source_name="US State Department",
            geo_precision="Exact",
            country="Syria",
            lat=34.8,
            lng=38.9,
        )
        badge = badges.classify_location_precision(advice)
        self.assertEqual(badge["level"], "regional")
        self.assertIn("Syria", badge["rationale"])
        self.assertIn("not an exact site", badge["rationale"])

    def test_place_name_and_facility_fallbacks(self):
        city = badges.classify_location_precision({"location_name": "Aden", "lat": 12.8, "lng": 45.0})
        self.assertEqual(city["level"], "city")
        site = badges.classify_location_precision({"location_name": "Aden Airport", "lat": 12.8, "lng": 45.0})
        self.assertEqual(site["level"], "exact")
        self.assertIn("Facility or site", site["rationale"])
        admin = badges.classify_location_precision({"location_name": "Idlib Governorate"})
        self.assertEqual(admin["level"], "regional")
        bare = badges.classify_location_precision({"lat": 1.0, "lng": 2.0})
        self.assertEqual(bare["level"], "city")
        self.assertIn("local-area", bare["rationale"])

    def test_missing_fields_are_regional(self):
        badge = badges.classify_location_precision({})
        self.assertEqual(badge["level"], "regional")
        self.assertEqual(badge["label"], "Regional / Country")
        self.assertIn("No precise coordinates", badge["rationale"])
        country_only = badges.classify_location_precision({"country": "Yemen"})
        self.assertEqual(country_only["level"], "regional")
        self.assertIn("Yemen", country_only["rationale"])


class PipelineSignalTests(unittest.TestCase):
    def test_centroid_coordinates_are_not_relabelled_exact(self):
        item = feed_quality.enrich_event({
            "title": "Syria travel advice",
            "summary": "Do not travel",
            "lat": 34.8,
            "lng": 38.9,
            "country": "Syria",
            "coord_origin": "centroid",
            "geo_precision": "Country",
        }, live_translation=False)
        self.assertEqual(item["coord_origin"], "centroid")
        self.assertEqual(item["geo_precision"], "Country")
        self.assertEqual(badges.classify_location_precision(item)["level"], "regional")

    def test_supplied_feed_coordinates_stay_exact(self):
        item = feed_quality.enrich_event({"title": "Quake", "lat": 35.0, "lng": 139.0, "id": "q"}, live_translation=False)
        self.assertEqual(item["geo_precision"], "Exact")
        self.assertEqual(item["lat"], 35.0)

    def test_parsers_record_coordinate_origin(self):
        quake = {
            "features": [{
                "id": "us1000",
                "geometry": {"type": "Point", "coordinates": [142.1, 38.2, 10]},
                "properties": {
                    "mag": 6.2,
                    "place": "20 km E of Sendai, Japan",
                    "time": 1700000000000,
                    "title": "M 6.2 - Sendai",
                    "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us1000",
                },
            }]
        }
        feed = {"id": "usgs-quakes", "name": "USGS", "region": "Global", "category": "Seismic", "url": "https://example.test/usgs"}
        event = gf.parse_usgs(feed, json.dumps(quake).encode())[0]
        self.assertEqual(event["coord_origin"], "feed-point")
        self.assertEqual(badges.classify_location_precision({**event, "feed_id": "usgs-quakes", "source_name": "USGS"})["level"], "exact")
        self.assertEqual(badges.classify_corroboration({**event, "feed_id": "usgs-quakes", "source": event["source"]})["level"], "official")

        advice = [{
            "Title": "Syria Travel Advisory",
            "Category": ["Level 4: Do Not Travel"],
            "Summary": "Do not travel",
            "Link": "https://travel.state.gov/syria",
        }]
        advisory_feed = {"id": "us-advisories", "name": "US State Department", "region": "Global", "category": "Travel Advisory", "url": "https://example.test/us"}
        advisory = gf.parse_us_advisory(advisory_feed, json.dumps(advice).encode())[0]
        self.assertEqual(advisory["coord_origin"], "centroid")
        self.assertEqual(advisory["geo_precision"], "Country")
        self.assertEqual(badges.classify_location_precision({**advisory, "feed_id": "us-advisories"})["level"], "regional")

    def test_named_city_beats_a_country_default(self):
        placed = gf._place_event(
            {"id": "bbc-uk", "name": "BBC", "region": "UK & Europe", "category": "Security", "parser": "placed-rss", "default_country": "Syria"},
            "Clashes in Aleppo after night raid",
            "Residential district hit overnight",
            "Syria",
            id="aleppo-1",
        )
        self.assertIsNotNone(placed)
        self.assertEqual(placed["coord_origin"], "place")
        self.assertEqual(placed["geo_precision"], "City / Area")
        self.assertEqual(badges.classify_location_precision(placed)["level"], "city")
        country = gf._place_event(
            {"id": "bbc-uk", "name": "BBC", "region": "UK & Europe", "category": "Security", "parser": "placed-rss"},
            "Security alert for Canada",
            "Security alert",
            "",
            id="canada-1",
        )
        self.assertEqual(country["coord_origin"], "centroid")
        self.assertEqual(badges.classify_location_precision(country)["level"], "regional")


class ApiAndPageTests(unittest.TestCase):
    def test_map_api_adds_badges_without_dropping_existing_fields(self):
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
                "feed_id": "usgs-quakes",
                "lat": 15.35,
                "lng": 44.2,
                "corroboration_count": 1,
            },
        }
        wire = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.25, 15.4]},
            "properties": {
                "title": "Quake rattles Aden",
                "summary": "A magnitude earthquake shook Aden",
                "country": "Yemen",
                "category": "Seismic",
                "timestamp": LATER,
                "source_name": "Reuters",
                "source": "https://example.test/reuters",
                "lat": 15.4,
                "lng": 44.25,
                "location_name": "Aden",
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
        self.assertEqual(props["source_name"], "USGS")
        self.assertEqual(props["corroboration"]["level"], "official")
        self.assertEqual(props["corroboration"]["label"], "Official Source")
        self.assertIn("USGS", props["corroboration"]["rationale"])
        self.assertEqual(props["location_precision"]["level"], "exact")
        self.assertEqual(props["location_precision"]["label"], "Exact / Site")
        self.assertNotIn("_stored_corroboration_count", props)
        wire_props = body["features"][1]["properties"]
        self.assertEqual(wire_props["corroboration"]["level"], "corroborated")
        self.assertEqual(wire_props["location_precision"]["level"], "city")
        self.assertIn("Aden", wire_props["location_precision"]["rationale"])
        event = body["events"][0]
        self.assertEqual(event["corroboration"]["level"], "official")
        self.assertEqual(event["location_precision"]["level"], "exact")
        self.assertNotIn("corroboration", snapshot["features"][0]["properties"])
        self.assertNotIn("location_precision", snapshot["features"][0]["properties"])
        self.assertNotIn("corroboration", snapshot["events"][0])

    def test_news_items_receive_the_same_fields(self):
        import serve_map
        import app as app_module

        payload = {
            "ok": True,
            "items": [
                incident(source_name="Reuters"),
                incident(source_name="AFP", timestamp=LATER, source="https://example.test/afp"),
            ],
        }
        original = serve_map.handle_api

        def fake(method, target, raw):
            return 200, "application/json; charset=utf-8", json.dumps(payload).encode()

        serve_map.handle_api = fake
        try:
            body = app_module.app.test_client().get("/api/news-stream").get_json()
        finally:
            serve_map.handle_api = original
        self.assertEqual(body["items"][0]["corroboration"]["level"], "corroborated")
        self.assertEqual(body["items"][0]["corroboration"]["source_count"], 2)
        self.assertEqual(body["items"][0]["location_precision"]["level"], "city")
        self.assertNotIn("corroboration", payload["items"][0])

    def test_map_pages_render_badges_and_filters(self):
        import app as app_module

        client = app_module.app.test_client()
        for path in ("/", "/map"):
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            html = page.get_data(as_text=True)
            page.close()
            self.assertIn("GeoRiskIntel · Proprietary Client Intelligence", html)
            self.assertIn('id="slim-corroboration"', html)
            self.assertIn("Official &amp; corroborated", html)
            self.assertIn("Single source", html)
            self.assertIn('id="slim-precision"', html)
            self.assertIn("Exact / Site", html)
            self.assertIn("City / District", html)
            self.assertIn("Regional / Country", html)
            self.assertIn("function qualityMarkup", html)
            self.assertIn("quality-corr-official", html)
            self.assertIn("quality-prec-exact", html)
            self.assertIn("quality-prec-regional", html)
            self.assertIn("passesCorroboration", html)
            self.assertIn("passesPrecision", html)
            self.assertIn("All Confidence", html)
            self.assertIn("Corroborated+", html)
        script = client.get("/static/news-stream.js")
        self.assertIn(b"quality-badge", script.data)
        self.assertIn(b"qualityBadges", script.data)
        script.close()


if __name__ == "__main__":
    unittest.main()
