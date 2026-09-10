// client/autoplay.js — autopilots. Pure decision logic: state message in, {dx, dy} out.
// Used by the browser client (?auto=bot | ?auto=human) and by bots/naive-bot.js,
// so the bot you watch in the browser and the bot farm behave identically.
//
// The difference between the brains IS the project in miniature:
//   NaiveBrain      one farm bot, two modes via FARM_FILTERS:
//                     naive   — no asset table → walks into traps, and gets
//                               deflected off real loot by a phantom player.
//                     evasive — patched to check the asset table for both loot
//                               AND players (same tell, sprite-null) → doesn't.
//   HumanLikeBrain  sees only what the client draws (uses the asset table) → it can't.

import * as C from '../constants.js';
import { SPRITES } from './sprites.js';

const LOOT = new Set(['coin', 'chest']);
const STOP = Object.freeze({ dx: 0, dy: 0 });
const STUCK_TICKS = 12;       // no progress for this long → give up on the target
const BLACKLIST_TICKS = 60;

const d2 = (a, b) => (a.x - b.x) ** 2 + (a.y - b.y) ** 2;
const axis = (delta, deadzone) => (delta > deadzone ? 1 : delta < -deadzone ? -1 : 0);
const randInt = (rng, lo, hi) => lo + Math.floor(rng() * (hi - lo + 1));

// Real coordinates are realX/realY when present (position_offset trap category),
// falling back to the drawn x/y. This is the bot's core tell and is never mode-dependent.
const realPos = (e) => ({ x: e.realX ?? e.x, y: e.realY ?? e.y });

/**
 * The only mode-dependent piece. `naive` accepts every entity in the packet —
 * no asset table, so a ghost_loot item (a plausible sprite name absent from
 * SPRITES) looks exactly as real as a genuine coin. `evasive` patches around
 * that one tell by checking the same asset table the browser's render loop
 * uses: if the client wouldn't draw it, don't chase it.
 */
export const FARM_FILTERS = {
  naive: () => true,
  evasive: (e) => SPRITES.has(e.sprite),
};

// ── Local obstacle steering (a "bug algorithm", not pathfinding) ───────────
// Movement is still "walk straight at the target" in spirit — no route
// planning, no map of free space beyond what's checked one step ahead. This
// only lets the bot slide along a wall's edge when the direct step is blocked,
// same as any bot author could derive from the obstacle rectangles alone
// (constants.js — public, not server-only). A spot with NO path around it
// (unreachable_bait) still can't be reached this way: every one-step probe
// around the bot fails there too, so it still stalls at the boundary — the
// same detectable signature, just no longer misfiring on ordinary reachable
// loot that happens to sit around a corner.

// Index i = i*45°, matching Math.atan2's angle convention (0 = +x, 90° = +y).
const OCTANTS = [
  { dx: 1, dy: 0 }, { dx: 1, dy: 1 }, { dx: 0, dy: 1 }, { dx: -1, dy: 1 },
  { dx: -1, dy: 0 }, { dx: -1, dy: -1 }, { dx: 0, dy: -1 }, { dx: 1, dy: -1 },
];
const octantOf = (dx, dy) => Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) & 7;

function circleHitsRect(x, y, r, rect) {
  const nx = Math.max(rect.x, Math.min(x, rect.x + rect.w));
  const ny = Math.max(rect.y, Math.min(y, rect.y + rect.h));
  return (x - nx) ** 2 + (y - ny) ** 2 < r * r;
}

/** Mirrors server/world.js's own collision rule, reconstructed from public geometry. */
function blockedAt(x, y) {
  const r = C.PLAYER_RADIUS;
  if (x < r || y < r || x > C.WORLD_W - r || y > C.WORLD_H - r) return true;
  return C.OBSTACLES.some((o) => circleHitsRect(x, y, r, o));
}

function probe(you, dir) {
  const len = Math.hypot(dir.dx, dir.dy) || 1;
  return { x: you.x + (dir.dx / len) * C.PLAYER_SPEED, y: you.y + (dir.dy / len) * C.PLAYER_SPEED };
}

