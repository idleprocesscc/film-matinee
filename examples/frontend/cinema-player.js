// cove-cinema 浮窗播放器 — 参考实现
//
// 设计：挂在 body 上的全局浮动 <video>，跨页面/路由不销毁。
// 可拖拽、可缩放、可最小化、可关闭。位置/大小 localStorage 持久化。
//
// 关键 API（给"聊天前端"调用）：
//   cinemaPlayer.init({ baseUrl })         初始化（一次性）
//   cinemaPlayer.open(id, title)           开始播放
//   cinemaPlayer.close()                   关闭并清 src
//   cinemaPlayer.status()                  → {id, title, ts} | null
//   cinemaPlayer.snapshot()                → "data:image/jpeg;base64,..." | null
//   cinemaPlayer.subscribe(fn)             订阅状态变化（可选）
//
// 朋友把这个文件丢进自己的 shell，按需改样式 / 改交互即可。
// 跟你自己的 chat 前端集成的细节见 examples/INTEGRATION.md。

const POS_KEY = 'cove-cinema:pos';
const SIZE_KEY = 'cove-cinema:size';

let baseUrl = '/cinema';
let wrap = null;
let videoEl = null;
let dragging = false, offX = 0, offY = 0;
let resizing = false, startW = 0, startH = 0, startX = 0, startY = 0;
let _state = { id: null, title: null, ts: 0, playing: false };
const subs = new Set();

function notify() { subs.forEach(fn => { try { fn({ ..._state }); } catch {} }); }

function loadPos() { try { return JSON.parse(localStorage.getItem(POS_KEY)) || null; } catch { return null; } }
function savePos(x, y) { localStorage.setItem(POS_KEY, JSON.stringify({ x, y })); }
function loadSize() { try { return JSON.parse(localStorage.getItem(SIZE_KEY)) || null; } catch { return null; } }
function saveSize(w, h) { localStorage.setItem(SIZE_KEY, JSON.stringify({ w, h })); }

function ensureDom() {
  if (wrap) return;
  wrap = document.createElement('div');
  wrap.id = 'cove-cinema';
  wrap.innerHTML = `
    <div class="cc-bar">
      <span class="cc-title" id="cc-title"></span>
      <button class="cc-btn cc-min" title="最小化">—</button>
      <button class="cc-btn cc-close" title="关闭">×</button>
    </div>
    <video id="cove-cinema-video" playsinline controls preload="metadata"></video>
    <div class="cc-resize" title="拖拽改变大小"></div>
  `;
  document.body.appendChild(wrap);
  videoEl = wrap.querySelector('#cove-cinema-video');
}

function applySavedGeom() {
  const pos = loadPos(), size = loadSize();
  if (size) { wrap.style.width = size.w + 'px'; wrap.style.height = size.h + 'px'; }
  if (pos) {
    wrap.style.left = pos.x + 'px';
    wrap.style.top = pos.y + 'px';
    wrap.style.right = 'auto';
    wrap.style.bottom = 'auto';
  }
}

function setupDrag() {
  const bar = wrap.querySelector('.cc-bar');
  bar.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button')) return;
    dragging = true;
    bar.setPointerCapture(e.pointerId);
    const r = wrap.getBoundingClientRect();
    offX = e.clientX - r.left;
    offY = e.clientY - r.top;
  });
  bar.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    let x = e.clientX - offX, y = e.clientY - offY;
    const w = wrap.offsetWidth, h = wrap.offsetHeight;
    x = Math.max(8, Math.min(window.innerWidth - w - 8, x));
    y = Math.max(8, Math.min(window.innerHeight - h - 8, y));
    wrap.style.left = x + 'px'; wrap.style.top = y + 'px';
    wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
  });
  bar.addEventListener('pointerup', () => {
    if (!dragging) return;
    dragging = false;
    const r = wrap.getBoundingClientRect();
    savePos(r.left, r.top);
  });
}

function setupResize() {
  const handle = wrap.querySelector('.cc-resize');
  handle.addEventListener('pointerdown', (e) => {
    resizing = true;
    handle.setPointerCapture(e.pointerId);
    startX = e.clientX; startY = e.clientY;
    startW = wrap.offsetWidth; startH = wrap.offsetHeight;
    e.stopPropagation();
  });
  handle.addEventListener('pointermove', (e) => {
    if (!resizing) return;
    const w = Math.max(240, Math.min(window.innerWidth - 20, startW + (e.clientX - startX)));
    const h = Math.max(180, Math.min(window.innerHeight - 20, startH + (e.clientY - startY)));
    wrap.style.width = w + 'px';
    wrap.style.height = h + 'px';
  });
  handle.addEventListener('pointerup', () => {
    if (!resizing) return;
    resizing = false;
    saveSize(wrap.offsetWidth, wrap.offsetHeight);
  });
}

function setupButtons() {
  wrap.querySelector('.cc-min').onclick = () => wrap.classList.toggle('cc-mini');
  wrap.querySelector('.cc-close').onclick = () => cinemaPlayer.close();
}

function setupVideo() {
  videoEl.addEventListener('play', () => { _state.playing = true; notify(); });
  videoEl.addEventListener('pause', () => { _state.playing = false; notify(); });
  videoEl.addEventListener('timeupdate', () => { _state.ts = videoEl.currentTime; notify(); });
}

export const cinemaPlayer = {
  init(opts) {
    if (opts?.baseUrl) baseUrl = opts.baseUrl.replace(/\/+$/, '');
    ensureDom();
    applySavedGeom();
    setupDrag();
    setupResize();
    setupButtons();
    setupVideo();
    wrap.style.display = 'none';
  },
  open(id, title) {
    ensureDom();
    wrap.querySelector('#cc-title').textContent = title || id;
    const newSrc = `${baseUrl}/stream/${encodeURIComponent(id)}`;
    if (videoEl.dataset.cinId !== id) {
      videoEl.src = newSrc;
      videoEl.dataset.cinId = id;
    }
    wrap.style.display = 'flex';
    wrap.classList.remove('cc-mini');
    _state = { id, title: title || id, ts: 0, playing: false };
    notify();
    videoEl.play().catch(() => {});
  },
  close() {
    if (!wrap) return;
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
    videoEl.dataset.cinId = '';
    wrap.style.display = 'none';
    _state = { id: null, title: null, ts: 0, playing: false };
    notify();
  },
  status() {
    if (!_state.id) return null;
    return { id: _state.id, title: _state.title, ts: videoEl?.currentTime ?? _state.ts ?? 0 };
  },
  // 抓当前帧 → JPEG dataURL。video 没就绪/没在播返 null。
  // 用于：发消息给 AI 前抓一帧塞进 images 数组。
  snapshot() {
    if (!videoEl || !videoEl.dataset.cinId || videoEl.readyState < 2) return null;
    const canvas = document.createElement('canvas');
    const ratio = videoEl.videoWidth / (videoEl.videoHeight || 1);
    canvas.width = Math.min(640, videoEl.videoWidth || 640);
    canvas.height = Math.round(canvas.width / (ratio || 16 / 9));
    canvas.getContext('2d').drawImage(videoEl, 0, 0, canvas.width, canvas.height);
    try { return canvas.toDataURL('image/jpeg', 0.7); }
    catch (e) { console.warn('[cove-cinema] snapshot failed', e); return null; }
  },
  subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
};
