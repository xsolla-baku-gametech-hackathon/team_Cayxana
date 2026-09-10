// server/world.js — authoritative world state. Everything lives in memory.
//
// Also exports helpers the traps module SHOULD use (newEntityId, isWalkable,
// randomWalkablePoint, spriteNamesOfKind) so trap entities are generated the
// same way as real ones and cannot be told apart by id format or placement.

import { randomBytes } from 'node:crypto';
import * as C from '../constants.js';
import { spriteNamesOfKind } from '../client/sprites.js';
import { RingBuffer } from './ringbuffer.js';

export { spriteNamesOfKind };

const COIN_SPRITES = spriteNamesOfKind('coin');
const CHEST_SPRITES = spriteNamesOfKind('chest');
const PLAYER_SPRITE = spriteNamesOfKind('player')[0];

/** Look of real loot. Traps import this so ghost loot is visually identical on the wire. */
export const ITEM_STYLE = {
  coin:  { sprites: COIN_SPRITES,  color: '#f2c14e', value: C.COIN_VALUE },
  chest: { sprites: CHEST_SPRITES, color: '#b5713a', value: C.CHEST_VALUE },
};

const PLAYER_COLORS = ['#4ea8f2', '#7bd389', '#e07be0', '#f28c4e', '#b7a4f5', '#5fd4d0', '#f2e24e', '#f25f7a'];

// ── Shared helpers ─────────────────────────────────────────────────────

/** Random opaque id. Use for EVERY entity (including traps) so ids leak nothing. */
export function newEntityId() {
  return randomBytes(6).toString('hex');
}

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const dist2 = (ax, ay, bx, by) => (ax - bx) ** 2 + (ay - by) ** 2;

function circleHitsRect(x, y, r, rect) {
  const nx = clamp(x, rect.x, rect.x + rect.w);
  const ny = clamp(y, rect.y, rect.y + rect.h);
  return dist2(x, y, nx, ny) < r * r;
}

function hitsObstacle(x, y, r) {
  for (const o of C.OBSTACLES) if (circleHitsRect(x, y, r, o)) return true;
  return false;
}

/** True if a circle of radius `clearance` centred at (x, y) is fully on walkable ground. */
export function isWalkable(x, y, clearance = C.PLAYER_RADIUS) {
  if (x < clearance || y < clearance || x > C.WORLD_W - clearance || y > C.WORLD_H - clearance) return false;
  return !hitsObstacle(x, y, clearance);
}

// ── Reachability ───────────────────────────────────────────────────────
// Flood fill from HUMAN_SPAWN over an 8-unit grid, once at startup. A cell is
// open if a player centred there would not overlap an obstacle. Anything the
// fill never reaches (the vault) is unreachable: real loot never spawns there,
// and unreachable_bait can use exactly that space.

const REACH_CELL = 8;
const REACH_COLS = Math.ceil(C.WORLD_W / REACH_CELL);
const REACH_ROWS = Math.ceil(C.WORLD_H / REACH_CELL);
const reachable = new Uint8Array(REACH_COLS * REACH_ROWS);

(function floodFill() {
  const open = (cx, cy) => isWalkable((cx + 0.5) * REACH_CELL, (cy + 0.5) * REACH_CELL, C.PLAYER_RADIUS);
  const sx = Math.floor(C.HUMAN_SPAWN.x / REACH_CELL);
  const sy = Math.floor(C.HUMAN_SPAWN.y / REACH_CELL);
  if (!open(sx, sy)) throw new Error('HUMAN_SPAWN is inside an obstacle — fix OBSTACLES in constants.js');
  const queue = [sx + sy * REACH_COLS];
  reachable[queue[0]] = 1;
  while (queue.length) {
    const i = queue.pop();
    const cx = i % REACH_COLS;
    const cy = (i - cx) / REACH_COLS;
    for (const [nx, ny] of [[cx + 1, cy], [cx - 1, cy], [cx, cy + 1], [cx, cy - 1]]) {
      if (nx < 0 || ny < 0 || nx >= REACH_COLS || ny >= REACH_ROWS) continue;
      const j = nx + ny * REACH_COLS;
      if (reachable[j] || !open(nx, ny)) continue;
      reachable[j] = 1;
      queue.push(j);
    }
  }
})();

/**
 * True if a player walking from the spawn can get their centre to (x, y) or
 * within one grid cell of it. The one-cell tolerance keeps wall-hugging spots
 * (a few units closer to the wall than a player centre can go) reachable.
 */
export function isReachable(x, y) {
  const cx = Math.floor(x / REACH_CELL);
  const cy = Math.floor(y / REACH_CELL);
  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -1; dx <= 1; dx++) {
      const nx = cx + dx, ny = cy + dy;
      if (nx >= 0 && ny >= 0 && nx < REACH_COLS && ny < REACH_ROWS && reachable[nx + ny * REACH_COLS]) return true;
    }
  }
  return false;
}

/**
 * Uniform random walkable point.
 * @param {object} [opts]
 * @param {number} [opts.clearance]  radius that must be free of obstacles
 * @param {{x:number,y:number,minDist:number}[]} [opts.avoid]  points to keep away from
 * @param {boolean} [opts.reachable=true]  also require that players can walk there
 * @param {number} [opts.tries]
 * @returns {{x:number,y:number}|null} null only if no point found (should not happen)
 */