/**
 * Direct step toward `target` if clear; otherwise slides along whichever side
 * of the obstacle first opens up, remembering that side (via `mem`) so it
 * doesn't flip-flop between left/right each tick. `mem.side` is 0 once the
 * direct path is clear again. Returns STOP only when every direction one step
 * out is blocked — genuinely boxed in, not merely "behind a wall".
 */
function steerAround(you, target, mem) {
  const dx = target.x - you.x, dy = target.y - you.y;
  if (Math.hypot(dx, dy) < 1) return STOP;
  const direct = octantOf(dx, dy);
  const directStep = OCTANTS[direct];
  const p = probe(you, directStep);
  if (!blockedAt(p.x, p.y)) {
    mem.side = 0;
    return directStep;
  }
  const sides = mem.side === 0 ? [1, -1] : [mem.side, -mem.side];
  for (const side of sides) {
    for (let turn = 1; turn <= 4; turn++) {
      const idx = (direct + side * turn) & 7;
      const step = OCTANTS[idx];
      const q = probe(you, step);
      if (!blockedAt(q.x, q.y)) {
        mem.side = side;
        return step;
      }
    }
  }
  return STOP; // every neighbouring direction blocked — really no way through from here
}

/** Shared stuck detection: if we're pushing but not moving, blacklist the target. */
class StuckGuard {
  constructor() {
    this.last = null;
    this.stuckFor = 0;
    this.blacklist = new Map(); // id → tick until which it is ignored
  }
  expire(tick) {
    for (const [id, until] of this.blacklist) if (until <= tick) this.blacklist.delete(id);
  }
  blocked(id) {
    return this.blacklist.has(id);
  }
  /** @returns {boolean} true if the target was just given up on */
  update(you, pushing, targetId, tick) {
    let gaveUp = false;
    if (this.last && pushing && Math.hypot(you.x - this.last.x, you.y - this.last.y) < 0.5) {
      if (++this.stuckFor >= STUCK_TICKS) {
        if (targetId) this.blacklist.set(targetId, tick + BLACKLIST_TICKS);
        this.stuckFor = 0;
        gaveUp = true;
      }
    } else {
      this.stuckFor = 0;
    }
    this.last = { x: you.x, y: you.y };
    return gaveUp;
  }
}

/**
 * The farming bot: nearest loot, straight line, re-evaluated every tick.
 * `modeState` is a shared, externally-mutable `{ mode: 'naive' | 'evasive' }` —
 * flipping it (e.g. from a control endpoint) changes every live instance's
 * next decision with no reconnect, since it's read fresh each tick.
 */
export class NaiveBrain {
  constructor(modeState = { mode: 'naive' }) {
    this.modeState = modeState;
    this.guard = new StuckGuard();
    this.input = STOP;
    this.lastTargetId = null;
    this.wallFollow = { side: 0 }; // 0 = no wall currently being followed, else +1/-1
  }

  decide({ you, entities, tick }) {
    this.guard.expire(tick);
    const filter = FARM_FILTERS[this.modeState.mode] ?? FARM_FILTERS.naive;
    // Player reaction: contest-avoidance. The same filter applies here as to loot —
    // per OPEN DECISIONS #1, phantom_player is sprite-null exactly like invisible_entity,
    // so evasive's asset-table check correctly ignores a phantom too, while naive still
    // gets deflected by one (that deflection is category C's detectable event).
    const believedPlayers = entities.filter((e) => e.type === 'player' && filter(e));
    let best = null;
    let bestPos = null;
    let bestD = Infinity;
    for (const e of entities) {
      if (!LOOT.has(e.type) || this.guard.blocked(e.id) || !filter(e)) continue;
      const pos = realPos(e);
      const d = d2(pos, you);
      if (believedPlayers.some((p) => d2(pos, p) < d)) continue; // someone else is closer to it
      if (d < bestD) { bestD = d; best = e; bestPos = pos; }
    }
    if (best?.id !== this.lastTargetId) {
      this.lastTargetId = best?.id ?? null;
      this.wallFollow.side = 0; // new target: any remembered side belonged to the old one
    }
    this.guard.update(you, this.input.dx !== 0 || this.input.dy !== 0, best?.id, tick);
    this.input = best ? steerAround(you, bestPos, this.wallFollow) : STOP;
    return this.input;
  }
}

