// bots/naive-bot.js — the naive farming bot.
//
//   node bots/naive-bot.js                 one bot
//   node bots/naive-bot.js --count 10      a farm of ten
//   node bots/naive-bot.js --host 192.168.1.5:8080
//
// It does what a cheap real-world farming bot does: read the raw state off the
// socket and walk in a straight line to the nearest loot. It has no asset
// table, so it cannot tell a real coin from a ghost one — and that is exactly
// what the traps catch. It connects like any browser: no back door.

import { WebSocket } from 'ws';
import * as C from '../constants.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const HOST = arg('host', `localhost:${C.PORT}`);
const COUNT = Math.max(1, Number(arg('count', 1)) || 1);

const LOOT = new Set(['coin', 'chest']);
const DEADZONE = C.PLAYER_SPEED / 2;  // stop adjusting an axis once this close, to avoid jitter
const STUCK_TICKS = 12;               // no progress for this long → give up on the target
const BLACKLIST_TICKS = 60;

const sign = (v) => (v > DEADZONE ? 1 : v < -DEADZONE ? -1 : 0);

function runBot(n) {
  const ws = new WebSocket(`ws://${HOST}${C.PATHS.GAME}?kind=bot`);
  let sent = { dx: 0, dy: 0 };
  let sinceSend = 0;
  let last = null;            // last position, for stuck detection
  let stuckFor = 0;
  let targetId = null;
  const blacklist = new Map(); // entity id → tick until which it is ignored

  function send(dx, dy, force = false) {
    if (!force && dx === sent.dx && dy === sent.dy) return;
    ws.send(JSON.stringify({ type: 'input', dx, dy }));
    sent = { dx, dy };
    sinceSend = 0;
  }

  ws.on('open', () => console.log(`[bot ${n}] connected`));
  ws.on('close', () => {
    console.log(`[bot ${n}] disconnected, retrying in 2 s`);
    setTimeout(() => runBot(n), 2000);
  });
  ws.on('error', () => {}); // 'close' follows and handles the retry

  ws.on('message', (data) => {
    const msg = JSON.parse(data.toString());
    if (msg.type !== 'state') return;
    const { you, entities, tick } = msg;

    for (const [id, until] of blacklist) if (until <= tick) blacklist.delete(id);

    // Nearest loot. Re-evaluated every tick, like the simplest real bots.
    let best = null;
    let bestD = Infinity;
    for (const e of entities) {
      if (!LOOT.has(e.type) || blacklist.has(e.id)) continue;
      const d = (e.x - you.x) ** 2 + (e.y - you.y) ** 2;
      if (d < bestD) { bestD = d; best = e; }
    }

    // Stuck on a wall? Blacklist the target for a while and pick another.
    if (last && best && Math.hypot(you.x - last.x, you.y - last.y) < 0.5 && (sent.dx || sent.dy)) {
      if (++stuckFor >= STUCK_TICKS) {
        blacklist.set(best.id, tick + BLACKLIST_TICKS);
        stuckFor = 0;
      }
    } else {
      stuckFor = 0;
    }
    last = { x: you.x, y: you.y };

    if (best?.id !== targetId) targetId = best?.id ?? null;
    const dx = best ? sign(best.x - you.x) : 0;
    const dy = best ? sign(best.y - you.y) : 0;
    send(dx, dy, ++sinceSend >= C.INPUT_KEEPALIVE_TICKS);
  });
}

console.log(`Launching ${COUNT} naive bot(s) against ws://${HOST}${C.PATHS.GAME}`);
for (let i = 0; i < COUNT; i++) setTimeout(() => runBot(i + 1), i * 100); // stagger connects
