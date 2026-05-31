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
import os
import re
import shlex
import subprocess
import sys
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
        "with film_note when a chunk deserves a durable comment for the user. "
        "Use film_generate when the user has a local video/subtitle that has "
        "not been converted into sheets yet."
    ),
)

_cursors: dict[str, int] = {}
_jobs: dict[str, subprocess.Popen[str]] = {}


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


def _generator_script() -> Path:
    return Path(__file__).resolve().parent / "generate_film_matinee_sheets.py"


def _job_path(out_dir: Path) -> Path:
    return out_dir / ".film-matinee-generate.json"


def _log_path(out_dir: Path) -> Path:
    return out_dir / "film-matinee-generate.log"


def _slug(value: str) -> str:
    value = Path(value).stem if value else "film"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return slug or "film"


def _default_out_dir(video_path: Path) -> Path:
    return Path.cwd() / ".film-matinee-cache" / _slug(video_path.stem)


def _normalize_layout(layout: str) -> str:
    layout = str(layout or "4x4").strip().lower()
    if not re.fullmatch(r"\d+x\d+", layout):
        raise ValueError("layout must look like 4x4, 5x4, or 4x3")
    return layout


def _build_generate_command(
    video_path: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    layout: str = "4x4",
    target_keyframes: int = 16,
    max_sheets: int = 0,
    start_time: str = "",
    end_time: str = "",
    subtitle_offset_sec: float = 0.0,
    subtitle_style_include: str = "",
    subtitle_style_exclude: str = "JP|Ruby",
    max_sheet_sec: float = 420.0,
    sample_step_sec: float = 1.0,
) -> tuple[list[str], Path, Path, Path]:
    video = Path(video_path).expanduser().resolve()
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")
    subtitle = Path(subtitle_path).expanduser().resolve() if subtitle_path else None
    if subtitle is not None and not subtitle.exists():
        raise FileNotFoundError(f"subtitle not found: {subtitle}")
    out = Path(out_dir).expanduser().resolve() if out_dir else _default_out_dir(video)
    out.mkdir(parents=True, exist_ok=True)
    layout = _normalize_layout(layout)

    cmd = [
        sys.executable,
        str(_generator_script()),
        "--video", str(video),
        "--out-dir", str(out),
        "--layout", layout,
        "--target-keyframes", str(int(target_keyframes)),
        "--max-sheets", str(int(max_sheets)),
        "--subtitle-style-exclude", subtitle_style_exclude,
        "--max-sheet-sec", str(float(max_sheet_sec)),
        "--sample-step-sec", str(float(sample_step_sec)),
    ]
    if subtitle:
        cmd.extend(["--subtitle", str(subtitle)])
    if title:
        cmd.extend(["--title", title])
    if start_time:
        seconds = _parse_timecode(start_time)
        if seconds is None:
            raise ValueError(f"bad start_time: {start_time}")
        cmd.extend(["--from", str(seconds)])
    if end_time:
        seconds = _parse_timecode(end_time)
        if seconds is None:
            raise ValueError(f"bad end_time: {end_time}")
        cmd.extend(["--to", str(seconds)])
    if subtitle_offset_sec:
        cmd.extend(["--subtitle-offset-sec", str(float(subtitle_offset_sec))])
    if subtitle_style_include:
        cmd.extend(["--subtitle-style-include", subtitle_style_include])

    return cmd, out, out / "manifest.json", _log_path(out)


