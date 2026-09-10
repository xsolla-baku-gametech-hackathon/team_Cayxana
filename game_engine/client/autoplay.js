// client/autoplay.js — autopilots. Pure decision logic: state message in, {dx, dy} out.
// Used by the browser client (?auto=bot | ?auto=human) and by bots/naive-bot.js,
// so the bot you watch in the browser and the bot farm behave identically.
//
// The difference between the two brains IS the project in miniature:
//   NaiveBrain      sees every entity in the packet (no asset table) → walks into traps.
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
 * The cheap farming bot: nearest loot, straight line, re-evaluated every tick.
 * Deliberately has NO asset table — it treats every coin/chest in the packet as real.
 */
export class NaiveBrain {
  constructor() {
    this.guard = new StuckGuard();
    this.input = STOP;
  }

  decide({ you, entities, tick }) {
    this.guard.expire(tick);
    let best = null;
    let bestD = Infinity;
    for (const e of entities) {
      if (!LOOT.has(e.type) || this.guard.blocked(e.id)) continue;
      const d = d2(e, you);
      if (d < bestD) { bestD = d; best = e; }
    }
    this.guard.update(you, this.input.dx !== 0 || this.input.dy !== 0, best?.id, tick);
    const dz = C.PLAYER_SPEED / 2;
    this.input = best ? { dx: axis(best.x - you.x, dz), dy: axis(best.y - you.y, dz) } : STOP;
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
