// client/client.js — draws only what the server sends, and only what the asset table knows.
// No game logic lives here. No trap logic lives here. Ever.
//
//   /             manual play (WASD / arrows)
//   /?auto=bot    watch the naive bot autopilot  (connects as kind=bot)
//   /?auto=human  watch the human-like autopilot (connects as kind=human)

import * as C from '/constants.js';
import { SPRITES, drawSprite } from '/client/sprites.js';

const AUTO = ['bot', 'human'].includes(new URLSearchParams(location.search).get('auto'))
  ? new URLSearchParams(location.search).get('auto')
  : null;
const autoplay = AUTO ? await import('/client/autoplay.js') : null;
let brain = null;

const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const hud = document.getElementById('hud');

let ws = null;
let latest = null;   // most recent 'state' message
let connected = false;

// ── Connection ─────────────────────────────────────────────────────────

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}${C.PATHS.GAME}?kind=${AUTO === 'bot' ? 'bot' : 'human'}`);
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
    if (brain) {
      desired = brain.decide(msg);
      sendInput();
    }
    announceId(msg.you.id);
  };
  ws.onclose = () => {
    connected = false;
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
    if (!dir) return;
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
  hud.textContent = connected ? `tick ${latest?.tick ?? '—'}  ·  ${mode}` : 'disconnected — reconnecting…';

  requestAnimationFrame(render);
}

connect();
requestAnimationFrame(render);
