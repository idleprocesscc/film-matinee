from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.film_matinee_cache import cache_status, cleanup_expired, touch_cache


class CacheExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.default_root = self.root / "default-cache"
        self.default_root.mkdir()
        self.environment = patch.dict(os.environ, {
            "FILM_MATINEE_CONFIG_DIR": str(self.config),
            "FILM_MATINEE_DEFAULT_CACHE_ROOT": str(self.default_root),
        })
        self.environment.start()
        self.now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _cache(self, name: str, *, kind: str = "url", age_hours: float = 25) -> Path:
        out = self.default_root / name
        source = out / "source"
        source.mkdir(parents=True)
        video = source / "video.mp4"
        video.write_bytes(b"video-data")
        record = {
            "version": 1,
            "source": "https://example.com/video.mp4" if kind == "url" else "/Movies/video.mp4",
            "video_path": str(video),
            "subtitle_path": None,
            "title": name,
            "metadata": {"kind": kind},
            "prepared_at": (self.now - timedelta(hours=age_hours)).isoformat(),
        }
        (source / "source.json").write_text(json.dumps(record), "utf-8")
        (out / "manifest.json").write_text("{}", "utf-8")
        (out / "annotations.json").write_text('{"annotations": []}', "utf-8")
        sheets = out / "sheets"
        sheets.mkdir()
        (sheets / "sheet-000.png").write_bytes(b"sheet")
        return out

    def test_cleanup_deletes_only_expired_url_video_and_preserves_records(self) -> None:
        out = self._cache("expired-url")
        result = cleanup_expired(max_age_hours=24, now=self.now)
        self.assertEqual(result["files_deleted"], 1)
        self.assertFalse((out / "source" / "video.mp4").exists())
        self.assertTrue((out / "manifest.json").exists())
        self.assertTrue((out / "annotations.json").exists())
        self.assertTrue((out / "sheets" / "sheet-000.png").exists())
        record = json.loads((out / "source" / "source.json").read_text("utf-8"))
        self.assertIn("source_media_deleted_at", record)

    def test_cleanup_never_deletes_local_source(self) -> None:
        out = self._cache("local", kind="local")
        result = cleanup_expired(max_age_hours=24, now=self.now)
        self.assertEqual(result["files_deleted"], 0)
        self.assertTrue((out / "source" / "video.mp4").exists())

    def test_dry_run_reports_without_deleting(self) -> None:
        out = self._cache("preview")
        result = cleanup_expired(max_age_hours=24, dry_run=True, now=self.now)
        self.assertEqual(result["files_would_delete"], 1)
        self.assertEqual(result["files_deleted"], 0)
        self.assertTrue((out / "source" / "video.mp4").exists())

    def test_touch_extends_expiry_from_last_viewing_activity(self) -> None:
        out = self._cache("active", age_hours=25)
        self.assertTrue(touch_cache(out, when=self.now))
        entries = cache_status(max_age_hours=24, now=self.now)
        entry = next(item for item in entries if item["path"] == str(out.resolve()))
        self.assertEqual(entry["age_hours"], 0.0)
        self.assertFalse(entry["expired"])

    def test_running_generation_is_skipped(self) -> None:
        out = self._cache("running")
        (out / ".film-matinee-generate.json").write_text(json.dumps({
            "status": "running",
            "pid": os.getpid(),
        }), "utf-8")
        result = cleanup_expired(max_age_hours=24, now=self.now)
        self.assertEqual(result["files_deleted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "generation-running")
        self.assertTrue((out / "source" / "video.mp4").exists())


if __name__ == "__main__":
    unittest.main()
