// client/client.js — draws only what the server sends, and only what the asset table knows.
// No game logic lives here. No trap logic lives here. Ever.
//
//   /             manual play (WASD / arrows)
//   /?auto=bot    watch the naive bot autopilot  (connects as kind=bot)
//   /?auto=human  watch the human-like autopilot (connects as kind=bot: it is a script)
//   /?collect=CODE  human data collection: name, 10 minutes of manual play

import * as C from '/constants.js';
import { SPRITES, drawSprite } from '/client/sprites.js';

const params = new URLSearchParams(location.search);
const COLLECT = params.get('collect');
// No autopilot in a collection run: it would be recorded as a labelled human.
const AUTO = !COLLECT && ['bot', 'human'].includes(params.get('auto')) ? params.get('auto') : null;
const autoplay = AUTO ? await import('/client/autoplay.js') : null;
let brain = null;

const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const hud = document.getElementById('hud');
const overlay = document.getElementById('overlay');

let ws = null;
let latest = null;   // most recent 'state' message
let connected = false;
let finished = false;

// ── Human data collection ──────────────────────────────────────────────
// Per tab: a refresh or dropped connection resumes the same 10 minutes.

function storage(key, value) {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    sessionStorage.setItem(key, value);
  } catch { /* private mode: progress just won't survive a refresh */ }
  return value;
}

let collectName = storage('collectName');
const collectToken = storage('collectToken') ??
  storage('collectToken', Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => (b % 36).toString(36)).join(''));

function showOverlay(html) {
  overlay.innerHTML = `<div class="card">${html}</div>`;
  overlay.hidden = false;
}

function askName() {
  showOverlay(`
    <h1>Human data — 10 dəqiqə</h1>
    <p>Klaviatura ilə (WASD / oxlar) <b>özün</b> oyna, qızılları və sandıqları topla.</p>
    <p>Taymer 10 dəqiqədir. Səhifəni yeniləsən vaxt davam edir, amma başqa tab açma.</p>
    <form id="nameForm">
      <label for="nameInput">Adın</label>
      <input id="nameInput" maxlength="40" autocomplete="off" required autofocus>
      <button type="submit">Başla</button>
    </form>`);
  document.getElementById('nameForm').addEventListener('submit', (e) => {
    e.preventDefault();
    collectName = storage('collectName', document.getElementById('nameInput').value.trim() || 'anonymous');
    overlay.hidden = true;
    connect();
  });
}

function finish() {
  finished = true;
  held.clear();
  showOverlay(`<h1>Təşəkkürlər, ${escapeHtml(collectName)}!</h1>
    <p>10 dəqiqə tamamlandı, data yadda saxlanıldı. Xal: ${latest?.you?.score ?? 0}.</p>
    <p>Bu tabı bağlaya bilərsən.</p>`);
  ws?.close();
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

// ── Connection ─────────────────────────────────────────────────────────

function connect() {
  if (finished) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const query = COLLECT
    ? `kind=human&collect=${encodeURIComponent(COLLECT)}&token=${collectToken}&name=${encodeURIComponent(collectName)}`
    : `kind=${AUTO ? 'bot' : 'human'}`;
  ws = new WebSocket(`${proto}://${location.host}${C.PATHS.GAME}?${query}`);
  if (autoplay) brain = AUTO === 'bot' ? new autoplay.NaiveBrain() : new autoplay.HumanLikeBrain();

  ws.onopen = () => {
    connected = true;
    sendInput(true);
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type !== 'state') return;
    latest = msg;
    if (COLLECT && msg.you.collect?.done) { finish(); return; }
    if (brain) {
      desired = brain.decide(msg);
      sendInput();
    }
    announceId(msg.you.id);
  };
  ws.onclose = () => {
    connected = false;
    if (finished) return;
    latest = null;
    setTimeout(connect, 1000); // server restarts during dev are constant
  };
}

// ── Input ──────────────────────────────────────────────────────────────

