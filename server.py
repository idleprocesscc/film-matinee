"""clove-cinema — 极简放映室后端：扫目录 + Range 视频流 + SRT 字幕同步。

设计原则：不上传、不抽帧、不碰 ffmpeg。把电影 + 字幕丢在
CLOVE_CINEMA_ROOT/<片名>/ 下就出现在片库，没有"导入"这一步。

截图（给 AI 看当前帧）由前端 canvas 在发消息时实时抓，
后端不参与抓帧 —— 因为后端是个无头进程，看不到浏览器画面。

启动:
  python server.py                          # 默认 :8770，根目录 ~/cinema
  python server.py --port 8800 --root /data/films
  CLOVE_CINEMA_ROOT=/data/films python server.py

嵌入到已有 aiohttp 应用:
  from clove_cinema_server import setup_routes
  setup_routes(app, root=Path("/data/films"))
"""

import argparse
import json
import os
import re
from pathlib import Path

from aiohttp import web

DEFAULT_ROOT = Path.home() / "cinema"
STREAM_CHUNK = 1024 * 1024  # 1MB 一块推流

VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov", ".mkv")
_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",  # 浏览器多半放不了，下载时尽量挑 mp4 (H.264)
}

# SRT 时间戳：00:01:02,500 或 00:01:02.500
_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


# ---------- helpers ----------

def _json(data, status: int = 200, *, allow_origin: str = "") -> web.Response:
    headers = {}
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
    return web.json_response(
        data, status=status, headers=headers,
        dumps=lambda d: json.dumps(d, ensure_ascii=False),
    )


def _err(message: str, status: int = 400, *, allow_origin: str = "") -> web.Response:
    return _json({"error": message}, status=status, allow_origin=allow_origin)


def _safe_id(cid: str) -> bool:
    """id 是文件夹名（允许中文/空格），只挡路径穿越。"""
    return bool(cid) and "/" not in cid and "\\" not in cid \
        and ".." not in cid and not cid.startswith(".")


def _guess_mime(path: Path) -> str:
    return _VIDEO_MIME.get(path.suffix.lower(), "video/mp4")