export function randomWalkablePoint({ clearance = C.PLAYER_RADIUS, avoid = [], reachable: mustReach = true, tries = 1000 } = {}) {
  for (let i = 0; i < tries; i++) {
    const x = clearance + Math.random() * (C.WORLD_W - 2 * clearance);
    const y = clearance + Math.random() * (C.WORLD_H - 2 * clearance);
    if (!isWalkable(x, y, clearance)) continue;
    if (mustReach && !isReachable(x, y)) continue;
    if (avoid.some((a) => dist2(x, y, a.x, a.y) < a.minDist * a.minDist)) continue;
    return { x, y };
  }
  return null;
}

// ── World ──────────────────────────────────────────────────────────────

export class World {
  constructor() {
    this.tick = 0;
    /** @type {Map<string, object>} coins and chests */
    this.items = new Map();
    /** @type {{type:string, atTick:number}[]} */
    this.respawnQueue = [];
    /** @type {Map<string, object>} */
    this.players = new Map();

    for (let i = 0; i < C.COIN_COUNT; i++) this.spawnItem('coin');
    for (let i = 0; i < C.CHEST_COUNT; i++) this.spawnItem('chest');
  }

  spawnItem(type) {
    const style = ITEM_STYLE[type];
    const pos = randomWalkablePoint({ clearance: 12 });
    if (!pos) return; // world too crowded — skip rather than crash
    const item = {
      id: newEntityId(),
      type,
      x: pos.x,
      y: pos.y,
      sprite: pick(style.sprites),
      color: style.color,
      value: style.value,
    };
    this.items.set(item.id, item);
  }

  /** @param {'human'|'bot'} kind  demo-only label; used for spawn point and dashboard colour, never for detection */
  addPlayer(kind) {
    let pos;
    if (kind === 'bot') {
      pos = randomWalkablePoint({
        avoid: [{ ...C.HUMAN_SPAWN, minDist: C.BOT_SPAWN_MIN_DIST }],
      }) ?? { ...C.HUMAN_SPAWN };
    } else {
      pos = { ...C.HUMAN_SPAWN };
    }
    const player = {
      id: newEntityId(),
      kind,
      x: pos.x,
      y: pos.y,
      color: pick(PLAYER_COLORS),
      input: { dx: 0, dy: 0 },
      lastInputTick: this.tick,
      trace: new RingBuffer(C.TRACE_LEN),
      /** @type {{tick:number, trapEvent:object, score:number, features:object}[]} */
      results: [],
    };
    this.players.set(player.id, player);
    return player;
  }

  removePlayer(id) {
    this.players.delete(id);
  }

  /** Validates and stores input. Returns false (and ignores it) if malformed. */
  setInput(id, dx, dy) {
    const p = this.players.get(id);
    const ok = (v) => v === -1 || v === 0 || v === 1;
    if (!p || !ok(dx) || !ok(dy)) return false;
    p.input.dx = dx;
    p.input.dy = dy;
    p.lastInputTick = this.tick;
    return true;
  }

  processRespawns() {
    if (this.respawnQueue.length === 0) return;
    const due = this.respawnQueue.filter((r) => r.atTick <= this.tick);
    if (due.length === 0) return;
    this.respawnQueue = this.respawnQueue.filter((r) => r.atTick > this.tick);
    for (const r of due) this.spawnItem(r.type);
  }

  /** Apply inputs and move every player, with obstacle collision and world bounds. */
  movePlayers() {
    const R = C.PLAYER_RADIUS;
    for (const p of this.players.values()) {
      // A client that stops talking (closed laptop lid, lost focus) stops moving.
      if (this.tick - p.lastInputTick > C.INPUT_TIMEOUT_TICKS) {
        p.input.dx = 0;
        p.input.dy = 0;
      }
      const { dx, dy } = p.input;
      if (dx === 0 && dy === 0) continue;

      const len = Math.hypot(dx, dy); // 1 or √2 — normalises diagonals
      const vx = (dx / len) * C.PLAYER_SPEED;
      const vy = (dy / len) * C.PLAYER_SPEED;

      // Resolve each axis separately so players slide along walls.
      const nx = clamp(p.x + vx, R, C.WORLD_W - R);
      if (!hitsObstacle(nx, p.y, R)) p.x = nx;
      const ny = clamp(p.y + vy, R, C.WORLD_H - R);
      if (!hitsObstacle(p.x, ny, R)) p.y = ny;
    }
  }

  /** Must run BEFORE the trap check: the classifier needs the lead-up movement. */
  recordTraces() {
    for (const p of this.players.values()) p.trace.push({ tick: this.tick, x: p.x, y: p.y });
  }

  handlePickups() {
    const r2 = C.PICKUP_RADIUS * C.PICKUP_RADIUS;
    for (const p of this.players.values()) {
      for (const [id, item] of this.items) {
        if (dist2(p.x, p.y, item.x, item.y) <= r2) {
          this.items.delete(id);
          this.respawnQueue.push({ type: item.type, atTick: this.tick + C.RESPAWN_DELAY_TICKS });
        }
      }
    }
  }

  /** Fresh plain objects for one viewer: all items plus every OTHER player. */
  baseEntitiesFor(viewerId) {
    const out = [];
    for (const it of this.items.values()) {
      out.push({ id: it.id, type: it.type, x: it.x, y: it.y, sprite: it.sprite, color: it.color, value: it.value });
    }
    for (const p of this.players.values()) {
      if (p.id === viewerId) continue;
      out.push({ id: p.id, type: 'player', x: p.x, y: p.y, sprite: PLAYER_SPRITE, color: p.color });
    }
    return out;
  }
}
