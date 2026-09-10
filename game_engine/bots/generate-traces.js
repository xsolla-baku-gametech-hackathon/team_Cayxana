// bots/generate-traces.js — synthetic offline trace generator.
//
//   node bots/generate-traces.js                20 files (default)
//   node bots/generate-traces.js --count 40 --out traces/
//
// Deliberately has NO server dependency: this is step 1 in the build order,
// shipped before the WebSocket client even exists, so P3 is never blocked
// waiting on a live game. It fabricates plausible episodes across all four
// documented trap categories (doc 02), respecting the real game's physics
// constants so the shapes are representative:
//   - "bot" episodes: near-straight line from a random start, dwelling in the
//     trap radius for the last few ticks (mirrors TRAP_DWELL_TICKS).
//   - "human" episodes: a wandering, paused, imperfect path that only happens
//     to land in the trap radius for the fire — the rare accidental hit,
//     included so the classifier has real geometric contrast to learn from
//     rather than every file being a trivial straight line.
//
// Once bots/naive-bot.js is running against a live server, bots/trace-emitter.js
// produces real (not synthetic) episodes in this exact same format.

import { randomBytes } from 'node:crypto';
import * as C from '../constants.js';
import { makeEpisode, writeEpisode } from './trace-schema.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const COUNT = Math.max(1, Number(arg('count', 20)) || 20);
const OUT_DIR = arg('out', new URL('../traces', import.meta.url).pathname);
const CATEGORIES = ['invisible_entity', 'position_offset', 'phantom_player', 'unreachable_bait'];

const newId = () => randomBytes(6).toString('hex');
const rand = (lo, hi) => lo + Math.random() * (hi - lo);
const randInt = (lo, hi) => Math.floor(rand(lo, hi + 1));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const round1 = (v) => Math.round(v * 10) / 10;

function randomWorldPoint(margin = 40) {
  // Rounded at creation so a dwelling trace point (set to exactly `target`) matches
  // trapEvent.x/y after round1 without drifting — round1 must be idempotent on it.
  return { x: round1(rand(margin, C.WORLD_W - margin)), y: round1(rand(margin, C.WORLD_H - margin)) };
}

/** Near-straight line toward the target, dwelling inside the trap radius at the tail. */
function botTrace(target, startTick, len) {
  const start = randomWorldPoint();
  const out = [];
  for (let i = 0; i < len; i++) {
    const dwelling = i >= len - C.TRAP_DWELL_TICKS;
    const t = dwelling ? 1 : i / (len - 1);
    out.push({
      tick: startTick + i,
      x: round1(start.x + (target.x - start.x) * t),
      y: round1(start.y + (target.y - start.y) * t),
    });
  }
  return out;
}

/** Wandering, pausing, imperfect — only converges on the target for the final dwell. */
function humanTrace(target, startTick, len) {
  const out = [];
  let x = clamp(target.x + rand(-250, 250), 20, C.WORLD_W - 20);
  let y = clamp(target.y + rand(-250, 250), 20, C.WORLD_H - 20);
  for (let i = 0; i < len; i++) {
    const remaining = len - 1 - i;
    if (remaining < C.TRAP_DWELL_TICKS) {
      x = target.x; y = target.y;
    } else if (remaining < 20) {
      x += (target.x - x) * 0.15 + rand(-2, 2);
      y += (target.y - y) * 0.15 + rand(-2, 2);
    } else if (Math.random() < 0.1) {
      // pause: no movement this tick
    } else {
      x = clamp(x + rand(-6, 6), 20, C.WORLD_W - 20);
      y = clamp(y + rand(-6, 6), 20, C.WORLD_H - 20);
    }
    out.push({ tick: startTick + i, x: round1(x), y: round1(y) });
  }
  return out;
}

async function main() {
  const files = [];
  for (let i = 0; i < COUNT; i++) {
    const kind = Math.random() < 0.75 ? 'bot' : 'human'; // mostly bot episodes, some human contrast
    const mode = kind === 'bot' ? (Math.random() < 0.5 ? 'naive' : 'evasive') : null;
    const category = CATEGORIES[randInt(0, CATEGORIES.length - 1)];
    const target = randomWorldPoint(80);
    const len = randInt(40, C.TRACE_LEN);
    const startTick = randInt(1000, 100000); // arbitrary but plausible tick range

    const trace = kind === 'bot' ? botTrace(target, startTick, len) : humanTrace(target, startTick, len);
    const fireTick = trace[trace.length - 1].tick;
    const playerId = newId();
    const trapEvent = {
      trapId: newId(),
      category,
      tick: fireTick,
      x: target.x,
      y: target.y,
      playerId,
      spawnTick: startTick - randInt(20, 200),
    };

    const episode = makeEpisode({ playerId, kind, mode, trapEvent, trace, stubScore: 0 });
    files.push(await writeEpisode(OUT_DIR, episode));
  }
  console.log(`Wrote ${files.length} synthetic trace episode(s) to ${OUT_DIR}`);
}

main();
