# 集成指南 — 把 cove-cinema 接进你自己的聊天前端

这份给的不是"复制能跑的代码"，而是 **4 个关键挂点**。你的前端长啥样我不知道
（vanilla JS / React / Vue / Svelte 都行），但只要按这 4 步抓住挂点就能接上。

参考代码用 vanilla JS 写，逻辑直接抄不用改。

---

## 0. 前提

后端 `cove-cinema` 已经在跑（默认 `127.0.0.1:8770/cinema/*`）。
你的前端要能访问 `/cinema/list`、`/cinema/sync/...`、`/cinema/stream/...` 这几个路由
—— 同源最省事，跨域记得给后端配 `--allow-origin`。

下文假设这些路由都从前端能直接 fetch，不管你是同源还是反代。

---

## 1. 挂浮窗（一次性）

app 启动的最早时机（HTML 加载完、shell init 时），把浮窗挂上。
浮窗 DOM 自己创建，挂在 body，你的路由切换不要碰它。

```html
<!-- index.html 头里加 -->
<link rel="stylesheet" href="/static/cinema-player.css">
```

```js
// shell.js / main.js / app.js — 启动入口
import { cinemaPlayer } from './cinema-player.js';

cinemaPlayer.init({ baseUrl: '/cinema' });   // 跨域时填完整 URL
window.cinemaPlayer = cinemaPlayer;          // 可选：暴露给其他模块用
```

`baseUrl` 是 cove-cinema 的路由前缀。同源用 `/cinema`，跨域填
`https://films.your.site/cinema` 之类的。

挂完它默认隐藏。`open(id, title)` 才显示并开始播。

---

## 2. 片库页

在某个路由 / 页面里调 `GET /cinema/list` 渲染列表，点开调
`cinemaPlayer.open(id, title)` 启动播放。

```js
// 片名是文件夹名，理论可控；但你部署给别人用就别假设可控了 —— 务必转义。
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function renderCinemaPage(root) {
  const r = await fetch('/cinema/list');
  const { films } = await r.json();
  root.innerHTML = films.map(f => `
    <div class="film-card" data-id="${escapeHtml(f.id)}">
      <h3>${escapeHtml(f.title)}</h3>
      <p>${f.subtitle_count} 条字幕</p>
      <button>播放</button>
    </div>
  `).join('');
  root.querySelectorAll('.film-card').forEach(card => {
    card.querySelector('button').onclick = () => {
      cinemaPlayer.open(card.dataset.id, card.querySelector('h3').textContent);
    };
  });
}
```

不喜欢字符串拼接 + 转义？用 `document.createElement` + `textContent` 是更稳的写法，
就是代码长 3 倍。你 framework 用 React/Vue/Svelte 的话自带 escape，按平时风格写就行。

`cinemaPlayer.open()` 之后浮窗立刻可见，video 元素自己开始拉 `/cinema/stream/{id}`
（Range 流，浏览器原生处理 seek、缓冲，不用你管）。

---

## 3. 发消息前 — 采集 cinema 上下文

这是核心。每次你的聊天前端要发消息给 AI 时，**在调 chat API 前**先调一次
`collectCinemaContext()`，把当前帧 + 增量字幕拼上。

### 增量字幕的语义

回拖时不要重复给 AI 看过的字幕。维护一个"已注入高水位" Map：

```js
const cinemaInjected = {};  // { [filmId]: lastInjectedTs }
```

每次发消息时：
- 算 `lastTs = cinemaInjected[id] || 0`，`curTs = status.ts`
- 如果 `curTs > lastTs` → 请求 `[lastTs, curTs]` 区间的字幕
- 更新 `cinemaInjected[id] = max(lastTs, curTs)`
- 回拖检测：`curTs < lastTs - 30s` 视为重开（关掉重开同一片），
  把 lastTs 重置成 `max(0, curTs - 60)` 保留前一分钟字幕做衔接

### 完整函数

