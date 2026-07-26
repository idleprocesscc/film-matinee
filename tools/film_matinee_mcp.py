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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

try:
    from film_matinee_source import (
        DEFAULT_SUBTITLE_LANGUAGES,
        default_out_dir as _default_source_out_dir,
        is_url as _is_url_source,
    )
except ImportError:  # Imported as tools.film_matinee_mcp in tests.
    from .film_matinee_source import (
        DEFAULT_SUBTITLE_LANGUAGES,
        default_out_dir as _default_source_out_dir,
        is_url as _is_url_source,
    )

try:
    from film_matinee_cache import (
        DEFAULT_MAX_AGE_HOURS,
        cache_status as _cache_status,
        cleanup_expired as _cleanup_expired_cache,
        touch_cache as _touch_url_cache,
    )
except ImportError:  # Imported as tools.film_matinee_mcp in tests.
    from .film_matinee_cache import (
        DEFAULT_MAX_AGE_HOURS,
        cache_status as _cache_status,
        cleanup_expired as _cleanup_expired_cache,
        touch_cache as _touch_url_cache,
    )


mcp = FastMCP(
    "film-matinee",
    instructions=(
        "Read generated film-matinee sheets linearly. Prefer film_next for "
        "the normal viewing flow; use film_locate only as a fallback when the "
        "user mentions a timecode, subtitle, or remembered event. Add notes "
        "with film_note when a chunk deserves a durable comment for the user. "
        "Use film_generate when the user has a local video/subtitle that has "
        "not been converted into sheets yet. Use film_open for a URL or when "
        "subtitle discovery and source preparation should be automatic. Use "
        "film_refine_chunk only after a known-important timestamp was missed; "
        "it is a repair lens, not the normal viewing flow. "
        "Use film_focus_range when a specific short span deserves denser visual "
        "inspection without changing the linear cursor or canonical chunks. "
        "URL-downloaded source media expires after 24 hours without viewing activity; sheets, subtitles, "
        "progress, and notes remain available."
    ),
)

_cursors: dict[str, int] = {}
_jobs: dict[str, subprocess.Popen[str]] = {}
_guide_shown: set[str] = set()  # manifest paths that already showed viewing guide this session


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


def _source_script() -> Path:
    return Path(__file__).resolve().parent / "film_matinee_source.py"


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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
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
        "--burned-subtitles", burned_subtitles,
        "--ocr-fps", str(float(ocr_fps)),
        "--ocr-crop-ratio", str(float(ocr_crop_ratio)),
        "--ocr-width", str(int(ocr_width)),
        "--audio-transcript", audio_transcript,
        "--asr-model", asr_model,
        "--asr-language", asr_language,
        "--asr-device", asr_device,
        "--asr-context-sec", str(float(asr_context_sec)),
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
    if allow_small_video:
        cmd.append("--allow-small-video")
    if ffmpeg_hwaccel and ffmpeg_hwaccel.lower() not in {"none", "off", "false", "0"}:
        cmd.extend(["--ffmpeg-hwaccel", ffmpeg_hwaccel])
    if ffmpeg_hwaccel_device:
        cmd.extend(["--ffmpeg-hwaccel-device", ffmpeg_hwaccel_device])

    return cmd, out, out / "manifest.json", _log_path(out)


def _build_open_command(
    source: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    max_height: int = 720,
    cookies_from_browser: str = "",
    refresh_source: bool = False,
    extract_embedded_subs: bool = True,
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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
) -> tuple[list[str], Path, Path, Path]:
    source = str(source or "").strip()
    if not source:
        raise ValueError("source is required")
    if not _is_url_source(source):
        local = Path(source).expanduser().resolve()
        if not local.exists():
            raise FileNotFoundError(f"video not found: {local}")
        source = str(local)
    subtitle = Path(subtitle_path).expanduser().resolve() if subtitle_path else None
    if subtitle is not None and not subtitle.exists():
        raise FileNotFoundError(f"subtitle not found: {subtitle}")
    out = Path(out_dir).expanduser().resolve() if out_dir else _default_source_out_dir(source).resolve()
    out.mkdir(parents=True, exist_ok=True)
    layout = _normalize_layout(layout)
    if max_height < 0:
        raise ValueError("max_height must be 0 or greater")

    cmd = [
        sys.executable,
        str(_source_script()),
        "--source", source,
        "--out-dir", str(out),
        "--subtitle-languages", subtitle_languages,
        "--max-height", str(int(max_height)),
        "--layout", layout,
        "--target-keyframes", str(int(target_keyframes)),
        "--max-sheets", str(int(max_sheets)),
        "--max-sheet-sec", str(float(max_sheet_sec)),
        "--sample-step-sec", str(float(sample_step_sec)),
        "--subtitle-style-exclude", subtitle_style_exclude,
        "--burned-subtitles", burned_subtitles,
        "--ocr-fps", str(float(ocr_fps)),
        "--ocr-crop-ratio", str(float(ocr_crop_ratio)),
        "--ocr-width", str(int(ocr_width)),
        "--audio-transcript", audio_transcript,
        "--asr-model", asr_model,
        "--asr-language", asr_language,
        "--asr-device", asr_device,
        "--asr-context-sec", str(float(asr_context_sec)),
    ]
    if subtitle:
        cmd.extend(["--subtitle", str(subtitle)])
    if title:
        cmd.extend(["--title", title])
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    if refresh_source:
        cmd.append("--refresh-source")
    if not extract_embedded_subs:
        cmd.append("--no-extract-embedded-subs")
    if start_time:
        seconds = _parse_timecode(start_time)
        if seconds is None:
            raise ValueError(f"bad start_time: {start_time}")
        cmd.extend(["--start-time", str(seconds)])
    if end_time:
        seconds = _parse_timecode(end_time)
        if seconds is None:
            raise ValueError(f"bad end_time: {end_time}")
        cmd.extend(["--end-time", str(seconds)])
    if subtitle_offset_sec:
        cmd.extend(["--subtitle-offset-sec", str(float(subtitle_offset_sec))])
    if subtitle_style_include:
        cmd.extend(["--subtitle-style-include", subtitle_style_include])
    if allow_small_video:
        cmd.append("--allow-small-video")
    if ffmpeg_hwaccel and ffmpeg_hwaccel.lower() not in {"none", "off", "false", "0"}:
        cmd.extend(["--ffmpeg-hwaccel", ffmpeg_hwaccel])
    if ffmpeg_hwaccel_device:
        cmd.extend(["--ffmpeg-hwaccel-device", ffmpeg_hwaccel_device])
    return cmd, out, out / "manifest.json", _log_path(out)


