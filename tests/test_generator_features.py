from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from tools.generate_film_matinee_core import (
    Segment,
    Selection,
    dedupe_keyframe_candidates,
    parse_pin_times,
    parse_vtt,
)


class GeneratorFeatureTests(unittest.TestCase):
    def test_vtt_rolling_captions_are_collapsed(self) -> None:
        content = """WEBVTT

00:00:01.000 --> 00:00:02.000
Hello

00:00:01.500 --> 00:00:03.000
Hello world

00:00:03.000 --> 00:00:04.000
Next line
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "captions.vtt"
            path.write_text(content, "utf-8")
            cues = parse_vtt(path)
        self.assertEqual([cue.text for cue in cues], ["Hello world", "Next line"])
        self.assertEqual(cues[0].start, 1.0)
        self.assertEqual(cues[0].end, 3.0)

    def test_pin_time_parser_accepts_film_timecodes(self) -> None:
        self.assertEqual(parse_pin_times("25,01:30,1:02:03.5"), [25.0, 90.0, 3723.5])

    def test_vtt_voice_tag_preserves_speaker_identity(self) -> None:
        content = """WEBVTT

00:00:01.000 --> 00:00:02.000
<v JOI>Hello, K.</v>
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "speaker.vtt"
            path.write_text(content, "utf-8")
            cues = parse_vtt(path)
        self.assertEqual(cues[0].text, "[JOI] Hello, K.")

    def test_pinned_frame_wins_nearby_candidate_competition(self) -> None:
        options = argparse.Namespace(
            min_micro_keyframe_gap_sec=2.0,
            similar_keyframe_window_sec=6.0,
            near_duplicate_window_sec=5.0,
            similar_keyframe_distance=0.06,
            near_duplicate_distance=0.11,
        )
        regular = Selection(10.2, 50.0, "segment-representative", Segment(9.0, 12.0))
        pinned = Selection(10.0, 1000.0, "pinned", Segment(9.75, 10.25))
        selected = dedupe_keyframe_candidates([regular, pinned], 1, options)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].reason, "pinned")


if __name__ == "__main__":
    unittest.main()
