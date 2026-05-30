# cove-cinema

> 极简放映室后端 — 给"我和我的 AI 一起看电影"用的。
>
> 扫文件夹列片，HTTP Range 流，SRT 字幕按区间增量返回。**没有上传、没有抽帧、不依赖 ffmpeg。**
> 截图给 AI 看当前画面这步由你自己的前端 canvas 实时抓，本服务不参与。

## 适用场景

你已经有一套自己写的"和 Claude / 其他 AI 聊天"的前端（不管是浏览器 web 还是别的）。
你想在聊天的同时跟 AI 一起看电影，让 AI 能知道：

- 你正在看哪部片
- 当前播到几分几秒
- 自上次发消息之后这段播了哪些字幕
- 当前画面长什么样

这个服务负责前三条。第四条（截图）由你前端 canvas 在发消息时抓当前帧塞 images 数组。

## 装

```bash
git clone <this-repo> cove-cinema
cd cove-cinema
pip install -r requirements.txt
```

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
COVE_CINEMA_PORT=8800
COVE_CINEMA_BIND=0.0.0.0
COVE_CINEMA_ROOT=/data/films
COVE_CINEMA_PREFIX=/cinema           # 路由前缀，默认 /cinema
COVE_CINEMA_ALLOW_ORIGIN=https://your.site  # 跨域时设；同源不用
```

部署模板见 `examples/`：
- `launchd.com.cove-cinema.plist.example` — Mac mini
- `systemd-cove-cinema.service.example` — Linux/VPS

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

集成的 4 步（详见 `examples/INTEGRATION.md`）：

1. **挂浮窗**：app 启动时调 `cinemaPlayer.init({ baseUrl: '/cinema' })`，浮窗就挂上了
2. **片库页**：`GET /cinema/list` 拿片列表，点开调 `cinemaPlayer.open(id, title)`
3. **发消息前**：调 `cinemaPlayer.status()` 拿当前 ts，`fetch('/cinema/sync/{id}?from=lastTs&to=curTs')` 拿增量字幕，`cinemaPlayer.snapshot()` 拿当前帧
4. **拼进 chat payload**：字幕放 text 前缀，截图 dataURL 拆成 `{media_type, data}` 加进 images 数组

## 嵌入式（不想跑独立服务）

如果你已经有自己的 aiohttp 服务，可以直接挂到一起：

```python
from aiohttp import web
from server import setup_routes  # 或 from cove_cinema_server import setup_routes
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