def _start_background_job(
    cmd: list[str],
    out: Path,
    manifest: Path,
    log: Path,
    *,
    source: str = "",
    phase: str = "generating",
) -> str:
    job_key = str(out)
    existing = _jobs.get(job_key)
    if existing and existing.poll() is None:
        return f"already running pid={existing.pid}\nout_dir: {out}\nmanifest: {manifest}\nlog: {log}"
    saved_job = _read_job(out)
    if (
        str(saved_job.get("status") or "").lower() in {"running", "running-untracked"}
        and _pid_is_running(saved_job.get("pid"))
    ):
        return (
            f"already running pid={saved_job.get('pid')}\n"
            f"phase: {saved_job.get('phase', 'running')}\n"
            f"out_dir: {out}\nmanifest: {manifest}\nlog: {log}"
        )

    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)
    log.chmod(0o600)
    log_handle = open(str(log), "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()),
            start_new_session=True,
        )
    finally:
        log_handle.close()
    _jobs[job_key] = proc
    _write_job(out, {
        "status": "running",
        "phase": phase,
        "pid": proc.pid,
        "started_at": _now(),
        "source": source or None,
        "command": cmd,
        "manifest": str(manifest),
        "log": str(log),
    })
    return "\n".join([
        f"started pid={proc.pid}",
        f"phase: {phase}",
        f"out_dir: {out}",
        f"manifest: {manifest}",
        f"log: {log}",
        "Call film_generate_status(out_dir) to monitor progress.",
    ])


def _parse_pin_time_list(value: str) -> list[float]:
    times: list[float] = []
    for part in str(value or "").split(","):
        if not part.strip():
            continue
        seconds = _parse_timecode(part.strip())
        if seconds is None:
            raise ValueError(f"bad pin time: {part.strip()}")
        times.append(round(seconds, 3))
    if not times:
        raise ValueError("pin_times must contain at least one timestamp")
    return sorted(set(times))


def _build_refine_command(
    manifest_path: str,
    chunk_index: int,
    pin_times: str,
) -> tuple[list[str], Path, Path, Path]:
    manifest_file, manifest = _load_manifest(manifest_path)
    sheet = _sheet_by_index(manifest, int(chunk_index))
    video = Path(str(manifest.get("video") or "")).expanduser().resolve()
    if not video.exists():
        raise FileNotFoundError(f"source video not found: {video}")
    subtitle_value = manifest.get("subtitle")
    subtitle = Path(str(subtitle_value)).expanduser().resolve() if subtitle_value else None
    if subtitle is not None and not subtitle.exists():
        raise FileNotFoundError(f"subtitle not found: {subtitle}")
    audio_transcript_value = manifest.get("audio_transcript")
    audio_transcript = (
        Path(str(audio_transcript_value)).expanduser().resolve()
        if audio_transcript_value
        else None
    )
    if audio_transcript is not None and not audio_transcript.exists():
        raise FileNotFoundError(f"audio transcript not found: {audio_transcript}")
    pins = _parse_pin_time_list(pin_times)
    start, end = [float(value) for value in sheet.get("time_range", [0, 0])]
    pins = [value for value in pins if start <= value <= end]
    if not pins:
        raise ValueError(
            f"none of the pin times fall inside chunk {chunk_index}: {_fmt_time(start)}-{_fmt_time(end)}"
        )

    options = dict(manifest.get("options") or {})
    skipped = {
        "video", "subtitle", "out_dir", "title", "start", "end", "max_sheets",
        "start_index", "replace_existing_sheet", "pin_times", "min_sheet_sec",
        "max_sheet_sec", "dry_run",
        "audio_transcript",
    }
    boolean_optional = {"auto_pack_rows", "audio_rail"}
    command = [sys.executable, str(_generator_script())]
    for key, value in options.items():
        if key in skipped or value is None:
            continue
        flag = "--" + key.replace("_", "-")
        if key in boolean_optional:
            command.append(flag if bool(value) else "--no-" + key.replace("_", "-"))
        elif isinstance(value, bool):
            if value:
                command.append(flag)
        elif isinstance(value, (str, int, float)):
            command.extend([flag, str(value)])

    duration = max(0.001, end - start)
    command.extend([
        "--video", str(video),
        "--out-dir", str(manifest_file.parent),
        "--title", str(manifest.get("title") or video.stem),
        "--from", str(start),
        "--to", str(end),
        "--min-sheet-sec", str(duration),
        "--max-sheet-sec", str(duration),
        "--max-sheets", "1",
        "--start-index", str(int(chunk_index)),
        "--replace-existing-sheet",
        "--pin-times", ",".join(str(value) for value in pins),
    ])
    if subtitle:
        command.extend(["--subtitle", str(subtitle)])
    if audio_transcript:
        asr_info = dict(manifest.get("audio_transcript_info") or {})
        command.extend([
            "--audio-transcript", "off",
            "--audio-transcript-file", str(audio_transcript),
            "--asr-track-backend", str(asr_info.get("backend") or "existing"),
        ])
    out = manifest_file.parent
    return command, out, manifest_file, _log_path(out)


