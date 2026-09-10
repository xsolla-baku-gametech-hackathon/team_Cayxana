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
//
// The decision logic lives in client/autoplay.js (NaiveBrain), shared with the
// browser's ?auto=bot mode, so what you watch on screen is exactly what the farm does.

import { WebSocket } from 'ws';
import * as C from '../constants.js';
import { NaiveBrain } from '../client/autoplay.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const HOST = arg('host', `localhost:${C.PORT}`);
const COUNT = Math.max(1, Number(arg('count', 1)) || 1);

function runBot(n) {
  const ws = new WebSocket(`ws://${HOST}${C.PATHS.GAME}?kind=bot`);
  const brain = new NaiveBrain();
  let sent = { dx: 0, dy: 0 };
  let sinceSend = 0;

  ws.on('open', () => console.log(`[bot ${n}] connected`));
  ws.on('close', () => {
    console.log(`[bot ${n}] disconnected, retrying in 2 s`);
    setTimeout(() => runBot(n), 2000);
  });
  ws.on('error', () => {}); // 'close' follows and handles the retry

  ws.on('message', (data) => {
    const msg = JSON.parse(data.toString());
    if (msg.type !== 'state') return;
    const { dx, dy } = brain.decide(msg);
    if (dx !== sent.dx || dy !== sent.dy || ++sinceSend >= C.INPUT_KEEPALIVE_TICKS) {
      ws.send(JSON.stringify({ type: 'input', dx, dy }));
      sent = { dx, dy };
      sinceSend = 0;
    }
  });
}

console.log(`Launching ${COUNT} naive bot(s) against ws://${HOST}${C.PATHS.GAME}`);
for (let i = 0; i < COUNT; i++) setTimeout(() => runBot(i + 1), i * 100); // stagger connects