def _ts_to_sec(h, m, s, ms) -> float:
    ms = ms.ljust(3, "0")  # "5" -> "500"
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(text: str):
    """容错 SRT 解析 → [{start,end,text}]（秒）。
    忽略序号行、兼容 ,/. 毫秒、CRLF、BOM、多行字幕。"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.split("\n")
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        stamps = _TS_RE.findall(lines[ts_idx])
        if len(stamps) < 2:
            continue
        start, end = _ts_to_sec(*stamps[0]), _ts_to_sec(*stamps[1])
        body = "\n".join(lines[ts_idx + 1:]).strip()
        if not body:
            continue
        cues.append({"start": start, "end": end, "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues


def _first_match(d: Path, exts):
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in exts:
            return p
    return None


def _resolve(root: Path, cid: str):
    """(dir, video_path|None, srt_path|None) 或 None。"""
    if not _safe_id(cid):
        return None
    d = root / cid
    if not d.is_dir():
        return None
    return d, _first_match(d, VIDEO_EXTS), _first_match(d, (".srt",))


def _film_info(d: Path):
    video = _first_match(d, VIDEO_EXTS)
    if not video:
        return None
    srt = _first_match(d, (".srt",))
    cues = parse_srt(srt.read_text("utf-8", "ignore")) if srt else []
    return {
        "id": d.name,
        "title": d.name,
        "video_file": video.name,
        "video_mime": _guess_mime(video),
        "video_size": video.stat().st_size,
        "has_subtitle": bool(cues),
        "subtitle_count": len(cues),
        # 注意：这个 duration 用字幕末尾算的，不是视频真长度
        # 如果字幕不全，duration 会偏小。装饰用，浏览器播放器自己读真实长度。
        "duration": cues[-1]["end"] if cues else 0,
    }


async def _send_file_range(resp: web.StreamResponse, path: Path, start: int, length: int):
    remaining = length
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            data = f.read(min(STREAM_CHUNK, remaining))
            if not data:
                break
            try:
                await resp.write(data)
            except ConnectionResetError:
                return  # 客户端断开（seek/换片），静默退出
            remaining -= len(data)
    try:
        await resp.write_eof()
    except ConnectionResetError:
        pass


# ---------- routes ----------

def _make_handlers(root: Path, allow_origin: str):

    async def http_list(request):
        films = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                info = _film_info(d)
                if info:
                    films.append(info)
        return _json({"films": films}, allow_origin=allow_origin)

    async def http_meta(request):
        r = _resolve(root, request.match_info["id"])
        if not r:
            return _err("not found", 404, allow_origin=allow_origin)
        info = _film_info(r[0])
        if not info:
            return _err("no video in folder", 404, allow_origin=allow_origin)
        return _json(info, allow_origin=allow_origin)

    async def http_sync(request):
        cid = request.match_info["id"]
        r = _resolve(root, cid)
        if not r:
            return _err("not found", 404, allow_origin=allow_origin)
        srt = r[2]
        cues = parse_srt(srt.read_text("utf-8", "ignore")) if srt else []
        try:
            frm = float(request.query.get("from", "0"))
            to = float(request.query.get("to", "0"))
        except ValueError:
            return _err("bad from/to", allow_origin=allow_origin)
        if to < frm:
            frm, to = to, frm
        hit = [c for c in cues if c["end"] >= frm and c["start"] <= to]
        return _json({"id": cid, "from": frm, "to": to, "subtitles": hit},
                     allow_origin=allow_origin)

    async def http_stream(request):
        r = _resolve(root, request.match_info["id"])
        if not r or not r[1]:
            return _err("not found", 404, allow_origin=allow_origin)
        video_path = r[1]
        file_size = video_path.stat().st_size
        mime = _guess_mime(video_path)

        base_headers = {"Accept-Ranges": "bytes", "Content-Type": mime}
        if allow_origin:
            base_headers["Access-Control-Allow-Origin"] = allow_origin

        # 默认无 Range：整片 200
        status = 200
        start, length = 0, file_size
        extra = {"Content-Length": str(file_size)}

        range_header = request.headers.get("Range")
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                return web.Response(status=416, headers={
                    **base_headers, "Content-Range": f"bytes */{file_size}",
                })
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "":
                length = int(end_s)
                start = max(0, file_size - length)
                end = file_size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                return web.Response(status=416, headers={
                    **base_headers, "Content-Range": f"bytes */{file_size}",
                })
            length = end - start + 1
            status = 206
            extra = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            }

        resp_headers = {**base_headers, **extra}

        # HEAD: 只发头不发 body，但要 mirror Range 的 206/Content-Range
        if request.method == "HEAD":
            return web.Response(status=status, headers=resp_headers)

        resp = web.StreamResponse(status=status, headers=resp_headers)
        await resp.prepare(request)
        await _send_file_range(resp, video_path, start, length)
        return resp

    async def http_options(request):
        """CORS preflight。"""
        if not allow_origin:
            return web.Response(status=405)
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Range, Content-Type",
            "Access-Control-Max-Age": "86400",
        })

    return http_list, http_meta, http_sync, http_stream, http_options


def setup_routes(app: web.Application, *, root: Path = DEFAULT_ROOT,
                 prefix: str = "/cinema", allow_origin: str = ""):
    """把 cinema 路由挂到一个已存在的 aiohttp app 上。

    prefix      路由前缀，默认 /cinema
    allow_origin  CORS 头值（"*" 或具体 origin），默认空 = 不发 CORS 头（同源）
    """
    root.mkdir(parents=True, exist_ok=True)
    h_list, h_meta, h_sync, h_stream, h_options = _make_handlers(root, allow_origin)
    # 静态路径先注册，避免被 {id} 通配吃掉
    # 注：aiohttp 的 add_get 自动包含 HEAD（同一 handler 内 method 分支处理）
    app.router.add_get(f"{prefix}/list", h_list)
    app.router.add_get(f"{prefix}/stream/{{id}}", h_stream)
    app.router.add_get(f"{prefix}/sync/{{id}}", h_sync)
    app.router.add_get(f"{prefix}/{{id}}/meta", h_meta)
    if allow_origin:
        app.router.add_options(f"{prefix}/{{tail:.*}}", h_options)


def main():
    p = argparse.ArgumentParser(description="clove-cinema — 极简放映室后端")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("CLOVE_CINEMA_PORT", "8770")))
    p.add_argument("--bind", default=os.environ.get("CLOVE_CINEMA_BIND", "127.0.0.1"),
                   help="监听地址，默认 127.0.0.1（仅本机）。VPS 上反代用就保持默认，"
                        "想直接对外暴露设 0.0.0.0")
    p.add_argument("--root", type=Path,
                   default=Path(os.environ.get("CLOVE_CINEMA_ROOT", str(DEFAULT_ROOT))),
                   help=f"视频根目录，默认 {DEFAULT_ROOT}")
    p.add_argument("--prefix", default=os.environ.get("CLOVE_CINEMA_PREFIX", "/cinema"),
                   help="路由前缀，默认 /cinema")
    p.add_argument("--allow-origin", default=os.environ.get("CLOVE_CINEMA_ALLOW_ORIGIN", ""),
                   help="CORS Access-Control-Allow-Origin 头，前端跟后端不同源时配。"
                        "例：--allow-origin https://your.site 或 --allow-origin '*'")
    args = p.parse_args()

    app = web.Application()
    setup_routes(app, root=args.root, prefix=args.prefix, allow_origin=args.allow_origin)
    print(f"[clove-cinema] root={args.root} prefix={args.prefix} bind={args.bind}:{args.port} "
          f"allow_origin={args.allow_origin!r}", flush=True)
    web.run_app(app, host=args.bind, port=args.port, print=None)


if __name__ == "__main__":
    main()
