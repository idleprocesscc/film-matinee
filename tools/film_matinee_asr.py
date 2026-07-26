#!/usr/bin/env python3
"""Timestamped audio transcription for film-matinee.

The default auto mode is deliberately local-only: it uses an already-cached
OpenAI Whisper model and never downloads a model or invokes a paid API without
an explicit backend choice. Network backends use the same Whisper-compatible
multipart shape as bradautomates/claude-video (MIT; see NOTICE).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import Request, urlopen


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "whisper"


class ASRUnavailable(RuntimeError):
    """Raised when the requested transcription backend cannot be used."""


@dataclass
class ASRSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


def cached_model_path(model: str, model_dir: Path = DEFAULT_MODEL_DIR) -> Path:
    return model_dir.expanduser() / f"{model}.pt"


def _dotenv_value(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text("utf-8", "ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        return value or None
    return None


def load_api_key(backend: str) -> str | None:
    name = "GROQ_API_KEY" if backend == "groq" else "OPENAI_API_KEY"
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    for path in (
        Path.home() / ".config" / "film-matinee" / ".env",
        Path.home() / ".config" / "watch" / ".env",
        Path.cwd() / ".env",
    ):
        value = _dotenv_value(path, name)
        if value:
            return value
    return None


def resolve_asr_backend(
    mode: str,
    model: str,
    *,
    model_dir: Path = DEFAULT_MODEL_DIR,
) -> dict:
    mode = str(mode or "off").strip().lower()
    if mode == "off":
        return {"active": False, "mode": mode, "reason": "disabled"}
    if mode in {"auto", "local"}:
        module_available = importlib.util.find_spec("whisper") is not None
        cached = cached_model_path(model, model_dir)
        if mode == "auto" and (not module_available or not cached.exists()):
            reason = "local-whisper-module-missing" if not module_available else "local-model-not-cached"
            return {
                "active": False,
                "mode": mode,
                "backend": "local",
                "model": model,
                "reason": reason,
                "expected_model_path": str(cached),
            }
        if not module_available:
            raise ASRUnavailable("local Whisper Python module is not installed")
        return {
            "active": True,
            "mode": mode,
            "backend": "local",
            "model": model,
            "model_path": str(cached),
            "download_allowed": mode == "local",
        }
    if mode in {"groq", "openai"}:
        key = load_api_key(mode)
        if not key:
            name = "GROQ_API_KEY" if mode == "groq" else "OPENAI_API_KEY"
            raise ASRUnavailable(f"{name} is required for audio_transcript={mode}")
        return {
            "active": True,
            "mode": mode,
            "backend": mode,
            "model": GROQ_MODEL if mode == "groq" else OPENAI_MODEL,
        }
    raise ASRUnavailable(f"unknown audio transcript mode: {mode}")


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(detail or f"command failed: {' '.join(command)}")
    return result


def extract_audio_range(video: Path, start: float, end: float, out_path: Path) -> Path:
    duration = max(0.001, end - start)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-b:a",
        "64k",
        "-y",
        str(out_path),
    ])
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no audio; the source may have no audio stream")
    if out_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"audio chunk is {out_path.stat().st_size / (1024 * 1024):.1f} MB; "
            "reduce max_sheet_sec so it stays below the 24 MB upload boundary"
        )
    return out_path


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def segments_from_response(data: dict, offset: float = 0.0) -> list[ASRSegment]:
    segments: list[ASRSegment] = []
    for item in data.get("segments") or []:
        text = _clean_text(item.get("text", ""))
        if not text:
            continue
        avg_logprob = item.get("avg_logprob")
        no_speech_prob = item.get("no_speech_prob")
        compression_ratio = item.get("compression_ratio")
        try:
            avg_logprob = float(avg_logprob) if avg_logprob is not None else None
            no_speech_prob = float(no_speech_prob) if no_speech_prob is not None else None
            compression_ratio = float(compression_ratio) if compression_ratio is not None else None
        except (TypeError, ValueError):
            avg_logprob = no_speech_prob = compression_ratio = None
        if no_speech_prob is not None and avg_logprob is not None:
            if no_speech_prob >= 0.8 and avg_logprob <= -1.0:
                continue
        if compression_ratio is not None and compression_ratio > 3.0:
            continue
        start = max(0.0, float(item.get("start") or 0.0)) + offset
        end = max(start, float(item.get("end") or 0.0) + offset)
        if end <= start:
            continue
        segments.append(ASRSegment(
            round(start, 3),
            round(end, 3),
            text,
            avg_logprob,
            no_speech_prob,
        ))
    return segments


_LOCAL_MODELS: dict[tuple[str, str], object] = {}


def transcribe_local(
    audio_path: Path,
    *,
    model: str,
    model_dir: Path = DEFAULT_MODEL_DIR,
    language: str = "",
    device: str = "cpu",
    download_allowed: bool = False,
) -> list[ASRSegment]:
    cached = cached_model_path(model, model_dir)
    if not cached.exists() and not download_allowed:
        raise ASRUnavailable(f"local Whisper model is not cached: {cached}")
    try:
        import whisper
    except ImportError as exc:
        raise ASRUnavailable("local Whisper Python module is not installed") from exc
    load_name = model if download_allowed else str(cached)
    key = (load_name, device)
    whisper_model = _LOCAL_MODELS.get(key)
    if whisper_model is None:
        whisper_model = whisper.load_model(load_name, device=device, download_root=str(model_dir))
        _LOCAL_MODELS[key] = whisper_model
    result = whisper_model.transcribe(
        str(audio_path),
        language=language or None,
        task="transcribe",
        fp16=device != "cpu",
        verbose=None,
        temperature=0,
        condition_on_previous_text=True,
    )
    return segments_from_response(result)


def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----FilmMatineeBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buffer = io.BytesIO()
    for name, value in fields.items():
        buffer.write(f"--{boundary}".encode()); buffer.write(eol)
        buffer.write(f'Content-Disposition: form-data; name="{name}"'.encode()); buffer.write(eol + eol)
        buffer.write(value.encode()); buffer.write(eol)
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buffer.write(f"--{boundary}".encode()); buffer.write(eol)
    buffer.write(f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode())
    buffer.write(eol)
    buffer.write(f"Content-Type: {mime}".encode()); buffer.write(eol + eol)
    buffer.write(file_path.read_bytes()); buffer.write(eol)
    buffer.write(f"--{boundary}--".encode()); buffer.write(eol)
    return buffer.getvalue(), boundary


def transcribe_api(
    audio_path: Path,
    *,
    backend: str,
    language: str = "",
) -> list[ASRSegment]:
    api_key = load_api_key(backend)
    if not api_key:
        raise ASRUnavailable(f"API key is missing for {backend}")
    endpoint = GROQ_ENDPOINT if backend == "groq" else OPENAI_ENDPOINT
    model = GROQ_MODEL if backend == "groq" else OPENAI_MODEL
    fields = {"model": model, "response_format": "verbose_json", "temperature": "0"}
    if language:
        fields["language"] = language
    body, boundary = _multipart(fields, audio_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "film-matinee/1.0 (python-urllib)",
    }
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(3):
        request = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=300, context=context) as response:
                payload = response.read().decode("utf-8", "replace")
            data = json.loads(payload)
            segments = segments_from_response(data)
            if not segments:
                raise RuntimeError("Whisper returned no timestamped segments")
            return segments
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"{backend} Whisper HTTP {exc.code}: {detail}") from exc
            last_error = RuntimeError(f"{backend} Whisper HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{backend} Whisper failed after retries: {last_error}")


def transcribe_video_range(
    video: Path,
    start: float,
    end: float,
    *,
    backend: str,
    model: str = "medium",
    language: str = "",
    device: str = "cpu",
    download_allowed: bool = False,
) -> list[ASRSegment]:
    with tempfile.TemporaryDirectory(prefix="film-matinee-asr-") as temporary:
        audio = extract_audio_range(video, start, end, Path(temporary) / "audio.mp3")
        if backend == "local":
            segments = transcribe_local(
                audio,
                model=model,
                language=language,
                device=device,
                download_allowed=download_allowed,
            )
        else:
            segments = transcribe_api(audio, backend=backend, language=language)
    shifted = []
    for segment in segments:
        shifted.append(ASRSegment(
            round(segment.start + start, 3),
            round(segment.end + start, 3),
            segment.text,
            segment.avg_logprob,
            segment.no_speech_prob,
        ))
    return shifted


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_srt(segments: list[ASRSegment], path: Path) -> None:
    blocks = [
        f"{index}\n{_srt_time(segment.start)} --> {_srt_time(segment.end)}\n{segment.text}"
        for index, segment in enumerate(segments, 1)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), "utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe a film range into an independent ASR track.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=float, default=0.0)
    parser.add_argument("--to", dest="end", type=float, required=True)
    parser.add_argument("--backend", choices=("local", "groq", "openai"), default="local")
    parser.add_argument("--model", default="medium")
    parser.add_argument("--language", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    video = options.video.expanduser().resolve()
    segments = transcribe_video_range(
        video,
        options.start,
        options.end,
        backend=options.backend,
        model=options.model,
        language=options.language,
        device=options.device,
        download_allowed=options.backend == "local",
    )
    if options.out:
        write_srt(segments, options.out.expanduser().resolve())
    print(json.dumps({
        "backend": options.backend,
        "model": options.model if options.backend == "local" else (GROQ_MODEL if options.backend == "groq" else OPENAI_MODEL),
        "time_range": [options.start, options.end],
        "segments": [asdict(segment) for segment in segments],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[film-matinee] ASR error: {exc}", file=sys.stderr)
        raise SystemExit(1)