const KEYMAP = {
  KeyW: 'up', ArrowUp: 'up',
  KeyS: 'down', ArrowDown: 'down',
  KeyA: 'left', ArrowLeft: 'left',
  KeyD: 'right', ArrowRight: 'right',
};
const held = new Set();
let desired = { dx: 0, dy: 0 }; // from the keyboard, or from the autopilot
let sent = { dx: 0, dy: 0 };

function keyboardInput() {
  return {
    dx: (held.has('right') ? 1 : 0) - (held.has('left') ? 1 : 0),
    dy: (held.has('down') ? 1 : 0) - (held.has('up') ? 1 : 0),
  };
}

/** Sends on change; `force` sends regardless (keepalive). */
function sendInput(force = false) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!force && desired.dx === sent.dx && desired.dy === sent.dy) return;
  ws.send(JSON.stringify({ type: 'input', dx: desired.dx, dy: desired.dy }));
  sent = desired;
}

if (!AUTO) {
  addEventListener('keydown', (e) => {
    const dir = KEYMAP[e.code];
    if (!dir || !overlay.hidden) return; // typing a name, or finished
    e.preventDefault(); // arrows would otherwise scroll the page
    held.add(dir);
    desired = keyboardInput();
    sendInput();
  });
  addEventListener('keyup', (e) => {
    const dir = KEYMAP[e.code];
    if (!dir) return;
    held.delete(dir);
    desired = keyboardInput();
    sendInput();
  });
  // alt-tab must not leave you running
  addEventListener('blur', () => { held.clear(); desired = keyboardInput(); sendInput(); });
}

// When embedded in the split view, tell it which player this is so the admin view can highlight it.
let announced = null;
function announceId(id) {
  if (id === announced || window.parent === window) return;
  announced = id;
  window.parent.postMessage({ type: 'watch', id }, location.origin);
}

setInterval(() => sendInput(true), C.INPUT_KEEPALIVE_TICKS * C.TICK_MS);

// ── Render ─────────────────────────────────────────────────────────────

function render() {
  ctx.clearRect(0, 0, C.WORLD_W, C.WORLD_H);

  ctx.fillStyle = '#2d333b';
  for (const o of C.OBSTACLES) ctx.fillRect(o.x, o.y, o.w, o.h);

  if (latest) {
    for (const e of latest.entities) {
      const spec = SPRITES.get(e.sprite);
      // ── THE CRITICAL LINE ──────────────────────────────────────────
      // If the sprite name is not in our asset table, draw nothing and move on.
      // Do NOT log, count, highlight or otherwise surface skipped entities:
      // that would put trap information in the browser console.
      // Write it once. Never touch it again.
      if (spec === undefined) continue;
      drawSprite(ctx, spec, e.x, e.y, e.color);
    }

    // You, drawn last and outlined so you can always find yourself.
    const me = latest.you;
    ctx.beginPath();
    ctx.arc(me.x, me.y, C.PLAYER_RADIUS, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#4ea8f2';
    ctx.stroke();
  }

  const mode = AUTO === 'bot'
    ? 'autoplay: naive bot — it chases things you cannot see'
    : AUTO === 'human' ? 'autoplay: human-like — it only chases what is drawn'
    : 'WASD / arrow keys to move';
  if (COLLECT) {
    hud.className = 'collect';
    const c = latest?.you?.collect;
    hud.textContent = finished ? 'tamamlandı ✓'
      : !connected ? 'bağlantı yoxdur — yenidən qoşulur (vaxt saxlanılır)…'
      : !latest ? 'qoşulur…'
      : !c ? 'collect kodu yanlışdır — data insan kimi yazılmır, linki yoxla'
      : `⏱ ${mmss(c.remaining)} qaldı  ·  xal ${latest.you.score ?? 0}  ·  ${collectName}  ·  WASD / oxlar`;
  } else {
    hud.textContent = connected ? `tick ${latest?.tick ?? '—'}  ·  ${mode}` : 'disconnected — reconnecting…';
  }

  requestAnimationFrame(render);
}

if (COLLECT && !collectName) askName();
else connect();
requestAnimationFrame(render);