def _read_job(out_dir: Path) -> dict[str, Any]:
    path = _job_path(out_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _job_in_progress(out_dir: Path) -> bool:
    job = _read_job(out_dir)
    return str(job.get("status") or "").lower() in {"running", "running-untracked"}


def _pid_is_running(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _availability_message(manifest: Path, available: int) -> str:
    job = _read_job(manifest.parent)
    status = str(job.get("status") or "unknown")
    phase = str(job.get("phase") or status)
    if _job_in_progress(manifest.parent):
        return "\n".join([
            "[film-matinee-waiting]",
            f"manifest: {manifest}",
            f"available_sheets: {available}",
            f"phase: {phase}",
            "The next chunk is not generated yet. Call film_generate_status, then film_next again when available_sheets increases.",
            "[/film-matinee-waiting]",
        ])
    return "\n".join([
        "[film-matinee-end]",
        f"manifest: {manifest}",
        f"available_sheets: {available}",
        f"generation_status: {status}",
        "No later generated chunk is available.",
        "[/film-matinee-end]",
    ])


def _write_job(out_dir: Path, data: dict[str, Any]) -> None:
    _job_path(out_dir).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _read_saved_state(manifest: Path) -> dict:
    path = _state_path(manifest)
    if not path.exists():
        return {"cursor": 0}
    try:
        data = json.loads(path.read_text("utf-8"))
        data.setdefault("cursor", 0)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {"cursor": 0}


def _read_saved_cursor(manifest: Path) -> int:
    return max(0, int(_read_saved_state(manifest).get("cursor", 0)))


def _write_saved_cursor(manifest: Path, cursor: int, generation_id: str = "", last_timecode: float | None = None) -> None:
    state = _read_saved_state(manifest)
    state["cursor"] = cursor
    if generation_id:
        state["generation_id"] = generation_id
    if last_timecode is not None:
        state["last_timecode"] = last_timecode
    _state_path(manifest).write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def _find_chunk_for_timecode(sheets: list[dict], timecode: float) -> int:
    """Find the first chunk whose end is strictly after the given timecode.

    This avoids replaying the chunk that just ended when the saved timecode
    equals the previous chunk's end time (e.g. 60s should map to the
    60-120 chunk, not the 0-60 chunk).  Falls back to nearest-distance if
    no chunk's end exceeds the timecode (i.e. past the last chunk).
    """
    for i, sheet in enumerate(sheets):
        start, end = sheet.get("time_range", [0, 0])
        if float(end) > timecode:
            return i
    # Past the end -- return the last chunk
    return max(0, len(sheets) - 1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _touch_cache_safely(out_dir: Path) -> None:
    try:
        _touch_url_cache(out_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[film-matinee] cache access update skipped: {exc}", file=sys.stderr)


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    """Acquire a cross-platform exclusive byte-range/file lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:
        import msvcrt

        fd = lock_path.open("a+b")
        try:
            fd.seek(0)
            if not fd.read(1):
                fd.write(b"\0")
                fd.flush()
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fd.close()
    else:
        fd = lock_path.open("w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()


@contextmanager
def _annotations_lock(manifest: Path):
    """Queue annotation read-modify-write cycles across local processes."""
    with _exclusive_file_lock(_annotations_path(manifest).with_suffix(".lock")):
        yield


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
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _cursor(manifest: Path, manifest_data: dict | None = None) -> int:
    key = str(manifest)
    if key not in _cursors:
        state = _read_saved_state(manifest)
        saved_cursor = max(0, int(state.get("cursor", 0)))
        # Check generation_id: if manifest was regenerated, recover cursor from timecode
        if manifest_data is not None:
            manifest_gen_id = manifest_data.get("generation_id", "")
            saved_gen_id = state.get("generation_id", "")
            if manifest_gen_id and saved_gen_id and manifest_gen_id != saved_gen_id:
                last_tc = state.get("last_timecode")
                sheets = manifest_data.get("sheets", [])
                if last_tc is not None and sheets:
                    saved_cursor = _find_chunk_for_timecode(sheets, float(last_tc))
        _cursors[key] = saved_cursor
    return _cursors[key]


def _set_cursor(manifest: Path, cursor: int, manifest_data: dict | None = None) -> int:
    key = str(manifest)
    cursor = max(0, cursor)
    _cursors[key] = cursor
    gen_id = ""
    last_tc = None
    if manifest_data is not None:
        gen_id = manifest_data.get("generation_id", "")
        sheets = manifest_data.get("sheets", [])
        # Store the timecode of the chunk we just viewed (cursor-1)
        viewed = cursor - 1
        if 0 <= viewed < len(sheets):
            time_range = sheets[viewed].get("time_range", [0, 0])
            last_tc = float(time_range[1])  # end of viewed chunk
    _write_saved_cursor(manifest, cursor, gen_id, last_tc)
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


def _manifest_file(root: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    rel_path = Path(str(rel).replace("\\", "/"))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None
    path = (root / rel_path).resolve()
    root = root.resolve()
    if root not in path.parents and path != root:
        return None
    return path


def _sidecar_text(root: Path, sheet: dict[str, Any]) -> str:
    path = _manifest_file(root, sheet.get("sidecar"))
    if path is None:
        return ""
    if not path.exists():
        return ""
    return path.read_text("utf-8", "ignore")


def _sheet_image(root: Path, sheet: dict[str, Any]) -> Image | None:
    path = _manifest_file(root, sheet.get("sheet"))
    if path is None:
        return None
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
    if manifest.get("subtitle_source") == "burned-subtitle-ocr":
        subtitle_guidance = "The subtitle track was OCR-read from burned-in image text. Treat its OCR provenance and any confidence labels as uncertainty signals, and resolve doubtful wording against the visible frame."
    elif manifest.get("subtitle_source"):
        subtitle_guidance = "Short subtitles under keyframes are only semantic anchors. Read the source-subtitle section below for dialogue and precise text."
    else:
        subtitle_guidance = "No source-subtitle track is available for this span; do not infer exact dialogue from a visual anchor alone."
    guidance = [
        "[viewing-guide]",
        "You are watching a span of film time compressed into a film-matinee sheet, not merely scanning an infographic.",
        "Watch linearly from left to right, top to bottom. Treat each keyframe as a visual anchor in the film's time flow.",
        "Use image content as the primary source: notice character placement, composition, shot scale, movement, light, color, editing rhythm, and sound changes.",
        "Color bands between keyframes represent elapsed visual time, color, and rhythm; longer bands mean more time passed, not necessarily greater importance.",
        "The thin blue audio rail is normalized within this chunk; compare loud/quiet moments inside this chunk, not across the whole film.",
        subtitle_guidance,
    ]
    if manifest.get("audio_transcript"):
        guidance.append(
            "The audio-transcript section is independent ASR evidence, not an authoritative subtitle or speaker label. Compare it with source subtitles or burned-text OCR when they disagree; do not silently merge conflicting words or invent who said them."
        )
    guidance.extend([
        f"Layout: {layout}. Empty visual capacity is meaningful: this chunk did not need every slot.",
        "If you have a worthwhile thought, uncertainty, motif, or user-facing observation, you may think aloud or call film_note; otherwise keep watching without forcing notes.",
        "[/viewing-guide]",
    ])
    return "\n".join(guidance)


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
            next_line = (
                "next: waiting for the next generated sheet"
                if _job_in_progress(manifest_path.parent)
                else "next: end of available generated sheets"
            )

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
    if image is not None:
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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
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
        allow_small_video,
        ffmpeg_hwaccel,
        ffmpeg_hwaccel_device,
        burned_subtitles,
        ocr_fps,
        ocr_crop_ratio,
        ocr_width,
        audio_transcript,
        asr_model,
        asr_language,
        asr_device,
        asr_context_sec,
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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
    background: bool = True,
) -> str:
    """Generate film-matinee sheets from local video/subtitles.

    Defaults to a background full-film run. Use film_generate_status(out_dir)
    until it reports complete, then pass the returned manifest path to
    film_overview / film_start. On macOS, burned_subtitles='auto' uses Apple
    Vision OCR only when no source subtitle is available.
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
        allow_small_video,
        ffmpeg_hwaccel,
        ffmpeg_hwaccel_device,
        burned_subtitles,
        ocr_fps,
        ocr_crop_ratio,
        ocr_width,
        audio_transcript,
        asr_model,
        asr_language,
        asr_device,
        asr_context_sec,
    )
    if background:
        return _start_background_job(cmd, out, manifest, log, phase="generating")

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
    log.chmod(0o600)
    if result.returncode != 0:
        raise RuntimeError(f"film generation failed with code {result.returncode}; see {log}")
    return f"generated\nmanifest: {manifest}\nlog: {log}"


@mcp.tool()
def film_open_command(
    source: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    max_height: int = 720,
    cookies_from_browser: str = "",
    refresh_source: bool = False,
    extract_embedded_subs: bool = True,
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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
) -> str:
    """Return the URL/local-source preparation and generation command without running it."""
    cmd, out, manifest, log = _build_open_command(
        source, subtitle_path, out_dir, title, subtitle_languages, max_height,
        cookies_from_browser, refresh_source, extract_embedded_subs, layout,
        target_keyframes, max_sheets, start_time, end_time, subtitle_offset_sec,
        subtitle_style_include, subtitle_style_exclude, max_sheet_sec,
        sample_step_sec, allow_small_video, ffmpeg_hwaccel, ffmpeg_hwaccel_device,
        burned_subtitles, ocr_fps, ocr_crop_ratio, ocr_width,
        audio_transcript, asr_model, asr_language, asr_device, asr_context_sec,
    )
    return "\n".join([
        f"out_dir: {out}",
        f"manifest: {manifest}",
        f"log: {log}",
        "command:",
        " ".join(shlex.quote(part) for part in cmd),
    ])


@mcp.tool()
def film_open(
    source: str,
    subtitle_path: str = "",
    out_dir: str = "",
    title: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    max_height: int = 720,
    cookies_from_browser: str = "",
    refresh_source: bool = False,
    extract_embedded_subs: bool = True,
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
    allow_small_video: bool = False,
    ffmpeg_hwaccel: str = "none",
    ffmpeg_hwaccel_device: str = "",
    burned_subtitles: str = "auto",
    ocr_fps: float = 2.0,
    ocr_crop_ratio: float = 0.34,
    ocr_width: int = 960,
    audio_transcript: str = "auto",
    asr_model: str = "medium",
    asr_language: str = "",
    asr_device: str = "cpu",
    asr_context_sec: float = 1.5,
    background: bool = True,
) -> str:
    """Prepare a URL or local film, discover subtitles, and generate sheets.

    URL media is downloaded into the film's private cache. Manual source
    captions are preferred over automatic captions. For local containers,
    sidecar and text-based embedded subtitles are discovered automatically.
    On macOS, burned_subtitles='auto' then detects and OCRs burned-in text when
    no source subtitle exists; OCR provenance and confidence stay visible.
    Use film_generate_status on the returned out_dir; film_start can begin as
    soon as the first sheet appears while later sheets continue generating.
    """
    cmd, out, manifest, log = _build_open_command(
        source, subtitle_path, out_dir, title, subtitle_languages, max_height,
        cookies_from_browser, refresh_source, extract_embedded_subs, layout,
        target_keyframes, max_sheets, start_time, end_time, subtitle_offset_sec,
        subtitle_style_include, subtitle_style_exclude, max_sheet_sec,
        sample_step_sec, allow_small_video, ffmpeg_hwaccel, ffmpeg_hwaccel_device,
        burned_subtitles, ocr_fps, ocr_crop_ratio, ocr_width,
        audio_transcript, asr_model, asr_language, asr_device, asr_context_sec,
    )
    if background:
        return _start_background_job(
            cmd, out, manifest, log, source=source, phase="preparing",
        )

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))
    log.write_text((result.stdout or "") + (result.stderr or ""), "utf-8")
    log.chmod(0o600)
    if result.returncode != 0:
        raise RuntimeError(f"film source preparation failed with code {result.returncode}; see {log}")
    return f"prepared and generated\nmanifest: {manifest}\nlog: {log}"


@mcp.tool()
def film_refine_chunk_command(
    manifest_path: str,
    chunk_index: int,
    pin_times: str,
) -> str:
    """Return a command that regenerates one chunk with required visual timestamps."""
    cmd, out, manifest, log = _build_refine_command(manifest_path, chunk_index, pin_times)
    return "\n".join([
        f"out_dir: {out}",
        f"manifest: {manifest}",
        f"log: {log}",
        "command:",
        " ".join(shlex.quote(part) for part in cmd),
    ])


@mcp.tool()
def film_refine_chunk(
    manifest_path: str,
    chunk_index: int,
    pin_times: str,
    background: bool = True,
) -> str:
    """Regenerate one existing chunk while pinning known-important moments.

    This is a repair/detail lens after linear viewing, not the normal selection
    path. Later chunks, cursor state, notes, and the source manifest are kept.
    """
    cmd, out, manifest, log = _build_refine_command(manifest_path, chunk_index, pin_times)
    if background:
        return _start_background_job(
            cmd, out, manifest, log, source=str(manifest), phase="refining",
        )
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))
    log.write_text((result.stdout or "") + (result.stderr or ""), "utf-8")
    log.chmod(0o600)
    if result.returncode != 0:
        raise RuntimeError(f"chunk refinement failed with code {result.returncode}; see {log}")
    return f"refined chunk {int(chunk_index):03d}\nmanifest: {manifest}\nlog: {log}"


