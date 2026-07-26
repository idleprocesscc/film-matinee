#!/usr/bin/env python3
"""Prepare URL or local film sources, then run the sheet generator.

The source layer deliberately stops at producing trustworthy local media paths.
All visual analysis remains in generate_film_matinee_core.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

try:
    from film_matinee_cache import register_cache, touch_cache
except ImportError:  # Imported as tools.film_matinee_source in tests.
    from .film_matinee_cache import register_cache, touch_cache


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}
SUBTITLE_EXTS = {".ass", ".ssa", ".srt", ".vtt"}
TEXT_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}
DEFAULT_SUBTITLE_LANGUAGES = "zh-Hans,zh-Hant,zh-CN,zh-TW,zh.*,en-orig,en.*,ja.*"


@dataclass
class PreparedSource:
    source: str
    video_path: Path
    subtitle_path: Path | None
    title: str
    metadata: dict[str, Any]
    reused: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def public_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return str(value or "")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def slug(value: str) -> str:
    value = Path(value).stem if value else "film"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return cleaned or "film"


def default_out_dir(source: str, cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()) / ".film-matinee-cache"
    if is_url(source):
        parsed = urlparse(source)
        hint = Path(parsed.path.rstrip("/")).name or parsed.netloc
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
        return root / f"{slug(hint)}-{digest}"
    return root / slug(Path(source).expanduser().stem)


def parse_language_preferences(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _language_matches(language: str, preference: str) -> bool:
    language = language.lower().replace("_", "-")
    preference = preference.lower().replace("_", "-")
    if preference.endswith(".*"):
        prefix = preference[:-2]
        return language == prefix or language.startswith(prefix + "-")
    return language == preference


def _language_rank(language: str, preferences: list[str]) -> int:
    for index, preference in enumerate(preferences):
        if _language_matches(language, preference):
            return index
    return len(preferences) + 100


def choose_remote_subtitle(
    info: dict[str, Any],
    preferences: list[str],
) -> tuple[str, str] | None:
    """Choose a manual subtitle before any auto-generated alternative."""
    for kind, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        tracks = info.get(field) or {}
        languages = [
            str(language) for language, formats in tracks.items()
            if formats and str(language).lower() != "live_chat"
        ]
        for preference in preferences:
            matches = [language for language in languages if _language_matches(language, preference)]
            if matches:
                return kind, sorted(matches, key=lambda item: (len(item), item))[0]
        if languages:
            return kind, sorted(languages, key=lambda item: (len(item), item))[0]
    return None


def _run(
    command: list[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=capture, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {detail[-2000:]}")
    return result


def _require(binary: str, install_hint: str) -> None:
    if shutil.which(binary) is None:
        raise RuntimeError(f"{binary} is required. {install_hint}")


def probe_url(source: str, cookies_from_browser: str = "") -> dict[str, Any]:
    _require("yt-dlp", "Install it with: brew install yt-dlp")
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
    ]
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    command.extend(["--", source])
    result = _run(command, capture=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid metadata JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("yt-dlp returned unexpected metadata")
    return data


def _video_candidates(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.glob("video.*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS
    )


def _subtitle_candidates(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.glob("video.*")
        if path.is_file() and path.suffix.lower() in SUBTITLE_EXTS
    )


def _write_source_record(out_dir: Path, prepared: PreparedSource) -> None:
    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "source": prepared.source,
        "video_path": str(prepared.video_path),
        "subtitle_path": str(prepared.subtitle_path) if prepared.subtitle_path else None,
        "title": prepared.title,
        "metadata": prepared.metadata,
        "prepared_at": utc_now(),
    }
    path = source_dir / "source.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def _read_cached_source(out_dir: Path, source: str) -> PreparedSource | None:
    path = out_dir / "source" / "source.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("source") != source:
        return None
    video = Path(str(record.get("video_path") or "")).expanduser()
    subtitle_value = record.get("subtitle_path")
    subtitle = Path(str(subtitle_value)).expanduser() if subtitle_value else None
    if not video.exists() or (subtitle is not None and not subtitle.exists()):
        return None
    return PreparedSource(
        source=source,
        video_path=video.resolve(),
        subtitle_path=subtitle.resolve() if subtitle else None,
        title=str(record.get("title") or video.stem),
        metadata=dict(record.get("metadata") or {}),
        reused=True,
    )


def prepare_url(
    source: str,
    out_dir: Path,
    *,
    subtitle_path: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    max_height: int = 720,
    cookies_from_browser: str = "",
    refresh: bool = False,
    on_phase: Callable[[str, dict[str, Any]], None] | None = None,
) -> PreparedSource:
    if not refresh:
        cached = _read_cached_source(out_dir, source)
        if cached is not None:
            touch_cache(out_dir)
            return cached

    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    if on_phase:
        on_phase("probing", {})
    info = probe_url(source, cookies_from_browser)
    preferences = parse_language_preferences(subtitle_languages)
    explicit_subtitle = Path(subtitle_path).expanduser().resolve() if subtitle_path else None
    if explicit_subtitle is not None and not explicit_subtitle.exists():
        raise FileNotFoundError(f"subtitle not found: {explicit_subtitle}")
    subtitle_choice = None if explicit_subtitle else choose_remote_subtitle(info, preferences)

    if on_phase:
        on_phase("downloading", {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "subtitle": subtitle_choice,
        })

    format_selector = "bv*+ba/b"
    if max_height > 0:
        format_selector = f"bv*[height<={int(max_height)}]+ba/b[height<={int(max_height)}]/b"
    for old_path in source_dir.glob("video.*"):
        if old_path.is_file():
            old_path.unlink()

    command = [
        "yt-dlp",
        "-N", "8",
        "-f", format_selector,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(source_dir / "video.%(ext)s"),
    ]
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if subtitle_choice:
        kind, language = subtitle_choice
        command.append("--write-subs" if kind == "manual" else "--write-auto-subs")
        command.extend([
            "--sub-langs", language,
            "--sub-format", "ass/srt/vtt/best",
        ])
    command.extend(["--", source])
    _run(command)

    videos = _video_candidates(source_dir)
    if not videos:
        raise RuntimeError(f"yt-dlp did not produce a video under {source_dir}")
    subtitles = _subtitle_candidates(source_dir)
    video = videos[0].resolve()
    subtitle = explicit_subtitle or (subtitles[0].resolve() if subtitles else None)
    metadata = {
        "kind": "url",
        "source_id": info.get("id"),
        "webpage_url": public_url(str(info.get("webpage_url") or source)),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "subtitle_kind": "explicit" if explicit_subtitle else (subtitle_choice[0] if subtitle_choice else None),
        "subtitle_language": subtitle_choice[1] if subtitle_choice else None,
        "subtitle_languages_requested": subtitle_languages,
        "max_height": max_height,
    }
    prepared = PreparedSource(
        source=source,
        video_path=video,
        subtitle_path=subtitle,
        title=str(info.get("title") or video.stem),
        metadata=metadata,
    )
    _write_source_record(out_dir, prepared)
    register_cache(out_dir, "url")
    return prepared


def discover_sidecar(video: Path, preferences: list[str]) -> Path | None:
    candidates = [
        path for path in video.parent.iterdir()
        if path.is_file() and path.suffix.lower() in SUBTITLE_EXTS
    ]
    stem_matches = [
        path for path in candidates
        if path.stem == video.stem or path.stem.startswith(video.stem + ".")
        or path.stem.startswith(video.stem + "-")
    ]
    if stem_matches:
        candidates = stem_matches
    elif len(candidates) == 1:
        videos = [
            path for path in video.parent.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTS
        ]
        if len(videos) != 1:
            return None
    else:
        return None

    extension_rank = {".ass": 0, ".ssa": 0, ".srt": 1, ".vtt": 2}

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.stem.lower().replace("_", "-")
        language_rank = len(preferences) + 100
        for index, preference in enumerate(preferences):
            token = preference.lower().replace("_", "-").removesuffix(".*")
            if re.search(rf"(^|[.\-]){re.escape(token)}($|[.\-])", name):
                language_rank = index
                break
        return language_rank, extension_rank.get(path.suffix.lower(), 9), path.name

    return sorted(candidates, key=rank)[0].resolve() if candidates else None


def _probe_subtitle_streams(video: Path) -> list[dict[str, Any]]:
    _require("ffprobe", "Install it with ffmpeg: brew install ffmpeg")
    command = [
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced,hearing_impaired",
        "-of", "json",
        str(video),
    ]
    result = _run(command, capture=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [stream for stream in data.get("streams", []) if isinstance(stream, dict)]


def choose_embedded_subtitle(
    streams: list[dict[str, Any]],
    preferences: list[str],
) -> dict[str, Any] | None:
    text_streams = [
        stream for stream in streams
        if str(stream.get("codec_name") or "").lower() in TEXT_SUBTITLE_CODECS
    ]
    if not text_streams:
        return None

    def rank(stream: dict[str, Any]) -> tuple[int, int, int, int, int]:
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        language = str(tags.get("language") or "und")
        title = str(tags.get("title") or "").lower()
        incomplete_or_commentary = any(
            marker in title for marker in ("commentary", "director", "signs", "songs", "forced")
        )
        return (
            _language_rank(language, preferences),
            1 if incomplete_or_commentary else 0,
            0 if disposition.get("default") else 1,
            1 if disposition.get("forced") else 0,
            int(stream.get("index", 0)),
        )

    return sorted(text_streams, key=rank)[0]


def extract_embedded_subtitle(
    video: Path,
    out_dir: Path,
    preferences: list[str],
) -> tuple[Path | None, dict[str, Any]]:
    stream = choose_embedded_subtitle(_probe_subtitle_streams(video), preferences)
    if stream is None:
        return None, {}
    _require("ffmpeg", "Install it with: brew install ffmpeg")
    tags = stream.get("tags") or {}
    language = str(tags.get("language") or "und")
    codec = str(stream.get("codec_name") or "").lower()
    output_codec = "ass" if codec in {"ass", "ssa"} else "srt"
    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    output = source_dir / f"embedded.{slug(language)}.{output_codec}"
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-i", str(video),
        "-map", f"0:{int(stream['index'])}",
        "-c:s", output_codec,
        str(output),
    ]
    try:
        _run(command)
    except RuntimeError as exc:
        print(f"[film-matinee] embedded subtitle extraction skipped: {exc}", file=sys.stderr)
        return None, {}
    metadata = {
        "subtitle_kind": "embedded",
        "subtitle_language": language,
        "subtitle_title": tags.get("title"),
        "subtitle_stream_index": stream.get("index"),
        "subtitle_codec": codec,
    }
    return output.resolve(), metadata


def prepare_local(
    source: str,
    out_dir: Path,
    *,
    subtitle_path: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    extract_embedded: bool = True,
) -> PreparedSource:
    video = Path(source).expanduser().resolve()
    if not video.exists() or not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    preferences = parse_language_preferences(subtitle_languages)
    subtitle: Path | None = None
    metadata: dict[str, Any] = {"kind": "local"}
    if subtitle_path:
        subtitle = Path(subtitle_path).expanduser().resolve()
        if not subtitle.exists():
            raise FileNotFoundError(f"subtitle not found: {subtitle}")
        metadata["subtitle_kind"] = "explicit"
    else:
        subtitle = discover_sidecar(video, preferences)
        if subtitle is not None:
            metadata["subtitle_kind"] = "sidecar"
        elif extract_embedded:
            subtitle, embedded_metadata = extract_embedded_subtitle(video, out_dir, preferences)
            metadata.update(embedded_metadata)
    prepared = PreparedSource(
        source=source,
        video_path=video,
        subtitle_path=subtitle,
        title=video.stem,
        metadata=metadata,
    )
    _write_source_record(out_dir, prepared)
    return prepared


def prepare_source(
    source: str,
    out_dir: Path,
    *,
    subtitle_path: str = "",
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES,
    max_height: int = 720,
    cookies_from_browser: str = "",
    refresh: bool = False,
    extract_embedded: bool = True,
    on_phase: Callable[[str, dict[str, Any]], None] | None = None,
) -> PreparedSource:
    if is_url(source):
        return prepare_url(
            source,
            out_dir,
            subtitle_path=subtitle_path,
            subtitle_languages=subtitle_languages,
            max_height=max_height,
            cookies_from_browser=cookies_from_browser,
            refresh=refresh,
            on_phase=on_phase,
        )
    return prepare_local(
        source,
        out_dir,
        subtitle_path=subtitle_path,
        subtitle_languages=subtitle_languages,
        extract_embedded=extract_embedded,
    )


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def attach_source_metadata(manifest_path: Path, prepared: PreparedSource) -> None:
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not attach source metadata to {manifest_path}") from exc
    manifest["source_info"] = {
        **prepared.metadata,
        "prepared_video": str(prepared.video_path),
        "prepared_subtitle": str(prepared.subtitle_path) if prepared.subtitle_path else None,
        "source_reused": prepared.reused,
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(manifest_path)


def build_generator_command(options: argparse.Namespace, prepared: PreparedSource) -> list[str]:
    generator = Path(__file__).resolve().parent / "generate_film_matinee_sheets.py"
    command = [
        sys.executable,
        str(generator),
        "--video", str(prepared.video_path),
        "--out-dir", str(options.out_dir),
        "--title", options.title or prepared.title,
        "--layout", options.layout,
        "--target-keyframes", str(options.target_keyframes),
        "--max-sheets", str(options.max_sheets),
        "--max-sheet-sec", str(options.max_sheet_sec),
        "--sample-step-sec", str(options.sample_step_sec),
        "--subtitle-style-exclude", options.subtitle_style_exclude,
        "--burned-subtitles", options.burned_subtitles,
        "--ocr-fps", str(options.ocr_fps),
        "--ocr-crop-ratio", str(options.ocr_crop_ratio),
        "--ocr-width", str(options.ocr_width),
        "--audio-transcript", options.audio_transcript,
        "--asr-model", options.asr_model,
        "--asr-language", options.asr_language,
        "--asr-device", options.asr_device,
        "--asr-context-sec", str(options.asr_context_sec),
    ]
    if prepared.subtitle_path:
        command.extend(["--subtitle", str(prepared.subtitle_path)])
    if options.start_time:
        command.extend(["--from", str(parse_timecode(options.start_time))])
    if options.end_time:
        command.extend(["--to", str(parse_timecode(options.end_time))])
    if options.subtitle_offset_sec:
        command.extend(["--subtitle-offset-sec", str(options.subtitle_offset_sec)])
    if options.subtitle_style_include:
        command.extend(["--subtitle-style-include", options.subtitle_style_include])
    if options.allow_small_video:
        command.append("--allow-small-video")
    if options.ffmpeg_hwaccel.lower() not in {"", "none", "off", "false", "0"}:
        command.extend(["--ffmpeg-hwaccel", options.ffmpeg_hwaccel])
    if options.ffmpeg_hwaccel_device:
        command.extend(["--ffmpeg-hwaccel-device", options.ffmpeg_hwaccel_device])
    return command


def parse_timecode(value: str) -> float:
    value = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if not parts or not all(re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        raise ValueError(f"bad timecode: {value}")
    values = [float(part) for part in parts]
    if len(values) == 2:
        return values[0] * 60 + values[1]
    if len(values) == 3:
        return values[0] * 3600 + values[1] * 60 + values[2]
    raise ValueError(f"bad timecode: {value}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a film source and generate film-matinee sheets.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--subtitle-languages", default=DEFAULT_SUBTITLE_LANGUAGES)
    parser.add_argument("--max-height", type=int, default=720)
    parser.add_argument("--cookies-from-browser", default="")
    parser.add_argument("--refresh-source", action="store_true")
    parser.add_argument("--extract-embedded-subs", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--layout", default="4x4")
    parser.add_argument("--target-keyframes", type=int, default=16)
    parser.add_argument("--max-sheets", type=int, default=0)
    parser.add_argument("--start-time", default="")
    parser.add_argument("--end-time", default="")
    parser.add_argument("--subtitle-offset-sec", type=float, default=0.0)
    parser.add_argument("--subtitle-style-include", default="")
    parser.add_argument("--subtitle-style-exclude", default="JP|Ruby")
    parser.add_argument("--max-sheet-sec", type=float, default=420.0)
    parser.add_argument("--sample-step-sec", type=float, default=1.0)
    parser.add_argument("--burned-subtitles", choices=("off", "auto", "ocr"), default="auto")
    parser.add_argument("--ocr-fps", type=float, default=2.0)
    parser.add_argument("--ocr-crop-ratio", type=float, default=0.34)
    parser.add_argument("--ocr-width", type=int, default=960)
    parser.add_argument("--audio-transcript", choices=("off", "auto", "local", "groq", "openai"), default="auto")
    parser.add_argument("--asr-model", default="medium")
    parser.add_argument("--asr-language", default="")
    parser.add_argument("--asr-device", default="cpu")
    parser.add_argument("--asr-context-sec", type=float, default=1.5)
    parser.add_argument("--allow-small-video", action="store_true")
    parser.add_argument("--ffmpeg-hwaccel", default="none")
    parser.add_argument("--ffmpeg-hwaccel-device", default="")
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    options.out_dir = options.out_dir.expanduser().resolve()
    options.out_dir.mkdir(parents=True, exist_ok=True)
    job_path = options.out_dir / ".film-matinee-generate.json"
    job: dict[str, Any] = {
        "status": "running",
        "phase": "preparing",
        "pid": os.getpid(),
        "source": options.source,
        "started_at": utc_now(),
        "manifest": str(options.out_dir / "manifest.json"),
        "log": str(options.out_dir / "film-matinee-generate.log"),
    }
    _atomic_json(job_path, job)

    def phase(name: str, details: dict[str, Any]) -> None:
        job["phase"] = name
        job.update({key: value for key, value in details.items() if value is not None})
        _atomic_json(job_path, job)
        print(f"[film-matinee] source phase={name}", flush=True)

    try:
        prepared = prepare_source(
            options.source,
            options.out_dir,
            subtitle_path=options.subtitle,
            subtitle_languages=options.subtitle_languages,
            max_height=options.max_height,
            cookies_from_browser=options.cookies_from_browser,
            refresh=options.refresh_source,
            extract_embedded=options.extract_embedded_subs,
            on_phase=phase,
        )
        job.update({
            "video_path": str(prepared.video_path),
            "subtitle_path": str(prepared.subtitle_path) if prepared.subtitle_path else None,
            "title": options.title or prepared.title,
            "source_reused": prepared.reused,
        })
        if options.prepare_only:
            job.update({"status": "complete", "phase": "prepared", "finished_at": utc_now()})
            _atomic_json(job_path, job)
            print(json.dumps(job, ensure_ascii=False, indent=2))
            return 0

        command = build_generator_command(options, prepared)
        job.update({"phase": "generating", "command": command})
        _atomic_json(job_path, job)
        print(f"[film-matinee] prepared video={prepared.video_path}", flush=True)
        print(f"[film-matinee] prepared subtitle={prepared.subtitle_path or 'none'}", flush=True)
        result = subprocess.run(command)
        if result.returncode != 0:
            raise RuntimeError(f"sheet generator exited with code {result.returncode}")
        attach_source_metadata(options.out_dir / "manifest.json", prepared)
        job.update({"status": "complete", "phase": "complete", "finished_at": utc_now()})
        _atomic_json(job_path, job)
        return 0
    except Exception as exc:
        job.update({
            "status": "failed",
            "phase": "failed",
            "error": str(exc),
            "finished_at": utc_now(),
        })
        _atomic_json(job_path, job)
        print(f"[film-matinee] source error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
