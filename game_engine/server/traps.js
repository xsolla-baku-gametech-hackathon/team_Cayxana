// server/traps.js — OWNER: Traps workstream.
//
// STARTER IMPLEMENTATION: one working category, "ghost_loot".
// A ghost loot is an ordinary-looking coin or chest whose sprite name is NOT in
// the client's asset table. Humans never see it; a bot reading raw state does.
// It is placed in odd spots a human has no reason to visit: map corners, hard
// against the map edge, or hugging a wall. A player who walks there and lingers
// is chasing something they could not have seen.
//
// CHAIN REACTION: each time a player triggers a trap, CHAIN_PER_HIT more appear
// around the spot, up to CHAIN_MAX extra traps per player at once. A bot keeps
// feeding the chain; a human who hit one by accident walks on, the extras expire,
// and everything falls back to the base level. Chain traps are never placed in
// the cone ahead of the player's direction of travel, so an accidental hit
// doesn't put new traps straight onto a human's path.
//
// The Traps workstream extends this with the other three categories. Keep the
// export names; the server calls them.

import * as C from '../constants.js';
import { newEntityId, isWalkable, isReachable, spriteNamesOfKind, ITEM_STYLE } from './world.js';

const CATEGORY = 'ghost_loot';
const TRAPS_PER_PLAYER = 10;
const TTL_MIN_TICKS = 80;        // 4 s   (plan said 40–120; longer so the fast bot can reach them)
const TTL_MAX_TICKS = 200;       // 10 s
const RESPAWN_GAP_MEAN_TICKS = 40;
const MIN_DIST_FROM_PLAYER = 150;
const PREFERRED_MAX_DIST = 550;  // prefer spots the player could plausibly reach before TTL
const HUG = C.PLAYER_RADIUS + 3; // distance from a wall/edge: tight, but reachable

// Chain reaction
const CHAIN_PER_HIT = 2;         // new traps around each triggered one
const CHAIN_MAX = 6;             // cap on chain traps alive per player (on top of the base 3)
const CHAIN_RADIUS_MIN = 70;     // never right on top of the player
const CHAIN_RADIUS_MAX = 180;
const CHAIN_TTL_MIN_TICKS = 60;  // 3 s
const CHAIN_TTL_MAX_TICKS = 140; // 7 s
const HEADING_EXCLUSION = (40 * Math.PI) / 180; // keep the ±40° cone ahead of the player clear

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

/** @type {Map<string, {pos:{x:number,y:number}|null, heading:{x:number,y:number}|null, slots:Array<object>, chain:object[]}>} */
const players = new Map();
const stats = { fired: 0, spawned: 0, chainSpawned: 0 };

function stateFor(playerId, tick) {
  let s = players.get(playerId);
  if (!s) {
    // Stagger the first spawns so traps don't all appear the instant someone connects.
    s = {
      pos: null,
      heading: null, // last non-zero direction of travel, unit vector
      slots: Array.from({ length: TRAPS_PER_PLAYER }, () => ({ trap: null, respawnAt: tick + randInt(20, 100) })),
      chain: [],
    };
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
    if (!isWalkable(p.x, p.y, C.PLAYER_RADIUS) || !isReachable(p.x, p.y)) continue;
    if (dist(p, C.HUMAN_SPAWN) < C.SPAWN_CLEAR_RADIUS) continue;
    const d = dist(p, playerPos);
    if (d < MIN_DIST_FROM_PLAYER) continue;
    if (existing.some((t) => dist(t, p) < 2 * C.TRAP_RADIUS)) continue;
    if (d <= PREFERRED_MAX_DIST) return p;
    fallback ??= p;
  }
  return fallback;
}

function activeTraps(s) {
  return [...s.slots.filter((sl) => sl.trap).map((sl) => sl.trap), ...s.chain];
}

function makeTrap(playerId, spot, tick, ttlMin, ttlMax, chainDepth) {
  const type = Math.random() < 0.75 ? 'coin' : 'chest';
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
    expiresTick: tick + randInt(ttlMin, ttlMax),
    chainDepth, // 0 = base trap, 1+ = spawned by a chain reaction
    dwell: 0,
  };
}

function spawnTrap(playerId, s, tick) {
  const spot = findOddSpot(s.pos, activeTraps(s));
  if (!spot) return null;
  stats.spawned++;
  return makeTrap(playerId, spot, tick, TTL_MIN_TICKS, TTL_MAX_TICKS, 0);
}

