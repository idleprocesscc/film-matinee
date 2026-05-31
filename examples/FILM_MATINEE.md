# film-matinee

`film-matinee` 是给 AI 线性读片的工作流：一部电影先被切成多个 chunk，每个 chunk 有一张视觉 sheet、一个字幕 sidecar、一份可同步的批注文件。正常观影用 `film_next` 一节一节读，`film_locate` 只在上下文太杂或用户明确提到时间/台词时兜底定位。

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

- `film_generate(video_path, subtitle_path="", out_dir="", ...)`：从本地视频/字幕生成 sheets。默认后台运行，完成后读返回的 `manifest`。
- `film_generate_status(out_dir)`：查看后台生成进度和最新 sheet。
- `film_generate_command(video_path, ...)`：只返回命令，适合用户想先检查参数时。
- `film_overview(manifest_path)`：看一共有多少 chunk。
- `film_start(manifest_path, start_index=0)`：从某节开始，并返回该 sheet 图像和 sidecar。
- `film_next(manifest_path)`：正常线性观影。
- `film_chunk(manifest_path, index)`：直接读某节。
- `film_locate(manifest_path, timecode="", text="")`：兜底检索。
- `film_note(manifest_path, chunk_index, text, timecode="")`：AI 留批注。
- `film_reply(manifest_path, note_id, text, author="user")`：把聊天回复挂到某条批注下。
- `film_notes(manifest_path, chunk_index=None)`：读批注。

每个 chunk 包里会带 `[viewing-guide]`，提醒 AI：

- 这是被压缩成 sheet 的电影时间，不是一张普通信息图。
- 按从左到右、从上到下的顺序线性观看。
- 以画面为主，留意人物位置、构图、镜头距离、动作方向、光线、色彩、剪辑节奏和声音变化。
- 色带表示关键帧之间持续经过的画面时间、色彩和节奏；长短主要代表时长。
- 音频 rail 在 chunk 内归一化，只比较这一节内部的强弱。
- 关键帧下方短句只是语义锚点，完整字幕以 sidecar 为准。
- 有值得保留的观察时可以碎碎念或用 `film_note` 写入批注；没有也可以安静看完继续下一段。

### 让 Claude 直接导入

如果 Claude Code 已经注册了 MCP，也能直接从本地资源开始：

```text
用 film_generate 处理这部电影：
video_path=/path/to/movie.mkv
subtitle_path=/path/to/subtitles.ass
out_dir=.film-matinee-cache/movie-title
layout=4x4
subtitle_offset_sec=-29.5
```

然后让它调用：

```text
film_generate_status(".film-matinee-cache/movie-title")
film_overview(".film-matinee-cache/movie-title/manifest.json")
film_start(".film-matinee-cache/movie-title/manifest.json")
```

多部电影不会串台：每部电影一个 `out_dir`，游标状态和批注都存在这个目录里。导入新片时换一个新的 `out_dir` 即可。

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
