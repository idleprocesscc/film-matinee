#!/usr/bin/env python3
"""Local annotation bridge for generated film-matinee manifests."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _cors_headers(allow_origin: str) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
    return headers


def _json(data: Any, *, status: int = 200, allow_origin: str = "") -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers=_cors_headers(allow_origin),
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def _err(message: str, *, status: int = 400, allow_origin: str = "") -> web.Response:
    return _json({"error": message}, status=status, allow_origin=allow_origin)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise web.HTTPInternalServerError(text=f"invalid JSON: {path}") from exc


def _annotations_path(manifest_path: Path) -> Path:
    return manifest_path.parent / "annotations.json"


def _read_annotations(manifest_path: Path) -> dict[str, Any]:
    path = _annotations_path(manifest_path)
    if not path.exists():
        return {"version": 1, "annotations": []}
    data = _load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("annotations", []), list):
        raise web.HTTPInternalServerError(text=f"invalid annotations schema: {path}")
    data.setdefault("version", 1)
    data.setdefault("annotations", [])
    return data


def _write_annotations(manifest_path: Path, data: dict[str, Any]) -> None:
    path = _annotations_path(manifest_path)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


@contextmanager
def _annotations_lock(manifest_path: Path):
    """Acquire an exclusive lock around annotation read-modify-write cycles.

    Uses fcntl on Unix and msvcrt byte-range locks on Windows so the MCP
    server and notes bridge queue writes to the same annotations file.
    """
    lock_path = _annotations_path(manifest_path).with_suffix(".lock")
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


def _sheet_by_index(manifest: dict[str, Any], chunk_index: int) -> dict[str, Any]:
    for sheet in manifest.get("sheets", []):
        if int(sheet.get("index", -1)) == chunk_index:
            return sheet
    raise web.HTTPNotFound(text=f"chunk not found: {chunk_index}")


def _safe_manifest_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        path = path / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    return path


def _resolve_manifest_file(root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise web.HTTPForbidden(text="path is outside manifest directory")
    path = (root / rel_path).resolve()
    if root not in path.parents and path != root:
        raise web.HTTPForbidden(text="path is outside manifest directory")
    return path


def make_app(manifest_path: Path, *, allow_origin: str = "*") -> web.Application:
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent
    app = web.Application(client_max_size=1024 * 1024)

    async def http_root(request: web.Request) -> web.Response:
        return _json({
            "name": "film-matinee-notes",
            "manifest": str(manifest_path),
            "annotations": str(_annotations_path(manifest_path)),
        }, allow_origin=allow_origin)

    async def http_manifest(request: web.Request) -> web.Response:
        manifest = _load_json(manifest_path)
        return _json(manifest, allow_origin=allow_origin)

    async def http_file(request: web.Request) -> web.StreamResponse:
        rel = request.match_info["rel"]
        path = _resolve_manifest_file(root, rel)
        if not path.exists() or not path.is_file():
            return _err("not found", status=404, allow_origin=allow_origin)
        return web.FileResponse(path, headers=_cors_headers(allow_origin))

    async def http_annotations(request: web.Request) -> web.Response:
        return _json(_read_annotations(manifest_path), allow_origin=allow_origin)

    async def http_post_note(request: web.Request) -> web.Response:
        body = await request.json()
        manifest = _load_json(manifest_path)
        chunk_index = int(body.get("chunk_index", -1))
        sheet = _sheet_by_index(manifest, chunk_index)
        text = str(body.get("text", "")).strip()
        if not text:
            return _err("note text is empty", allow_origin=allow_origin)
        start, end = sheet.get("time_range", [0, 0])
        timecode = str(body.get("timecode", "")).strip()
        note = {
            "id": f"N{uuid.uuid4().hex[:8]}",
            "chunk_index": int(sheet.get("index", chunk_index)),
            "chunk_time_range": [float(start), float(end)],
            "timecode": timecode,
            "time_seconds": _parse_timecode(timecode) if timecode else None,
            "kind": str(body.get("kind", "observation") or "observation"),
            "visibility": str(body.get("visibility", "user") or "user"),
            "author": str(body.get("author", "user") or "user"),
            "text": text,
            "created_at": _now(),
            "replies": [],
        }
        with _annotations_lock(manifest_path):
            data = _read_annotations(manifest_path)
            data.setdefault("annotations", []).append(note)
            _write_annotations(manifest_path, data)
        return _json(note, status=201, allow_origin=allow_origin)

    async def http_post_reply(request: web.Request) -> web.Response:
        body = await request.json()
        note_id = request.match_info["note_id"]
        text = str(body.get("text", "")).strip()
        if not text:
            return _err("reply text is empty", allow_origin=allow_origin)
        with _annotations_lock(manifest_path):
            data = _read_annotations(manifest_path)
            for note in data.get("annotations", []):
                if note.get("id") == note_id:
                    reply = {
                        "id": f"R{uuid.uuid4().hex[:8]}",
                        "author": str(body.get("author", "user") or "user"),
                        "text": text,
                        "created_at": _now(),
                    }
                    note.setdefault("replies", []).append(reply)
                    _write_annotations(manifest_path, data)
                    return _json(reply, status=201, allow_origin=allow_origin)
        return _err("note not found", status=404, allow_origin=allow_origin)

    async def http_options(request: web.Request) -> web.Response:
        headers = {
            **_cors_headers(allow_origin),
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
        return web.Response(status=204, headers=headers)

    app.router.add_get("/", http_root)
    app.router.add_get("/manifest", http_manifest)
    app.router.add_get("/file/{rel:.*}", http_file)
    app.router.add_get("/annotations", http_annotations)
    app.router.add_post("/annotations", http_post_note)
    app.router.add_post("/annotations/{note_id}/replies", http_post_reply)
    app.router.add_options("/{tail:.*}", http_options)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a film-matinee manifest and shared annotations.")
    parser.add_argument("--manifest", type=str, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--allow-origin", default="*")
    args = parser.parse_args()

    manifest_path = _safe_manifest_path(args.manifest)
    print(
        f"[film-matinee-notes] manifest={manifest_path} bind={args.bind}:{args.port} "
        f"allow_origin={args.allow_origin!r}",
        flush=True,
    )
    web.run_app(
        make_app(manifest_path, allow_origin=args.allow_origin),
        host=args.bind,
        port=args.port,
        print=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
