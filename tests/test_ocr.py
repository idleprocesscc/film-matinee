from __future__ import annotations

import unittest

from tools.film_matinee_ocr import (
    OCRObservation,
    detection_times,
    merge_observations,
    subtitle_text_from_rows,
    text_similarity,
)


class OCRTests(unittest.TestCase):
    def test_bilingual_rows_are_ordered_and_joined(self) -> None:
        text, confidence = subtitle_text_from_rows([
            {"text": "You cannot pass!", "confidence": 0.8, "x": 0.3, "y": 0.2, "width": 0.4},
            {"text": "你休想过!", "confidence": 0.9, "x": 0.4, "y": 0.4, "width": 0.2},
        ])
        self.assertEqual(text, "你休想过! / You cannot pass!")
        self.assertAlmostEqual(confidence, 0.85)

    def test_edge_text_and_tiny_noise_are_not_subtitles(self) -> None:
        text, _ = subtitle_text_from_rows([
            {"text": "BILIBILI", "confidence": 0.9, "x": 0.9, "y": 0.8, "width": 0.08},
            {"text": "I", "confidence": 0.9, "x": 0.5, "y": 0.2, "width": 0.02},
        ])
        self.assertEqual(text, "")

    def test_one_bilingual_line_can_bridge_a_temporarily_missing_line(self) -> None:
        self.assertGreater(
            text_similarity(
                "这里不正常 全都不正常 / It's not natural. None of it.",
                "这里不正常 全都不正常",
            ),
            0.9,
        )

    def test_consecutive_observations_merge_but_new_dialogue_does_not(self) -> None:
        cues = merge_observations([
            OCRObservation(10.0, "你休想过 / You cannot pass", 0.8),
            OCRObservation(10.5, "你休想过 / You cannot pass!", 0.9),
            OCRObservation(11.0, "你休想过", 0.8),
            OCRObservation(12.0, "甘道夫 / Gandalf", 0.9),
        ], fps=2.0)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 10.0)
        self.assertEqual(cues[0].end, 11.5)
        self.assertIn("You cannot pass", cues[0].text)
        self.assertEqual(cues[1].text, "甘道夫 / Gandalf")

    def test_low_confidence_single_frame_noise_is_dropped(self) -> None:
        cues = merge_observations([
            OCRObservation(10.0, "VWANTD JNTT AAN", 0.3),
            OCRObservation(12.0, "Gandalf", 0.8),
        ], fps=2.0)
        self.assertEqual([cue.text for cue in cues], ["Gandalf"])

    def test_detection_sampling_spans_long_film_and_includes_early_dialogue(self) -> None:
        times = detection_times(14_000)
        self.assertIn(300.0, times)
        self.assertGreater(max(times), 12_000)
        self.assertLess(len(times), 30)


if __name__ == "__main__":
    unittest.main()
