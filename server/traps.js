// server/traps.js — OWNER: Traps workstream.
//
// STARTER IMPLEMENTATION: one working category, "ghost_loot".
// A ghost loot is an ordinary-looking coin or chest whose sprite name is NOT in
// the client's asset table. Humans never see it; a bot reading raw state does.
// It is placed in odd spots a human has no reason to visit: map corners, hard
// against the map edge, or hugging a wall. A player who walks there and lingers
// is chasing something they could not have seen.
//
// The Traps workstream extends this with the other three categories. Keep the
// export names; the server calls them.

import * as C from '../constants.js';
import { newEntityId, isWalkable, spriteNamesOfKind, ITEM_STYLE } from './world.js';

const CATEGORY = 'ghost_loot';
const TRAPS_PER_PLAYER = 3;
const TTL_MIN_TICKS = 80;        // 4 s   (plan said 40–120; longer so the fast bot can reach them)
const TTL_MAX_TICKS = 200;       // 10 s
const RESPAWN_GAP_MEAN_TICKS = 40;
const MIN_DIST_FROM_PLAYER = 150;
const PREFERRED_MAX_DIST = 550;  // prefer spots the player could plausibly reach before TTL
const HUG = C.PLAYER_RADIUS + 3; // distance from a wall/edge: tight, but reachable

// Plausible variant names that are deliberately absent from client/sprites.js.
const GHOST_SPRITES = {
  coin: ['coin_gold_05', 'coin_gold_06', 'coin_gold_07'],
  chest: ['chest_wood_04', 'chest_wood_05'],
};
for (const [kind, names] of Object.entries(GHOST_SPRITES)) {
  const drawable = new Set(spriteNamesOfKind(kind));
  const clash = names.filter((n) => drawable.has(n));
  if (clash.length) throw new Error(`ghost sprite(s) ${clash} exist in the asset table — humans would see them`);
}

const rand = (lo, hi) => lo + Math.random() * (hi - lo);
const randInt = (lo, hi) => Math.floor(rand(lo, hi + 1));
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
const expGap = (mean) => Math.max(1, Math.round(-Math.log(1 - Math.random()) * mean)); // Poisson-process gap
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

/** @type {Map<string, {pos:{x:number,y:number}|null, slots:Array<object>}>} */
const players = new Map();
const stats = { fired: 0, spawned: 0 };

function stateFor(playerId, tick) {
  let s = players.get(playerId);
  if (!s) {
    // Stagger the first spawns so traps don't all appear the instant someone connects.
    s = { pos: null, slots: Array.from({ length: TRAPS_PER_PLAYER }, () => ({ trap: null, respawnAt: tick + randInt(20, 100) })) };
    players.set(playerId, s);
  }
  return s;
}

/** One candidate in an odd location. */
function oddSpotCandidate() {
  const r = Math.random();
  if (r < 0.25) {
    // Map corner.
    return {
      x: Math.random() < 0.5 ? HUG + rand(0, 40) : C.WORLD_W - HUG - rand(0, 40),
      y: Math.random() < 0.5 ? HUG + rand(0, 40) : C.WORLD_H - HUG - rand(0, 40),
    };
  }
  if (r < 0.55) {
    // Hard against a map edge.
    switch (randInt(0, 3)) {
      case 0: return { x: rand(HUG, C.WORLD_W - HUG), y: HUG };
      case 1: return { x: rand(HUG, C.WORLD_W - HUG), y: C.WORLD_H - HUG };
      case 2: return { x: HUG, y: rand(HUG, C.WORLD_H - HUG) };
      default: return { x: C.WORLD_W - HUG, y: rand(HUG, C.WORLD_H - HUG) };
    }
  }
  // Hugging one side of an obstacle.
  const o = pick(C.OBSTACLES);
  switch (randInt(0, 3)) {
    case 0: return { x: rand(o.x, o.x + o.w), y: o.y - HUG };
    case 1: return { x: rand(o.x, o.x + o.w), y: o.y + o.h + HUG };
    case 2: return { x: o.x - HUG, y: rand(o.y, o.y + o.h) };
    default: return { x: o.x + o.w + HUG, y: rand(o.y, o.y + o.h) };
  }
}

