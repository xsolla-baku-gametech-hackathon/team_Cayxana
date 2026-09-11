// server/features.js — the fixed, hand-written half of the detection layer.
//
// Features are code. Rules are data. This file is the boundary between them:
// it turns a movement trace plus one trap event into a flat record of finite
// numbers, and nothing downstream — not the rule engine, not the LLM agent —
// ever sees a coordinate again.
//
// Every function here is synchronous and must never throw. A short or
// degenerate trace produces the documented default, not an exception: this
// runs inside the server tick.

import * as C from '../constants.js';

/**
 * The contract with the agent. The prompt is built from this table, and every
 * rule the agent proposes is validated against it: unknown `feat` names are
 * dropped and out-of-range thresholds are clamped to [min, max].
 *
 * Keep `desc` to one line and written for a reader who has never seen the
 * code — it is the only thing the model knows about what a number means.
 */
export const FEATURE_SCHEMA = Object.freeze([
  { name: 'path_straightness', min: 0, max: 1,
    desc: 'net displacement over path length on the run-in; 1.0 is a perfect straight line' },
  { name: 'speed_cv', min: 0, max: 3,
    desc: 'stddev over mean of per-tick speed; near 0 means constant velocity' },
  { name: 'turn_count', min: 0, max: 200,
    desc: 'number of heading changes sharper than 45 degrees' },
  { name: 'pause_count', min: 0, max: 40,
    desc: 'number of stops lasting at least 5 ticks' },
  { name: 'pause_mean_ticks', min: 0, max: 200,
    desc: 'mean length of those stops, in ticks' },
  { name: 'pause_stddev_ticks', min: 0, max: 100,
    desc: 'stddev of pause lengths in ticks; near 0 means a timer, not a person' },
  { name: 'revisit_ratio', min: 0, max: 1,
    desc: 'unique grid cells visited over trace length; low means circling the same ground' },
  { name: 'reaction_delay', min: 0, max: 400,
    desc: 'ticks between the trap appearing and the player reaching it; low means it was seen instantly' },
  { name: 'dwell_ticks', min: 0, max: 200,
    desc: 'trailing ticks spent inside the trap radius' },
  { name: 'approach_error', min: 0, max: 500,
    desc: 'mean deviation from a straight line to the trap over the approach; near 0 means machine-perfect aim' },
]);

/** name → schema entry, for validation and clamping. */
export const FEATURE_BY_NAME = new Map(FEATURE_SCHEMA.map((f) => [f.name, f]));

const PAUSE_SPEED = 0.5;      // units/tick below which a player counts as stopped
const PAUSE_MIN_RUN = 5;      // consecutive ticks that make a run a "pause"
const TURN_THRESHOLD = Math.PI / 4; // 45 degrees
const APPROACH_WINDOW = 40;   // longest run-in we will look at
const APPROACH_MIN = 8;       // shortest, so a one-tick window cannot read 1.0
const APPROACH_SLACK = 5;     // ticks of moving away we forgive (sliding along a wall)

const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

/** Clamp to the feature's declared range, and never let a NaN escape. */
function bounded(name, value) {
  const f = FEATURE_BY_NAME.get(name);
  if (!Number.isFinite(value)) return f ? f.min : 0;
  return f ? clamp(value, f.min, f.max) : value;
}

function mean(xs) {
  if (xs.length === 0) return 0;
  let s = 0;
  for (const x of xs) s += x;
  return s / xs.length;
}

function stddev(xs) {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  let s = 0;
  for (const x of xs) s += (x - m) ** 2;
  return Math.sqrt(s / xs.length);
}

/** Per-tick speed between consecutive samples, tolerant of gaps in the trace. */
function speeds(trace) {
  const out = [];
  for (let i = 1; i < trace.length; i++) {
    const dt = Math.max(1, (trace[i].tick ?? i) - (trace[i - 1].tick ?? i - 1));
    out.push(Math.hypot(trace[i].x - trace[i - 1].x, trace[i].y - trace[i - 1].y) / dt);
  }
  return out;
}