_FOCUS_PROFILES = {
    "balanced": {
        "layout": "4x4",
        "target_keyframes": 16,
        "sample_step_sec": 0.75,
        "min_segment_sec": 2.0,
        "max_segment_sec": 12.0,
        "micro_event_sensitivity": 1.45,
        "min_micro_keyframe_gap_sec": 1.5,
        "max_micro_event_keyframes_per_sheet": 8,
        "max_keyframe_gap_sec": 20.0,
        "action_gap_sec": 12.0,
    },
    "dense": {
        "layout": "5x4",
        "target_keyframes": 20,
        "sample_step_sec": 0.5,
        "min_segment_sec": 1.5,
        "max_segment_sec": 8.0,
        "micro_event_sensitivity": 1.3,
        "min_micro_keyframe_gap_sec": 1.0,
        "max_micro_event_keyframes_per_sheet": 10,
        "max_keyframe_gap_sec": 12.0,
        "action_gap_sec": 8.0,
    },
}


def _build_focus_command(
    manifest_path: str,
    start_time: str,
    end_time: str,
    detail: str = "dense",
) -> tuple[list[str], Path, Path, dict[str, Any], float, float]:
    parent_path, parent = _load_manifest(manifest_path)
    detail = str(detail or "dense").strip().lower()
    if detail not in _FOCUS_PROFILES:
        raise ValueError("detail must be balanced or dense")
    start = _parse_timecode(start_time)
    end = _parse_timecode(end_time)
    if start is None or end is None:
        raise ValueError("start_time and end_time must be SS, MM:SS, or HH:MM:SS")
    if start < 0 or end <= start:
        raise ValueError("focus range must have a non-negative start and end after start")
    if end - start > 300:
        raise ValueError("focus range is limited to 5 minutes; split a longer span into adjacent ranges")
    try:
        film_duration = float((parent.get("probe") or {}).get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        film_duration = 0.0
    if film_duration > 0 and end > film_duration + 0.5:
        raise ValueError(f"focus end {_fmt_time(end)} is past the film duration {_fmt_time(film_duration)}")

    video = Path(str(parent.get("video") or "")).expanduser().resolve()
    if not video.exists():
        raise FileNotFoundError(
            f"source video not found: {video}; reopen the URL or restore the local source before focusing"
        )
    subtitle_value = parent.get("subtitle")
    subtitle = Path(str(subtitle_value)).expanduser().resolve() if subtitle_value else None
    if subtitle is not None and not subtitle.exists():
        raise FileNotFoundError(f"subtitle not found: {subtitle}")
    asr_value = parent.get("audio_transcript")
    asr_track = Path(str(asr_value)).expanduser().resolve() if asr_value else None
    if asr_track is not None and not asr_track.exists():
        raise FileNotFoundError(f"audio transcript not found: {asr_track}")

    profile = _FOCUS_PROFILES[detail]
    generation = str(parent.get("generation_id") or "legacy")[:10]
    focus_name = f"v1-{generation}-{round(start * 1000):010d}-{round(end * 1000):010d}-{detail}"
    out = parent_path.parent / "focus" / focus_name
    options = dict(parent.get("options") or {})
    command = [
        sys.executable,
        str(_generator_script()),
        "--video", str(video),
        "--out-dir", str(out),
        "--title", f"{parent.get('title') or video.stem} - focus",
        "--from", str(start),
        "--to", str(end),
        "--min-sheet-sec", str(end - start),
        "--max-sheet-sec", str(end - start),
        "--max-sheets", "1",
        "--layout", str(profile["layout"]),
        "--target-keyframes", str(profile["target_keyframes"]),
        "--sample-step-sec", str(profile["sample_step_sec"]),
        "--min-segment-sec", str(profile["min_segment_sec"]),
        "--max-segment-sec", str(profile["max_segment_sec"]),
        "--micro-event-sensitivity", str(profile["micro_event_sensitivity"]),
        "--min-micro-keyframe-gap-sec", str(profile["min_micro_keyframe_gap_sec"]),
        "--max-micro-event-keyframes-per-sheet", str(profile["max_micro_event_keyframes_per_sheet"]),
        "--max-keyframe-gap-sec", str(profile["max_keyframe_gap_sec"]),
        "--action-gap-sec", str(profile["action_gap_sec"]),
        "--burned-subtitles", "off" if subtitle else str(options.get("burned_subtitles") or "auto"),
        "--ocr-fps", str(options.get("ocr_fps") or 2.0),
        "--ocr-crop-ratio", str(options.get("ocr_crop_ratio") or 0.34),
        "--ocr-width", str(options.get("ocr_width") or 960),
        "--asr-model", str(options.get("asr_model") or "medium"),
        "--asr-language", str(options.get("asr_language") or ""),
        "--asr-device", str(options.get("asr_device") or "cpu"),
        "--asr-context-sec", str(options.get("asr_context_sec") or 1.5),
    ]
    if subtitle:
        command.extend(["--subtitle", str(subtitle)])
    if asr_track:
        asr_info = dict(parent.get("audio_transcript_info") or {})
        command.extend([
            "--audio-transcript", "off",
            "--audio-transcript-file", str(asr_track),
            "--asr-track-backend", str(asr_info.get("backend") or "existing"),
        ])
    else:
        command.extend(["--audio-transcript", str(options.get("audio_transcript") or "auto")])
    hwaccel = str(options.get("ffmpeg_hwaccel") or "none")
    if hwaccel.lower() not in {"none", "off", "false", "0"}:
        command.extend(["--ffmpeg-hwaccel", hwaccel])
    if options.get("ffmpeg_hwaccel_device"):
        command.extend(["--ffmpeg-hwaccel-device", str(options["ffmpeg_hwaccel_device"])])
    return command, out, parent_path, parent, start, end


@mcp.tool(structured_output=False)
def film_focus_range(
    manifest_path: str,
    start_time: str,
    end_time: str,
    detail: str = "dense",
    refresh: bool = False,
) -> list[Any]:
    """Read a short film range as a denser temporary sheet without moving the linear cursor.

    Use this after a specific action, montage, visual transition, or ambiguous
    moment deserves closer inspection. It does not replace canonical chunks or
    notes. Detail is balanced (4x4) or dense (5x4); ranges are capped at 5 min.
    """
    command, out, parent_path, parent, start, end = _build_focus_command(
        manifest_path, start_time, end_time, detail,
    )
    focus_manifest = out / "manifest.json"
    out.mkdir(parents=True, exist_ok=True)
    with _exclusive_file_lock(out / ".focus-generation.lock"):
        if refresh or not focus_manifest.exists():
            result = subprocess.run(command, capture_output=True, text=True, cwd=str(Path.cwd()))
            log = out / "film-matinee-focus.log"
            log.write_text((result.stdout or "") + (result.stderr or ""), "utf-8")
            log.chmod(0o600)
            if result.returncode != 0:
                raise RuntimeError(f"focus generation failed with code {result.returncode}; see {log}")
    focus_path, focus = _load_manifest(str(focus_manifest))
    sheets = focus.get("sheets") or []
    if not sheets:
        raise RuntimeError(f"focus manifest has no sheet: {focus_path}")
    sheet = sheets[0]
    canonical = _sheet_for_time(parent, (start + end) / 2)
    canonical_index = int(canonical.get("index", 0)) if canonical else None
    sidecar = _sidecar_text(focus_path.parent, sheet)
    text = "\n".join([
        "[film-matinee-focus]",
        f"title: {parent.get('title', 'Film')}",
        f"parent_manifest: {parent_path}",
        f"focus_manifest: {focus_path}",
        f"time: {_fmt_time(start)}-{_fmt_time(end)}",
        f"detail: {detail}",
        f"canonical_chunk: {canonical_index if canonical_index is not None else 'none'}",
        "linear_cursor_changed: false",
        "This is a detail lens over the existing linear viewing timeline, not a replacement chunk.",
        "",
        _viewing_guide(focus, sheet),
        "",
        sidecar,
        "[/film-matinee-focus]",
    ])
    image = _sheet_image(focus_path.parent, sheet)
    _touch_cache_safely(parent_path.parent)
    return [text, image] if image is not None else [text]


@mcp.tool()
def film_cache_status(max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> str:
    """List URL source-media caches and their inactivity expiry times.

    Generated sheets, subtitle sidecars, cursor state, and annotations are not
    expiry targets. Local source files are never managed by this cache policy.
    """
    entries = _cache_status(max_age_hours=float(max_age_hours))
    lines = [
        f"policy: delete URL-downloaded source video after {float(max_age_hours):g} hours without film access",
        "preserved: sheets, subtitle sidecars, manifest, progress, annotations",
        f"url_caches: {len(entries)}",
    ]
    for entry in entries:
        state = "expired" if entry["expired"] else "active"
        if not entry["video_exists"]:
            state = "source-media-removed"
        if entry["job_running"]:
            state = "generation-running"
        lines.append(
            f"- {entry.get('title') or Path(entry['path']).name}: "
            f"{_format_bytes(int(entry['video_bytes']))}, {state}, "
            f"idle={float(entry['age_hours']):.2f}h, expires={entry['expires_at']}, "
            f"path={entry['path']}"
        )
    return "\n".join(lines)


@mcp.tool()
def film_cache_cleanup(
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    dry_run: bool = True,
) -> str:
    """Remove expired URL-downloaded source videos; defaults to preview only.

    Set dry_run=false to delete. The operation cannot delete local source
    videos or generated sheets/sidecars/progress/annotations. Active generation
    jobs are skipped. A removed URL source can be downloaded again by film_open.
    """
    result = _cleanup_expired_cache(
        max_age_hours=float(max_age_hours),
        dry_run=bool(dry_run),
    )
    reclaimed_key = "bytes_would_reclaim" if dry_run else "bytes_reclaimed"
    count_key = "files_would_delete" if dry_run else "files_deleted"
    lines = [
        f"dry_run: {bool(dry_run)}",
        f"caches_checked: {result['caches_checked']}",
        f"source_videos: {result[count_key]}",
        f"space: {_format_bytes(int(result[reclaimed_key]))}",
        "preserved: sheets, subtitle sidecars, manifest, progress, annotations",
    ]
    for item in result["deleted"]:
        verb = "would delete" if dry_run else "deleted"
        lines.append(f"- {verb}: {item['video_path']} ({_format_bytes(int(item['bytes']))})")
    for item in result["skipped"]:
        lines.append(f"- skipped: {item['path']} ({item['reason']})")
    return "\n".join(lines)


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
    subtitle_source = ""
    audio_transcript_info: dict[str, Any] = {}
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text("utf-8"))
            title = data.get("title", "")
            sheets = data.get("sheets", [])
            subtitle_source = str(data.get("subtitle_source") or "")
            audio_transcript_info = dict(data.get("audio_transcript_info") or {})
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
        f"phase: {job.get('phase')}" if job.get("phase") else "",
        f"source: {job.get('source')}" if job.get("source") else "",
        f"title: {title}" if title else "",
        f"sheets: {len(sheets)}",
        f"available_sheets: {len(sheets)}",
        f"subtitle_source: {subtitle_source}" if subtitle_source else "",
        (
            f"audio_transcript: {audio_transcript_info.get('backend') or 'none'} "
            f"model={audio_transcript_info.get('model') or 'unknown'} "
            f"cues={audio_transcript_info.get('cue_count', 0)}"
            if audio_transcript_info else ""
        ),
    ]
    if job.get("video_path"):
        lines.append(f"video_path: {job['video_path']}")
    if job.get("subtitle_path"):
        lines.append(f"subtitle_path: {job['subtitle_path']}")
    if job.get("source_reused") is not None:
        lines.append(f"source_reused: {bool(job['source_reused'])}")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
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
    _touch_cache_safely(path.parent)
    source_info = manifest.get("source_info") or {}
    lines = [
        f"title: {manifest.get('title', 'Film')}",
        f"manifest: {path}",
        f"source_kind: {source_info.get('kind')}" if source_info.get("kind") else "",
        f"source_url: {source_info.get('webpage_url')}" if source_info.get("webpage_url") else "",
        f"subtitle_kind: {source_info.get('subtitle_kind')}" if source_info.get("subtitle_kind") else "",
        f"subtitle_language: {source_info.get('subtitle_language')}" if source_info.get("subtitle_language") else "",
        f"subtitle_source: {manifest.get('subtitle_source')}" if manifest.get("subtitle_source") else "",
        (
            f"audio_transcript: {(manifest.get('audio_transcript_info') or {}).get('backend') or 'none'} "
            f"model={(manifest.get('audio_transcript_info') or {}).get('model') or 'unknown'} "
            f"cues={(manifest.get('audio_transcript_info') or {}).get('cue_count', 0)}"
            if manifest.get("audio_transcript_info") else ""
        ),
        f"chunks: {len(manifest.get('sheets', []))}",
        f"cursor: {_cursor(path, manifest)}",
    ]
    for sheet in manifest.get("sheets", []):
        start, end = sheet.get("time_range", [0, 0])
        lines.append(
            f"{int(sheet.get('index', 0)):03d} "
            f"{_fmt_time(float(start))}-{_fmt_time(float(end))} "
            f"k={len(sheet.get('keyframes', []))} "
            f"subs={sheet.get('subtitle_count', 0)}"
            f" asr={sheet.get('audio_transcript_count', 0)}"
        )
    return "\n".join(line for line in lines if line)


