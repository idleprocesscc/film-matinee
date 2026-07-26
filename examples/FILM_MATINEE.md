# film-matinee

`film-matinee` 是给 AI 线性读片的工作流：一部电影先被切成多个 chunk，每个 chunk 有一张视觉 sheet、一个字幕 sidecar、一份可同步的批注文件。正常观影用 `film_next` 一节一节读，`film_locate` 只在上下文太杂或用户明确提到时间/台词时兜底定位。

URL 素材入口受 [bradautomates/claude-video](https://github.com/bradautomates/claude-video) 的 `yt-dlp`、原生字幕优先与 VTT 工作流启发；长片视觉压缩、线性 chunks 和批注仍由 film-matinee 处理。

## 一句话导入 URL 或本地电影

Claude 已注册 MCP 时，推荐直接调用：

```text
film_open(
  source="https://example.com/video",
  layout="4x4",
  subtitle_languages="zh-Hans,zh-Hant,zh.*,en-orig,en.*,ja.*",
  ffmpeg_hwaccel="videotoolbox"
)
```

`source` 也可以是本地 `.mkv` / `.mp4` 路径。没有显式传字幕时，本地视频会先寻找同名 sidecar，再尝试提取文本型内嵌字幕；URL 会优先人工字幕，再选择自动字幕。VTT 原本带有 `<v Speaker>` 时会保留为 `[Speaker]`，不会在清理标签时丢掉已有角色信息。默认不会读取浏览器 cookies，需要时再显式传 `cookies_from_browser="chrome"`。

如果站点把字幕烧进画面而没有独立轨，macOS 默认会跨全片抽样探测；确认存在后，再按 chunk 用 Apple Vision 识别。sidecar 会明确写 `source=burned-subtitle-ocr` 和每段置信度，不把 OCR 冒充原始字幕。可传 `burned_subtitles="off"` 关闭，或用 `ocr_fps=3` 提高短台词覆盖（也会近似线性增加耗时）。已有 ASS/SRT/VTT/内封文本轨时始终优先使用原字幕。

同样在没有可靠原字幕时，`audio_transcript="auto"` 会使用本机已经缓存的 Whisper `medium` 模型生成独立 ASR 轨；不会自动下载模型或调用付费 API。OCR 与 ASR 会在 sidecar 里分成两个区块，冲突时交给 AI 结合画面判断。显式用 `audio_transcript="local"` 才允许下载缺失模型；`groq` / `openai` 必须显式选择并配置对应 key。

拿返回的 `out_dir` 看进度：

```text
film_generate_status(out_dir)
```

状态会显示 `phase: probing / downloading / generating / complete` 和 `available_sheets`。只要 `available_sheets` 大于 0，就能立刻 `film_start(manifest)`；追上后台进度时，`film_next` 会返回 waiting，稍后继续即可。

URL 下载的源视频在连续 24 小时没有 `film_overview` / `film_start` / `film_next` / `film_chunk` 活动后过期。清理只针对输出目录下安全识别出的 `source/video.*`；本地原片、sheets、sidecars、manifest、游标和批注均保留。运行中的生成任务会自动跳过。

## 生成 Sheet

```bash
python3 tools/generate_film_matinee_sheets.py \
  --video "$HOME/Downloads/蓦然回首.Look.Back.2024.2160p.WEB-DL.DDP5.1.H265.2Audio-ParkHD.mkv/蓦然回首.Look.Back.2024.2160p.WEB-DL.DDP5.1.H265.2Audio-ParkHD.mkv" \
  --subtitle "$HOME/Downloads/[SweetSub] Look Back.chs.ass" \
  --subtitle-offset-sec -29.5 \
  --title "Look Back (2024)" \
  --layout 4x4 \
  --target-keyframes 16 \
  --max-sheet-sec 420 \
  --sample-step-sec 1 \
  --subtitle-style-include '^(Text - CN|Default)' \
  --subtitle-style-exclude 'JP|Ruby' \
  --out-dir .film-matinee-cache/look-back-2024-film-matinee \
  --max-sheets 0
```

`--layout 4x4` 表示一张图最多 16 张关键帧，是默认的观影密度。想更轻地边看边聊，可以换成 `4x3`；想精读蒙太奇、动作或强视觉段落时，可以换成 `5x4` 提高信息密度。空格不是浪费，而是说明这一节没必要填满。

可选硬件解码：在命令末尾追加 `--ffmpeg-hwaccel videotoolbox`（macOS）、`--ffmpeg-hwaccel d3d11va`（Windows）、`--ffmpeg-hwaccel cuda --ffmpeg-hwaccel-device 0`（NVIDIA），或者用 `--ffmpeg-hwaccel auto` 让 ffmpeg 自选。它只加速/改变视频解码阶段，不改变关键帧算法；如果某个片源硬解失败，去掉参数即可回到默认 CPU 路径。

关键帧不是按固定秒数硬抽。生成器会综合全局/局部色彩变化、motion、短促 micro event、音频瞬态，以及超过约 20 秒仍有动作变化的空档覆盖。暗场会看边缘、对比、纹理和饱和度；低调摄影、夜景和霓虹暗部不会因为“暗”本身被丢掉。

## Claude / MCP

```json
{
  "mcpServers": {
    "film-matinee": {
      "command": "python3",
      "args": [
        "/path/to/film-matinee/tools/film_matinee_reader_mcp.py"
      ]
    }
  }
}
```

常用工具：

- `film_open(source, ...)`：URL / 本地路径的一键入口，自动准备素材并生成。
- `film_open(source, burned_subtitles="auto", ocr_fps=2, ...)`：没有字幕轨时在 macOS 自动探测并 OCR 烧录字幕。
- `film_open_command(source, ...)`：只返回准备命令，不执行。
- `film_generate(video_path, subtitle_path="", out_dir="", ...)`：从本地视频/字幕生成 sheets。默认后台运行，完成后读返回的 `manifest`。
- `film_generate_status(out_dir)`：查看后台生成进度和最新 sheet。
- `film_generate_command(video_path, ...)`：只返回命令，适合用户想先检查参数时。
- `film_overview(manifest_path)`：看一共有多少 chunk。
- `film_start(manifest_path, start_index=0)`：从某节开始，并返回该 sheet 图像和 sidecar。
- `film_next(manifest_path)`：正常线性观影。
- `film_chunk(manifest_path, index)`：直接读某节。
- `film_locate(manifest_path, timecode="", text="")`：兜底检索。
- `film_refine_chunk(manifest_path, chunk_index, pin_times="01:30:39")`：已知某个短事件遗漏时重做单节，指定时刻优先保留；后续 chunks、游标和批注不变。
- `film_focus_range(manifest_path, start_time="46:30", end_time="47:45", detail="dense")`：临时生成 `5x4` 局部精读 sheet，不替换原 chunk，也不移动正常观影游标。
- `film_cache_status()`：查看 URL 源视频大小、最近活动和过期时间。
- `film_cache_cleanup(dry_run=true)`：预演当前过期清理；设 `dry_run=false` 才执行。
- `film_note(manifest_path, chunk_index, text, timecode="")`：AI 留批注。
- `film_reply(manifest_path, note_id, text, author="user")`：把聊天回复挂到某条批注下。
- `film_notes(manifest_path, chunk_index=None)`：读批注。

`film_start` 和显式 `film_chunk` 返回时会带 `[viewing-guide]`，提醒 AI：

- 这是被压缩成 sheet 的电影时间，不是一张普通信息图。
- 按从左到右、从上到下的顺序线性观看。
- 以画面为主，留意人物位置、构图、镜头距离、动作方向、光线、色彩、剪辑节奏和声音变化。
- 色带表示关键帧之间持续经过的画面时间、色彩和节奏；长短主要代表时长。
- 音频 rail 在 chunk 内归一化，只比较这一节内部的强弱。
- 关键帧下方短句只是语义锚点，完整字幕以 sidecar 为准。
- 有值得保留的观察时可以碎碎念或用 `film_note` 写入批注；没有也可以安静看完继续下一段。

连续调用 `film_next` 时会省略这段完整 tips，避免正常观影流里反复打断。

当某个蒙太奇、动作或视觉转折需要看细一点时，直接调用：

```text
film_focus_range(
  manifest_path="/path/to/manifest.json",
  start_time="46:30",
  end_time="47:45",
  detail="dense"
)
```

结果会直接返回一张高密 sheet 和同范围文字轨；缓存写入主输出目录的 `focus/`，但 canonical chunks、`film_next` 游标和批注结构不变。

### 让 Claude 从手动准备的本地资源导入

如果 Claude Code 已经注册了 MCP，也能直接从本地资源开始：

```text
用 film_generate 处理这部电影：
video_path=/path/to/movie.mkv
subtitle_path=/path/to/subtitles.ass
out_dir=.film-matinee-cache/movie-title
layout=4x4
subtitle_offset_sec=-29.5
ffmpeg_hwaccel=videotoolbox
```

然后让它调用：

```text
film_generate_status(".film-matinee-cache/movie-title")
film_overview(".film-matinee-cache/movie-title/manifest.json")
film_start(".film-matinee-cache/movie-title/manifest.json")
```

多部电影不会串台：每部电影一个 `out_dir`，游标状态和批注都存在这个目录里。导入新片时换一个新的 `out_dir` 即可。

### 局部补镜头

主流程仍然依靠无 AI 的通用视觉/音频算法。当用户或 AI 已经知道某个短事件发生在准确时刻，但 sheet 没保留下来时，不需要重跑全片：

```text
film_refine_chunk(
  manifest_path="/path/to/manifest.json",
  chunk_index=7,
  pin_times="01:30:39,01:30:42"
)
```

pin 是“让这些时刻进入本节关键帧竞争并优先保留”，不是给所有电影硬编码特殊规则。它是线性观影后的修补镜头，不取代正常选帧，也不把流程改成 search-first。

## 批注同步

MCP 写入的批注在输出目录的 `annotations.json`。可以启动一个本地桥接服务，让前端实时查看并回复：

```bash
python3 tools/film_matinee_notes_server.py \
  --manifest .film-matinee-cache/look-back-2024-film-matinee/manifest.json \
  --port 8792
```

打开 viewer：

```text
http://127.0.0.1:8788/examples/frontend/film-matinee-viewer.html?notes=http://127.0.0.1:8792
```

viewer 会轮询 `annotations.json`。AI 用 `film_note` 写的评论会出现在右侧；用户在评论下回复时，会追加到同一条 note 的 `replies` 数组里。

右侧 `Chunk Notes` 只显示当前 chunk 的批注；`All Notes` 是整部片子的批注入口，可以浏览 Claude/用户留下的全部评论，并跳回对应 chunk 继续看图和回复。

## 视觉密度

现在推荐两档：

- `4x4`：默认，适合完整读片。比 `5x4` 清爽一点，但比 `4x3` 保留更多镜头节奏。
- `4x3`：轻量观影档，适合慢节奏段落或更想压低单页信息量时用。
- `5x4`：高密度精读，适合蒙太奇、动作、表演变化很密的段落。信息更足，但更容易把 AI 推向整理/总结模式。

切分不是固定分钟数，而是按视觉/字幕/音频信息量自适应。信息密度高的段落会更短、关键帧更多；信息密度低的段落会覆盖更长时间。
