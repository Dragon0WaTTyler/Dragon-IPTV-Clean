from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_iptv_dist",
    ROOT / "scripts" / "build_iptv_dist.py",
)
assert SPEC and SPEC.loader
BUILD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILD
SPEC.loader.exec_module(BUILD)


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = BUILD.load_rules(ROOT / "config" / "iptv_rules.json")

    def test_language_uses_explicit_language_before_country_prefix(self) -> None:
        self.assertEqual(BUILD.infer_language("AR: Al Jazeera English", self.rules), "english")
        self.assertEqual(BUILD.infer_language("UK: BBC Arabic", self.rules), "arabic")

    def test_country_prefixes_keep_other_languages_out_of_english(self) -> None:
        self.assertEqual(BUILD.infer_language("UK: BBC News", self.rules), "english")
        self.assertEqual(BUILD.infer_language("AR: Al Jazeera", self.rules), "arabic")
        self.assertIsNone(BUILD.infer_language("DE: Sky Sport News", self.rules))
        self.assertIsNone(BUILD.infer_language("CNN TURK", self.rules))
        self.assertIsNone(BUILD.infer_language("CL: CNN", self.rules))
        self.assertIsNone(BUILD.infer_language("CNN CHILE", self.rules))
        self.assertIsNone(BUILD.infer_language("AR: Al Jazeera Balkan", self.rules))

    def test_documentary_has_priority_over_news_brand(self) -> None:
        categories = BUILD.infer_categories("AR: AL JAZEERA DOCUMENTARY", self.rules)
        self.assertEqual(categories[0], "documentary")
        self.assertEqual(
            BUILD.known_channel_category("AR: AL JAZEERA DOCUMENTARY", self.rules),
            "documentary",
        )
        self.assertEqual(
            BUILD.known_channel_category("AR|DOCU: BBC ARABIC", self.rules),
            "news",
        )

    def test_channel_identity_merges_common_al_jazeera_variants(self) -> None:
        self.assertEqual(
            BUILD.channel_identity_key("AR|NEWS: ALJAZEERA SD"),
            BUILD.channel_identity_key("AR: AL JAZEERA HD"),
        )

    def test_vod_is_not_live_tv(self) -> None:
        self.assertTrue(
            BUILD.is_vod_entry(
                "Dubai Bling S01 E01",
                "http://example.test/series/user/pass/42.mkv",
                self.rules,
            )
        )
        self.assertFalse(
            BUILD.is_vod_entry(
                "Al Jazeera Arabic",
                "https://example.test/live/master.m3u8",
                self.rules,
            )
        )

    def test_playback_url_keeps_authentication_query(self) -> None:
        url = "https://example.test/live.m3u8?token=secret&expires=999&quality=hd"
        _, playback_url, _ = BUILD.canonicalize_url(url, {"token", "expires"})
        self.assertEqual(playback_url, url)


class BuildIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = BUILD.load_rules(ROOT / "config" / "iptv_rules.json")

    def test_build_places_key_channels_in_expected_outputs(self) -> None:
        playlist = """#EXTM3U
#EXTINF:-1,AR: Al Jazeera Arabic
https://example.test/aljazeera-ar.m3u8?token=keep-me
#EXTINF:-1,AR: Al Jazeera Documentary
https://example.test/aljazeera-doc.m3u8
#EXTINF:-1 group-title="ARAB VIP NEW",AR: Al Jazeera English
https://example.test/aljazeera-en.m3u8
#EXTINF:-1,UK: BBC News
https://example.test/bbc-news.m3u8
#EXTINF:-1,AR|DOCU: BBC Arabic
https://example.test/bbc-arabic.m3u8
#EXTINF:-1,DE: Sky Sport News
https://example.test/de-sport.m3u8
#EXTINF:-1,Dubai Bling S01 E01
https://example.test/series/user/pass/episode.mkv
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            output = temp / "dist"
            source.mkdir()
            (source / "FIW_1700000000_test.m3u").write_text(playlist, encoding="utf-8")

            BUILD.build_dist(source, output, self.rules)

            arabic_news = (output / "arabic" / "news.m3u").read_text(encoding="utf-8")
            arabic_documentary = (output / "arabic" / "documentary.m3u").read_text(encoding="utf-8")
            english_news = (output / "english" / "news.m3u").read_text(encoding="utf-8")
            arabic_manifest = json.loads((output / "arabic" / "manifest.json").read_text(encoding="utf-8"))

            self.assertIn("Al Jazeera Arabic", arabic_news)
            self.assertIn("token=keep-me", arabic_news)
            self.assertIn("Al Jazeera Documentary", arabic_documentary)
            self.assertNotIn("BBC Arabic", arabic_documentary)
            self.assertIn("BBC Arabic", arabic_news)
            self.assertIn("Al Jazeera English", english_news)
            self.assertIn("BBC News", english_news)
            self.assertNotIn("DE: Sky Sport News", english_news)
            self.assertEqual(arabic_manifest["counts"]["dropped_vod"], 1)


if __name__ == "__main__":
    unittest.main()
