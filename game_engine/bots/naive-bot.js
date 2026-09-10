// bots/naive-bot.js — the farm: naive, evasive, or human-imitating, same process.
//
//   node bots/naive-bot.js                     one bot, naive
//   node bots/naive-bot.js --count 10          a farm of ten
//   node bots/naive-bot.js --mode evasive      start already patched
//   node bots/naive-bot.js --mode human        headless human-imitating farm (see note below)
//   node bots/naive-bot.js --host 192.168.1.5:8080
//   node bots/naive-bot.js --control-port 8090 # GET /status, POST /mode {"mode":"evasive"}
//   node bots/naive-bot.js --telemetry-key demo --traces ../traces
//
// It does what a cheap real-world farming bot does: read the raw state off the
// socket and walk in a straight line to the nearest loot. In naive mode it has
// no asset table, so it cannot tell a real coin from a ghost one — and that is
// exactly what the traps catch. It connects like any browser: no back door.
//
// naive <-> evasive is the one live, no-reconnect toggle (all instances share
// `modeState`; POST /mode just flips its `.mode` field for the next tick).
// `human` is a separate, optional style (per CLAUDE.md, it stays outside that
// live-toggle contract): it's fixed for the process's lifetime — POST /mode only
// ever accepts the FARM_FILTERS keys ('naive'/'evasive'), so switching in or out
// of human-imitating mode means restarting with a different --mode.
//
// Every mode always emits trace files (bots/trace-emitter.js), matching the
// schema in bots/trace-schema.js.
//
// The decision logic lives in client/autoplay.js (NaiveBrain / HumanLikeBrain),
// shared with the browser's ?auto=bot / ?auto=human modes, so what you watch on
// screen is exactly what the farm does.

import http from 'node:http';
import { WebSocket } from 'ws';
import * as C from '../constants.js';
import { NaiveBrain, HumanLikeBrain, FARM_FILTERS } from '../client/autoplay.js';
import { TraceBuffer, startTraceEmitter } from './trace-emitter.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const HOST = arg('host', `localhost:${C.PORT}`);
const COUNT = Math.max(1, Number(arg('count', 1)) || 1);
const CONTROL_PORT = Number(arg('control-port', 8090));
const TELEMETRY_KEY = arg('telemetry-key', process.env.TELEMETRY_KEY || 'demo');
const TRACES_DIR = arg('traces', new URL('../traces', import.meta.url).pathname);

const START_MODE = arg('mode', 'naive');
const USE_HUMAN_BRAIN = START_MODE === 'human';
// modeState only governs NaiveBrain farms (naive/evasive); meaningless when USE_HUMAN_BRAIN.
const modeState = { mode: START_MODE in FARM_FILTERS ? START_MODE : 'naive' };

const emitter = startTraceEmitter({
  host: HOST,
  telemetryKey: TELEMETRY_KEY,
  outDir: TRACES_DIR,
  onEpisode: (file) => console.log(`[trace] wrote ${file}`),
});

/** @type {Map<number, {playerId: string|null, connected: boolean}>} n -> bot status, for /status */
const bots = new Map();

function runBot(n) {
  const ws = new WebSocket(`ws://${HOST}${C.PATHS.GAME}?kind=bot`);
  const brain = USE_HUMAN_BRAIN ? new HumanLikeBrain() : new NaiveBrain(modeState);
  const traceBuf = new TraceBuffer();
  const status = bots.get(n) ?? { playerId: null, connected: false };
  bots.set(n, status);
  let sent = { dx: 0, dy: 0 };
  let sinceSend = 0;

  ws.on('open', () => {
    status.connected = true;
    console.log(`[bot ${n}] connected`);
  });
  ws.on('close', () => {
    status.connected = false;
    if (status.playerId) emitter.unregister(status.playerId);
    status.playerId = null;
    console.log(`[bot ${n}] disconnected, retrying in 2 s`);
    setTimeout(() => runBot(n), 2000);
  });
  ws.on('error', () => {}); // 'close' follows and handles the retry

  ws.on('message', (data) => {
    const msg = JSON.parse(data.toString());
    if (msg.type !== 'state') return;

    traceBuf.record(msg.tick, msg.you.x, msg.you.y);
    if (!status.playerId) {
      status.playerId = msg.you.id;
      emitter.register(status.playerId, traceBuf, () => ({
        kind: 'bot',
        mode: USE_HUMAN_BRAIN ? 'human-like' : modeState.mode,
      }));
    }

    const { dx, dy } = brain.decide(msg);
    if (dx !== sent.dx || dy !== sent.dy || ++sinceSend >= C.INPUT_KEEPALIVE_TICKS) {
      ws.send(JSON.stringify({ type: 'input', dx, dy }));
      sent = { dx, dy };
      sinceSend = 0;
    }
  });
}

// ── Control endpoint ─────────────────────────────────────────────────────
// Local-only demo control surface: no auth, matches the rest of this bot's
// "ordinary client, no back door" posture (it controls the bot, not the game).

function readJsonBody(req, cb) {
  let body = '';
  req.on('data', (chunk) => {
    body += chunk;
    if (body.length > 1024) req.destroy(); // guard against a runaway body
  });
  req.on('end', () => {
    try { cb(null, body ? JSON.parse(body) : {}); } catch (err) { cb(err); }
  });
}

const controlServer = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({
      style: USE_HUMAN_BRAIN ? 'human' : 'farm',
      mode: USE_HUMAN_BRAIN ? null : modeState.mode,
      count: COUNT,
      connected: [...bots.values()].filter((b) => b.connected).length,
      bots: [...bots.entries()].map(([n, b]) => ({ n, playerId: b.playerId, connected: b.connected })),
    }));
    return;
  }
  if (req.method === 'POST' && req.url === '/mode') {
    readJsonBody(req, (err, body) => {
      if (err || !(body.mode in FARM_FILTERS)) {
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(JSON.stringify({
          error: `mode must be one of: ${Object.keys(FARM_FILTERS).join(', ')}`
            + (body?.mode === 'human' ? ' (human-imitating mode is fixed at startup via --mode human, not live-togglable)' : ''),
        }));
        return;
      }
      if (USE_HUMAN_BRAIN) {
        res.writeHead(409, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'this farm was started with --mode human; naive/evasive toggling does not apply' }));
        return;
      }
      modeState.mode = body.mode; // live flip: every bot reads this on its next tick
      console.log(`[control] mode -> ${modeState.mode}`);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ mode: modeState.mode }));
    });
    return;
  }
  res.writeHead(404, { 'content-type': 'application/json' }).end(JSON.stringify({ error: 'not found' }));
});
controlServer.listen(CONTROL_PORT, () => {
  console.log(`[control] listening on :${CONTROL_PORT} (GET /status, POST /mode)`);
});

console.log(`Launching ${COUNT} bot(s) [mode=${USE_HUMAN_BRAIN ? 'human' : modeState.mode}] against ws://${HOST}${C.PATHS.GAME}`);
for (let i = 0; i < COUNT; i++) setTimeout(() => runBot(i + 1), i * 100); // stagger connects
