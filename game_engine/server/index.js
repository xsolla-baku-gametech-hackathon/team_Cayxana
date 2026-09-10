// server/index.js — entry point. One Node process: static files + /game + /telemetry.
//
//   node server/index.js
//   Game:      http://localhost:8080/
//   Dashboard: http://localhost:8080/dashboard/?key=<TELEMETRY_KEY>
//   Admin:     http://localhost:8080/admin/?key=<TELEMETRY_KEY>   (server truth, traps visible)
//   Split:     http://localhost:8080/split/?key=<TELEMETRY_KEY>   (player view + admin view)

import http from 'node:http';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';
import { WebSocketServer, WebSocket } from 'ws';

import * as C from '../constants.js';
import { World } from './world.js';
import * as traps from './traps.js';
import * as detection from './classifier.js';

const ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const PORT = Number(process.env.PORT) || C.PORT;

// The telemetry and admin channels expose flags, scores and trap positions.
// Anyone who can connect could read them — including a bot — so both require a key.
const TELEMETRY_KEY = process.env.TELEMETRY_KEY || 'demo';
const ADMIN_EVERY_TICKS = 2; // 10 Hz is plenty for watching

const world = new World();
/** @type {Map<string, WebSocket>} playerId → socket */
const gameSockets = new Map();
/** Most recent trap events across all players, for the admin view. */
const recentEvents = [];

// ── Logging helpers ────────────────────────────────────────────────────
const warned = new Set();
function warnOnce(key, ...args) {
  if (warned.has(key)) return;
  warned.add(key);
  console.warn(`[warn] ${key}:`, ...args);
}

// ── Safe wrappers around other workstreams' code ───────────────────────
// A bug in a module we don't own must never take the server down mid-demo.

function safeCheckTraps(playerId, position, tick) {
  try {
    const ev = traps.checkTraps(playerId, position, tick);
    return ev && typeof ev === 'object' ? ev : null;
  } catch (err) {
    warnOnce('checkTraps threw', err);
    return null;
  }
}

function safeClassify(trace, trapEvent) {
  try {
    const r = detection.classify(trace, trapEvent);
    const score = Number(r?.score);
    return {
      score: Number.isFinite(score) ? Math.min(1, Math.max(0, score)) : 0,
      features: r?.features && typeof r.features === 'object' ? r.features : {},
    };
  } catch (err) {
    warnOnce('classify threw', err);
    return { score: 0, features: {} };
  }
}

function safeInjectTraps(entities, playerId, tick) {
  try {
    const out = traps.injectTraps(entities, playerId, tick);
    if (Array.isArray(out)) return out;
    warnOnce('injectTraps returned a non-array', out);
  } catch (err) {
    warnOnce('injectTraps threw', err);
  }
  return entities;
}

function safeAssess(results) {
  if (typeof detection.assessPlayer === 'function') {
    try {
      const a = detection.assessPlayer(results);
      return { score: Number(a?.score) || 0, flagged: Boolean(a?.flagged) };
    } catch (err) {
      warnOnce('assessPlayer threw', err);
    }
  }
  const last = results[results.length - 1];
  return { score: last ? last.score : 0, flagged: false };
}

function safeCategoryStats() {
  if (typeof traps.getCategoryStats !== 'function') return [];
  try {
    const s = traps.getCategoryStats();
    return Array.isArray(s) ? s : [];
  } catch (err) {
    warnOnce('getCategoryStats threw', err);
    return [];
  }
}

function safeAdminTraps() {
  if (typeof traps.getAdminSnapshot !== 'function') return [];
  try {
    const s = traps.getAdminSnapshot();
    return Array.isArray(s) ? s : [];
  } catch (err) {
    warnOnce('getAdminSnapshot threw', err);
    return [];
  }
}

function safePlayerLeave(playerId) {
  if (typeof traps.onPlayerLeave !== 'function') return;
  try {
    traps.onPlayerLeave(playerId);
  } catch (err) {
    warnOnce('onPlayerLeave threw', err);
  }
}

// ── Wire serialisation ─────────────────────────────────────────────────
// Whitelist, not blacklist: whatever another module attaches to an entity,
// only these fields ever reach the client. This is our guarantee that no
// trap metadata can leak onto the wire by accident.

const WIRE_TYPES = new Set(['coin', 'chest', 'player']);
const round1 = (v) => Math.round(v * 10) / 10;

function toWire(e) {
  if (!e || !WIRE_TYPES.has(e.type)) {
    warnOnce('dropped entity with invalid type', e?.type);
    return null;
  }
  if (typeof e.sprite !== 'string' || e.sprite.length === 0) {
    warnOnce('dropped entity with null/empty sprite (spec forbids this)', e.id);
    return null;
  }
  if (!Number.isFinite(e.x) || !Number.isFinite(e.y)) {
    warnOnce('dropped entity with non-numeric position', e.id);
    return null;
  }
  const w = {
    id: String(e.id),
    type: e.type,
    x: round1(e.x),
    y: round1(e.y),
    sprite: e.sprite,
    color: String(e.color),
  };
  if (e.type !== 'player') w.value = Number(e.value) || 0;
  return w;
}

