// tools/smoke-test.js — protocol conformance check against a RUNNING server.
//   npm start            (in one terminal)
//   npm run smoke        (in another)
// Exits 0 if the wire contract holds, 1 otherwise. Run it after every merge.

import { WebSocket } from 'ws';
import * as C from '../constants.js';

const HOST = process.env.HOST || `localhost:${C.PORT}`;
const KEY = process.env.TELEMETRY_KEY || 'demo';
const ENTITY_FIELDS = new Set(['id', 'type', 'x', 'y', 'sprite', 'color', 'value']);

let failures = 0;
const check = (cond, msg) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg}`);
  if (!cond) failures++;
};

function open(path) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://${HOST}${path}`);
    ws.once('open', () => resolve(ws));
    ws.once('error', reject);
    ws.once('unexpected-response', (_req, res) => reject(new Error(`HTTP ${res.statusCode}`)));
  });
}

function nextMessage(ws, type, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout waiting for ${type}`)), timeoutMs);
    const onMsg = (data) => {
      const msg = JSON.parse(data.toString());
      if (msg.type !== type) return;
      clearTimeout(timer);
      ws.off('message', onMsg);
      resolve(msg);
    };
    ws.on('message', onMsg);
  });
}

async function main() {
  // 1. Game connection and state shape.
  const game = await open(`${C.PATHS.GAME}?kind=bot`);
  const s1 = await nextMessage(game, 'state');
  check(Number.isInteger(s1.tick), 'state.tick is an integer');
  check(typeof s1.you?.id === 'string' && Number.isFinite(s1.you.x) && Number.isFinite(s1.you.y), 'state.you has id, x, y');
  check(Array.isArray(s1.entities) && s1.entities.length >= C.COIN_COUNT + C.CHEST_COUNT - 5, `state.entities present (${s1.entities.length})`);

  const badFields = s1.entities.flatMap((e) => Object.keys(e).filter((k) => !ENTITY_FIELDS.has(k)));
  check(badFields.length === 0, `entities carry only whitelisted fields ${badFields.length ? '→ extra: ' + [...new Set(badFields)] : ''}`);
  check(s1.entities.every((e) => ['coin', 'chest', 'player'].includes(e.type)), 'entity.type ∈ {coin, chest, player}');
  check(s1.entities.every((e) => typeof e.sprite === 'string' && e.sprite.length > 0), 'no null/empty sprites on the wire');
  check(s1.entities.filter((e) => e.type !== 'player').every((e) => typeof e.value === 'number'), 'coins and chests carry value');
  check(s1.entities.every((e) => e.id !== s1.you.id), 'own player not duplicated in entities');

  // 2. Movement: hold right, expect x to increase (unless spawned against a wall).
  game.send(JSON.stringify({ type: 'input', dx: 1, dy: 0 }));
  await new Promise((r) => setTimeout(r, 500));
  const s2 = await nextMessage(game, 'state');
  const moved = s2.you.x - s1.you.x;
  check(moved > 0 && moved <= C.PLAYER_SPEED * ((s2.tick - s1.tick) + 1), `input dx=1 moves right (+${moved.toFixed(1)} over ${s2.tick - s1.tick} ticks)`);

  // 3. Malformed input is ignored, server survives.
  game.send('not json');
  game.send(JSON.stringify({ type: 'input', dx: 5, dy: 'x' }));
  game.send(JSON.stringify({ type: 'interact' }));
  const s3 = await nextMessage(game, 'state');
  check(s3.tick > s2.tick, 'server survives malformed messages');

  // 4. Telemetry requires the key.
  let rejected = false;
  try { (await open(C.PATHS.TELEMETRY)).close(); } catch { rejected = true; }
  check(rejected, 'telemetry without key is rejected');

  let adminRejected = false;
  try { (await open(C.PATHS.ADMIN)).close(); } catch { adminRejected = true; }
  check(adminRejected, 'admin feed without key is rejected');

  const tel = await open(`${C.PATHS.TELEMETRY}?key=${encodeURIComponent(KEY)}`);
  const t1 = await nextMessage(tel, 'telemetry');
  const me = t1.players.find((p) => p.id === s1.you.id);
  check(Boolean(me), 'telemetry lists our player');
  check(me?.kind === 'bot', 'telemetry carries kind');
  check(Array.isArray(me?.trace) && me.trace.length > 0 && me.trace.length <= C.TRACE_LEN, `telemetry trace length ok (${me?.trace.length})`);
  check(typeof me?.score === 'number' && typeof me?.flagged === 'boolean' && Array.isArray(me?.events), 'telemetry score/flagged/events typed');
  check(Array.isArray(t1.categories), 'telemetry.categories is an array');

  game.close();
  tel.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
}

main().catch((err) => {
  console.error('FAIL ', err.message);
  process.exit(1);
});
