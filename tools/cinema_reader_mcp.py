#!/usr/bin/env python3
"""MCP reader for generated film-matinee sheets.

This server lets an AI read a film one chunk at a time:

  1. film_start(manifest_path)
  2. film_next(manifest_path)
  3. film_next(manifest_path)

Each chunk returns a compact text packet plus the corresponding sheet image.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image


mcp = FastMCP(
    "film-matinee",
    instructions=(
        "Read generated film-matinee sheets linearly. Prefer film_next for "
        "the normal viewing flow; use film_locate only as a fallback when the "
        "user mentions a timecode, subtitle, or remembered event. Add notes "
        "with film_note when a chunk deserves a durable comment for the user."
    ),
)

_cursors: dict[str, int] = {}


def _manifest_path(manifest_path: str) -> Path:
    path = Path(manifest_path).expanduser().resolve()
    if path.is_dir():
        path = path / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    return path


def _load_manifest(manifest_path: str) -> tuple[Path, dict[str, Any]]:
    path = _manifest_path(manifest_path)
    data = json.loads(path.read_text("utf-8"))
    data.setdefault("sheets", [])
    data["sheets"] = sorted(data["sheets"], key=lambda item: int(item.get("index", 0)))
    return path, data


def _state_path(manifest: Path) -> Path:
    return manifest.parent / ".film-matinee-state.json"


def _annotations_path(manifest: Path) -> Path:
    return manifest.parent / "annotations.json"


def _read_saved_cursor(manifest: Path) -> int:
    path = _state_path(manifest)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text("utf-8"))
        return max(0, int(data.get("cursor", 0)))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_saved_cursor(manifest: Path, cursor: int) -> None:
    _state_path(manifest).write_text(json.dumps({"cursor": cursor}, ensure_ascii=False, indent=2), "utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_annotations(manifest: Path) -> dict[str, Any]:
    path = _annotations_path(manifest)
    if not path.exists():
        return {"version": 1, "annotations": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError as exc:
        raise RuntimeError(f"could not read annotations: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"annotations file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"annotations file must contain an object: {path}")
    data.setdefault("version", 1)
    data.setdefault("annotations", [])
    if not isinstance(data["annotations"], list):
        raise ValueError(f"annotations must be a list: {path}")
    return data


def _write_annotations(manifest: Path, data: dict[str, Any]) -> None:
    path = _annotations_path(manifest)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _cursor(manifest: Path) -> int:
    key = str(manifest)
    if key not in _cursors:
        _cursors[key] = _read_saved_cursor(manifest)
    return _cursors[key]


def _set_cursor(manifest: Path, cursor: int) -> int:
    key = str(manifest)
    cursor = max(0, cursor)
    _cursors[key] = cursor
    _write_saved_cursor(manifest, cursor)
    return cursor


def _fmt_time(seconds: float) -> str:
    total = max(0, int(round(seconds or 0)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_timecode(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return None


def _sheet_by_index(manifest: dict[str, Any], index: int) -> dict[str, Any]:
    sheets = manifest.get("sheets", [])
    if not sheets:
        raise ValueError("manifest has no sheets")
    for sheet in sheets:
        if int(sheet.get("index", -1)) == index:
            return sheet
    raise IndexError(f"sheet index not found: {index}")


def _sheet_for_time(manifest: dict[str, Any], seconds: float) -> dict[str, Any] | None:
    for sheet in manifest.get("sheets", []):
        start, end = sheet.get("time_range", [0, 0])
        if float(start) <= seconds <= float(end):
            return sheet
    return None


def _sidecar_text(root: Path, sheet: dict[str, Any]) -> str:
    rel = sheet.get("sidecar")
    if not rel:
        return ""
    path = root / rel
    if not path.exists():
        return ""
    return path.read_text("utf-8", "ignore")


def _sheet_image(root: Path, sheet: dict[str, Any]) -> Image | None:
    rel = sheet.get("sheet")
    if not rel:
        return None
    path = root / rel
    if not path.exists():
        return None
    return Image(path=path)


def _viewing_guide(manifest: dict[str, Any], sheet: dict[str, Any]) -> str:
    options = manifest.get("options", {})
    layout = options.get("layout")
    if not layout:
        columns = options.get("keyframes_per_row", "?")
        max_frames = options.get("max_keyframes", "?")
        layout = f"{columns} columns / up to {max_frames} keyframes"
    return "\n".join([
        "[viewing-guide]",
        "Use the attached sheet as the primary visual source; do not replace it with a prose-only summary.",
        f"layout: {layout}. Empty visual capacity is meaningful: this chunk did not need every slot.",
        "read order: left to right, top to bottom. K labels are keyframes; short text under a frame is only a subtitle anchor.",
        "color bands: each band compresses the elapsed time between adjacent keyframes; longer bands mean more time passed, not bigger importance.",
        "subtitle markers: small pale bars below rows show where sidecar subtitles occur in that row's time span.",
        "audio rail: thin blue waveform below rows is normalized within the chunk; compare loud/quiet moments inside this chunk, not across the whole film.",
        "sidecar: full dialogue/subtitle text for this chunk follows below; trust sidecar text over tiny rendered anchors.",
        "notes: if you notice a durable interpretation, question, motif, or user-facing comment, call film_note for this chunk/time.",
        "[/viewing-guide]",
    ])


def _notes_for_chunk(manifest_path: Path, chunk_index: int) -> list[dict[str, Any]]:
    annotations = _read_annotations(manifest_path)
    return [
        note for note in annotations.get("annotations", [])
        if int(note.get("chunk_index", -1)) == chunk_index
    ]


def _notes_text(manifest_path: Path, chunk_index: int) -> str:
    notes = _notes_for_chunk(manifest_path, chunk_index)
    if not notes:
        return "[notes]\n[/notes]"
    lines = ["[notes]"]
    for note in notes:
        timecode = note.get("timecode") or ""
        header = f"{note.get('id')} {timecode} {note.get('kind', 'note')}: {note.get('text', '')}".strip()
        lines.append(header)
        for reply in note.get("replies", []):
            lines.append(f"  reply {reply.get('id')}: {reply.get('text', '')}")
    lines.append("[/notes]")
    return "\n".join(lines)


def _chunk_text(manifest_path: Path, manifest: dict[str, Any], sheet: dict[str, Any], cursor_after: int | None = None) -> str:
    root = manifest_path.parent
    sheets = manifest.get("sheets", [])
    start, end = sheet.get("time_range", [0, 0])
    index = int(sheet.get("index", 0))
    sidecar = _sidecar_text(root, sheet)
    keyframes = []
    for frame in sheet.get("keyframes", []):
        anchor = frame.get("subtitle_anchor") or {}
        anchor_text = anchor.get("text") or ""
        label = f'{frame.get("id", "K")} {_fmt_time(float(frame.get("time", 0)))}'
        if anchor_text:
            label += f' "{anchor_text}"'
        keyframes.append(label)
    next_line = ""
    if cursor_after is not None:
        if cursor_after < len(sheets):
            next_sheet = sheets[cursor_after]
            ns, ne = next_sheet.get("time_range", [0, 0])
            next_line = f"next: chunk {int(next_sheet.get('index', cursor_after)):03d} {_fmt_time(float(ns))}-{_fmt_time(float(ne))}"
        else:
            next_line = "next: end of available generated sheets"

    return "\n".join([
        "[film-matinee-chunk]",
        f"title: {manifest.get('title', 'Film')}",
        f"manifest: {manifest_path}",
        f"chunk: {index:03d}/{max(0, len(sheets) - 1):03d}",
        f"time: {_fmt_time(float(start))}-{_fmt_time(float(end))}",
        f"duration_seconds: {sheet.get('duration')}",
        f"keyframes: {' | '.join(keyframes)}",
        next_line,
        "",
        _viewing_guide(manifest, sheet),
        "",
        _notes_text(manifest_path, index),
        "",
        sidecar,
        "[/film-matinee-chunk]",
    ]).strip()


def _chunk_response(manifest_path: Path, manifest: dict[str, Any], sheet: dict[str, Any], cursor_after: int | None = None) -> list[Any]:
    text = _chunk_text(manifest_path, manifest, sheet, cursor_after)
    image = _sheet_image(manifest_path.parent, sheet)
    if image:
        return [text, image]
    return [text]


@mcp.tool()
def film_overview(manifest_path: str) -> str:
    """Summarize available generated chunks for a film."""
    path, manifest = _load_manifest(manifest_path)
    lines = [
        f"title: {manifest.get('title', 'Film')}",
        f"manifest: {path}",
        f"chunks: {len(manifest.get('sheets', []))}",
        f"cursor: {_cursor(path)}",
    ]
    for sheet in manifest.get("sheets", []):
        start, end = sheet.get("time_range", [0, 0])
        lines.append(
            f"{int(sheet.get('index', 0)):03d} "
            f"{_fmt_time(float(start))}-{_fmt_time(float(end))} "
            f"k={len(sheet.get('keyframes', []))} "
            f"subs={sheet.get('subtitle_count', 0)}"
        )
    return "\n".join(lines)


@mcp.tool()
def film_start(manifest_path: str, start_index: int = 0) -> list[Any]:
    """Set the reading cursor and return the first chunk to read."""
    path, manifest = _load_manifest(manifest_path)
    sheets = manifest.get("sheets", [])
    if not sheets:
        raise ValueError("manifest has no sheets")
    start_index = max(0, min(int(start_index), len(sheets) - 1))
    _set_cursor(path, start_index + 1)
    return _chunk_response(path, manifest, sheets[start_index], cursor_after=start_index + 1)


@mcp.tool()
def film_next(manifest_path: str) -> list[Any]:
    """Read the chunk at the current cursor, then advance the cursor."""
    path, manifest = _load_manifest(manifest_path)
    sheets = manifest.get("sheets", [])
    if not sheets:
        raise ValueError("manifest has no sheets")
    cursor = min(_cursor(path), len(sheets) - 1)
    _set_cursor(path, cursor + 1)
    return _chunk_response(path, manifest, sheets[cursor], cursor_after=cursor + 1)


@mcp.tool()
def film_chunk(manifest_path: str, index: int, advance_cursor: bool = False) -> list[Any]:
    """Read one explicit chunk by index."""
    path, manifest = _load_manifest(manifest_path)
    sheet = _sheet_by_index(manifest, int(index))
    if advance_cursor:
        sheets = manifest.get("sheets", [])
        position = sheets.index(sheet)
        _set_cursor(path, position + 1)
        return _chunk_response(path, manifest, sheet, cursor_after=position + 1)
    return _chunk_response(path, manifest, sheet)


@mcp.tool()
def film_locate(manifest_path: str, timecode: str = "", text: str = "", set_cursor: bool = False) -> str:
    """Locate generated chunks by timecode or subtitle text. Use as fallback, not normal reading flow."""
    path, manifest = _load_manifest(manifest_path)
    matches: list[dict[str, Any]] = []
    seconds = _parse_timecode(timecode)
    if seconds is not None:
        sheet = _sheet_for_time(manifest, seconds)
        if sheet:
            matches.append(sheet)

    query = text.strip()
    if query:
        lowered = query.lower()
        for sheet in manifest.get("sheets", []):
            sidecar = _sidecar_text(path.parent, sheet)
            if lowered in sidecar.lower() and sheet not in matches:
                matches.append(sheet)

    if not matches:
        return "no matching generated chunk"

    if set_cursor:
        sheets = manifest.get("sheets", [])
        first = matches[0]
        _set_cursor(path, sheets.index(first))

    lines = []
    for sheet in matches:
        start, end = sheet.get("time_range", [0, 0])
        lines.append(
            f"{int(sheet.get('index', 0)):03d} "
            f"{_fmt_time(float(start))}-{_fmt_time(float(end))} "
            f"sheet={sheet.get('sheet')} sidecar={sheet.get('sidecar')}"
        )
    return "\n".join(lines)


@mcp.tool()
def film_note(
    manifest_path: str,
    chunk_index: int,
    text: str,
    timecode: str = "",
    kind: str = "observation",
    visibility: str = "user",
    author: str = "ai",
) -> str:
    """Add a durable AI/user-facing note to a chunk. App viewers can render annotations.json."""
    path, manifest = _load_manifest(manifest_path)
    sheet = _sheet_by_index(manifest, int(chunk_index))
    text = text.strip()
    if not text:
        raise ValueError("note text is empty")
    note_id = f"N{uuid.uuid4().hex[:8]}"
    start, end = sheet.get("time_range", [0, 0])
    seconds = _parse_timecode(timecode) if timecode else None
    note = {
        "id": note_id,
        "chunk_index": int(sheet.get("index", chunk_index)),
        "chunk_time_range": [float(start), float(end)],
        "timecode": timecode,
        "time_seconds": seconds,
        "kind": kind,
        "visibility": visibility,
        "author": author,
        "text": text,
        "created_at": _now(),
        "replies": [],
    }
    data = _read_annotations(path)
    data.setdefault("annotations", []).append(note)
    _write_annotations(path, data)
    return f"saved {note_id} to {_annotations_path(path)}"


@mcp.tool()
def film_reply(manifest_path: str, note_id: str, text: str, author: str = "ai") -> str:
    """Attach a reply/chat continuation under an existing film note."""
    path, _manifest = _load_manifest(manifest_path)
    text = text.strip()
    if not text:
        raise ValueError("reply text is empty")
    data = _read_annotations(path)
    for note in data.get("annotations", []):
        if note.get("id") == note_id:
            reply_id = f"R{uuid.uuid4().hex[:8]}"
            note.setdefault("replies", []).append({
                "id": reply_id,
                "author": author,
                "text": text,
                "created_at": _now(),
            })
            _write_annotations(path, data)
            return f"saved {reply_id} under {note_id}"
    raise ValueError(f"note not found: {note_id}")


@mcp.tool()
def film_notes(manifest_path: str, chunk_index: int | None = None) -> str:
    """List saved notes, optionally limited to one chunk."""
    path, _manifest = _load_manifest(manifest_path)
    data = _read_annotations(path)
    notes = data.get("annotations", [])
    if chunk_index is not None:
        notes = [note for note in notes if int(note.get("chunk_index", -1)) == int(chunk_index)]
    if not notes:
        return "no notes"
    lines = [f"annotations: {_annotations_path(path)}"]
    for note in notes:
        timecode = note.get("timecode") or ""
        lines.append(f"{note.get('id')} chunk={note.get('chunk_index')} {timecode} {note.get('kind')}: {note.get('text')}")
        for reply in note.get("replies", []):
            lines.append(f"  {reply.get('id')} {reply.get('author')}: {reply.get('text')}")
    return "\n".join(lines)


# Backward-compatible aliases while the project migrates from "cinema" wording.
@mcp.tool()
def cinema_overview(manifest_path: str) -> str:
    """Deprecated alias for film_overview."""
    return film_overview(manifest_path)


@mcp.tool()
def cinema_start(manifest_path: str, start_index: int = 0) -> list[Any]:
    """Deprecated alias for film_start."""
    return film_start(manifest_path, start_index)


@mcp.tool()
def cinema_next(manifest_path: str) -> list[Any]:
    """Deprecated alias for film_next."""
    return film_next(manifest_path)


@mcp.tool()
def cinema_chunk(manifest_path: str, index: int, advance_cursor: bool = False) -> list[Any]:
    """Deprecated alias for film_chunk."""
    return film_chunk(manifest_path, index, advance_cursor)


@mcp.tool()
def cinema_locate(manifest_path: str, timecode: str = "", text: str = "", set_cursor: bool = False) -> str:
    """Deprecated alias for film_locate."""
    return film_locate(manifest_path, timecode, text, set_cursor)


if __name__ == "__main__":
    mcp.run("stdio")