// Traps are appended by injectTraps; shuffling stops "the last N entities are traps".
function shuffleInPlace(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

// Skip a tick for clients that can't keep up instead of buffering unboundedly.
const MAX_BUFFERED_BYTES = 512 * 1024;

function send(ws, payload) {
  if (ws.readyState !== WebSocket.OPEN) return;
  if (ws.bufferedAmount > MAX_BUFFERED_BYTES) return;
  ws.send(payload);
}

// ── The tick ───────────────────────────────────────────────────────────

function tick() {
  const t = ++world.tick;

  // 1–2. Respawns, then inputs + movement with collision.
  world.processRespawns();
  world.movePlayers();

  // 3. Trace BEFORE the trap check, so the classifier sees the lead-up.
  world.recordTraces();

  // 4. Proximity pickups.
  world.handlePickups();

  // 5. Trap checks → classify → record. The server never acts on a score.
  for (const p of world.players.values()) {
    const trapEvent = safeCheckTraps(p.id, { x: p.x, y: p.y }, t);
    if (!trapEvent) continue;
    const { score, features } = safeClassify(p.trace.toArray(), trapEvent);
    p.results.push({ tick: t, trapEvent, score, features });
    if (p.results.length > 200) p.results.shift(); // bound memory on long runs
    recentEvents.push({ tick: t, playerId: p.id, kind: p.kind, category: trapEvent.category, x: trapEvent.x, y: trapEvent.y, score });
    if (recentEvents.length > 50) recentEvents.shift();
  }

  // 6. Second loop: per-player entity arrays (traps differ per player), then send.
  for (const [playerId, ws] of gameSockets) {
    const p = world.players.get(playerId);
    if (!p) continue;
    const withTraps = safeInjectTraps(world.baseEntitiesFor(playerId), playerId, t);
    const entities = [];
    for (const e of withTraps) {
      const w = toWire(e);
      if (w) entities.push(w);
    }
    shuffleInPlace(entities);
    send(ws, JSON.stringify({
      type: 'state',
      tick: t,
      you: { id: p.id, x: round1(p.x), y: round1(p.y) },
      entities,
    }));
  }

  // 7. Telemetry at 2 Hz.
  if (t % C.TELEMETRY_EVERY_TICKS === 0 && telemetryWss.clients.size > 0) {
    broadcastTelemetry(t);
  }

  // 8. Admin view at 10 Hz — server truth, including every player's traps.
  if (t % ADMIN_EVERY_TICKS === 0 && adminWss.clients.size > 0) {
    broadcastAdmin(t);
  }
}

function broadcastAdmin(t) {
  const items = [];
  for (const it of world.items.values()) items.push({ id: it.id, type: it.type, x: round1(it.x), y: round1(it.y), color: it.color });
  const players = [];
  for (const p of world.players.values()) {
    const { score, flagged } = safeAssess(p.results);
    players.push({
      id: p.id, kind: p.kind, color: p.color,
      x: round1(p.x), y: round1(p.y),
      hits: p.results.length, score, flagged,
    });
  }
  const payload = JSON.stringify({
    type: 'admin',
    tick: t,
    items,
    players,
    traps: safeAdminTraps(),
    events: recentEvents,
  });
  for (const ws of adminWss.clients) send(ws, payload);
}

function broadcastTelemetry(t) {
  const players = [];
  for (const p of world.players.values()) {
    const { score, flagged } = safeAssess(p.results);
    players.push({
      id: p.id,
      kind: p.kind, // demo-only: dashboard colouring. Never passed to detection.
      trace: p.trace.toArray().map((s) => [round1(s.x), round1(s.y)]), // oldest → newest
      score,
      flagged,
      events: p.results.map((r) => ({ ...r.trapEvent, score: r.score })),
    });
  }
  const payload = JSON.stringify({ type: 'telemetry', tick: t, players, categories: safeCategoryStats() });
  for (const ws of telemetryWss.clients) send(ws, payload);
}

// Drift-corrected fixed-step loop (setInterval drifts and bunches up under load).
let nextTickAt = performance.now();
function loop() {
  try {
    tick();
  } catch (err) {
    console.error('[tick] error (loop continues):', err);
  }
  nextTickAt += C.TICK_MS;
  const now = performance.now();
  if (now - nextTickAt > 1000) nextTickAt = now; // fell far behind: resync, don't burst
  setTimeout(loop, Math.max(0, nextTickAt - now));
}

// ── Static files ───────────────────────────────────────────────────────
// Allowlist. The server/ folder (trap and classifier code) is never served.

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

function resolveStatic(urlPath) {
  if (urlPath === '/' || urlPath === '/index.html') return path.join(ROOT, 'client', 'index.html');
  for (const page of ['dashboard', 'admin', 'split']) {
    if (urlPath === `/${page}` || urlPath === `/${page}/`) return path.join(ROOT, page, 'index.html');
  }
  if (urlPath === '/constants.js') return path.join(ROOT, 'constants.js');
  for (const dir of ['client', 'dashboard', 'admin', 'split']) {
    if (urlPath.startsWith(`/${dir}/`)) {
      const base = path.join(ROOT, dir);
      const file = path.normalize(path.join(ROOT, decodeURIComponent(urlPath)));
      if (file.startsWith(base + path.sep)) return file; // blocks ../ traversal
    }
  }
  return null;
}

const server = http.createServer(async (req, res) => {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405).end();
    return;
  }
  let file;
  try {
    file = resolveStatic(new URL(req.url, 'http://localhost').pathname);
  } catch {
    file = null; // malformed URL / bad percent-encoding
  }
  if (!file) {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('Not found');
    return;
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, {
      'content-type': MIME[path.extname(file)] ?? 'application/octet-stream',
      'cache-control': 'no-store', // never serve a stale client during the hackathon
    });
    res.end(req.method === 'HEAD' ? undefined : body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('Not found');
  }
});

