# film-matinee

`film-matinee` 是给 AI 线性读片的工作流：一部电影先被切成多个 chunk，每个 chunk 有一张视觉 sheet、一个字幕 sidecar、一份可同步的批注文件。正常观影用 `film_next` 一节一节读，`film_locate` 只在上下文太杂或用户明确提到时间/台词时兜底定位。

## 生成 Sheet

```bash
python3 tools/generate_film_matinee_sheets.py \
  --video "$HOME/Downloads/蓦然回首.Look.Back.2024.2160p.WEB-DL.DDP5.1.H265.2Audio-ParkHD.mkv/蓦然回首.Look.Back.2024.2160p.WEB-DL.DDP5.1.H265.2Audio-ParkHD.mkv" \
  --subtitle "$HOME/Downloads/[SweetSub] Look Back.chs.ass" \
  --subtitle-offset-sec -29.5 \
  --title "Look Back (2024)" \
  --layout 5x4 \
  --target-keyframes 18 \
  --max-sheet-sec 420 \
  --sample-step-sec 1 \
  --subtitle-style-include '^(Text - CN|Default)' \
  --subtitle-style-exclude 'JP|Ruby' \
  --out-dir .cinema-cache/look-back-2024-film-matinee \
  --max-sheets 0
```

`--layout 5x4` 表示一张图最多 20 张关键帧。可以换成 `4x3` 降低视觉密度；空格不是浪费，而是说明这一节没必要填满。

## Claude / MCP

```json
{
  "mcpServers": {
    "film-matinee": {
      "command": "python3",
      "args": [
        "/Users/koshijia/Documents/New project/clove-cinema/tools/film_matinee_reader_mcp.py"
      ]
    }
  }
}
```

常用工具：

- `film_overview(manifest_path)`：看一共有多少 chunk。
- `film_start(manifest_path, start_index=0)`：从某节开始，并返回该 sheet 图像和 sidecar。
- `film_next(manifest_path)`：正常线性观影。
- `film_chunk(manifest_path, index)`：直接读某节。
- `film_locate(manifest_path, timecode="", text="")`：兜底检索。
- `film_note(manifest_path, chunk_index, text, timecode="")`：AI 留批注。
- `film_reply(manifest_path, note_id, text, author="user")`：把聊天回复挂到某条批注下。
- `film_notes(manifest_path, chunk_index=None)`：读批注。

每个 chunk 包里会带 `[viewing-guide]`，提醒 AI：

- sheet 是主要视觉来源，不要把图像替换成纯文字复述。
- 阅读顺序是从左到右、从上到下。
- 色带压缩关键帧之间经过的时间，长短只代表时长。
- 音频 rail 在 chunk 内归一化，只比较这一节内部的强弱。
- 关键帧下方短句只是语义锚点，完整字幕以 sidecar 为准。
- 看到值得留给用户的解释、疑问、母题或观影提示时，用 `film_note` 写入批注。

## 批注同步

MCP 写入的批注在输出目录的 `annotations.json`。可以启动一个本地桥接服务，让前端实时查看并回复：

```bash
python3 tools/film_matinee_notes_server.py \
  --manifest .cinema-cache/look-back-2024-film-matinee/manifest.json \
  --port 8792
```

打开 viewer：

```text
http://127.0.0.1:8788/examples/frontend/film-matinee-viewer.html?notes=http://127.0.0.1:8792
```

viewer 会轮询 `annotations.json`。AI 用 `film_note` 写的评论会出现在右侧；用户在评论下回复时，会追加到同一条 note 的 `replies` 数组里。

## 视觉密度

现在推荐两档：

- `5x4`：默认，适合完整读片。信息密度高，蒙太奇、动作、表演变化比较不容易漏。
- `4x3`：节奏慢或想保守 token 时用。单张图更清爽，但 chunk 可能变多。

切分不是固定分钟数，而是按视觉/字幕/音频信息量自适应。信息密度高的段落会更短、关键帧更多；信息密度低的段落会覆盖更长时间。
