from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.film_matinee_source import (
    choose_embedded_subtitle,
    choose_remote_subtitle,
    default_out_dir,
    discover_sidecar,
    is_url,
    parse_language_preferences,
    public_url,
)


class SourceTests(unittest.TestCase):
    def test_url_validation_rejects_option_like_input(self) -> None:
        self.assertTrue(is_url("https://example.com/video"))
        self.assertFalse(is_url("--config-location=https://example.com"))
        self.assertFalse(is_url("file:///tmp/video.mp4"))

    def test_url_cache_path_is_deterministic_and_distinct(self) -> None:
        root = Path("/tmp/example")
        first = default_out_dir("https://example.com/watch?v=one", root)
        again = default_out_dir("https://example.com/watch?v=one", root)
        second = default_out_dir("https://example.com/watch?v=two", root)
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, root / ".film-matinee-cache")

    def test_public_url_drops_signed_query_and_fragment(self) -> None:
        self.assertEqual(
            public_url("https://example.com/video.mp4?token=secret#part"),
            "https://example.com/video.mp4",
        )

    def test_manual_caption_beats_preferred_language_auto_translation(self) -> None:
        info = {
            "subtitles": {"en": [{"ext": "vtt"}]},
            "automatic_captions": {"zh-Hans": [{"ext": "vtt"}]},
        }
        preferences = parse_language_preferences("zh-Hans,zh.*,en.*")
        self.assertEqual(choose_remote_subtitle(info, preferences), ("manual", "en"))

    def test_remote_caption_uses_language_preference_within_same_source(self) -> None:
        info = {
            "subtitles": {
                "en": [{"ext": "vtt"}],
                "zh-Hant": [{"ext": "ass"}],
            }
        }
        preferences = parse_language_preferences("zh-Hans,zh-Hant,en.*")
        self.assertEqual(choose_remote_subtitle(info, preferences), ("manual", "zh-Hant"))

    def test_remote_caption_falls_back_to_any_manual_track(self) -> None:
        info = {
            "subtitles": {"fr": [{"ext": "vtt"}]},
            "automatic_captions": {"zh-Hans": [{"ext": "vtt"}]},
        }
        preferences = parse_language_preferences("zh-Hans,en.*")
        self.assertEqual(choose_remote_subtitle(info, preferences), ("manual", "fr"))

    def test_sidecar_discovery_prefers_named_language_and_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "movie.mkv"
            video.touch()
            (root / "movie.en.srt").touch()
            expected = root / "movie.zh-Hans.ass"
            expected.touch()
            chosen = discover_sidecar(video, parse_language_preferences("zh-Hans,en.*"))
            self.assertEqual(chosen, expected.resolve())

    def test_embedded_selection_rejects_bitmap_and_prefers_default_language(self) -> None:
        streams = [
            {"index": 2, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "zh"}},
            {"index": 3, "codec_name": "subrip", "tags": {"language": "en"}},
            {
                "index": 4,
                "codec_name": "ass",
                "tags": {"language": "zh-Hans"},
                "disposition": {"default": 1, "forced": 0},
            },
        ]
        selected = choose_embedded_subtitle(
            streams,
            parse_language_preferences("zh-Hans,en.*"),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["index"], 4)


if __name__ == "__main__":
    unittest.main()