// ── WebSockets ─────────────────────────────────────────────────────────

const gameWss = new WebSocketServer({ noServer: true, maxPayload: 1024 });
const telemetryWss = new WebSocketServer({ noServer: true, maxPayload: 1024 });
const adminWss = new WebSocketServer({ noServer: true, maxPayload: 1024 });

server.on('upgrade', (req, socket, head) => {
  let url;
  try {
    url = new URL(req.url, 'http://localhost');
  } catch {
    socket.destroy();
    return;
  }
  if (url.pathname === C.PATHS.GAME) {
    gameWss.handleUpgrade(req, socket, head, (ws) => gameWss.emit('connection', ws, url));
  } else if (url.pathname === C.PATHS.TELEMETRY && url.searchParams.get('key') === TELEMETRY_KEY) {
    telemetryWss.handleUpgrade(req, socket, head, (ws) => telemetryWss.emit('connection', ws, url));
  } else if (url.pathname === C.PATHS.ADMIN && url.searchParams.get('key') === TELEMETRY_KEY) {
    adminWss.handleUpgrade(req, socket, head, (ws) => adminWss.emit('connection', ws, url));
  } else {
    socket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
    socket.destroy();
  }
});

gameWss.on('connection', (ws, url) => {
  // Self-declared, demo-only label: picks the spawn rule and dashboard colour.
  // Grants nothing — bots get exactly the same protocol as browsers.
  const kind = url.searchParams.get('kind') === 'bot' ? 'bot' : 'human';
  const player = world.addPlayer(kind);
  gameSockets.set(player.id, ws);
  console.log(`[game] +${kind} ${player.id} (players: ${gameSockets.size})`);

  ws.on('message', (data, isBinary) => {
    if (isBinary) return;
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return; // ignore garbage
    }
    if (msg?.type === 'input') world.setInput(player.id, msg.dx, msg.dy);
    // Any other message type is ignored: only three message types exist.
  });

  ws.on('close', () => {
    gameSockets.delete(player.id);
    world.removePlayer(player.id);
    safePlayerLeave(player.id);
    console.log(`[game] -${kind} ${player.id} (players: ${gameSockets.size})`);
  });

  ws.on('error', (err) => warnOnce('game socket error', err.message));
});

adminWss.on('connection', (ws) => {
  console.log(`[admin] admin view connected (${adminWss.clients.size})`);
  ws.on('message', () => {}); // receive-only
  ws.on('error', (err) => warnOnce('admin socket error', err.message));
});

telemetryWss.on('connection', (ws) => {
  console.log(`[telemetry] dashboard connected (${telemetryWss.clients.size})`);
  ws.on('message', () => {}); // dashboards are receive-only
  ws.on('error', (err) => warnOnce('telemetry socket error', err.message));
});

server.listen(PORT, () => {
  console.log(`Game:      http://localhost:${PORT}/`);
  console.log(`Dashboard: http://localhost:${PORT}/dashboard/?key=${TELEMETRY_KEY}`);
  console.log(`Admin:     http://localhost:${PORT}/admin/?key=${TELEMETRY_KEY}`);
  console.log(`Split:     http://localhost:${PORT}/split/?key=${TELEMETRY_KEY}`);
  nextTickAt = performance.now() + C.TICK_MS;
  setTimeout(loop, C.TICK_MS);
});
