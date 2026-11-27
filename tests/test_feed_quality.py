import unittest

import feed_quality as fq


class FeedQualityTests(unittest.TestCase):
    def test_bare_url_and_promo_are_noise(self):
        self.assertTrue(fq.is_noise("https://almaalomah.me/12345"))
        self.assertTrue(fq.is_noise("Subscribe to our Telegram channel"))
        self.assertTrue(fq.is_noise("🔥🔥🔥"))
        self.assertFalse(fq.is_noise("Airstrike reported near Aleppo", "Residential district hit overnight"))

    def test_city_match_beats_country_centroid(self):
        placed = fq.geolocate("Clashes in Aleppo after night raid", country="Syria", salt="aleppo-1")
        self.assertEqual(placed["area"], "Aleppo")
        self.assertEqual(placed["country"], "Syria")
        self.assertAlmostEqual(placed["lat"], 36.2021, delta=0.1)
        self.assertEqual(placed["geo_precision"], "City / Area")

    def test_country_only_uses_centroid_with_jitter(self):
        one = fq.geolocate("Security alert for Canada", salt="a")
        two = fq.geolocate("Security alert for Canada", salt="b")
        self.assertEqual(one["country"], "Canada")
        self.assertEqual(one["geo_precision"], "Country")
        self.assertNotEqual((one["lat"], one["lng"]), (two["lat"], two["lng"]))

    def test_feed_coordinates_are_kept(self):
        placed = fq.geolocate("Quake", lat=35.0, lng=139.0, salt="q")
        self.assertEqual(placed["geo_precision"], "Exact")
        self.assertEqual(placed["lat"], 35.0)
        self.assertEqual(placed["lng"], 139.0)

    def test_duplicate_headlines_collapse(self):
        rows = fq.dedupe([
            {"title": "Port blast in Aden"},
            {"title": "Port blast in Aden"},
            {"title": "Separate clash in Daraa"},
        ])
        self.assertEqual(len(rows), 2)

    def test_translation_cache_skips_a_second_lookup(self):
        calls = {"n": 0}

        def fake(text):
            calls["n"] += 1
            return "Aleppo update"

        original = fq._google_translate
        fq._google_translate = fake
        fq._grok_translate = lambda text: ""
        fq._mymemory_translate = lambda text: ""
        try:
            fq._cache_put = lambda digest, text: None
            fq._cache_get = lambda digest: ""
            first = fq.translate_text("تحديث في حلب")
            fq._cache_get = lambda digest: "Aleppo update"
            second = fq.translate_text("تحديث في حلب")
        finally:
            fq._google_translate = original
        self.assertEqual(first, "Aleppo update")
        self.assertEqual(second, "Aleppo update")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