function findOddSpot(playerPos, existing) {
  let fallback = null;
  for (let i = 0; i < 80; i++) {
    const p = oddSpotCandidate();
    if (!isWalkable(p.x, p.y, C.PLAYER_RADIUS)) continue;
    if (dist(p, C.HUMAN_SPAWN) < C.SPAWN_CLEAR_RADIUS) continue;
    const d = dist(p, playerPos);
    if (d < MIN_DIST_FROM_PLAYER) continue;
    if (existing.some((t) => dist(t, p) < 2 * C.TRAP_RADIUS)) continue;
    if (d <= PREFERRED_MAX_DIST) return p;
    fallback ??= p;
  }
  return fallback;
}

function spawnTrap(playerId, s, tick) {
  const active = s.slots.filter((sl) => sl.trap).map((sl) => sl.trap);
  const spot = findOddSpot(s.pos, active);
  if (!spot) return null;
  const type = Math.random() < 0.75 ? 'coin' : 'chest';
  stats.spawned++;
  return {
    trapId: newEntityId(), // same id format as every real entity
    playerId,
    category: CATEGORY,
    type,
    sprite: pick(GHOST_SPRITES[type]),
    color: ITEM_STYLE[type].color,
    value: ITEM_STYLE[type].value,
    x: spot.x,
    y: spot.y,
    spawnTick: tick,
    expiresTick: tick + randInt(TTL_MIN_TICKS, TTL_MAX_TICKS),
    dwell: 0,
  };
}

/**
 * Called once per player per tick, after movement. Also advances this player's
 * trap lifecycle (expire → gap → respawn), since this is where we learn their position.
 */
export function checkTraps(playerId, position, tick) {
  const s = stateFor(playerId, tick);
  s.pos = { x: position.x, y: position.y };

  // Lifecycle: expire old traps, spawn due ones.
  for (const slot of s.slots) {
    if (slot.trap && tick >= slot.trap.expiresTick) {
      slot.trap = null;
      slot.respawnAt = tick + expGap(RESPAWN_GAP_MEAN_TICKS);
    }
    if (!slot.trap && tick >= slot.respawnAt) {
      slot.trap = spawnTrap(playerId, s, tick);
      if (!slot.trap) slot.respawnAt = tick + 10; // no spot found; retry shortly
    }
  }

  // Firing: inside the radius for TRAP_DWELL_TICKS consecutive ticks.
  let fired = null;
  for (const slot of s.slots) {
    const t = slot.trap;
    if (!t) continue;
    t.dwell = dist(t, position) <= C.TRAP_RADIUS ? t.dwell + 1 : 0;
    if (!fired && t.dwell >= C.TRAP_DWELL_TICKS) {
      fired = {
        trapId: t.trapId,
        category: t.category,
        tick,
        x: t.x,
        y: t.y,
        playerId,
        spawnTick: t.spawnTick, // for the reaction-delay feature
      };
      slot.trap = null; // consumed, like picked-up loot
      slot.respawnAt = tick + expGap(RESPAWN_GAP_MEAN_TICKS);
      stats.fired++;
    }
  }
  return fired;
}

/** Called once per player per tick, after checkTraps. Appends that player's active traps. */
export function injectTraps(entities, playerId, tick) {
  const s = players.get(playerId);
  if (!s) return entities;
  for (const { trap: t } of s.slots) {
    if (!t) continue;
    // Only wire-shaped fields; the server whitelists anyway.
    entities.push({ id: t.trapId, type: t.type, x: t.x, y: t.y, sprite: t.sprite, color: t.color, value: t.value });
  }
  return entities;
}

/** "caught" is left to the dashboard (events + kind), so this module never sees the kind label. */
export function getCategoryStats() {
  return [{ category: CATEGORY, weight: 1, fired: stats.fired, caught: 0, burned: false }];
}

/** ADMIN ONLY — sent over the keyed /admin channel, never to game clients. */
export function getAdminSnapshot() {
  const out = [];
  for (const s of players.values()) {
    for (const { trap: t } of s.slots) {
      if (t) out.push({ trapId: t.trapId, playerId: t.playerId, category: t.category, type: t.type, x: t.x, y: t.y, expiresTick: t.expiresTick });
    }
  }
  return out;
}

export function onPlayerLeave(playerId) {
  players.delete(playerId);
}
