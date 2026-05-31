#!/usr/bin/env python3
"""Offline film-matinee sheet generator.

This is the batch counterpart to examples/frontend/film-matinee-visual-context.js.
It uses ffmpeg/ffprobe for decoding, then writes:

  manifest.json
  sheets/sheet-000.png
  sidecars/sheet-000.txt

The goal is not perfect shot detection. It is a practical "AI pre-read"
artifact: keyframes, color bands, subtitle sidecars, and a navigable index.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import struct
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


@dataclass
class Cue:
    id: str
    start: float
    end: float
    text: str
    style: str = ""


@dataclass
class Segment:
    start: float
    end: float
    cut_score: float = 0.0


@dataclass
class Selection:
    time: float
    score: float
    reason: str
    segment: Segment
    event_time: float | None = None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fmt_time(seconds: float) -> str:
    total = max(0, int(round(seconds or 0)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def clean_subtitle_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\{[^}]*\}", "", value)
    value = value.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def cue_id(cue: Cue, index: int) -> str:
    tenths = max(0, round(cue.start * 10))
    return f"S{index + 1:02d}_{tenths}"


def short_cue_label(identifier: str) -> str:
    return (identifier or "S").split("_")[0] or "S"


def parse_srt_time(value: str) -> float:
    match = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", value.strip())
    if not match:
        raise ValueError(f"bad SRT timestamp: {value}")
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_ass_time(value: str) -> float:
    match = re.match(r"(\d+):(\d{2}):(\d{2})[.](\d{1,2})", value.strip())
    if not match:
        raise ValueError(f"bad ASS timestamp: {value}")
    h, m, s, cs = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs.ljust(2, "0")) / 100


def parse_srt(path: Path) -> list[Cue]:
    text = path.read_text("utf-8-sig", "ignore").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    blocks = re.split(r"\n[ \t]*\n", text.strip())
    for block in blocks:
        lines = block.split("\n")
        ts_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_idx is None:
            continue
        stamps = re.findall(r"\d+:\d{2}:\d{2}[,.]\d{1,3}", lines[ts_idx])
        if len(stamps) < 2:
            continue
        body = clean_subtitle_text("\n".join(lines[ts_idx + 1 :]))
        if not body:
            continue
        cues.append(Cue("", parse_srt_time(stamps[0]), parse_srt_time(stamps[1]), body))
    for index, cue in enumerate(cues):
        cue.id = cue_id(cue, index)
    return cues


def parse_ass(path: Path, include_style: str = "", exclude_style: str = "") -> list[Cue]:
    text = path.read_text("utf-8-sig", "ignore").replace("\r\n", "\n").replace("\r", "\n")
    include_re = re.compile(include_style) if include_style else None
    exclude_re = re.compile(exclude_style) if exclude_style else None
    cues: list[Cue] = []
    in_events = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "[Events]":
            in_events = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = False
        if not in_events or not stripped.startswith("Dialogue:"):
            continue
        payload = stripped[len("Dialogue:") :].strip()
        parts = payload.split(",", 9)
        if len(parts) < 10:
            continue
        _, start_s, end_s, style, *_rest, body = parts
        style = style.strip()
        if include_re and not include_re.search(style):
            continue
        if exclude_re and exclude_re.search(style):
            continue
        body = clean_subtitle_text(body)
        if not body:
            continue
        try:
            cues.append(Cue("", parse_ass_time(start_s), parse_ass_time(end_s), body, style))
        except ValueError:
            continue
    cues.sort(key=lambda cue: (cue.start, cue.end, cue.text))

    deduped: list[Cue] = []
    seen: set[tuple[int, int, str]] = set()
    for cue in cues:
        key = (round(cue.start * 10), round(cue.end * 10), cue.text)
        if key in seen:
            continue
        seen.add(key)
        cue.id = cue_id(cue, len(deduped))
        deduped.append(cue)
    return deduped


def parse_subtitles(path: Path | None, include_style: str, exclude_style: str) -> list[Cue]:
    if not path:
        return []
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(path)
    if suffix == ".ass":
        return parse_ass(path, include_style, exclude_style)
    raise ValueError(f"unsupported subtitle format: {path}")


def shift_cues(cues: list[Cue], offset_sec: float) -> list[Cue]:
    if abs(offset_sec) < 0.001:
        return cues
    shifted: list[Cue] = []
    for cue in cues:
        start = cue.start + offset_sec
        end = cue.end + offset_sec
        if end < 0:
            continue
        shifted.append(Cue("", max(0.0, start), max(0.0, end), cue.text, cue.style))
    shifted.sort(key=lambda cue: (cue.start, cue.end, cue.text))
    for index, cue in enumerate(shifted):
        cue.id = cue_id(cue, index)
    return shifted


def cues_in_range(cues: Iterable[Cue], start: float, end: float) -> list[Cue]:
    return [cue for cue in cues if cue.end >= start and cue.start <= end]


def truncate_opening(text: str, max_chars: int, max_words: int) -> str:
    cleaned = clean_subtitle_text(text)
    if not cleaned:
        return ""
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", cleaned))
    if not has_cjk and re.search(r"\s", cleaned):
        words = " ".join(cleaned.split()[:max_words])
        return re.sub(r"[,.!?;:，。！？；：]+$", "", words) + "..."
    chars = "".join(list(cleaned)[:max_chars])
    return re.sub(r"[,.!?;:，。！？；：]+$", "", chars) + "..."


def pick_subtitle_anchor(time: float, cues: list[Cue], options: argparse.Namespace) -> dict | None:
    nearby = []
    for cue in cues:
        inside = cue.start <= time <= cue.end
        distance = 0 if inside else min(abs(time - cue.start), abs(time - cue.end))
        if distance <= options.max_anchor_distance_sec:
            nearby.append((distance, cue))
    if not nearby:
        return None
    distance, cue = sorted(nearby, key=lambda item: item[0])[0]
    text = truncate_opening(cue.text, options.anchor_max_chars, options.anchor_max_words)
    if not text:
        return None
    return {"id": cue.id, "text": text, "distance": round(distance, 2)}


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(command)}")
    return json.loads(result.stdout)


def probe_video(path: Path) -> dict:
    return run_json([
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name:stream=index,codec_type,codec_name,profile,width,height",
        "-of",
        "json",
        str(path),
    ])


def rgb_saturation(r: int, g: int, b: int) -> float:
    high = max(r, g, b)
    low = min(r, g, b)
    return 0.0 if high <= 0 else (high - low) / high


def frame_stats(buffer: bytes, width: int, height: int, regional_grid: int = 4) -> dict:
    count = width * height
    r_sum = g_sum = b_sum = 0
    luma_sum = 0.0
    luma_sq = 0.0
    sat_sum = 0.0
    lumas = [0.0] * count
    grid = max(1, int(regional_grid or 1))
    tile_sums = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(grid * grid)]

    for idx in range(count):
        offset = idx * 3
        r = buffer[offset]
        g = buffer[offset + 1]
        b = buffer[offset + 2]
        luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        y = idx // width
        x = idx - y * width
        tile_x = min(grid - 1, x * grid // width)
        tile_y = min(grid - 1, y * grid // height)
        tile = tile_sums[tile_y * grid + tile_x]
        tile[0] += r
        tile[1] += g
        tile[2] += b
        tile[3] += luma
        tile[4] += 1
        r_sum += r
        g_sum += g
        b_sum += b
        luma_sum += luma
        luma_sq += luma * luma
        sat_sum += rgb_saturation(r, g, b)
        lumas[idx] = luma

    edge = 0.0
    edge_count = 0
    for y in range(height):
        row = y * width
        for x in range(width):
            current = lumas[row + x]
            if x + 1 < width:
                edge += abs(current - lumas[row + x + 1])
                edge_count += 1
            if y + 1 < height:
                edge += abs(current - lumas[row + width + x])
                edge_count += 1

    luma_avg = luma_sum / count
    variance = max(0.0, luma_sq / count - luma_avg * luma_avg)
    tiles = []
    for tile in tile_sums:
        tile_count = max(1, tile[4])
        tiles.append({
            "rgb": [round(tile[0] / tile_count), round(tile[1] / tile_count), round(tile[2] / tile_count)],
            "luma": tile[3] / tile_count,
        })
    return {
        "rgb": [round(r_sum / count), round(g_sum / count), round(b_sum / count)],
        "luma": luma_avg,
        "contrast": math.sqrt(variance),
        "edge": edge / edge_count if edge_count else 0.0,
        "saturation": sat_sum / count,
        "tiles": tiles,
    }


def pixel_motion(current: bytes, previous: bytes | None) -> float:
    if not previous:
        return 0.0
    total = 0
    length = min(len(current), len(previous))
    if not length:
        return 0.0
    for index in range(length):
        total += abs(current[index] - previous[index])
    return total / (length * 255)


def color_distance(a: list[int], b: list[int]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def regional_change(current: list[dict], previous: list[dict]) -> dict:
    count = min(len(current), len(previous))
    if not count:
        return {"delta": 0.0, "delta_avg": 0.0, "luma_delta": 0.0, "change": 0.0}
    color_deltas = [color_distance(current[index]["rgb"], previous[index]["rgb"]) for index in range(count)]
    luma_deltas = [abs(current[index]["luma"] - previous[index]["luma"]) for index in range(count)]
    delta = max(color_deltas) if color_deltas else 0.0
    delta_avg = average(color_deltas)
    luma_delta = max(luma_deltas) if luma_deltas else 0.0
    return {
        "delta": delta,
        "delta_avg": delta_avg,
        "luma_delta": luma_delta,
        "change": 0.7 * delta + 0.25 * delta_avg + 120 * luma_delta,
    }


def annotate_color_islands(samples: list[dict], options: argparse.Namespace) -> None:
    window = max(1, int(options.color_island_window))
    for sample in samples:
        sample["color_island"] = 0.0
    if len(samples) < window * 2 + 1:
        return
    for index in range(window, len(samples) - window):
        before = average_rgb(samples[index - window:index])
        after = average_rgb(samples[index + 1:index + 1 + window])
        score = min(
            color_distance(samples[index]["rgb"], before),
            color_distance(samples[index]["rgb"], after),
        )
        samples[index]["color_island"] = score


def sample_frame_quality(sample: dict, options: argparse.Namespace) -> float:
    not_black = clamp((sample["luma"] - options.low_info_luma) / 0.18, 0, 1)
    not_white = clamp((options.high_info_luma - sample["luma"]) / 0.18, 0, 1)
    contrast_score = clamp(sample["contrast"] / 0.16, 0, 1)
    edge_score = clamp(sample["edge"] / 0.12, 0, 1)
    saturation_score = clamp(sample["saturation"] / 0.35, 0, 1)
    detail_score = max(contrast_score, edge_score)
    luma_score = min(not_black, not_white)
    if sample["luma"] < 0.18:
        luma_score = max(luma_score, 0.55 * detail_score + 0.2 * saturation_score)
    elif sample["luma"] > 0.82:
        luma_score = max(luma_score, 0.45 * detail_score)
    return clamp(0.4 * luma_score + 0.25 * contrast_score + 0.25 * edge_score + 0.1 * saturation_score, 0, 1)


def is_low_information(sample: dict, options: argparse.Namespace) -> bool:
    return (
        sample["luma"] < options.low_info_luma
        and sample["contrast"] < 0.025
        and sample["edge"] < 0.02
        and sample["saturation"] < 0.08
    ) or (
        sample["luma"] > options.high_info_luma
        and sample["contrast"] < 0.025
        and sample["edge"] < 0.02
    )


def fps_expression(sample_step: float) -> str:
    fraction = Fraction(1 / sample_step).limit_denominator(1000)
    return f"{fraction.numerator}/{fraction.denominator}"


def sample_visual_frames(video: Path, start: float, end: float, options: argparse.Namespace) -> list[dict]:
    width = options.sample_width
    height = options.sample_height
    duration = max(0.001, end - start)
    vf = (
        f"fps={fps_expression(options.sample_step_sec)},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "format=rgb24"
    )
    command = [
        FFMPEG,
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration + 0.001:.3f}",
        "-i",
        str(video),
        "-vf",
        vf,
        "-an",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "ignore").strip())

    frame_bytes = width * height * 3
    samples: list[dict] = []
    previous: dict | None = None
    previous_buffer: bytes | None = None
    for index in range(0, len(result.stdout), frame_bytes):
        chunk = result.stdout[index : index + frame_bytes]
        if len(chunk) < frame_bytes:
            break
        sample_index = index // frame_bytes
        t = min(end, start + sample_index * options.sample_step_sec)
        stats = frame_stats(chunk, width, height, options.regional_grid)
        sample = {"t": round(t, 3), **stats}
        sample["motion"] = pixel_motion(chunk, previous_buffer)
        if previous:
            regional = regional_change(sample["tiles"], previous["tiles"])
            sample["delta"] = color_distance(sample["rgb"], previous["rgb"])
            sample["regional_delta"] = regional["delta"]
            sample["regional_delta_avg"] = regional["delta_avg"]
            sample["regional_luma_delta"] = regional["luma_delta"]
            sample["regional_change"] = regional["change"]
            sample["change"] = (
                sample["delta"]
                + abs(sample["luma"] - previous["luma"]) * 220
                + abs(sample["contrast"] - previous["contrast"]) * 90
                + abs(sample["edge"] - previous["edge"]) * 90
                + sample["motion"] * options.motion_weight
                + sample["regional_change"] * options.regional_change_weight
            )
        else:
            sample["delta"] = 0.0
            sample["regional_delta"] = 0.0
            sample["regional_delta_avg"] = 0.0
            sample["regional_luma_delta"] = 0.0
            sample["regional_change"] = 0.0
            sample["change"] = 0.0
        sample["quality"] = sample_frame_quality(sample, options)
        sample["low_information"] = is_low_information(sample, options)
        samples.append(sample)
        previous = sample
        previous_buffer = chunk
    annotate_color_islands(samples, options)
    return samples


def samples_in_range(samples: list[dict], start: float, end: float) -> list[dict]:
    return [sample for sample in samples if sample["t"] >= start - 0.001 and sample["t"] <= end + 0.001]


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = int(clamp(math.floor((len(sorted_values) - 1) * q), 0, len(sorted_values) - 1))
    return sorted_values[index]


def average_rgb(samples: list[dict]) -> list[int]:
    if not samples:
        return [0, 0, 0]
    return [
        round(average([sample["rgb"][0] for sample in samples])),
        round(average([sample["rgb"][1] for sample in samples])),
        round(average([sample["rgb"][2] for sample in samples])),
    ]


def visual_change_threshold(samples: list[dict], options: argparse.Namespace, sensitivity: float | None = None) -> float:
    changes = [sample["change"] for sample in samples[1:] if math.isfinite(sample["change"])]
    if not changes:
        return math.inf
    median = quantile(changes, 0.5)
    q75 = quantile(changes, 0.75)
    q90 = quantile(changes, 0.9)
    spread = max(1, q75 - median)
    return min(q90, median + spread * (sensitivity or options.visual_event_sensitivity))


def choose_cut_samples(samples: list[dict], start: float, end: float, options: argparse.Namespace) -> list[dict]:
    threshold = visual_change_threshold(samples, options)
    min_gap = max(2, options.min_segment_sec)
    cuts: list[dict] = []
    for sample in samples[1:]:
        if sample["change"] < threshold:
            continue
        if sample["t"] - start < options.min_segment_sec or end - sample["t"] < options.min_segment_sec:
            continue
        if cuts and sample["t"] - cuts[-1]["t"] < min_gap:
            if sample["change"] > cuts[-1]["change"]:
                cuts[-1] = sample
            continue
        cuts.append(sample)
    return cuts


def split_long_segment(segment: Segment, samples: list[dict], options: argparse.Namespace) -> list[Segment]:
    max_duration = max(options.max_segment_sec, options.min_segment_sec * 2)
    result: list[Segment] = []
    queue = [segment]
    while queue:
        current = queue.pop(0)
        if current.end - current.start <= max_duration:
            result.append(current)
            continue
        candidates = sorted(
            samples_in_range(samples, current.start + options.min_segment_sec, current.end - options.min_segment_sec),
            key=lambda sample: sample["change"],
            reverse=True,
        )
        cut = candidates[0]["t"] if candidates else current.start + (current.end - current.start) / 2
        queue.append(Segment(current.start, cut, candidates[0]["change"] if candidates else 0.0))
        queue.append(Segment(cut, current.end, 0.0))
    return result


def build_visual_segments(samples: list[dict], start: float, end: float, options: argparse.Namespace) -> list[Segment]:
    cuts = choose_cut_samples(samples, start, end, options)
    base: list[Segment] = []
    current = start
    for cut in cuts:
        base.append(Segment(current, cut["t"], cut["change"]))
        current = cut["t"]
    base.append(Segment(current, end, 0.0))
    segments: list[Segment] = []
    for segment in base:
        segments.extend(split_long_segment(segment, samples, options))
    return [segment for segment in segments if segment.end - segment.start >= min(1, options.min_segment_sec)]


def subtitle_proximity_score(time: float, cues: list[Cue], options: argparse.Namespace) -> float:
    if not cues:
        return 0.0
    best = math.inf
    for cue in cues:
        distance = 0.0 if cue.start <= time <= cue.end else min(abs(time - cue.start), abs(time - cue.end))
        best = min(best, distance)
    return 1 - best / options.max_anchor_distance_sec if best <= options.max_anchor_distance_sec else 0.0


def representative_score(sample: dict, segment_samples: list[dict], cues: list[Cue], selected_times: list[float], options: argparse.Namespace) -> float:
    segment_rgb = average_rgb(segment_samples)
    representativeness = 1 - clamp(color_distance(sample["rgb"], segment_rgb) / 160, 0, 1)
    segment_luma = average([item["luma"] for item in segment_samples])
    luma_representative = 1 - clamp(abs(sample["luma"] - segment_luma) / 0.35, 0, 1)
    change_q90 = max(1, quantile([item["change"] for item in segment_samples], 0.9))
    transition_penalty = clamp(sample["change"] / change_q90, 0, 1)
    subtitle_score = subtitle_proximity_score(sample["t"], cues, options)
    diversity = (
        clamp(min(abs(time - sample["t"]) for time in selected_times) / options.min_segment_sec, 0, 1)
        if selected_times
        else 1
    )
    return (
        0.42 * sample["quality"]
        + 0.18 * representativeness
        + 0.12 * luma_representative
        + 0.14 * subtitle_score
        + 0.08 * diversity
        - 0.12 * transition_penalty
        - (0.5 if sample["low_information"] else 0)
    )


def pick_segment_keyframe(segment: Segment, samples: list[dict], cues: list[Cue], selected_times: list[float], options: argparse.Namespace) -> Selection | None:
    duration = max(0.001, segment.end - segment.start)
    pad = min(1.5, duration * 0.18)
    segment_samples = samples_in_range(samples, segment.start + pad, segment.end - pad)
    if not segment_samples:
        segment_samples = samples_in_range(samples, segment.start, segment.end)
    if not segment_samples:
        return None
    useful = [sample for sample in segment_samples if not sample["low_information"]]
    candidates = useful or segment_samples
    scored = [
        Selection(
            time=sample["t"],
            score=representative_score(sample, segment_samples, cues, selected_times, options),
            reason="segment-representative",
            segment=segment,
        )
        for sample in candidates
    ]
    return sorted(scored, key=lambda item: item.score, reverse=True)[0] if scored else None


def choose_micro_event_samples(samples: list[dict], start: float, end: float, options: argparse.Namespace) -> list[dict]:
    threshold = visual_change_threshold(samples, options, sensitivity=options.micro_event_sensitivity)
    events: list[dict] = []
    for sample in samples[1:]:
        visual_score = clamp(sample["change"] / max(1.0, threshold), 0.0, 1.4)
        color_score = clamp(sample.get("color_island", 0.0) / max(1.0, options.color_island_threshold), 0.0, 1.4)
        audio_score = sample.get("audio_event", 0.0) if options.audio_event_weight > 0 else 0.0
        event_score = max(visual_score, color_score, audio_score)
        if audio_score and max(visual_score, color_score) > 0.4:
            event_score += 0.2 * audio_score
        event_strength = max(
            sample["change"],
            sample.get("color_island", 0.0) * options.color_island_event_weight,
            audio_score * options.audio_event_weight,
        )
        if (
            sample["change"] < threshold
            and sample.get("color_island", 0.0) < options.color_island_threshold
            and audio_score < options.audio_event_threshold
        ):
            continue
        if sample["t"] < start or sample["t"] > end:
            continue
        sample["event_strength"] = event_strength
        sample["event_score"] = event_score
        if events and sample["t"] - events[-1]["t"] < options.min_micro_keyframe_gap_sec:
            if event_score > events[-1].get("event_score", 0.0):
                events[-1] = sample
            continue
        events.append(sample)
    return events


def pick_post_event_keyframe(event_sample: dict, samples: list[dict], cues: list[Cue], selected_times: list[float], options: argparse.Namespace) -> Selection | None:
    start = event_sample["t"]
    end = event_sample["t"] + options.micro_event_lookahead_sec
    candidates = [sample for sample in samples_in_range(samples, start, end) if not sample["low_information"]]
    if not candidates:
        candidates = samples_in_range(samples, event_sample["t"], end)
    if not candidates:
        return None

    scored: list[Selection] = []
    for sample in candidates:
        distance_penalty = clamp((sample["t"] - event_sample["t"]) / max(0.001, options.micro_event_lookahead_sec), 0, 1)
        event_score = max(event_sample.get("event_score", 0.0), sample.get("audio_event", 0.0))
        event_bonus = 0.04 + 0.14 * clamp(event_score / 1.25, 0.0, 1.0)
        diversity = (
            clamp(min(abs(time - sample["t"]) for time in selected_times) / options.min_micro_keyframe_gap_sec, 0, 1)
            if selected_times
            else 1
        )
        score = (
            0.62 * sample["quality"]
            + 0.18 * subtitle_proximity_score(sample["t"], cues, options)
            + 0.14 * diversity
            + event_bonus
            - 0.12 * distance_penalty
            - (0.5 if sample["low_information"] else 0)
        )
        scored.append(Selection(sample["t"], score, "micro-event", Segment(event_sample["t"], min(end, sample["t"])), event_sample["t"]))
    return sorted(scored, key=lambda item: item.score, reverse=True)[0] if scored else None


def pick_gap_coverage_keyframes(
    selected: list[Selection],
    samples: list[dict],
    cues: list[Cue],
    start: float,
    end: float,
    options: argparse.Namespace,
) -> list[Selection]:
    max_gap = float(options.max_keyframe_gap_sec)
    if max_gap <= 0 or not samples:
        return []
    max_additions = max(0, int(options.max_gap_keyframes_per_sheet))
    if max_additions <= 0:
        return []

    anchors = [start, *[item.time for item in sorted(selected, key=lambda item: item.time)], end]
    additions: list[Selection] = []
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left
        if gap <= max_gap:
            continue
        gap_slots = max(1, math.ceil(gap / max_gap) - 1)
        for slot in range(gap_slots):
            if len(additions) >= max_additions:
                return additions
            target = left + gap * ((slot + 1) / (gap_slots + 1))
            radius = min(max_gap * 0.45, max(4.0, gap / (gap_slots + 1) * 0.5))
            candidates = samples_in_range(samples, target - radius, target + radius)
            useful = [sample for sample in candidates if not sample["low_information"]]
            candidates = useful or candidates
            if not candidates:
                continue
            segment = Segment(left, right, 0.0)
            segment_samples = samples_in_range(samples, left, right) or candidates
            scored = []
            selected_times = [item.time for item in selected] + [item.time for item in additions]
            for sample in candidates:
                target_score = 1 - clamp(abs(sample["t"] - target) / max(0.001, radius), 0, 1)
                duration_bonus = 0.12 * clamp((gap / max_gap) - 1, 0, 1.5)
                score = (
                    0.95
                    + duration_bonus
                    + 0.2 * representative_score(sample, segment_samples, cues, selected_times, options)
                    + 0.08 * target_score
                    + 0.08 * subtitle_proximity_score(sample["t"], cues, options)
                    - (0.2 if sample["low_information"] else 0)
                )
                scored.append(Selection(sample["t"], score, "gap-coverage", segment))
            if scored:
                additions.append(sorted(scored, key=lambda item: item.score, reverse=True)[0])
    return additions


def gap_activity_components(
    sample: dict,
    gap_samples: list[dict],
    left_sample: dict | None,
    right_sample: dict | None,
    options: argparse.Namespace,
) -> dict:
    change_ref = max(45.0, quantile([item["change"] for item in gap_samples], 0.9))
    regional_ref = max(35.0, quantile([item.get("regional_change", 0.0) for item in gap_samples], 0.9))
    motion_ref = max(0.04, quantile([item.get("motion", 0.0) for item in gap_samples], 0.9))
    action = clamp(
        0.35 * clamp(sample["change"] / change_ref, 0, 1)
        + 0.35 * clamp(sample.get("regional_change", 0.0) / regional_ref, 0, 1)
        + 0.3 * clamp(sample.get("motion", 0.0) / motion_ref, 0, 1),
        0,
        1,
    )
    color_island = clamp(sample.get("color_island", 0.0) / max(1.0, options.color_island_threshold), 0, 1)
    endpoint_shift = 0.0
    if left_sample and right_sample:
        endpoint_shift = clamp(
            min(
                color_distance(sample["rgb"], left_sample["rgb"]),
                color_distance(sample["rgb"], right_sample["rgb"]),
            ) / max(1.0, options.coverage_color_shift_threshold),
            0,
            1,
        )
    palette_deviation = clamp(
        color_distance(sample["rgb"], average_rgb(gap_samples)) / max(1.0, options.coverage_color_shift_threshold),
        0,
        1,
    )
    color = max(color_island, endpoint_shift, 0.7 * palette_deviation)
    return {"activity": max(action, color), "action": action, "color": color}


def pick_visual_coverage_keyframes(
    selected: list[Selection],
    samples: list[dict],
    cues: list[Cue],
    start: float,
    end: float,
    options: argparse.Namespace,
) -> list[Selection]:
    action_gap = float(options.action_gap_sec)
    if action_gap <= 0 or not samples:
        return []
    max_additions = max(0, int(options.max_action_coverage_keyframes_per_sheet))
    if max_additions <= 0:
        return []

    anchors = [start, *[item.time for item in sorted(selected, key=lambda item: item.time)], end]
    additions: list[Selection] = []
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left
        if gap <= action_gap:
            continue
        pad = min(4.0, gap * 0.2)
        gap_samples = samples_in_range(samples, left + pad, right - pad)
        useful = [sample for sample in gap_samples if not sample["low_information"]]
        gap_samples = useful or gap_samples
        if not gap_samples:
            continue

        scored: list[Selection] = []
        selected_times = [item.time for item in selected] + [item.time for item in additions]
        segment = Segment(left, right, 0.0)
        left_sample = sample_at_or_near(samples, left)
        right_sample = sample_at_or_near(samples, right)
        for sample in gap_samples:
            activity_parts = gap_activity_components(sample, gap_samples, left_sample, right_sample, options)
            activity = activity_parts["activity"]
            if activity < options.action_gap_min_activity:
                continue
            diversity = (
                clamp(min(abs(time - sample["t"]) for time in selected_times) / action_gap, 0, 1)
                if selected_times
                else 1
            )
            duration_bonus = 0.16 * clamp((gap / action_gap) - 1, 0, 2)
            score = (
                0.78
                + duration_bonus
                + 0.22 * activity
                + 0.12 * sample["quality"]
                + 0.08 * diversity
                + 0.05 * subtitle_proximity_score(sample["t"], cues, options)
                - (0.2 if sample["low_information"] else 0)
            )
            reason = "color-coverage" if activity_parts["color"] > activity_parts["action"] else "action-coverage"
            scored.append(Selection(sample["t"], score, reason, segment))
        if scored:
            additions.append(sorted(scored, key=lambda item: item.score, reverse=True)[0])
            if len(additions) >= max_additions:
                return additions
    return additions


def keyframe_visual_distance(left: dict, right: dict) -> float:
    tile_count = min(len(left.get("tiles", [])), len(right.get("tiles", [])))
    tile_avg = 0.0
    tile_max = 0.0
    if tile_count:
        distances = [
            color_distance(left["tiles"][index]["rgb"], right["tiles"][index]["rgb"]) / 255
            for index in range(tile_count)
        ]
        tile_avg = average(distances)
        tile_max = max(distances)
    global_distance = color_distance(left["rgb"], right["rgb"]) / 255
    luma_distance = abs(left["luma"] - right["luma"])
    contrast_distance = abs(left["contrast"] - right["contrast"])
    edge_distance = abs(left["edge"] - right["edge"])
    return (
        0.35 * global_distance
        + 0.42 * tile_avg
        + 0.12 * tile_max
        + 0.06 * luma_distance
        + 0.03 * contrast_distance
        + 0.02 * edge_distance
    )


def selections_are_visually_similar(
    left: Selection,
    right: Selection,
    samples: list[dict],
    options: argparse.Namespace,
) -> bool:
    if abs(left.time - right.time) > options.similar_keyframe_window_sec:
        return False
    left_sample = sample_at_or_near(samples, left.time)
    right_sample = sample_at_or_near(samples, right.time)
    if not left_sample or not right_sample:
        return False
    return keyframe_visual_distance(left_sample, right_sample) <= options.similar_keyframe_distance


def dedupe_keyframe_candidates(
    candidates: list[Selection],
    max_count: int,
    options: argparse.Namespace,
    samples: list[dict] | None = None,
) -> list[Selection]:
    deduped: list[Selection] = []
    for item in sorted(candidates, key=lambda sel: sel.score, reverse=True):
        if len(deduped) >= max_count:
            break
        if all(
            abs(existing.time - item.time) >= max(1, options.min_micro_keyframe_gap_sec)
            and (
                samples is None
                or not selections_are_visually_similar(existing, item, samples, options)
            )
            for existing in deduped
        ):
            deduped.append(item)
    return sorted(deduped, key=lambda item: item.time)


def pick_keyframe_selections(samples: list[dict], start: float, end: float, cues: list[Cue], options: argparse.Namespace) -> list[Selection]:
    segments = build_visual_segments(samples, start, end, options)
    max_count = max(2, int(options.max_keyframes))
    candidates: list[Selection] = []

    for segment in segments:
        pick = pick_segment_keyframe(segment, samples, cues, [item.time for item in candidates], options)
        if pick:
            candidates.append(pick)

    micro_events = sorted(
        choose_micro_event_samples(samples, start, end, options),
        key=lambda sample: sample.get("event_score", 0.0),
        reverse=True,
    )
    for event_sample in micro_events[: max(0, int(options.max_micro_event_keyframes_per_sheet))]:
        selected_times = [item.time for item in candidates]
        pick = pick_post_event_keyframe(event_sample, samples, cues, selected_times, options)
        if not pick:
            continue
        candidates.append(pick)

    preliminary = dedupe_keyframe_candidates(candidates, max_count, options, samples)
    coverage = [
        *pick_gap_coverage_keyframes(preliminary, samples, cues, start, end, options),
        *pick_visual_coverage_keyframes(preliminary, samples, cues, start, end, options),
    ]
    candidates.extend(coverage)

    if not candidates and samples:
        best = sorted(
            samples,
            key=lambda sample: sample["quality"] - (0.5 if sample["low_information"] else 0),
            reverse=True,
        )[0]
        candidates.append(Selection(best["t"], best["quality"], "fallback-quality", Segment(start, end)))

    return dedupe_keyframe_candidates(candidates, max_count, options, samples)


def selection_end(selection: Selection, fallback: float) -> float:
    if math.isfinite(selection.segment.end):
        return selection.segment.end
    return selection.time if math.isfinite(selection.time) else fallback


def choose_adaptive_end(start: float, candidate_end: float, selections: list[Selection], options: argparse.Namespace) -> float:
    if candidate_end <= start:
        return candidate_end
    min_end = min(candidate_end, start + max(0, options.min_sheet_sec))
    max_keyframes = max(2, int(options.max_keyframes))
    target = int(clamp(int(options.target_keyframes), 2, max_keyframes))
    sorted_selections = sorted([item for item in selections if math.isfinite(item.time)], key=lambda item: item.time)
    if not sorted_selections:
        return candidate_end
    end = candidate_end
    if len(sorted_selections) >= max_keyframes:
        end = selection_end(sorted_selections[max_keyframes - 1], sorted_selections[max_keyframes - 1].time)
    elif len(sorted_selections) >= target:
        end = selection_end(sorted_selections[target - 1], sorted_selections[target - 1].time)
    if end < min_end:
        end = min_end
    step = max(0.25, options.sample_step_sec)
    aligned = math.ceil(end / step) * step
    return clamp(round(aligned, 3), min_end, candidate_end)


def capture_frame(video: Path, time: float, width: int, height: int) -> Image.Image:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    command = [
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
        "-an",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return Image.new("RGB", (width, height), (0, 0, 0))
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def sample_audio_levels(video: Path, start: float, end: float, options: argparse.Namespace) -> list[dict]:
    if not options.audio_rail and options.audio_event_weight <= 0:
        return []
    duration = max(0.001, end - start)
    sample_rate = max(20, int(options.audio_sample_rate))
    step = max(0.05, options.audio_step_sec)
    command = [
        FFMPEG,
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video),
        "-map",
        "0:a:0?",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []

    pcm_count = len(result.stdout) // 2
    if not pcm_count:
        return []
    samples = struct.unpack(f"<{pcm_count}h", result.stdout[: pcm_count * 2])
    bin_size = max(1, int(sample_rate * step))
    levels = []
    for index in range(0, pcm_count, bin_size):
        chunk = samples[index : index + bin_size]
        if not chunk:
            continue
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) / 32768
        energy = math.sqrt(rms) * options.audio_gain
        level = clamp(energy, 0, 1)
        levels.append({
            "t": round(start + (index / sample_rate), 3),
            "level": round(level, 4),
            "energy": round(energy, 4),
        })
    return levels


def annotate_audio_events(samples: list[dict], audio_levels: list[dict], options: argparse.Namespace) -> None:
    for sample in samples:
        sample["audio_level"] = 0.0
        sample["audio_event"] = 0.0
    if not samples or not audio_levels or options.audio_event_weight <= 0:
        return

    levels = sorted(audio_levels, key=lambda item: item["t"])
    window = max(0.05, float(options.audio_event_window_sec))
    context = max(window * 2, float(options.audio_event_context_sec))

    def event_level(item: dict) -> float:
        return item.get("energy", item["level"])

    for sample in samples:
        t = sample["t"]
        near = [event_level(item) for item in levels if abs(item["t"] - t) <= window]
        if not near:
            nearest = min(levels, key=lambda item: abs(item["t"] - t))
            near = [event_level(nearest)]

        local = [event_level(item) for item in levels if abs(item["t"] - t) <= context]
        if not local:
            local = near

        peak = max(near)
        rendered_peak = min(peak, 1.0)
        baseline = quantile(local, 0.5)
        high = quantile(local, 0.9)
        dynamic = max(0.08, high - baseline)
        transient = max(0.0, peak - baseline)
        score = clamp(transient / dynamic, 0.0, 1.0)
        if rendered_peak < options.audio_event_min_level:
            score = 0.0

        sample["audio_level"] = round(rendered_peak, 4)
        sample["audio_event"] = round(score, 4)


def font_candidates(bold: bool = False) -> list[str]:
    if bold:
        return [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    return [
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in font_candidates(bold):
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: float) -> str:
    value = text
    while len(value) > 4 and text_width(draw, value, font) > max_width:
        value = value[:-4] + "..."
    return value


def draw_contained(canvas: Image.Image, image: Image.Image, x: int, y: int, width: int, height: int) -> None:
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([x, y, x + width, y + height], fill=(5, 5, 5))
    src_w, src_h = image.size
    src_ratio = src_w / max(1, src_h)
    box_ratio = width / max(1, height)
    draw_w = width
    draw_h = height
    if src_ratio > box_ratio:
        draw_h = round(width / src_ratio)
    else:
        draw_w = round(height * src_ratio)
    dx = x + (width - draw_w) // 2
    dy = y + (height - draw_h) // 2
    resized = image.resize((draw_w, draw_h), Image.Resampling.LANCZOS)
    canvas.paste(resized, (dx, dy))


def sample_at_or_near(samples: list[dict], time: float) -> dict | None:
    if not samples:
        return None
    return min(samples, key=lambda sample: abs(sample["t"] - time))


def readable_text_color(rgb: list[int]) -> tuple[int, int, int]:
    luma = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
    return (17, 17, 17) if luma > 0.55 else (246, 246, 246)


def compact_rows_from_keyframes(keyframes: list[dict], start: float, end: float, options: argparse.Namespace) -> list[dict]:
    per_row = choose_keyframes_per_row(len(keyframes), options)
    rows = []
    for index in range(0, len(keyframes), per_row):
        frames = keyframes[index : index + per_row]
        if not frames:
            continue
        previous_frame = keyframes[index - 1] if index > 0 else None
        next_frame = keyframes[index + per_row] if index + per_row < len(keyframes) else None
        row_start = (
            (previous_frame["time"] + frames[0]["time"]) / 2
            if previous_frame
            else start
        )
        row_end = (
            (frames[-1]["time"] + next_frame["time"]) / 2
            if next_frame
            else end
        )
        rows.append({
            "start": clamp(row_start, start, end),
            "end": clamp(row_end, start, end),
            "frames": frames,
        })
    return rows or [{"start": start, "end": end, "frames": []}]


def row_pack_score(total: int, per_row: int) -> tuple[int, int, int]:
    rows = math.ceil(total / per_row)
    last = total % per_row or per_row
    empty = per_row - last
    return (rows, empty, per_row)


def choose_keyframes_per_row(total: int, options: argparse.Namespace) -> int:
    base = max(1, int(options.keyframes_per_row))
    if not options.auto_pack_rows or total <= base:
        return base
    max_per_row = max(base, int(options.max_keyframes_per_row))
    candidates = range(base, max_per_row + 1)
    return min(candidates, key=lambda per_row: row_pack_score(total, per_row))


def band_width_for_duration(duration: float, options: argparse.Namespace) -> float:
    if duration <= 0.001:
        return 0.0
    return max(options.min_band_width, duration * options.band_pixels_per_second)


def layout_row(row: dict, left: int, content_width: int, options: argparse.Namespace) -> dict:
    frames = row["frames"]
    if not frames:
        return {"items": [], "x": left, "width": content_width, "lead_width": 0, "tail_width": 0}
    lead_width = band_width_for_duration(frames[0]["time"] - row["start"], options)
    tail_width = band_width_for_duration(row["end"] - frames[-1]["time"], options)
    band_widths = []
    for index in range(len(frames) - 1):
        duration = max(0.001, frames[index + 1]["time"] - frames[index]["time"])
        band_widths.append(band_width_for_duration(duration, options))
    total_frame_width = len(frames) * options.keyframe_width
    requested = lead_width + total_frame_width + sum(band_widths) + tail_width
    scale = min(1.0, content_width / requested) if requested else 1.0
    frame_width = max(96, int(options.keyframe_width * scale))
    frame_height = round(frame_width * options.keyframe_height / options.keyframe_width)
    scaled_lead = max(options.min_band_width * 0.6, lead_width * scale) if lead_width else 0
    scaled_tail = max(options.min_band_width * 0.6, tail_width * scale) if tail_width else 0
    scaled_bands = [
        max(options.min_band_width * 0.6, width * scale) if width else 0
        for width in band_widths
    ]
    total_width = scaled_lead + len(frames) * frame_width + sum(scaled_bands) + scaled_tail
    track_x = left + max(0, (content_width - total_width) / 2)
    x = track_x + scaled_lead
    items = []
    for index, frame in enumerate(frames):
        band_after = scaled_bands[index] if index < len(scaled_bands) else 0
        items.append({
            "frame": frame,
            "x": int(round(x)),
            "width": int(round(frame_width)),
            "height": int(round(frame_height)),
            "band_after": int(round(band_after)),
        })
        x += frame_width + band_after
    return {
        "items": items,
        "x": int(round(track_x)),
        "width": int(round(total_width)),
        "lead_width": int(round(scaled_lead)),
        "tail_width": int(round(scaled_tail)),
    }


def draw_color_band(draw: ImageDraw.ImageDraw, samples: list[dict], start: float, end: float, x: int, y: int, width: int, height: int) -> None:
    if width <= 0 or height <= 0 or end <= start + 0.001:
        return
    draw.rectangle([x, y, x + width, y + height], fill=(22, 22, 22), outline=(60, 60, 60))
    row_samples = samples_in_range(samples, start, end)
    if not row_samples:
        return
    duration = max(0.001, end - start)
    for index, sample in enumerate(row_samples):
        next_sample = row_samples[index + 1] if index + 1 < len(row_samples) else None
        t0 = clamp(sample["t"], start, end)
        t1 = clamp(next_sample["t"] if next_sample else sample["t"] + 1, start, end)
        sx = x + ((t0 - start) / duration) * width
        sw = max(1, ((t1 - t0) / duration) * width + 1)
        rgb = tuple(int(v) for v in sample["rgb"])
        draw.rectangle([int(sx), y, int(sx + sw), y + height], fill=rgb)
    draw.rectangle([x, y, x + width, y + height], outline=(70, 70, 70))


def draw_subtitle_markers(draw: ImageDraw.ImageDraw, cues: list[Cue], start: float, end: float, x: int, y: int, width: int) -> None:
    duration = max(0.001, end - start)
    for cue in cues:
        if cue.end < start or cue.start > end:
            continue
        s = clamp(cue.start, start, end)
        e = clamp(cue.end, start, end)
        sx = x + ((s - start) / duration) * width
        ex = x + ((e - start) / duration) * width
        draw.rectangle([int(sx), y, int(max(sx + 4, ex)), y + 4], fill=(150, 150, 150))


def draw_audio_waveform(
    draw: ImageDraw.ImageDraw,
    audio_levels: list[dict],
    start: float,
    end: float,
    x: int,
    y: int,
    width: int,
    height: int,
) -> None:
    draw.rectangle([x, y, x + width, y + height], fill=(15, 17, 18))
    center = y + height / 2
    draw.line([(x, int(center)), (x + width, int(center))], fill=(45, 52, 55), width=1)

    levels = [item for item in audio_levels if item["t"] >= start - 0.001 and item["t"] <= end + 0.001]
    if not levels:
        draw.rectangle([x, y, x + width, y + height], outline=(35, 38, 40))
        return

    duration = max(0.001, end - start)
    for item in levels:
        sx = int(x + ((item["t"] - start) / duration) * width)
        amp = item["level"] * (height / 2)
        draw.line([(sx, int(center - amp)), (sx, int(center + amp))], fill=(126, 172, 188), width=1)
    draw.rectangle([x, y, x + width, y + height], outline=(40, 46, 48))


def render_sheet(
    title: str,
    start: float,
    end: float,
    samples: list[dict],
    audio_levels: list[dict],
    keyframes: list[dict],
    cues: list[Cue],
    options: argparse.Namespace,
) -> Image.Image:
    width = options.sheet_width
    left = 34
    right = 34
    content_width = width - left - right
    top = 28
    bottom = 18
    rows = compact_rows_from_keyframes(keyframes, start, end, options)
    row_height = max(options.row_height, options.keyframe_height + 48)
    if options.audio_rail:
        row_height = max(row_height, options.keyframe_height + 68)
    height = top + len(rows) * row_height + bottom
    canvas = Image.new("RGB", (width, height), (11, 11, 12))
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(22, bold=True)
    meta_font = load_font(14)
    small_font = load_font(12)
    chip_font = load_font(11, bold=True)
    anchor_font = load_font(options.anchor_font_px, bold=True)

    draw.text((left, 4), fit_text(draw, title or "Film", title_font, width - left - right - 180), fill=(243, 240, 232), font=title_font)
    range_text = f"{fmt_time(start)}-{fmt_time(end)}"
    draw.text((width - right - text_width(draw, range_text, meta_font), 7), range_text, fill=(190, 186, 178), font=meta_font)

    global_index = 0
    for row_index, row in enumerate(rows):
        row_top = top + row_index * row_height
        label_y = row_top + 7
        frame_y = row_top + 26
        row_layout = layout_row(row, left, content_width, options)
        row_items = row_layout["items"]
        track_x = row_layout["x"]
        track_width = row_layout["width"]
        track_end = track_x + track_width
        frame_height = row_items[0]["height"] if row_items else options.keyframe_height
        frame_mid_y = frame_y + frame_height / 2
        marker_y = frame_y + frame_height + 30
        audio_y = marker_y + 8

        draw.text((left, label_y), f"{fmt_time(row['start'])}-{fmt_time(row['end'])}", fill=(80, 80, 80), font=small_font)
        draw.line([(track_x, int(frame_mid_y)), (track_end, int(frame_mid_y))], fill=(30, 30, 30), width=1)

        if row_items and row_layout["lead_width"] > 3:
            first_item = row_items[0]
            draw_color_band(
                draw,
                samples,
                row["start"],
                first_item["frame"]["time"],
                track_x,
                frame_y,
                row_layout["lead_width"],
                first_item["height"],
            )

        for item_index in range(len(row_items) - 1):
            item = row_items[item_index]
            next_item = row_items[item_index + 1]
            gap_x = item["x"] + item["width"]
            gap_end = next_item["x"]
            gap_width = gap_end - gap_x
            if gap_width > 3:
                draw_color_band(draw, samples, item["frame"]["time"], next_item["frame"]["time"], gap_x, frame_y, gap_width, item["height"])

        if row_items and row_layout["tail_width"] > 3:
            last_item = row_items[-1]
            tail_x = last_item["x"] + last_item["width"]
            draw_color_band(
                draw,
                samples,
                last_item["frame"]["time"],
                row["end"],
                tail_x,
                frame_y,
                row_layout["tail_width"],
                last_item["height"],
            )

        draw_subtitle_markers(draw, cues, row["start"], row["end"], track_x, marker_y, track_width)
        if options.audio_rail:
            draw_audio_waveform(draw, audio_levels, row["start"], row["end"], track_x, audio_y, track_width, options.audio_rail_height)

        for item in row_items:
            frame = item["frame"]
            global_index += 1
            x = item["x"]
            center = x + item["width"] // 2
            draw.line([(center, frame_y - 8), (center, frame_y + item["height"] + 8)], fill=(90, 90, 90), width=1)
            draw_contained(canvas, frame["image"], x, frame_y, item["width"], item["height"])
            draw.rectangle([x, frame_y, x + item["width"], frame_y + item["height"]], outline=(105, 105, 105))

            chip_sample = sample_at_or_near(samples, frame["time"])
            chip_color = chip_sample["rgb"] if chip_sample else [24, 24, 24]
            chip_text = f"K{global_index} {fmt_time(frame['time'])}"
            chip_width = min(item["width"] - 12, int(text_width(draw, chip_text, chip_font) + 14))
            draw.rectangle([x + 8, frame_y + 8, x + 8 + chip_width, frame_y + 28], fill=tuple(chip_color))
            draw.text((x + 15, frame_y + 10), chip_text, fill=readable_text_color(chip_color), font=chip_font)

            anchor = frame.get("subtitle_anchor")
            if anchor:
                label = f'{short_cue_label(anchor["id"])} "{anchor["text"]}"'
                draw.text((x, frame_y + item["height"] + 8), fit_text(draw, label, anchor_font, item["width"]), fill=(243, 240, 232), font=anchor_font)

        draw.line([(left, row_top + row_height - 4), (width - right, row_top + row_height - 4)], fill=(30, 30, 30), width=1)

    return canvas


def make_sidecar(cues: list[Cue], start: float, end: float) -> str:
    lines = [f"{cue.id} {fmt_time(cue.start)}-{fmt_time(cue.end)}: {cue.text}" for cue in cues]
    return "\n".join([f"[subtitles {fmt_time(start)}-{fmt_time(end)}]", *lines, "[/subtitles]"])


def selection_to_dict(selection: Selection) -> dict:
    return {
        "time": round(selection.time, 3),
        "score": round(selection.score, 3),
        "reason": selection.reason,
        "event_time": round(selection.event_time, 3) if selection.event_time is not None else None,
        "segment": {
            "start": round(selection.segment.start, 3),
            "end": round(selection.segment.end, 3),
            "cut_score": round(selection.segment.cut_score, 3),
        },
    }


def build_keyframes(video: Path, selections: list[Selection], cues: list[Cue], options: argparse.Namespace) -> list[dict]:
    capture_width = options.keyframe_width * 2
    capture_height = options.keyframe_height * 2
    keyframes = []
    for index, selection in enumerate(selections):
        image = capture_frame(video, selection.time, capture_width, capture_height)
        keyframes.append({
            "id": f"K{index + 1}",
            "time": selection.time,
            "score": round(selection.score, 3),
            "reason": selection.reason,
            "segment": {
                "start": selection.segment.start,
                "end": selection.segment.end,
                "cut_score": selection.segment.cut_score,
            },
            "subtitle_anchor": pick_subtitle_anchor(selection.time, cues, options),
            "image": image,
        })
    return keyframes


def process_sheet(
    video: Path,
    all_cues: list[Cue],
    title: str,
    start: float,
    max_end: float,
    index: int,
    out_dir: Path,
    options: argparse.Namespace,
) -> dict:
    candidate_end = min(max_end, start + options.max_sheet_sec)
    candidate_cues = cues_in_range(all_cues, start, candidate_end)
    samples = sample_visual_frames(video, start, candidate_end, options)
    candidate_audio_levels = sample_audio_levels(video, start, candidate_end, options)
    annotate_audio_events(samples, candidate_audio_levels, options)
    analysis_max = max(
        options.max_keyframes,
        options.target_keyframes + 8,
        math.ceil((candidate_end - start) / max(1, options.max_segment_sec)) + options.target_keyframes,
    )
    analysis_options = argparse.Namespace(**{**vars(options), "max_keyframes": analysis_max})
    candidate_selections = pick_keyframe_selections(samples, start, candidate_end, candidate_cues, analysis_options)
    end = choose_adaptive_end(start, candidate_end, candidate_selections, options)
    cues = cues_in_range(candidate_cues, start, end)
    final_samples = samples_in_range(samples, start, end)
    audio_levels = [
        item for item in candidate_audio_levels
        if item["t"] >= start - 0.001 and item["t"] <= end + 0.001
    ]
    annotate_audio_events(final_samples, audio_levels, options)
    selections = pick_keyframe_selections(final_samples, start, end, cues, options)
    render_audio_levels = audio_levels if not options.dry_run else []

    sheet_path = out_dir / "sheets" / f"sheet-{index:03d}.png"
    sidecar_path = out_dir / "sidecars" / f"sheet-{index:03d}.txt"
    keyframes = []
    if not options.dry_run:
        keyframes = build_keyframes(video, selections, cues, options)
        image = render_sheet(title, start, end, final_samples, render_audio_levels, keyframes, cues, options)
        sheet_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(sheet_path)
        sidecar_path.write_text(make_sidecar(cues, start, end), "utf-8")

    keyframe_payload = []
    for frame_index, selection in enumerate(selections):
        anchor = pick_subtitle_anchor(selection.time, cues, options)
        keyframe_payload.append({
            "id": f"K{frame_index + 1}",
            **selection_to_dict(selection),
            "subtitle_anchor": anchor,
        })

    return {
        "index": index,
        "time_range": [round(start, 3), round(end, 3)],
        "duration": round(end - start, 3),
        "candidate_time_range": [round(start, 3), round(candidate_end, 3)],
        "candidate_keyframes": len(candidate_selections),
        "sample_count": len(final_samples),
        "audio_sample_count": len(audio_levels),
        "subtitle_count": len(cues),
        "keyframes": keyframe_payload,
        "sheet": str(sheet_path.relative_to(out_dir)) if not options.dry_run else None,
        "sidecar": str(sidecar_path.relative_to(out_dir)) if not options.dry_run else None,
    }


def build_manifest(video: Path, subtitle: Path | None, probe: dict, title: str, options: argparse.Namespace) -> dict:
    return {
        "title": title,
        "video": str(video),
        "subtitle": str(subtitle) if subtitle else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "probe": probe,
        "options": {
            key: value
            for key, value in vars(options).items()
            if key not in {"video", "subtitle", "out_dir"}
        },
        "sheets": [],
    }


def ensure_valid_video(path: Path, probe: dict) -> float:
    if not path.exists():
        raise RuntimeError(f"video does not exist: {path}")
    size = path.stat().st_size
    if size < 1024 * 1024:
        raise RuntimeError(f"video file looks incomplete ({size} bytes): {path}")
    duration_text = probe.get("format", {}).get("duration")
    try:
        duration = float(duration_text)
    except (TypeError, ValueError):
        raise RuntimeError("ffprobe could not read video duration") from None
    if duration <= 0:
        raise RuntimeError("ffprobe returned non-positive duration")
    return duration


def parse_layout(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("layout must look like 4x4, 5x4, or 4x3")
    columns = int(match.group(1))
    rows = int(match.group(2))
    if columns < 1 or rows < 1:
        raise argparse.ArgumentTypeError("layout dimensions must be positive")
    return columns, rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate AI-readable film-matinee sheets from a local film.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--from", dest="start", type=float, default=0.0)
    parser.add_argument("--to", dest="end", type=float)
    parser.add_argument("--max-sheets", type=int, default=3, help="0 means no explicit limit.")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--layout", type=parse_layout, default=(4, 4), help="Grid capacity, e.g. 4x4, 5x4, or 4x3.")
    parser.add_argument("--subtitle-style-include", default="", help="Regex for ASS styles to include.")
    parser.add_argument("--subtitle-style-exclude", default="JP|Ruby", help="Regex for ASS styles to exclude.")
    parser.add_argument("--subtitle-offset-sec", type=float, default=0.0, help="Shift subtitle cues by this many seconds.")

    parser.add_argument("--sample-step-sec", type=float, default=1.0)
    parser.add_argument("--sample-width", type=int, default=64)
    parser.add_argument("--sample-height", type=int, default=36)
    parser.add_argument("--min-sheet-sec", type=float, default=60.0)
    parser.add_argument("--max-sheet-sec", type=float, default=600.0)
    parser.add_argument("--target-keyframes", type=int, default=16)
    parser.add_argument("--max-keyframes", type=int)
    parser.add_argument("--min-segment-sec", type=float, default=4.0)
    parser.add_argument("--max-segment-sec", type=float, default=18.0)
    parser.add_argument("--visual-event-sensitivity", type=float, default=1.35)
    parser.add_argument("--micro-event-sensitivity", type=float, default=1.6)
    parser.add_argument("--micro-event-lookahead-sec", type=float, default=2.0)
    parser.add_argument("--min-micro-keyframe-gap-sec", type=float, default=2.0)
    parser.add_argument("--max-micro-event-keyframes-per-sheet", type=int, default=6)
    parser.add_argument("--regional-grid", type=int, default=4, help="Grid size for local visual-change detection.")
    parser.add_argument("--regional-change-weight", type=float, default=0.8, help="Weight for local region color/luma changes.")
    parser.add_argument("--color-island-threshold", type=float, default=60.0, help="Local color-band anomaly threshold for event candidates.")
    parser.add_argument("--color-island-event-weight", type=float, default=1.8, help="Weight used to rank color-island event candidates.")
    parser.add_argument("--color-island-window", type=int, default=2, help="Neighbor samples used to detect short color islands.")
    parser.add_argument("--max-keyframe-gap-sec", type=float, default=45.0, help="Add coverage frames when selected keyframes leave longer gaps.")
    parser.add_argument("--max-gap-keyframes-per-sheet", type=int, default=4)
    parser.add_argument("--action-gap-sec", type=float, default=20.0, help="Add motion-aware candidates when final keyframes leave active gaps longer than this.")
    parser.add_argument("--action-gap-min-activity", type=float, default=0.45, help="Minimum normalized activity score for motion-aware gap candidates.")
    parser.add_argument("--max-action-coverage-keyframes-per-sheet", type=int, default=4)
    parser.add_argument("--coverage-color-shift-threshold", type=float, default=72.0, help="RGB distance for color-shift coverage inside long gaps.")
    parser.add_argument("--similar-keyframe-window-sec", type=float, default=6.0, help="Drop near-duplicate keyframes within this time window.")
    parser.add_argument("--similar-keyframe-distance", type=float, default=0.06, help="Visual distance threshold for near-duplicate keyframes.")
    parser.add_argument("--motion-weight", type=float, default=260.0)
    parser.add_argument("--low-info-luma", type=float, default=0.035)
    parser.add_argument("--high-info-luma", type=float, default=0.97)

    parser.add_argument("--sheet-width", type=int, default=1600)
    parser.add_argument("--row-height", type=int, default=168)
    parser.add_argument("--keyframes-per-row", type=int)
    parser.add_argument("--auto-pack-rows", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--max-keyframes-per-row", type=int, default=6)
    parser.add_argument("--keyframe-width", type=int, default=230)
    parser.add_argument("--keyframe-height", type=int, default=130)
    parser.add_argument("--band-pixels-per-second", type=float, default=3.0)
    parser.add_argument("--min-band-width", type=float, default=10.0)
    parser.add_argument("--max-anchor-distance-sec", type=float, default=6.0)
    parser.add_argument("--anchor-max-chars", type=int, default=24)
    parser.add_argument("--anchor-max-words", type=int, default=6)
    parser.add_argument("--anchor-font-px", type=int, default=12)
    parser.add_argument("--audio-rail", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--audio-sample-rate", type=int, default=400)
    parser.add_argument("--audio-step-sec", type=float, default=0.25)
    parser.add_argument("--audio-rail-height", type=int, default=12)
    parser.add_argument("--audio-gain", type=float, default=2.2)
    parser.add_argument("--audio-event-weight", type=float, default=130.0, help="Weight for audio transients as keyframe candidates. Use 0 to disable.")
    parser.add_argument("--audio-event-threshold", type=float, default=0.55, help="Normalized transient score needed for audio-only event candidates.")
    parser.add_argument("--audio-event-window-sec", type=float, default=0.5, help="Audio window around each visual sample.")
    parser.add_argument("--audio-event-context-sec", type=float, default=2.0, help="Local context used to score audio transients.")
    parser.add_argument("--audio-event-min-level", type=float, default=0.12, help="Ignore audio transient candidates below this rendered level.")
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    layout_columns, layout_rows = options.layout
    if options.keyframes_per_row is None:
        options.keyframes_per_row = layout_columns
    if options.max_keyframes is None:
        options.max_keyframes = layout_columns * layout_rows
    options.layout = f"{layout_columns}x{layout_rows}"
    video = options.video.expanduser().resolve()
    subtitle = options.subtitle.expanduser().resolve() if options.subtitle else None
    out_dir = options.out_dir.expanduser().resolve()
    options.out_dir = out_dir

    probe = probe_video(video)
    duration = ensure_valid_video(video, probe)
    title = options.title or video.stem
    start = max(0.0, options.start)
    end = min(duration, options.end if options.end is not None else duration)
    if start >= end:
        raise RuntimeError(f"empty requested range: {start}..{end}")

    out_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_subtitles(subtitle, options.subtitle_style_include, options.subtitle_style_exclude)
    cues = shift_cues(cues, options.subtitle_offset_sec)
    manifest = build_manifest(video, subtitle, probe, title, options)
    manifest_path = out_dir / "manifest.json"
    if options.start_index > 0 and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["options"] = build_manifest(video, subtitle, probe, title, options)["options"]
            manifest["sheets"] = [
                item for item in manifest.get("sheets", [])
                if int(item.get("index", -1)) < options.start_index
            ]
        except (OSError, json.JSONDecodeError):
            pass

    print(f"[film-matinee] video={video}")
    print(f"[film-matinee] duration={fmt_time(duration)} requested={fmt_time(start)}-{fmt_time(end)}")
    print(f"[film-matinee] subtitles={len(cues)}")
    print(f"[film-matinee] out={out_dir}")

    current = start
    index = max(0, options.start_index)
    generated = 0
    limit = options.max_sheets if options.max_sheets > 0 else math.inf
    while current < end - 0.001 and generated < limit:
        print(f"[film-matinee] sheet {index:03d} from {fmt_time(current)}", flush=True)
        item = process_sheet(video, cues, title, current, end, index, out_dir, options)
        manifest["sheets"].append(item)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        current = item["time_range"][1]
        print(
            f"[film-matinee] -> {fmt_time(item['time_range'][0])}-{fmt_time(item['time_range'][1])} "
            f"{len(item['keyframes'])} keyframes, {item['subtitle_count']} cues, {item['audio_sample_count']} audio bins",
            flush=True,
        )
        index += 1
        generated += 1
        if current >= end - 0.001:
            break

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    print(f"[film-matinee] wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[film-matinee] error: {exc}", file=sys.stderr)
        raise SystemExit(1)
