# film-matinee

> AI-first film reading tools: visual sheets, subtitle sidecars, linear chunk reading, and shared annotations.

`film-matinee` 把一部电影切成 AI 可以线性阅读的 chunk。每个 chunk 由一张视觉 sheet、一份字幕 sidecar 和一组可同步批注组成：AI 可以像读书一样一节一节“看”电影，也可以在值得停下的地方留下评论，用户继续在评论下聊天。

这个项目的灵感来自 [Echoes0302/clove-cinema](https://github.com/Echoes0302/clove-cinema)。`clove-cinema` 提供极简本地放映室；`film-matinee` 作为 AI 读片与观影批注的补足工具，专注于视觉压缩、字幕同步、MCP 线性阅读和共享批注。

## 现在能做什么

- 批量生成 `film-matinee sheet`：关键帧 + 色带 + 音频 rail + 极短字幕锚点。
- 把完整字幕作为 sidecar 文本交给 AI，避免把文字全塞进图片。
- 用 MCP 工具 `film_start` / `film_next` 让 AI 按顺序读 chunk。
- 用 `film_note` / `film_reply` 写入共享 `annotations.json`，前端 viewer 可实时显示。
- 保留原本轻量本地播放服务：扫文件夹、HTTP Range 视频流、SRT 字幕按区间返回。

## 快速开始

```bash
git clone <this-repo> film-matinee
cd film-matinee
pip install -r requirements.txt
```

线性读片工作流见 [examples/FILM_MATINEE.md](examples/FILM_MATINEE.md)。旧式播放器集成见 [examples/INTEGRATION.md](examples/INTEGRATION.md)。

## 生成视觉 Sheet

```bash
python3 tools/generate_film_matinee_sheets.py \
  --video /path/to/movie.mkv \
  --subtitle /path/to/subtitles.ass \
  --title "Movie Title" \
  --layout 5x4 \
  --target-keyframes 18 \
  --out-dir .cinema-cache/movie-title \
  --max-sheets 0
```

然后启动批注桥：

```bash
python3 tools/film_matinee_notes_server.py \
  --manifest .cinema-cache/movie-title/manifest.json \
  --port 8792
```

打开 viewer：

```text
http://127.0.0.1:8788/examples/frontend/film-matinee-viewer.html?notes=http://127.0.0.1:8792
```

## MCP 工具

```json
{
  "mcpServers": {
    "film-matinee": {
      "command": "python3",
      "args": ["./tools/film_matinee_reader_mcp.py"]
    }
  }
}
```

Claude Code 也可以直接注册当前 checkout：

```bash
claude mcp add -s local film-matinee -- python3 "$PWD/tools/film_matinee_reader_mcp.py"
```

常用工具：

- `film_generate(video_path, subtitle_path="", out_dir="", ...)`：从本地视频/字幕生成 sheets。
- `film_generate_status(out_dir)`：查看后台生成进度。
- `film_generate_command(video_path, ...)`：只生成命令，不执行。
- `film_overview(manifest_path)`：查看 chunk 索引。
- `film_start(manifest_path, start_index=0)`：从某节开始读。
- `film_next(manifest_path)`：继续下一节。
- `film_chunk(manifest_path, index)`：读取指定 chunk。
- `film_locate(manifest_path, timecode="", text="")`：兜底定位。
- `film_note(manifest_path, chunk_index, text, timecode="")`：AI 留批注。
- `film_reply(manifest_path, note_id, text, author="user")`：把聊天挂在批注下。

多部电影互不影响：每部电影生成到一个独立 `out_dir`，里面有自己的 `manifest.json`、`.film-matinee-state.json` 和 `annotations.json`。Claude 读哪部电影，就把那部电影的 `manifest.json` 传给 `film_start` / `film_next`。

## Local Cinema Server

下面这部分保留了 `clove-cinema` 风格的本地放映室后端：扫目录列片、HTTP Range 流、SRT 字幕按区间增量返回。

## 适用场景

你已经有一套自己写的"和 Claude / 其他 AI 聊天"的前端（不管是浏览器 web 还是别的）。
你想在聊天的同时跟 AI 一起看电影，让 AI 能知道：

- 你正在看哪部片
- 当前播到几分几秒
- 自上次发消息之后这段播了哪些字幕
- 当前画面长什么样

这个服务负责前三条。第四条（截图）由你前端 canvas 在发消息时抓当前帧塞 images 数组。

依赖就一个 `aiohttp>=3.9`。Python 3.9+。

## 起

最简单：

```bash
python server.py
# → 监听 127.0.0.1:8770，扫 ~/cinema/
```

带参数：

```bash
python server.py --port 8800 --root /data/films --bind 0.0.0.0
```

环境变量等价：

```bash
CLOVE_CINEMA_PORT=8800
CLOVE_CINEMA_BIND=0.0.0.0
CLOVE_CINEMA_ROOT=/data/films
CLOVE_CINEMA_PREFIX=/cinema           # 路由前缀，默认 /cinema
CLOVE_CINEMA_ALLOW_ORIGIN=https://your.site  # 跨域时设；同源不用
```

部署模板见 `examples/`：
- `launchd.com.clove-cinema.plist.example` — Mac mini
- `systemd-clove-cinema.service.example` — Linux/VPS

## 放片

在 `--root`（默认 `~/cinema/`）下建文件夹，名字 = 片名 = id。文件夹内丢视频和字幕：

```
~/cinema/
├── 源代码（2011）/
│   ├── source-code.mp4         # 任意文件名，取扫到的第一个视频
│   └── source-code.zh.srt      # 任意文件名，取第一个 .srt（可无字幕）
└── Hereditary (2018)/
    ├── hereditary.mp4
    └── hereditary.srt
```

视频格式：`.mp4` / `.m4v` / `.webm` / `.mov` / `.mkv`。
**强烈建议挑 H.264 编码的 mp4** —— mkv 容器和 HEVC 编码浏览器原生 `<video>` 多半放不了。

## HTTP API

| 路由 | 用途 | 返回 |
|---|---|---|
| `GET  /cinema/list` | 列片库 | `{films: [{id, title, video_size, has_subtitle, subtitle_count, duration, ...}]}` |
| `GET  /cinema/{id}/meta` | 单片元数据 | 同上单条 |
| `GET  /cinema/sync/{id}?from=&to=` | 拿 `[from, to]` 区间相交的字幕 | `{subtitles: [{start, end, text}]}` |
| `GET  /cinema/stream/{id}` | 视频流（认真支持 Range） | 206 / 200 / 416 |
| `HEAD /cinema/stream/{id}` | 拿总长 | 头里 `Content-Length` + `Accept-Ranges: bytes` |

`{id}` 是文件夹名（URL 编码）。`from` / `to` 是秒（浮点）。

`duration` 字段用字幕末尾 timestamp 算的，**不是视频真实长度**。装饰用 —— 浏览器播放器进度条会自己读真实长度。如果字幕不全，这里会偏小，不影响播放。

### CORS

默认不发 CORS 头，你的前端跟后端同源就够。

如果前端在 `https://your.site` 但后端跑别处（例：localhost:8770 或子域名），起服务时设：

```bash
python server.py --allow-origin https://your.site
# 或者宽松点：--allow-origin '*'
```

服务会在所有响应里发 `Access-Control-Allow-Origin`，并响应 OPTIONS preflight。

## 前端怎么集成

参考实现在 `examples/frontend/`：

- `cinema-player.js` — 浮窗播放器（拖拽 / 缩放 / 最小化 / 持久化位置 / `snapshot()` API）
- `cinema-player.css` — 样式
- `cinema-visual-context.js` — film-matinee sheet 原型：关键帧 + 色带 + 字幕 sidecar

集成的 4 步（详见 `examples/INTEGRATION.md`）：

1. **挂浮窗**：app 启动时调 `cinemaPlayer.init({ baseUrl: '/cinema' })`，浮窗就挂上了
2. **片库页**：`GET /cinema/list` 拿片列表，点开调 `cinemaPlayer.open(id, title)`
3. **发消息前**：调 `cinemaPlayer.status()` 拿当前 ts，`fetch('/cinema/sync/{id}?from=lastTs&to=curTs')` 拿增量字幕，`cinemaPlayer.snapshot()` 拿当前帧
4. **拼进 chat payload**：字幕放 text 前缀，截图 dataURL 拆成 `{media_type, data}` 加进 images 数组

如果要保留更多镜头语言，看 `examples/FILM_MATINEE.md` 和 `examples/VISUAL_CONTEXT.md`。`film-matinee` 是现在的 AI 线性读片工作流：批量生成视觉 sheet + 字幕 sidecar，并通过 MCP 一节一节读；旧的前端原型仍可用隐藏 video 抽取当前窗口。

## 嵌入式（不想跑独立服务）

如果你已经有自己的 aiohttp 服务，可以直接挂到一起：

```python
from aiohttp import web
from server import setup_routes  # 或 from clove_cinema_server import setup_routes
from pathlib import Path

app = web.Application()
# ... 你自己的路由 ...
setup_routes(app, root=Path.home() / "cinema", prefix="/cinema")
web.run_app(app)
```

## 常见坑

**浏览器不放视频** — 99% 是 codec 不对。mkv 容器或 HEVC (H.265) 编码 Chrome 都不认。
用 QuickTime 双击文件能放但浏览器不放，几乎一定是这个。换 H.264 mp4 片源。

**反复点开关停后卡住** — aiohttp 在客户端中途断开 Range 流时可能留下半死连接。
服务里已经加了 `ConnectionResetError` 兜底，但极端场景可能还是卡。这时重启服务就好。

**字幕乱码** — `.srt` 不是 UTF-8 编码。用 `iconv` 转一下：
```bash
iconv -f GBK -t UTF-8 input.srt > output.srt
```

**Safari 时间显示 `--:--`** — 进度条没出来。多半是 mp4 没 fast start（moov 在文件尾）。
用 ffmpeg remux 一下就行：`ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`

## License

MIT

The local cinema server portions include code adapted from [Echoes0302/clove-cinema](https://github.com/Echoes0302/clove-cinema), which states MIT licensing in its README. See [NOTICE](NOTICE) for attribution.

## Contributors

- GPT
- Claude
- koshi
