#!/usr/bin/env python3
"""Burned-in subtitle OCR for film-matinee on macOS.

Frames are decoded by ffmpeg, cropped to the subtitle region, recognized in a
single Apple Vision process, then merged into timestamped subtitle cues. OCR is
kept explicitly labelled so it is never mistaken for a source subtitle track.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


FFMPEG = "ffmpeg"
SWIFT_SOURCE = Path(__file__).with_name("film_matinee_vision_ocr.swift")
DEFAULT_HELPER = Path.home() / ".cache" / "film-matinee" / "bin" / "film-matinee-vision-ocr"


class OCRUnavailable(RuntimeError):
    """Raised when the local Apple Vision OCR backend cannot be used."""


@dataclass
class OCRObservation:
    time: float
    text: str
    confidence: float


@dataclass
class OCRCue:
    start: float
    end: float
    text: str
    confidence: float
    observations: int


def _run(command: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=text)
    if result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "ignore")
        raise RuntimeError(stderr.strip() or f"command failed: {' '.join(command)}")
    return result


def _swiftc_command() -> list[str]:
    if platform.system() != "Darwin":
        raise OCRUnavailable("burned-subtitle OCR currently requires macOS Apple Vision")
    xcrun = shutil.which("xcrun")
    if not xcrun:
        raise OCRUnavailable("xcrun was not found; install Xcode Command Line Tools")
    result = _run([xcrun, "--find", "swiftc"], text=True)
    if not result.stdout.strip():
        raise OCRUnavailable("swiftc was not found; install Xcode Command Line Tools")
    # Keep xcrun in the invocation. Calling the resolved Xcode binary directly
    # can lose SDK/toolchain selection when launched from a Python subprocess.
    return [xcrun, "swiftc"]


def build_vision_helper(binary: Path = DEFAULT_HELPER) -> Path:
    """Compile the tiny Vision bridge once, replacing it atomically."""
    if not SWIFT_SOURCE.exists():
        raise OCRUnavailable(f"Vision OCR source is missing: {SWIFT_SOURCE}")
    if binary.exists() and binary.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
        return binary

    swiftc = _swiftc_command()
    binary.parent.mkdir(parents=True, exist_ok=True)
    lock_path = binary.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        if binary.exists() and binary.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
            return binary
        temporary = binary.with_name(f".{binary.name}.{os.getpid()}.tmp")
        try:
            _run([*swiftc, str(SWIFT_SOURCE), "-O", "-o", str(temporary)])
            temporary.chmod(0o755)
            temporary.replace(binary)
        finally:
            temporary.unlink(missing_ok=True)
    return binary


def _fps_expression(fps: float) -> str:
    if fps <= 0:
        raise ValueError("OCR fps must be positive")
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def extract_range_frames(
    video: Path,
    start: float,
    end: float,
    destination: Path,
    *,
    fps: float = 2.0,
    crop_ratio: float = 0.34,
    width: int = 960,
    hwaccel_args: Iterable[str] = (),
) -> list[Path]:
    duration = max(0.001, end - start)
    ratio = max(0.15, min(0.65, crop_ratio))
    vf = (
        f"fps={_fps_expression(fps)},"
        f"crop=iw:floor(ih*{ratio:.5f}/2)*2:0:ih-floor(ih*{ratio:.5f}/2)*2,"
        f"scale={max(320, width)}:-2"
    )
    destination.mkdir(parents=True, exist_ok=True)
    pattern = destination / "frame-%06d.jpg"
    command = [
        FFMPEG,
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        *list(hwaccel_args),
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-an",
        "-q:v",
        "4",
        "-y",
        str(pattern),
    ]
    _run(command)
    return sorted(destination.glob("frame-*.jpg"))


def extract_sample_frames(
    video: Path,
    times: Iterable[float],
    destination: Path,
    *,
    crop_ratio: float = 0.34,
    width: int = 960,
) -> list[Path]:
    ratio = max(0.15, min(0.65, crop_ratio))
    vf = (
        f"crop=iw:floor(ih*{ratio:.5f}/2)*2:0:ih-floor(ih*{ratio:.5f}/2)*2,"
        f"scale={max(320, width)}:-2"
    )
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, time in enumerate(times):
        path = destination / f"sample-{index:04d}-{time:.3f}.jpg"
        _run([
            FFMPEG,
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{time:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            vf,
            "-q:v",
            "3",
            "-y",
            str(path),
        ])
        paths.append(path)
    return paths


def run_vision_ocr(paths: list[Path], *, batch_size: int = 240) -> list[dict]:
    if not paths:
        return []
    helper = build_vision_helper()
    output: list[dict] = []
    for offset in range(0, len(paths), max(1, batch_size)):
        batch = paths[offset : offset + max(1, batch_size)]
        result = _run([str(helper), *map(str, batch)])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Apple Vision OCR returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Apple Vision OCR returned an unexpected result")
        output.extend(payload)
    return output


def _clean_row_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _normalized_text(value: str) -> str:
    value = _clean_row_text(value).casefold()
    return "".join(character for character in value if character.isalnum())


def _meaningful_text(value: str) -> bool:
    normalized = _normalized_text(value)
    if re.search(r"[\u3400-\u9fff]", normalized):
        return len(normalized) >= 2
    return len(normalized) >= 3 and bool(re.search(r"[a-z0-9]", normalized))


def subtitle_text_from_rows(rows: list[dict], *, min_confidence: float = 0.25) -> tuple[str, float]:
    kept: list[tuple[float, str, float]] = []
    for row in rows:
        text = _clean_row_text(row.get("text", ""))
        confidence = float(row.get("confidence", 0.0) or 0.0)
        x = float(row.get("x", 0.0) or 0.0)
        width = float(row.get("width", 0.0) or 0.0)
        center = x + width / 2
        if confidence < min_confidence or width < 0.035 or not (0.12 <= center <= 0.88):
            continue
        if not _meaningful_text(text):
            continue
        kept.append((float(row.get("y", 0.0) or 0.0), text, confidence))

    kept.sort(key=lambda item: item[0], reverse=True)
    unique: list[tuple[str, float]] = []
    seen: set[str] = set()
    for _, text, confidence in kept:
        normalized = _normalized_text(text)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append((text, confidence))
    if not unique:
        return "", 0.0
    return " / ".join(text for text, _ in unique), sum(score for _, score in unique) / len(unique)


def text_similarity(left: str, right: str) -> float:
    left_lines = [_normalized_text(part) for part in left.split("/") if _normalized_text(part)]
    right_lines = [_normalized_text(part) for part in right.split("/") if _normalized_text(part)]
    if not left_lines or not right_lines:
        return 0.0
    scores: list[float] = []
    for left_line in left_lines:
        for right_line in right_lines:
            if left_line == right_line:
                scores.append(1.0)
            elif min(len(left_line), len(right_line)) >= 3 and (
                left_line in right_line or right_line in left_line
            ):
                ratio = min(len(left_line), len(right_line)) / max(len(left_line), len(right_line))
                scores.append(0.78 + 0.22 * ratio)
            else:
                scores.append(difflib.SequenceMatcher(None, left_line, right_line).ratio())
    return max(scores, default=0.0)


def merge_observations(
    observations: list[OCRObservation],
    *,
    fps: float,
    similarity_threshold: float = 0.78,
    missed_frames: int = 1,
) -> list[OCRCue]:
    if not observations:
        return []
    frame_step = 1.0 / fps
    max_gap = frame_step * (missed_frames + 1.25)
    groups: list[list[OCRObservation]] = []
    for observation in sorted(observations, key=lambda item: item.time):
        if groups:
            previous = groups[-1][-1]
            representative = max(
                groups[-1],
                key=lambda item: item.confidence + min(80, len(_normalized_text(item.text))) / 800,
            )
            if (
                observation.time - previous.time <= max_gap
                and text_similarity(observation.text, representative.text) >= similarity_threshold
            ):
                groups[-1].append(observation)
                continue
        groups.append([observation])

    cues: list[OCRCue] = []
    for group in groups:
        best = max(
            group,
            key=lambda item: item.confidence + min(80, len(_normalized_text(item.text))) / 800,
        )
        average_confidence = sum(item.confidence for item in group) / len(group)
        if len(group) == 1 and average_confidence < 0.5:
            continue
        cues.append(OCRCue(
            start=group[0].time,
            end=group[-1].time + frame_step,
            text=best.text,
            confidence=average_confidence,
            observations=len(group),
        ))
    return cues


def ocr_video_range(
    video: Path,
    start: float,
    end: float,
    *,
    fps: float = 2.0,
    crop_ratio: float = 0.34,
    width: int = 960,
    hwaccel_args: Iterable[str] = (),
) -> list[OCRCue]:
    build_vision_helper()
    with tempfile.TemporaryDirectory(prefix="film-matinee-ocr-") as temporary:
        root = Path(temporary)
        paths = extract_range_frames(
            video,
            start,
            end,
            root,
            fps=fps,
            crop_ratio=crop_ratio,
            width=width,
            hwaccel_args=hwaccel_args,
        )
        payload = run_vision_ocr(paths)
        by_path = {str(item.get("path", "")): item for item in payload}
        observations: list[OCRObservation] = []
        for index, path in enumerate(paths):
            item = by_path.get(str(path), {})
            text, confidence = subtitle_text_from_rows(item.get("rows") or [])
            if text:
                observations.append(OCRObservation(start + index / fps, text, confidence))
    return merge_observations(observations, fps=fps)


def detection_times(duration: float, count: int = 20) -> list[float]:
    if duration <= 0:
        return []
    margin = min(30.0, duration * 0.03)
    usable = max(0.0, duration - 2 * margin)
    distributed = [margin + usable * (index + 0.5) / count for index in range(count)]
    fixed = [60.0, 120.0, 180.0, 300.0, 450.0, 600.0]
    return sorted({round(time, 3) for time in [*distributed, *fixed] if 0 <= time < duration})


def detect_burned_subtitles(
    video: Path,
    duration: float,
    *,
    crop_ratio: float = 0.34,
    width: int = 960,
    minimum_hits: int = 3,
) -> dict:
    build_vision_helper()
    times = detection_times(duration)
    with tempfile.TemporaryDirectory(prefix="film-matinee-ocr-detect-") as temporary:
        paths = extract_sample_frames(video, times, Path(temporary), crop_ratio=crop_ratio, width=width)
        payload = run_vision_ocr(paths)
        by_path = {str(item.get("path", "")): item for item in payload}
        hits = []
        for time, path in zip(times, paths):
            text, confidence = subtitle_text_from_rows(by_path.get(str(path), {}).get("rows") or [])
            if text:
                hits.append({"time": time, "text": text, "confidence": round(confidence, 3)})
    return {
        "detected": len(hits) >= minimum_hits,
        "samples": len(times),
        "hits": len(hits),
        "examples": hits[:5],
    }


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_srt(cues: Iterable[OCRCue], path: Path) -> None:
    blocks = []
    for index, cue in enumerate(cues, 1):
        text = cue.text.replace(" / ", "\n")
        blocks.append(f"{index}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{text}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), "utf-8")


def _probe_duration(video: Path) -> float:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(video),
    ], text=True)
    return float(result.stdout.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR burned-in subtitles with Apple Vision.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=float, default=0.0)
    parser.add_argument("--to", dest="end", type=float)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--crop-ratio", type=float, default=0.34)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--detect", action="store_true")
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    video = options.video.expanduser().resolve()
    duration = _probe_duration(video)
    if options.detect:
        print(json.dumps(detect_burned_subtitles(video, duration, crop_ratio=options.crop_ratio, width=options.width), ensure_ascii=False, indent=2))
        return 0
    end = min(duration, options.end if options.end is not None else duration)
    cues = ocr_video_range(video, max(0.0, options.start), end, fps=options.fps, crop_ratio=options.crop_ratio, width=options.width)
    if options.out:
        write_srt(cues, options.out.expanduser().resolve())
    print(json.dumps({
        "video": str(video),
        "time_range": [max(0.0, options.start), end],
        "cue_count": len(cues),
        "output": str(options.out.expanduser().resolve()) if options.out else None,
        "cues": [asdict(cue) for cue in cues[:12]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[film-matinee] OCR error: {exc}", file=sys.stderr)
        raise SystemExit(1)
