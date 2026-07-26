from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.film_matinee_asr import (
    ASRSegment,
    cached_model_path,
    resolve_asr_backend,
    segments_from_response,
    transcribe_local,
    write_srt,
)
from tools.generate_film_matinee_core import Cue, make_sidecar, reindex_cues


class ASRTests(unittest.TestCase):
    def test_auto_never_downloads_a_missing_local_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            with patch("tools.film_matinee_asr.importlib.util.find_spec", return_value=object()):
                status = resolve_asr_backend("auto", "medium", model_dir=model_dir)
        self.assertFalse(status["active"])
        self.assertEqual(status["reason"], "local-model-not-cached")
        self.assertEqual(status["expected_model_path"], str(model_dir / "medium.pt"))

    def test_auto_uses_an_already_cached_local_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            cached_model_path("medium", model_dir).touch()
            with patch("tools.film_matinee_asr.importlib.util.find_spec", return_value=object()):
                status = resolve_asr_backend("auto", "medium", model_dir=model_dir)
        self.assertTrue(status["active"])
        self.assertEqual(status["backend"], "local")
        self.assertFalse(status["download_allowed"])

    def test_auto_local_loads_the_cached_path_not_a_downloadable_model_name(self) -> None:
        calls = []

        class FakeModel:
            def transcribe(self, *_args, **_kwargs):
                return {"segments": [{"start": 0, "end": 1, "text": "Hello"}]}

        fake_whisper = types.ModuleType("whisper")
        fake_whisper.load_model = lambda name, **kwargs: calls.append((name, kwargs)) or FakeModel()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = cached_model_path("medium", root)
            model.touch()
            audio = root / "audio.mp3"
            audio.touch()
            with patch.dict("sys.modules", {"whisper": fake_whisper}):
                with patch("tools.film_matinee_asr._LOCAL_MODELS", {}):
                    segments = transcribe_local(
                        audio,
                        model="medium",
                        model_dir=root,
                        download_allowed=False,
                    )
        self.assertEqual(calls[0][0], str(model))
        self.assertEqual(segments[0].text, "Hello")

    def test_response_parser_keeps_timestamps_and_filters_likely_silence(self) -> None:
        segments = segments_from_response({"segments": [
            {
                "start": 1.0,
                "end": 2.5,
                "text": "  Real dialogue  ",
                "avg_logprob": -0.3,
                "no_speech_prob": 0.1,
            },
            {
                "start": 3.0,
                "end": 5.0,
                "text": "hallucinated music words",
                "avg_logprob": -1.4,
                "no_speech_prob": 0.95,
            },
        ]}, offset=10.0)
        self.assertEqual(len(segments), 1)
        self.assertEqual((segments[0].start, segments[0].end), (11.0, 12.5))
        self.assertEqual(segments[0].text, "Real dialogue")

    def test_audio_transcript_stays_separate_from_ocr_in_sidecar(self) -> None:
        subtitles = [Cue("", 10.0, 12.0, "字幕文本", source="burned-subtitle-ocr", confidence=0.75)]
        asr = [Cue("", 10.1, 12.1, "Spoken words", source="audio-asr")]
        reindex_cues(subtitles)
        reindex_cues(asr)
        sidecar = make_sidecar(
            subtitles,
            asr,
            10.0,
            13.0,
            {"backend": "local", "model": "medium"},
        )
        self.assertIn("[subtitles 0:10-0:13 source=burned-subtitle-ocr]", sidecar)
        self.assertIn("S01_100 0:10-0:12 [OCR 0.75]: 字幕文本", sidecar)
        self.assertIn("[audio-transcript 0:10-0:13 source=audio-asr backend=local model=medium]", sidecar)
        self.assertIn("A01_101 0:10-0:12: Spoken words", sidecar)

    def test_asr_srt_preserves_absolute_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audio.asr.srt"
            write_srt([ASRSegment(3601.25, 3603.5, "Hello")], path)
            text = path.read_text("utf-8")
        self.assertIn("01:00:01,250 --> 01:00:03,500", text)


if __name__ == "__main__":
    unittest.main()
