# Visual Context — film-matinee sheet 原型

这个原型把当前播放点附近的一段电影压成一张 `film-matinee sheet`：

```text
[K1 frame] ━━━ color band ━━━ [K2 frame] ━ color band ━ [K3 frame]
   "I'm letting..."              "Until I..."              "What if..."
```

原则：

- 视觉材料保持为图像，不转写成“暖黄、低饱和”之类的报告。
- 完整字幕不画进图里，作为纯文本 sidecar 给 AI。
- Sheet 里只放关键帧附近的极短字幕开头，作为语义锚点。
- 默认使用多行 filmstrip，避免一条超长图被视觉模型压缩到看不清。

## 接入

```js
import { filmMatineePlayer } from './film-matinee-player.js';
import { createFilmMatineeVisualContext } from './film-matinee-visual-context.js';

filmMatineePlayer.init({ baseUrl: '/film-matinee' });

const filmMatineeVisual = createFilmMatineeVisualContext(filmMatineePlayer, {
  baseUrl: '/film-matinee',
  windowSec: 90,
  rowSec: 30,
  sheetWidth: 1600,
});
```

发消息前收集上下文：

```js
async function collectFilmMatineeContext() {
  const cin = await filmMatineeVisual.collect();
  return {
    textPrefix: cin.textPrefix,
    images: cin.images,
  };
}
```

默认 `collect()` 仍然生成“当前播放点附近”的窗口。如果要让 AI 线性预读电影，可以改用 adaptive 模式：

```js
const visual = await filmMatineeVisual.buildAdaptiveSheet({
  status: { id: filmId, title: filmTitle },
  from: lastSheetEnd || 0,
});

// 下一次从这里继续；为 null 表示已经到片尾。
lastSheetEnd = visual.nextFrom;
```

`collect()` 返回：

```js
{
  textPrefix: "...字幕 sidecar...",
  images: [
    { label: "film-matinee-sheet", media_type: "image/png", data: "..." }
  ],
  visual: {
    mode: "window",
    filmId: "...",
    timeRange: [480, 570],
    nextFrom: null,
    sheet: { dataUrl: "data:image/png;base64,..." },
    sidecar: "[字幕 8:00-9:30]...",
    subtitles: [...],
    keyframes: [...]
  }
}
```

## 拼进 Claude Messages API

```js
async function sendMessage(userText) {
  const cin = await filmMatineeVisual.collect();
  const content = [{ type: 'text', text: cin.textPrefix + userText }];

  for (const image of cin.images) {
    content.push({
      type: 'image',
      source: {
        type: 'base64',
        media_type: image.media_type,
        data: image.data,
      },
    });
  }

  await fetch('/your/chat/endpoint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages: [{ role: 'user', content }] }),
  });
}
```

OpenAI vision payload 可以直接用 `visual.sheet.dataUrl` 做 `image_url.url`。

## 参数

```js
createFilmMatineeVisualContext(filmMatineePlayer, {
  baseUrl: '/film-matinee',
  windowSec: 90,        // 当前播放点往前看多少秒
  minSheetSec: 60,      // adaptive sheet 至少覆盖多久
  maxSheetSec: 600,     // adaptive sheet 最多先分析多久
  targetKeyframesPerSheet: 16,
  rowSec: 30,           // 每行 filmstrip 表示多少秒
  sampleStepSec: 1,     // 色带采样间隔
  sheetWidth: 1600,
  keyframesPerRow: 4,
  maxKeyframes: 16,
  bandPixelsPerSecond: 2,
  includeCurrentFrame: false,
});
```

建议从两套模式开始：

- 聊天时看当前上下文：`buildWindowSheet()` / `collect()`，`windowSec=60-90`。
- 让 AI 线性“看完”一部电影：`buildAdaptiveSheet()`，默认 `targetKeyframesPerSheet=16`；轻量观影可降到 12，精读高密度段落可升到 20，`maxSheetSec=300-600`。

关键帧数量会按视觉信息密度自适应：普通段落较少，快切 / 蒙太奇 / 强视觉事件会补更多 micro keyframe。`maxKeyframes` 用来限制单张 sheet 的上限。

色带宽度按时间尺度计算：`bandPixelsPerSecond` 表示每秒占几个像素。这样每个采样柱等宽，色带不会因为某段连接器被拉得过大而抢走关键帧空间。

Adaptive sheet 把“一张图”当成信息单位，而不是固定分钟数：先在 `maxSheetSec` 范围内抽样，估算能选出多少关键帧；如果很快达到 `targetKeyframesPerSheet` 或 `maxKeyframes`，就提前截断；如果这一段视觉变化少，就覆盖更长时间。返回值里的 `nextFrom` 可用于下一张 sheet。

## 本地烟测页

如果片库里有 `Sintel Trailer (2010)`，可以启动 film-matinee 本地服务和静态文件服务后打开：

```bash
python server.py --root ~/film-matinee --port 8770 --allow-origin http://127.0.0.1:8788
python -m http.server 8788 --bind 127.0.0.1
```

然后访问：

```text
http://127.0.0.1:8788/examples/frontend/visual-test.html
```

如果 film-matinee 跑在别的端口：

```text
http://127.0.0.1:8788/examples/frontend/visual-test.html?base=http://127.0.0.1:8771/film-matinee
```

## 当前限制

- 这是前端原型：用隐藏 video 离屏 seek 抽帧，不会打断当前播放器，但第一次生成会有等待。
- 依赖浏览器能从 `/film-matinee/stream/{id}` seek。跨域部署时需要正确 CORS，否则 canvas 会被 taint。
- 还没有音轨 rail。
- 当前 shot detection 是启发式：用低分辨率抽样的色彩、亮度、对比、边缘变化来切段，再补强视觉事件 micro keyframe。它适合原型验证，但还没有接入专门的镜头检测模型。
