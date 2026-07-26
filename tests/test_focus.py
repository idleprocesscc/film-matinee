from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.film_matinee_mcp import _build_focus_command


class FocusRangeTests(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        video = root / "movie.mp4"
        video.touch()
        subtitle = root / "movie.srt"
        subtitle.write_text("", "utf-8")
        asr = root / "audio-transcript.asr.srt"
        asr.write_text("", "utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "title": "Test Film",
            "video": str(video),
            "subtitle": str(subtitle),
            "subtitle_source": "source-subtitle",
            "audio_transcript": str(asr),
            "probe": {"format": {"duration": "7200"}},
            "options": {
                "burned_subtitles": "auto",
                "audio_transcript": "auto",
                "asr_model": "medium",
                "ffmpeg_hwaccel": "none",
            },
            "sheets": [{"index": 4, "time_range": [2700, 3000]}],
        }), "utf-8")
        return manifest

    def test_dense_focus_is_one_cached_sheet_with_existing_text_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            command, out, parent_path, _, start, end = _build_focus_command(
                str(manifest), "46:30", "47:45", "dense",
            )
        self.assertEqual((start, end), (2790.0, 2865.0))
        self.assertEqual(parent_path, manifest.resolve())
        self.assertIn("focus", out.parts)
        self.assertIn("--layout", command)
        self.assertEqual(command[command.index("--layout") + 1], "5x4")
        self.assertEqual(command[command.index("--sample-step-sec") + 1], "0.5")
        self.assertEqual(command[command.index("--audio-transcript") + 1], "off")
        self.assertIn("--audio-transcript-file", command)

    def test_focus_rejects_ranges_over_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            with self.assertRaisesRegex(ValueError, "5 minutes"):
                _build_focus_command(str(manifest), "10:00", "15:01", "dense")


if __name__ == "__main__":
    unittest.main()
