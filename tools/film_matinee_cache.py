#!/usr/bin/env python3
"""Track and expire heavy URL source media without deleting film records."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MAX_AGE_HOURS = 24.0
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def config_dir() -> Path:
    override = os.environ.get("FILM_MATINEE_CONFIG_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else Path.home() / ".config" / "film-matinee"


def registry_path() -> Path:
    return config_dir() / "cache-registry.json"


def default_cache_root() -> Path:
    override = os.environ.get("FILM_MATINEE_DEFAULT_CACHE_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else Path.home() / ".film-matinee-cache"


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    path.chmod(0o600)


@contextmanager
def _registry_lock():
    path = registry_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:
        import msvcrt

        handle = path.open("a+b")
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
    else:
        handle = path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


@contextmanager
def _cache_lock(out_dir: Path):
    path = out_dir / "source" / ".cache-expiry.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:
        import msvcrt

        handle = path.open("a+b")
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
    else:
        handle = path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def _read_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "entries": {}}
    if not isinstance(data, dict):
        return {"version": 1, "entries": {}}
    data.setdefault("version", 1)
    data.setdefault("entries", {})
    if not isinstance(data["entries"], dict):
        data["entries"] = {}
    return data


def register_cache(out_dir: Path, source_kind: str = "url", when: datetime | None = None) -> None:
    out = out_dir.expanduser().resolve()
    timestamp = (when or utc_now()).isoformat()
    with _registry_lock():
        data = _read_registry()
        entry = dict(data["entries"].get(str(out)) or {})
        entry.update({
            "path": str(out),
            "source_kind": source_kind,
            "last_accessed_at": timestamp,
        })
        data["entries"][str(out)] = entry
        _atomic_json(registry_path(), data)


def _source_record_path(out_dir: Path) -> Path:
    return out_dir / "source" / "source.json"


def _read_source_record(out_dir: Path) -> dict[str, Any] | None:
    path = _source_record_path(out_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def touch_cache(out_dir: Path, when: datetime | None = None) -> bool:
    out = out_dir.expanduser().resolve()
    with _cache_lock(out):
        record = _read_source_record(out)
        if not record or (record.get("metadata") or {}).get("kind") != "url":
            return False
        timestamp = (when or utc_now()).isoformat()
        record["last_accessed_at"] = timestamp
        _atomic_json(_source_record_path(out), record)
    register_cache(out, "url", when=when)
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pid_is_running(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _job_is_running(out_dir: Path) -> bool:
    path = out_dir / ".film-matinee-generate.json"
    if not path.exists():
        return False
    try:
        job = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    status = str(job.get("status") or "").lower()
    return status in {"running", "running-untracked"} and _pid_is_running(job.get("pid"))


def _safe_url_video(out_dir: Path, record: dict[str, Any]) -> Path | None:
    if (record.get("metadata") or {}).get("kind") != "url":
        return None
    value = record.get("video_path")
    if not value:
        return None
    candidate = Path(str(value)).expanduser().resolve()
    source_root = (out_dir / "source").resolve()
    if source_root not in candidate.parents:
        return None
    if not candidate.name.startswith("video.") or candidate.suffix.lower() not in VIDEO_EXTS:
        return None
    return candidate


def _candidate_dirs(roots: Iterable[Path] = ()) -> list[Path]:
    candidates: set[Path] = set()
    registry = _read_registry()
    for entry in registry.get("entries", {}).values():
        if isinstance(entry, dict) and entry.get("path"):
            candidates.add(Path(str(entry["path"])).expanduser().resolve())
    default_root = default_cache_root()
    search_roots = {default_root.resolve(), *[root.expanduser().resolve() for root in roots]}
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        if _source_record_path(root).exists():
            candidates.add(root)
        for child in root.iterdir():
            if child.is_dir() and _source_record_path(child).exists():
                candidates.add(child.resolve())
    return sorted(candidates)


def cache_status(
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    roots: Iterable[Path] = (),
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or utc_now()
    output: list[dict[str, Any]] = []
    for out_dir in _candidate_dirs(roots):
        record = _read_source_record(out_dir)
        if not record:
            continue
        video = _safe_url_video(out_dir, record)
        last_used = (
            _parse_datetime(record.get("last_accessed_at"))
            or _parse_datetime(record.get("prepared_at"))
        )
        if last_used is None:
            last_used = datetime.fromtimestamp(_source_record_path(out_dir).stat().st_mtime, timezone.utc)
        age_hours = max(0.0, (current - last_used).total_seconds() / 3600)
        output.append({
            "path": str(out_dir),
            "title": record.get("title"),
            "source_url": record.get("source"),
            "video_path": str(video) if video else None,
            "video_exists": bool(video and video.exists()),
            "video_bytes": video.stat().st_size if video and video.exists() else 0,
            "last_accessed_at": last_used.isoformat(),
            "age_hours": round(age_hours, 3),
            "expires_at": (last_used + timedelta(hours=max_age_hours)).isoformat(),
            "expired": age_hours >= max_age_hours,
            "job_running": _job_is_running(out_dir),
            "deleted_at": record.get("source_media_deleted_at"),
        })
    return output


def cleanup_expired(
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    roots: Iterable[Path] = (),
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be greater than zero")
    current = now or utc_now()
    entries = cache_status(max_age_hours=max_age_hours, roots=roots, now=current)
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in entries:
        if not entry["expired"] or not entry["video_exists"]:
            continue
        out_dir = Path(str(entry["path"])).resolve()
        with _cache_lock(out_dir):
            record = _read_source_record(out_dir) or {}
            last_used = (
                _parse_datetime(record.get("last_accessed_at"))
                or _parse_datetime(record.get("prepared_at"))
            )
            if last_used and current - last_used < timedelta(hours=max_age_hours):
                skipped.append({"path": entry["path"], "reason": "recently-reactivated"})
                continue
            if _job_is_running(out_dir):
                skipped.append({"path": entry["path"], "reason": "generation-running"})
                continue
            video = _safe_url_video(out_dir, record)
            if video is None or not video.exists():
                continue
            size = video.stat().st_size
            if not dry_run:
                video.unlink()
                record["source_media_deleted_at"] = current.isoformat()
                record["source_media_deleted_bytes"] = size
                _atomic_json(_source_record_path(out_dir), record)
        deleted.append({
            "path": entry["path"],
            "video_path": str(video),
            "bytes": size,
            "dry_run": dry_run,
        })
    return {
        "checked_at": current.isoformat(),
        "max_age_hours": max_age_hours,
        "dry_run": dry_run,
        "caches_checked": len(entries),
        "files_deleted": 0 if dry_run else len(deleted),
        "files_would_delete": len(deleted) if dry_run else 0,
        "bytes_reclaimed": 0 if dry_run else sum(item["bytes"] for item in deleted),
        "bytes_would_reclaim": sum(item["bytes"] for item in deleted) if dry_run else 0,
        "deleted": deleted,
        "skipped": skipped,
    }


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or expire film-matinee URL source media.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "cleanup"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
        sub.add_argument("--root", action="append", type=Path, default=[])
        if name == "cleanup":
            sub.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    if options.command == "status":
        entries = cache_status(max_age_hours=options.max_age_hours, roots=options.root)
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    result = cleanup_expired(
        max_age_hours=options.max_age_hours,
        roots=options.root,
        dry_run=options.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    reclaimed = result["bytes_would_reclaim"] if options.dry_run else result["bytes_reclaimed"]
    print(f"[film-matinee] cache cleanup: {_format_bytes(reclaimed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