def _read_job(out_dir: Path) -> dict[str, Any]:
    path = _job_path(out_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_job(out_dir: Path, data: dict[str, Any]) -> None:
    _job_path(out_dir).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


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
        "You are watching a span of film time compressed into a film-matinee sheet, not merely scanning an infographic.",
        "Watch linearly from left to right, top to bottom. Treat each keyframe as a visual anchor in the film's time flow.",
        "Use image content as the primary source: notice character placement, composition, shot scale, movement, light, color, editing rhythm, and sound changes.",
        "Color bands between keyframes represent elapsed visual time, color, and rhythm; longer bands mean more time passed, not necessarily greater importance.",
        "The thin blue audio rail is normalized within this chunk; compare loud/quiet moments inside this chunk, not across the whole film.",
        "Short subtitles under keyframes are only semantic anchors. Read the sidecar subtitles below for dialogue and precise text.",
        f"Layout: {layout}. Empty visual capacity is meaningful: this chunk did not need every slot.",
        "If you have a worthwhile thought, uncertainty, motif, or user-facing observation, you may think aloud or call film_note; otherwise keep watching without forcing notes.",
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


def _chunk_text(
    manifest_path: Path,
    manifest: dict[str, Any],
    sheet: dict[str, Any],
    cursor_after: int | None = None,
    include_guide: bool = True,
) -> str:
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

    lines = [
        "[film-matinee-chunk]",
        f"title: {manifest.get('title', 'Film')}",
        f"manifest: {manifest_path}",
        f"chunk: {index:03d}/{max(0, len(sheets) - 1):03d}",
        f"time: {_fmt_time(float(start))}-{_fmt_time(float(end))}",
        f"duration_seconds: {sheet.get('duration')}",
        f"keyframes: {' | '.join(keyframes)}",
        next_line,
    ]
    if include_guide:
        lines.extend(["", _viewing_guide(manifest, sheet)])
    lines.extend([
        "",
        _notes_text(manifest_path, index),
        "",
        sidecar,
        "[/film-matinee-chunk]",
    ])
    return "\n".join(lines).strip()


def _chunk_response(
    manifest_path: Path,
    manifest: dict[str, Any],
    sheet: dict[str, Any],
    cursor_after: int | None = None,
    include_guide: bool = True,
) -> list[Any]:
    text = _chunk_text(manifest_path, manifest, sheet, cursor_after, include_guide)
    image = _sheet_image(manifest_path.parent, sheet)
    if image:
        return [text, image]
    return [text]


@mcp.tool()
def film_generate_command(
    video_path: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    layout: str = "4x4",
    target_keyframes: int = 16,
    max_sheets: int = 0,
    start_time: str = "",
    end_time: str = "",
    subtitle_offset_sec: float = 0.0,
    subtitle_style_include: str = "",
    subtitle_style_exclude: str = "JP|Ruby",
    max_sheet_sec: float = 420.0,
    sample_step_sec: float = 1.0,
) -> str:
    """Return the generator command for a local film without running it."""
    cmd, out, manifest, log = _build_generate_command(
        video_path,
        subtitle_path,
        out_dir,
        title,
        layout,
        target_keyframes,
        max_sheets,
        start_time,
        end_time,
        subtitle_offset_sec,
        subtitle_style_include,
        subtitle_style_exclude,
        max_sheet_sec,
        sample_step_sec,
    )
    return "\n".join([
        f"out_dir: {out}",
        f"manifest: {manifest}",
        f"log: {log}",
        "command:",
        " ".join(shlex.quote(part) for part in cmd),
    ])


@mcp.tool()
def film_generate(
    video_path: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    layout: str = "4x4",
    target_keyframes: int = 16,
    max_sheets: int = 0,
    start_time: str = "",
    end_time: str = "",
    subtitle_offset_sec: float = 0.0,
    subtitle_style_include: str = "",
    subtitle_style_exclude: str = "JP|Ruby",
    max_sheet_sec: float = 420.0,
    sample_step_sec: float = 1.0,
    background: bool = True,
) -> str:
    """Generate film-matinee sheets from local video/subtitles.

    Defaults to a background full-film run. Use film_generate_status(out_dir)
    until it reports complete, then pass the returned manifest path to
    film_overview / film_start.
    """
    cmd, out, manifest, log = _build_generate_command(
        video_path,
        subtitle_path,
        out_dir,
        title,
        layout,
        target_keyframes,
        max_sheets,
        start_time,
        end_time,
        subtitle_offset_sec,
        subtitle_style_include,
        subtitle_style_exclude,
        max_sheet_sec,
        sample_step_sec,
    )
    job_key = str(out)
    existing = _jobs.get(job_key)
    if existing and existing.poll() is None:
        return f"already running pid={existing.pid}\nout_dir: {out}\nmanifest: {manifest}\nlog: {log}"

    if background:
        log_handle = log.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path.cwd()),
            start_new_session=True,
        )
        log_handle.close()
        _jobs[job_key] = proc
        _write_job(out, {
            "status": "running",
            "pid": proc.pid,
            "started_at": _now(),
            "command": cmd,
            "manifest": str(manifest),
            "log": str(log),
        })
        return "\n".join([
            f"started pid={proc.pid}",
            f"out_dir: {out}",
            f"manifest: {manifest}",
            f"log: {log}",
            "Call film_generate_status(out_dir) to monitor progress.",
        ])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))
    _write_job(out, {
        "status": "complete" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "finished_at": _now(),
        "command": cmd,
        "manifest": str(manifest),
        "log": str(log),
    })
    log.write_text((result.stdout or "") + (result.stderr or ""), "utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"film generation failed with code {result.returncode}; see {log}")
    return f"generated\nmanifest: {manifest}\nlog: {log}"


@mcp.tool()
def film_generate_status(out_dir: str, tail_lines: int = 20) -> str:
    """Check a film_generate job and report manifest/sheet progress."""
    out = Path(out_dir).expanduser().resolve()
    manifest = out / "manifest.json"
    log = _log_path(out)
    job = _read_job(out)
    proc = _jobs.get(str(out))
    if proc is not None:
        code = proc.poll()
        if code is None:
            job["status"] = "running"
            job["pid"] = proc.pid
        else:
            job["status"] = "complete" if code == 0 else "failed"
            job["returncode"] = code
            job.setdefault("finished_at", _now())
            _write_job(out, job)
    elif job.get("status") == "running" and job.get("pid"):
        try:
            os.kill(int(job["pid"]), 0)
            job["status"] = "running-untracked"
        except ProcessLookupError:
            job["status"] = "stopped"
            job.setdefault("finished_at", _now())
            _write_job(out, job)
        except PermissionError:
            job["status"] = "running-untracked"
    elif not job and manifest.exists():
        job["status"] = "manifest-available"

    sheets = []
    title = ""
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text("utf-8"))
            title = data.get("title", "")
            sheets = data.get("sheets", [])
        except (OSError, json.JSONDecodeError):
            pass

    tail = ""
    if log.exists():
        lines = log.read_text("utf-8", "ignore").splitlines()
        tail = "\n".join(lines[-max(0, int(tail_lines)):])

    lines = [
        f"out_dir: {out}",
        f"manifest: {manifest}",
        f"log: {log}",
        f"status: {job.get('status', 'unknown')}",
        f"title: {title}" if title else "",
        f"sheets: {len(sheets)}",
    ]
    if sheets:
        start, end = sheets[-1].get("time_range", [0, 0])
        lines.append(f"latest: {int(sheets[-1].get('index', 0)):03d} {_fmt_time(float(start))}-{_fmt_time(float(end))}")
    if tail:
        lines.extend(["", "[log-tail]", tail, "[/log-tail]"])
    return "\n".join(line for line in lines if line)


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


@mcp.tool(structured_output=False)
def film_start(manifest_path: str, start_index: int = 0) -> list[Any]:
    """Set the reading cursor and return the first chunk to read."""
    path, manifest = _load_manifest(manifest_path)
    sheets = manifest.get("sheets", [])
    if not sheets:
        raise ValueError("manifest has no sheets")
    start_index = max(0, min(int(start_index), len(sheets) - 1))
    _set_cursor(path, start_index + 1)
    return _chunk_response(path, manifest, sheets[start_index], cursor_after=start_index + 1)


@mcp.tool(structured_output=False)
def film_next(manifest_path: str) -> list[Any]:
    """Read the chunk at the current cursor, then advance the cursor."""
    path, manifest = _load_manifest(manifest_path)
    sheets = manifest.get("sheets", [])
    if not sheets:
        raise ValueError("manifest has no sheets")
    cursor = min(_cursor(path), len(sheets) - 1)
    _set_cursor(path, cursor + 1)
    return _chunk_response(path, manifest, sheets[cursor], cursor_after=cursor + 1, include_guide=cursor == 0)


@mcp.tool(structured_output=False)
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


if __name__ == "__main__":
    mcp.run("stdio")
