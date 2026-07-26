from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import film_matinee_mcp as reader


class McpProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        reader._cursors.clear()

    def _manifest(self, root: Path, sheet_count: int = 1) -> Path:
        sheets = []
        for index in range(sheet_count):
            sidecar = root / f"sidecar-{index}.txt"
            sidecar.write_text(f"chunk {index}", "utf-8")
            sheets.append({
                "index": index,
                "time_range": [index * 60, (index + 1) * 60],
                "duration": 60,
                "keyframes": [],
                "sidecar": sidecar.name,
                "sheet": None,
            })
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"title": "Test", "sheets": sheets}), "utf-8")
        return manifest

    def test_film_next_waits_instead_of_repeating_last_available_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            (root / ".film-matinee-generate.json").write_text(
                json.dumps({"status": "running", "phase": "generating"}),
                "utf-8",
            )
            first = reader.film_start(str(manifest), 0)
            waiting = reader.film_next(str(manifest))
            self.assertIn("chunk: 000", first[0])
            self.assertIn("[film-matinee-waiting]", waiting[0])

    def test_film_next_reads_new_sheet_after_manifest_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            (root / ".film-matinee-generate.json").write_text(
                json.dumps({"status": "running", "phase": "generating"}),
                "utf-8",
            )
            reader.film_start(str(manifest), 0)
            self._manifest(root, sheet_count=2)
            next_chunk = reader.film_next(str(manifest))
            self.assertIn("chunk: 001", next_chunk[0])


if __name__ == "__main__":
    unittest.main()