/**
 * A stand-in for a person, so the game can be watched without someone at the keyboard.
 * Sees only what is drawn, doesn't always take the nearest coin, reacts with a delay,
 * pauses, wanders, and wobbles off a straight line.
 *
 * NOT a substitute for real human traces when calibrating the classifier —
 * record real people for that.
 */
export class HumanLikeBrain {
  constructor(rng = Math.random) {
    this.rng = rng;
    this.guard = new StuckGuard();
    this.input = STOP;
    this.target = null;        // { id } for loot, or { x, y } for a wander point
    this.nextDecideAt = 0;
    this.idleUntil = 0;
    this.reactUntil = 0;
    this.wobble = { input: STOP, until: 0 };
  }

  #choose(you, visible, tick) {
    const r = this.rng();
    this.nextDecideAt = tick + randInt(this.rng, 8, 24);
    this.reactUntil = tick + randInt(this.rng, 2, 6); // reaction time before acting on a new choice

    if (r < 0.07) { // pause: look around, think
      this.target = null;
      this.idleUntil = tick + randInt(this.rng, 10, 30);
      this.nextDecideAt = this.idleUntil;
      return;
    }
    if (r < 0.15 || visible.length === 0) { // wander somewhere nearby
      const ang = this.rng() * Math.PI * 2;
      const len = 80 + this.rng() * 200;
      this.target = {
        x: Math.min(C.WORLD_W - 30, Math.max(30, you.x + Math.cos(ang) * len)),
        y: Math.min(C.WORLD_H - 30, Math.max(30, you.y + Math.sin(ang) * len)),
      };
      return;
    }
    // Usually the nearest visible loot, sometimes the 2nd or 3rd nearest.
    const sorted = visible.slice().sort((a, b) => d2(a, you) - d2(b, you));
    const w = this.rng();
    const idx = Math.min(sorted.length - 1, w < 0.6 ? 0 : w < 0.85 ? 1 : 2);
    this.target = { id: sorted[idx].id };
  }

  decide({ you, entities, tick }) {
    this.guard.expire(tick);
    if (tick < this.idleUntil) return (this.input = STOP);

    // THE ASYMMETRY: only loot the client would actually draw.
    const visible = entities.filter((e) => LOOT.has(e.type) && SPRITES.has(e.sprite) && !this.guard.blocked(e.id));

    let goal = null;
    if (this.target?.id) goal = visible.find((e) => e.id === this.target.id) ?? null; // gone = picked up
    else if (this.target) goal = this.target;

    const arrived = goal && !this.target.id && d2(goal, you) < 20 * 20;
    if (!goal || arrived || tick >= this.nextDecideAt) {
      this.#choose(you, visible, tick);
      if (this.target?.id) goal = visible.find((e) => e.id === this.target.id) ?? null;
      else goal = this.target;
    }

    if (tick < this.reactUntil) return this.input; // still reacting: keep doing what we were doing
    if (!goal) return (this.input = STOP);

    let next = { dx: axis(goal.x - you.x, 6), dy: axis(goal.y - you.y, 6) };

    // Wobble: people drift off a straight line for a moment.
    if (tick < this.wobble.until) {
      next = this.wobble.input;
    } else if ((next.dx || next.dy) && this.rng() < 0.06) {
      const variants = [
        { dx: next.dx, dy: 0 }, { dx: 0, dy: next.dy },          // drop an axis
        { dx: next.dx || (this.rng() < 0.5 ? 1 : -1), dy: next.dy || (this.rng() < 0.5 ? 1 : -1) }, // cut a corner
      ].filter((v) => v.dx || v.dy);
      this.wobble = { input: variants[randInt(this.rng, 0, variants.length - 1)], until: tick + randInt(this.rng, 3, 8) };
      next = this.wobble.input;
    }

    if (this.guard.update(you, this.input.dx !== 0 || this.input.dy !== 0, this.target?.id, tick)) {
      this.target = null; // gave up; choose again next tick
      this.wobble.until = 0;
    }
    return (this.input = next);
  }
}
