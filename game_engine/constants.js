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
// Six fixed rectangles, identical on every run. {x, y} is the top-left corner.
// None of them overlap HUMAN_SPAWN or enclose an area.
export const OBSTACLES = Object.freeze([
  Object.freeze({ x: 350,  y: 120, w: 220, h: 60 }),
  Object.freeze({ x: 700,  y: 300, w: 60,  h: 300 }),
  Object.freeze({ x: 950,  y: 120, w: 300, h: 50 }),
  Object.freeze({ x: 1100, y: 620, w: 250, h: 60 }),
  Object.freeze({ x: 430,  y: 640, w: 180, h: 120 }),
  Object.freeze({ x: 1350, y: 280, w: 70,  h: 220 }),
]);