/** Lengths, in ticks, of every run of at least PAUSE_MIN_RUN slow samples. */
function pauseRuns(spd) {
  const runs = [];
  let run = 0;
  for (const s of spd) {
    if (s < PAUSE_SPEED) {
      run++;
    } else {
      if (run >= PAUSE_MIN_RUN) runs.push(run);
      run = 0;
    }
  }
  if (run >= PAUSE_MIN_RUN) runs.push(run); // a pause still in progress counts
  return runs;
}

/**
 * The approach: the trailing stretch during which the player was closing on
 * the trap, up to APPROACH_WINDOW ticks.
 *
 * The window has to be found rather than fixed, and this is the single
 * decision the whole layer is most sensitive to. Over all 200 ticks a player
 * covers ~1400 units, which for any farming loop spans several separate
 * errands — bot and human alike land near 0.2 and no threshold separates
 * anything. A fixed 40 ticks is better but still usually reaches back past
 * the previous target, so the window carries a corner belonging to a
 * different errand.
 *
 * Walking back while the distance to the trap is shrinking finds the errand
 * itself. A few ticks of moving away are forgiven, because sliding along a
 * wall is part of a straight-line walk in a world with obstacles in it.
 */
function approachWindow(trace, trap) {
  const dist = (s) => Math.hypot(s.x - trap.x, s.y - trap.y);

  // Cut the tail off first. Once a player is inside the trap radius they have
  // arrived, and what the last few ticks contain is 8-direction overshoot
  // around the target — a zig-zag a few units wide that is an artefact of the
  // input protocol, not of intent. Left in, it drags a dead-straight walk down
  // to ~0.85 and makes the approach unreadable. Arrival is dwell_ticks'
  // business; this window is about the walk that got them there.
  let end = trace.length;
  while (end > 1 && dist(trace[end - 1]) <= C.TRAP_RADIUS) end--;
  if (end < APPROACH_MIN) end = Math.min(trace.length, APPROACH_MIN);

  const floor = Math.max(0, end - APPROACH_WINDOW);
  let start = end - 1;
  let violations = 0;
  for (let i = end - 1; i > floor; i--) {
    // Going back in time, the player should be getting FURTHER from the trap.
    if (dist(trace[i - 1]) + 0.5 >= dist(trace[i])) violations = 0;
    else if (++violations > APPROACH_SLACK) break;
    start = i - 1;
  }
  const win = trace.slice(start, end);
  return win.length >= APPROACH_MIN ? win : trace.slice(Math.max(0, end - APPROACH_MIN), end);
}

/** Net displacement over path length across the approach. */
function pathStraightness(win) {
  if (win.length < 2) return 0;
  let total = 0;
  for (let i = 1; i < win.length; i++) {
    total += Math.hypot(win[i].x - win[i - 1].x, win[i].y - win[i - 1].y);
  }
  if (total <= 0) return 0; // stood still: no line to be straight
  const a = win[0];
  const b = win[win.length - 1];
  return Math.hypot(b.x - a.x, b.y - a.y) / total;
}

function turnCount(trace) {
  let count = 0;
  let prev = null;
  for (let i = 1; i < trace.length; i++) {
    const dx = trace[i].x - trace[i - 1].x;
    const dy = trace[i].y - trace[i - 1].y;
    if (Math.hypot(dx, dy) < 0.01) continue; // standing still has no heading
    const heading = Math.atan2(dy, dx);
    if (prev !== null) {
      let diff = Math.abs(heading - prev);
      if (diff > Math.PI) diff = 2 * Math.PI - diff; // shortest way round the circle
      if (diff > TURN_THRESHOLD) count++;
    }
    prev = heading;
  }
  return count;
}

function revisitRatio(trace) {
  const cells = new Set();
  for (const s of trace) {
    const cx = clamp(Math.floor(s.x / C.GRID_CELL), 0, C.GRID_COLS - 1);
    const cy = clamp(Math.floor(s.y / C.GRID_CELL), 0, C.GRID_ROWS - 1);
    cells.add(cy * C.GRID_COLS + cx);
  }
  return cells.size / trace.length;
}

