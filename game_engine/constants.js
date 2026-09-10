// constants.js — the single source of truth.
// Imported by the server, the browser client, the bots and the dashboard.
// Plain ESM with no Node-only APIs, so the browser can import it directly.
//
// NOTE: the sprite (asset) table deliberately does NOT live here.
// Bots import this file; the asset table is the one thing a headless bot
// must not get for free. It lives in client/sprites.js.

export const PORT = 8080;

export const PATHS = Object.freeze({
  GAME: '/game',
  TELEMETRY: '/telemetry',
  ADMIN: '/admin-feed', // keyed; server truth incl. traps. Never used by the game client.
});

// ── World ───────────────────────────────────────────────────────────────
// One unit = one canvas pixel at 100% zoom.
export const WORLD_W = 1600;
export const WORLD_H = 900;

// ── Timing ──────────────────────────────────────────────────────────────
export const TICK_HZ = 20;
export const TICK_MS = 1000 / TICK_HZ; // 50 ms

// ── Players ─────────────────────────────────────────────────────────────
export const PLAYER_SPEED = 7;   // units per tick (140 units/s), diagonals normalised. Was 4 in the plan; raised for a livelier demo.
export const PLAYER_RADIUS = 8;

// ── Pickups & traps ─────────────────────────────────────────────────────
export const PICKUP_RADIUS = 24;
export const TRAP_RADIUS = PICKUP_RADIUS; // MUST stay identical to PICKUP_RADIUS
export const TRAP_DWELL_TICKS = 2;        // consecutive ticks inside radius to fire

// ── Normal entities ─────────────────────────────────────────────────────
export const COIN_COUNT = 30;
export const CHEST_COUNT = 10;
export const COIN_VALUE = 1;
export const CHEST_VALUE = 5;
export const RESPAWN_DELAY_TICKS = 3 * TICK_HZ; // ~3 s after pickup

// ── Spawning ────────────────────────────────────────────────────────────
export const HUMAN_SPAWN = Object.freeze({ x: WORLD_W / 8, y: WORLD_H / 2 }); // (200, 450)
export const BOT_SPAWN_MIN_DIST = 300;  // bots spawn at least this far from HUMAN_SPAWN
export const SPAWN_CLEAR_RADIUS = 80;   // traps never placed this close to a spawn point

// ── Detection support ───────────────────────────────────────────────────
export const TRACE_LEN = 200;           // ticks of history per player (10 s)
export const GRID_CELL = 32;            // revisit grid cell size
// ceil, not floor: 900/32 = 28.1, so floor(y/32) can reach index 28 → 29 rows.
export const GRID_COLS = Math.ceil(WORLD_W / GRID_CELL); // 50
export const GRID_ROWS = Math.ceil(WORLD_H / GRID_CELL); // 29
export const SCORE_THRESHOLD = 0.75;

// ── Protocol cadence ────────────────────────────────────────────────────
export const TELEMETRY_EVERY_TICKS = 10; // 2 Hz
export const INPUT_KEEPALIVE_TICKS = 10; // client re-sends its input this often
export const INPUT_TIMEOUT_TICKS = 30;   // no input for 1.5 s → server stops the player

// ── Obstacles ───────────────────────────────────────────────────────────
// Fixed rectangles, identical on every run. {x, y} is the top-left corner.
// L-shapes are two overlapping rectangles. Rules the layout keeps:
//  - HUMAN_SPAWN stays clear;
//  - every gap a player must pass through is at least 3× PLAYER_RADIUS wide;
//  - exactly ONE sealed area exists: the vault (VAULT below). Nothing else is
//    enclosed. server/world.js flood-fills reachability at startup and never
//    spawns real loot in unreachable space.
export const OBSTACLES = Object.freeze([
  // top-left L
  { x: 300,  y: 100, w: 220, h: 36 },
  { x: 300,  y: 136, w: 36,  h: 150 },
  // centre-left long wall
  { x: 640,  y: 230, w: 36,  h: 300 },
  // top-centre bar
  { x: 820,  y: 110, w: 320, h: 36 },
  // scattered blocks
  { x: 470,  y: 420, w: 48,  h: 48 },
  { x: 900,  y: 360, w: 48,  h: 48 },
  { x: 1020, y: 470, w: 48,  h: 48 },
  { x: 1460, y: 120, w: 60,  h: 60 },
  // bottom-left L
  { x: 300,  y: 660, w: 280, h: 36 },
  { x: 544,  y: 540, w: 36,  h: 120 },
  // right-side corridor (two parallel walls, 64-unit lane between them)
  { x: 1180, y: 290, w: 260, h: 28 },
  { x: 1180, y: 382, w: 260, h: 28 },
  // bottom-right L
  { x: 1150, y: 650, w: 230, h: 36 },
  { x: 1344, y: 540, w: 36,  h: 146 },
  // the vault: a sealed 68×68 room (four walls)
  { x: 800,  y: 640, w: 100, h: 16 },
  { x: 800,  y: 724, w: 100, h: 16 },
  { x: 800,  y: 640, w: 16,  h: 100 },
  { x: 884,  y: 640, w: 16,  h: 100 },
].map(Object.freeze));

/** Interior of the sealed vault — visible to everyone, reachable by no one. For unreachable_bait. */
export const VAULT = Object.freeze({ x: 816, y: 656, w: 68, h: 68 });