```js
const cinemaInjected = {};

function fmtTs(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

async function collectCinemaContext() {
  const st = cinemaPlayer.status();
  if (!st || !st.id) return { textPrefix: '', image: null };

  const curTs = st.ts || 0;
  let lastTs = cinemaInjected[st.id] ?? 0;
  if (curTs < lastTs - 30) lastTs = Math.max(0, curTs - 60);

  let subBlock = '';
  if (curTs > lastTs) {
    try {
      const url = `/cinema/sync/${encodeURIComponent(st.id)}?from=${lastTs}&to=${curTs}`;
      const r = await fetch(url);
      if (r.ok) {
        const data = await r.json();
        const lines = (data.subtitles || [])
          .map(c => `${fmtTs(c.start)} ${String(c.text || '').replace(/\n/g, ' ')}`)
          .join('\n');
        if (lines) subBlock = `[这段新字幕]\n${lines}\n[/这段新字幕]\n`;
      }
    } catch (e) { console.warn('[cinema] sync failed', e); }
    cinemaInjected[st.id] = Math.max(lastTs, curTs);
  }

  const header = `[正在和你看 ${st.title || st.id}，播到 ${fmtTs(curTs)}]\n`;
  const textPrefix = header + subBlock + '\n';

  let image = null;
  const shot = cinemaPlayer.snapshot();
  if (shot) {
    const m = shot.match(/^data:([^;]+);base64,(.+)$/);
    if (m) image = { media_type: m[1], data: m[2] };
  }
  return { textPrefix, image };
}
```

---

## 4. 拼进你的 chat payload

具体怎么塞看你 chat API 形态。**典型 Claude Messages API**
（`{role: 'user', content: [{type:'text', text}, {type:'image', source:{...}}]}`）的话：

```js
async function sendMessage(userText) {
  const cin = await collectCinemaContext();
  const text = cin.textPrefix + userText;

  const content = [{ type: 'text', text }];
  if (cin.image) {
    content.push({
      type: 'image',
      source: { type: 'base64', media_type: cin.image.media_type, data: cin.image.data },
    });
  }

  await fetch('/your/chat/endpoint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      messages: [{ role: 'user', content }],
      // 其他你的参数...
    }),
  });
}
```

如果你的 chat backend 接的是 OpenAI 格式（`vision`），image 部分改成
`{type:'image_url', image_url:{url: shotDataUrl}}` 即可 —— 直接用
`cinemaPlayer.snapshot()` 返回的完整 dataURL，不用拆。

---

## AI 那边收到啥

如果用户当前正在看《源代码（2011）》12:35 处，刚发了"现在在演啥"，AI 实际收到的是：

```
[正在和你看 源代码（2011），播到 12:35]
[这段新字幕]
11:25 那位科学家又联系不上了
11:32 哥们 我们没时间了
12:05 ……
12:30 ……
[/这段新字幕]

现在在演啥
```

加上当前帧的 JPEG 截图（dataURL）作为图片附件。

AI 综合字幕 + 画面 + 你的问题答你。

---

## 不要做的事

- ❌ **不要把 cinemaInjected 持久化到 localStorage**（除非你想过 corner case）。
  刷新页面后重置就好，下次发消息会从 0 注入到当前 ts，最多重复看一次字幕，没事。

- ❌ **不要在每次 timeupdate 都拉字幕**。只在 user 发消息时拉一次增量。
  你的聊天上下文里塞太多无关字幕只是浪费 token。

- ❌ **不要让 video 元素的 src 在切换 SPA 路由时被清掉**。
  cinema-player.js 把浮窗挂在 body 上就是为了让它跨路由不销毁。
  你的路由器卸载某个房间时，别误把 `<video>` 一起拆了。

- ❌ **不要把 cinema 上下文跟其他 context（reading-context、code 等）放在同一个 prefix 段里**。
  让 AI 能从前缀块名字看出来这段是干啥的，混在一起它分不清。

---

## 改样式

`cinema-player.css` 里的颜色 / 圆角 / z-index 全是建议值。
你的 app 有自己的设计语言就改 `.cc-bar` 背景、`.cc-btn` 颜色、
`#cove-cinema` 的 border-radius 等。JS 那边不用动。

挂位置不喜欢右下角？改 `#cove-cinema` 的 `right` / `bottom` 默认值。
用户拖过之后位置会存 localStorage，下次开还在原位。