/**
 * Mean perpendicular distance from the straight line joining the start of the
 * approach to the trap. Straightness says the walk was straight; this says it
 * was straight AT THE TRAP, which is the part a person cannot produce by
 * happening to cross the same ground.
 */
function approachError(win, trap) {
  if (win.length < 2) return 0;
  const a = win[0];
  const bx = trap.x - a.x;
  const by = trap.y - a.y;
  const len = Math.hypot(bx, by);
  if (len < 1) return 0; // the approach started on top of the trap
  const ux = bx / len;
  const uy = by / len;
  const errs = [];
  for (const s of win) {
    const px = s.x - a.x;
    const py = s.y - a.y;
    errs.push(Math.abs(px * uy - py * ux)); // |cross product| with a unit vector
  }
  return mean(errs);
}

/**
 * Trailing samples inside TRAP_RADIUS — how long they stood there, not just
 * touched it. Measured from `trapEvent.dwellFrom` when the trap supplies one,
 * which is how an unreachable trap says "they never got to me, but they stood
 * HERE": without it every unreachable_bait trip would report dwell 0 and the
 * loitering the trap fired on would be invisible to every rule.
 */
function dwellTicks(trace, at) {
  let n = 0;
  for (let i = trace.length - 1; i >= 0; i--) {
    if (Math.hypot(trace[i].x - at.x, trace[i].y - at.y) > C.TRAP_RADIUS) break;
    n++;
  }
  return n;
}

/**
 * @param {{tick:number,x:number,y:number}[]} trace  oldest → newest
 * @param {object} trapEvent  as returned by checkTraps()
 * @returns {Record<string, number>}
 */
export function extractFeatures(trace, trapEvent) {
  const empty = {};
  for (const f of FEATURE_SCHEMA) empty[f.name] = f.name === 'revisit_ratio' ? 1 : 0;

  try {
    const t = Array.isArray(trace)
      ? trace.filter((s) => s && Number.isFinite(s.x) && Number.isFinite(s.y))
      : [];
    if (t.length < 2) return empty;

    const trap = {
      x: Number(trapEvent?.x),
      y: Number(trapEvent?.y),
      tick: Number(trapEvent?.tick),
      spawnTick: Number(trapEvent?.spawnTick),
    };
    const stood = {
      x: Number(trapEvent?.dwellFrom?.x ?? trapEvent?.x),
      y: Number(trapEvent?.dwellFrom?.y ?? trapEvent?.y),
    };
    const hasTrap = Number.isFinite(trap.x) && Number.isFinite(trap.y);
    const approach = hasTrap ? approachWindow(t, trap) : t.slice(-APPROACH_WINDOW);

    const spd = speeds(t);
    const runs = pauseRuns(spd);
    const m = mean(spd);
    const reaction = Number.isFinite(trap.tick) && Number.isFinite(trap.spawnTick)
      ? trap.tick - trap.spawnTick
      : 0;

    return {
      path_straightness: bounded('path_straightness', pathStraightness(approach)),
      speed_cv: bounded('speed_cv', m > 0 ? stddev(spd) / m : 0),
      turn_count: bounded('turn_count', turnCount(t)),
      pause_count: bounded('pause_count', runs.length),
      pause_mean_ticks: bounded('pause_mean_ticks', mean(runs)),
      pause_stddev_ticks: bounded('pause_stddev_ticks', stddev(runs)),
      revisit_ratio: bounded('revisit_ratio', revisitRatio(t)),
      reaction_delay: bounded('reaction_delay', reaction),
      dwell_ticks: bounded('dwell_ticks',
        Number.isFinite(stood.x) && Number.isFinite(stood.y) ? dwellTicks(t, stood) : 0),
      approach_error: bounded('approach_error', hasTrap ? approachError(approach, trap) : 0),
    };
  } catch {
    return empty; // a feature bug must never cost the server a tick
  }
}