/** A spot on a ring around `centre`, outside the cone ahead of the player's heading. */
function findChainSpot(centre, s) {
  const existing = activeTraps(s);
  for (let i = 0; i < 60; i++) {
    const ang = Math.random() * Math.PI * 2;
    const r = rand(CHAIN_RADIUS_MIN, CHAIN_RADIUS_MAX);
    const p = { x: centre.x + Math.cos(ang) * r, y: centre.y + Math.sin(ang) * r };
    if (!isWalkable(p.x, p.y, C.PLAYER_RADIUS) || !isReachable(p.x, p.y)) continue;
    if (dist(p, C.HUMAN_SPAWN) < C.SPAWN_CLEAR_RADIUS) continue;
    if (existing.some((t) => dist(t, p) < 2 * C.TRAP_RADIUS)) continue;
    if (s.heading) {
      const vx = p.x - s.pos.x, vy = p.y - s.pos.y;
      const len = Math.hypot(vx, vy) || 1;
      const cos = (vx * s.heading.x + vy * s.heading.y) / len;
      if (Math.acos(Math.max(-1, Math.min(1, cos))) < HEADING_EXCLUSION) continue;
    }
    return p;
  }
  return null;
}

function spawnChain(playerId, s, around, tick, parentDepth) {
  const room = CHAIN_MAX - s.chain.length;
  for (let i = 0; i < Math.min(CHAIN_PER_HIT, room); i++) {
    const spot = findChainSpot(around, s);
    if (!spot) break;
    s.chain.push(makeTrap(playerId, spot, tick, CHAIN_TTL_MIN_TICKS, CHAIN_TTL_MAX_TICKS, parentDepth + 1));
    stats.chainSpawned++;
  }
}

/**
 * Called once per player per tick, after movement. Also advances this player's
 * trap lifecycle (expire → gap → respawn), since this is where we learn their position.
 */
export function checkTraps(playerId, position, tick) {
  const s = stateFor(playerId, tick);
  if (s.pos) {
    const mx = position.x - s.pos.x, my = position.y - s.pos.y;
    const m = Math.hypot(mx, my);
    if (m > 0.5) s.heading = { x: mx / m, y: my / m };
  }
  s.pos = { x: position.x, y: position.y };
  s.chain = s.chain.filter((t) => tick < t.expiresTick);

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
  // One event per tick at most; dwell counters of the rest keep running.
  let firedTrap = null;
  const bump = (t) => {
    t.dwell = dist(t, position) <= C.TRAP_RADIUS ? t.dwell + 1 : 0;
    return !firedTrap && t.dwell >= C.TRAP_DWELL_TICKS;
  };
  for (const slot of s.slots) {
    if (slot.trap && bump(slot.trap)) {
      firedTrap = slot.trap;
      slot.trap = null; // consumed, like picked-up loot
      slot.respawnAt = tick + expGap(RESPAWN_GAP_MEAN_TICKS);
    }
  }
  for (const t of s.chain) {
    if (bump(t)) firedTrap = t;
  }
  // One category per tick: two trips at once would be two observation windows
  // opened on the same movement, which is one piece of evidence counted twice.
  if (!firedTrap) return null;

  s.chain = s.chain.filter((t) => t !== firedTrap);
  stats.fired++;
  spawnChain(playerId, s, firedTrap, tick, firedTrap.chainDepth);
  return {
    trapId: firedTrap.trapId,
    category: firedTrap.category,
    tick,
    x: firedTrap.x,
    y: firedTrap.y,
    playerId,
    spawnTick: firedTrap.spawnTick, // for the reaction-delay feature
    chainDepth: firedTrap.chainDepth,
  };
}

/** Called once per player per tick, after checkTraps. Appends that player's active traps. */
export function injectTraps(entities, playerId, tick) {
  const s = players.get(playerId);
  if (!s) return entities;
  for (const t of activeTraps(s)) {
    // Only wire-shaped fields; the server whitelists anyway.
    entities.push({ id: t.trapId, type: t.type, x: t.x, y: t.y, sprite: t.sprite, color: t.color, value: t.value });
  }
  return entities;
}

/** "caught" is left to the dashboard (events + kind), so this module never sees the kind label. */
export function getCategoryStats() {
  return [
    { category: CATEGORY, weight: 1, fired: stats.fired, caught: 0, burned: false },
  ];
}

/** ADMIN ONLY — sent over the keyed /admin channel, never to game clients. */
export function getAdminSnapshot() {
  const out = [];
  for (const s of players.values()) {
    for (const t of activeTraps(s)) {
      out.push({ trapId: t.trapId, playerId: t.playerId, category: t.category, type: t.type, x: t.x, y: t.y, expiresTick: t.expiresTick, chainDepth: t.chainDepth });
    }
  }
  return out;
}

export function onPlayerLeave(playerId) {
  players.delete(playerId);
}