@mcp.tool(structured_output=False)
def film_start(manifest_path: str, start_index: int = 0) -> list[Any]:
    """Set the reading cursor and return the first chunk to read."""
    path, manifest = _load_manifest(manifest_path)
    _touch_cache_safely(path.parent)
    sheets = manifest.get("sheets", [])
    if not sheets:
        return [_availability_message(path, 0)]
    start_index = max(0, min(int(start_index), len(sheets) - 1))
    _set_cursor(path, start_index + 1, manifest)
    key = str(path)
    include_guide = key not in _guide_shown
    _guide_shown.add(key)
    return _chunk_response(path, manifest, sheets[start_index], cursor_after=start_index + 1, include_guide=include_guide)


@mcp.tool(structured_output=False)
def film_next(manifest_path: str) -> list[Any]:
    """Read the chunk at the current cursor, then advance the cursor."""
    path, manifest = _load_manifest(manifest_path)
    _touch_cache_safely(path.parent)
    sheets = manifest.get("sheets", [])
    if not sheets:
        return [_availability_message(path, 0)]
    cursor = _cursor(path, manifest)
    if cursor >= len(sheets):
        return [_availability_message(path, len(sheets))]
    _set_cursor(path, cursor + 1, manifest)
    key = str(path)
    include_guide = key not in _guide_shown
    _guide_shown.add(key)
    return _chunk_response(path, manifest, sheets[cursor], cursor_after=cursor + 1, include_guide=include_guide)


@mcp.tool(structured_output=False)
def film_chunk(manifest_path: str, index: int, advance_cursor: bool = False) -> list[Any]:
    """Read one explicit chunk by index."""
    path, manifest = _load_manifest(manifest_path)
    _touch_cache_safely(path.parent)
    sheet = _sheet_by_index(manifest, int(index))
    if advance_cursor:
        sheets = manifest.get("sheets", [])
        position = sheets.index(sheet)
        _set_cursor(path, position + 1, manifest)
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
        _set_cursor(path, sheets.index(first), manifest)

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
    with _annotations_lock(path):
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
    with _annotations_lock(path):
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
