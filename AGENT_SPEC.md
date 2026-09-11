# P6 — Adaptive Rule Layer (implementation spec)

Read `CLAUDE.md` and `README.md` first. This document adds one layer on top of
the existing system. It does not change the game, the wire protocol, or the bot
architecture.

## What this layer is

Trap hits are evidence. Rules turn evidence into a score. Today the rules do not
exist (`server/classifier.js` is a stub) and there is nothing that adapts when a
bot evades them.

This layer adds:

1. a **feature extractor** — fixed, hand-written
2. a **rule engine** — evaluates rules loaded from JSON
3. an **evasion monitor** — fires when traps are hit but scores stay low
4. an **LLM agent** — proposes new rules as JSON
5. a **backtest + human approval gate** — nothing an agent writes affects a score
   until a person approves it

**Non-negotiable: the LLM emits JSON only. It never writes JavaScript, and it
never sees raw coordinates.** Features are code. Rules are data.

---

## Blockers to fix first

These are existing bugs. Nothing below works until they are fixed.

1. **`server/classifier.js` returns `{score: 0}` unconditionally.** Replace it
   (step 1 below). Keep the export names — `server/index.js` calls both
   `classify` and `assessPlayer` through safe wrappers.

2. **`assessPlayer` can never flag.** It requires
   `above.length >= 3 && categories.size >= 2`, but `server/traps.js` implements
   exactly one category (`ghost_loot`). Add a second category (step 2).

3. **The evasive bot defeats the whole system.** `FARM_FILTERS.evasive` is
   `SPRITES.has(e.sprite)` and `ghost_loot` uses sprite names absent from the
   asset table, so one line of bot code evades everything. The second category
   must survive that filter.

Do **not** implement `position_offset`: `toWire()` in `server/index.js`
whitelists six fields, so `realX`/`realY` cannot reach the wire and there is no
divergence for a bot to walk into. Do **not** implement `phantom_player`: it is
sprite-null, so the same evasive filter kills it and it adds no coverage.

---

## Step 1 — Feature extractor

New file: `server/features.js`

```js
/**
 * @param {{tick:number,x:number,y:number}[]} trace  oldest → newest
 * @param {object} trapEvent  as returned by checkTraps()
 * @returns {Record<string, number>}
 */
export function extractFeatures(trace, trapEvent) { ... }
```

Must be synchronous, must never throw, must return a finite number for every
feature (use the stated default when the trace is too short).

| Feature | Definition | Range | Default |
|---|---|---|---|
| `path_straightness` | net displacement ÷ total path length | 0–1 | 0 |
| `speed_cv` | stddev ÷ mean of per-tick speed | 0–3 | 0 |
| `turn_count` | count of heading changes > 45° between consecutive ticks | 0–200 | 0 |
| `pause_count` | number of runs of ≥ 5 consecutive ticks with speed < 0.5 | 0–40 | 0 |
| `pause_mean_ticks` | mean length of those runs | 0–200 | 0 |
| `pause_stddev_ticks` | stddev of those run lengths | 0–100 | 0 |
| `revisit_ratio` | unique `GRID_CELL` cells visited ÷ trace length | 0–1 | 1 |
| `reaction_delay` | `trapEvent.tick - trapEvent.spawnTick` | 0–400 | 0 |
| `approach_error` | mean perpendicular distance from the straight line joining the first trace point to the trap, over the last 40 ticks | 0–500 | 0 |
| `dwell_ticks` | trailing ticks within `TRAP_RADIUS` of the trap | 0–200 | 0 |

Use `GRID_CELL`, `GRID_COLS`, `GRID_ROWS`, `TRAP_RADIUS`, `PLAYER_SPEED` from
`constants.js`. Do not hardcode them.

Export the schema too — the agent prompt is built from it:

```js
export const FEATURE_SCHEMA = [
  { name: 'pause_stddev_ticks', min: 0, max: 100,
    desc: 'stddev of pause lengths in ticks; near 0 means a timer, not a person' },
  ...
];
```

## Step 2 — Second trap category

In `server/traps.js`, add `unreachable_bait`. `world.js` already exports
`isReachable()` and flood-fills at startup; `constants.js` exports `VAULT`.

- Place a high-value chest inside `VAULT` using a sprite that **is** in the
  asset table. Visible to everyone, reachable by no one.
- Fire when the player stays within ~60 units of the vault boundary for
  15 consecutive ticks, or makes no net progress toward it for 20 ticks.
- Return the same event shape `checkTraps` already returns, with
  `category: 'unreachable_bait'`.
- Add it to `getCategoryStats()` and `getAdminSnapshot()`.

A human has no reason to stand against a sealed wall. A bot that walked there is
chasing loot it cannot see a path to.

## Step 3 — Rule engine

New file: `rules/active.json` (array), plus `server/ruleEngine.js`.

Rule shape:

```json
{
  "id": "r_pause_metronome",
  "when": [
    { "feat": "pause_count", "op": ">=", "v": 2 },
    { "feat": "pause_stddev_ticks", "op": "<", "v": 3 }
  ],
  "weight": 0.6,
  "rationale": "pauses of near-identical length — a timer, not a person",
  "author": "agent",
  "status": "shadow",
  "createdAt": "2026-09-10T...",
  "backtest": { "botCaught": 17, "botTotal": 20, "humanFlagged": 0, "humanTotal": 12 },
  "approvedBy": null
}
```

- `op` ∈ `>=`, `>`, `<=`, `<`. All conditions in `when` must hold (AND).
- `status` ∈ `active` | `shadow` | `rejected`.
- **`shadow` rules are evaluated and logged but contribute 0 to the score.**
  This is the human-in-the-loop gate and it is the whole point of the layer.

Scoring:

```js
score = min(1, sum(r.weight for active rules whose conditions all hold))
```

Also record which rules fired, into `features._fired = [ruleId, ...]`, so the
dashboard can show *why* a player scored what they scored.

`classify()` becomes: `extractFeatures()` → `evaluate()` → `{score, features}`.

Hot-reload `rules/active.json` on change (`fs.watch`, debounced, with a
try/catch that keeps the previous ruleset if the file is malformed — a bad JSON
write must never take the server down).

### Rules v1 — write these by hand

```
r_straight_line   path_straightness >= 0.92                       weight 0.45
r_fast_reaction   reaction_delay <= 25                            weight 0.35
r_precise_approach approach_error <= 6 AND dwell_ticks >= 2        weight 0.30
```

**Deliberately no pause rule.** That is the gap the patient bot walks through
and the rule the agent gets to discover. Do not add it here.

## Step 4 — Evasion monitor

In `server/index.js`, every 100 ticks, per player:

```
hits   = trap events in the last 60 s
mean   = mean score of those events
alarm  = hits >= 4 AND mean < 0.4
```

Traps firing is ground truth that something inhuman is happening. A low score
means the current rules cannot see it. The gap between the two is the alarm.

On alarm, emit `{type: 'evasion_suspected', playerId, hits, meanScore, episodes}`
on the admin channel and hand it to the agent. Rate-limit to one invocation per
2 minutes globally — do not fire the agent once per tick.

## Step 5 — The agent

New file: `server/agent.js`.

**Input to the model** — a feature *diff table*, never raw traces:

- `FEATURE_SCHEMA` (names, ranges, one-line descriptions)
- the current `active` rules
- mean and stddev of every feature for the **suspect cohort** (the alarming
  players' episodes)
- mean and stddev of every feature for a **known-human cohort** (episodes from
  `traces/` labelled `kind: 'human'`)

Roughly 600 tokens in. Ask for **1–3 candidate rules, JSON array only, no
prose, no markdown fences**. Strip fences defensively before parsing anyway.

Model: `claude-sonnet-4-6`.

**Validation, before anything else touches it:**

1. Parses as JSON and is an array.
2. Every `feat` exists in `FEATURE_SCHEMA`; every `op` is legal; every `v` is
   finite and within that feature's declared range (clamp, don't reject).
3. `weight` clamped to 0.1–0.7.
4. `id` is unique against existing rules; regenerate if it collides.
5. Malformed → retry once → give up silently and log. Never crash, never
   surface a broken rule to the human.

**Backtest, before the human sees it.** Load every episode in `traces/` (they
carry a `kind` label from `bots/trace-schema.js`). For each candidate rule,
count how many `bot` episodes it would catch and how many `human` episodes it
would falsely flag. Write those numbers into the rule's `backtest` field. A rule
with `humanFlagged / humanTotal > 0.1` should be marked as such in the UI —
still show it, let the human reject it.

Then append the candidates to `rules/active.json` with `status: "shadow"`.

**Have a fallback ready.** `--replay` flag that loads a pre-recorded proposal
from `rules/proposal-sample.json` instead of calling the API. Build it now, not
when the API is slow during the demo.

## Step 6 — Approval endpoint and UI

In `server/index.js`, keyed by `TELEMETRY_KEY` exactly like the admin feed:

```
GET  /rules            → the full ruleset
POST /rules/approve    { id }           → status: 'active',  approvedBy: <name>
POST /rules/reject     { id, reason }   → status: 'rejected'
```

On the admin page, a panel listing shadow rules with: rationale, the conditions
in plain text, backtest numbers, and Approve / Reject buttons. Approve writes
the file; the hot-reload picks it up on the next tick.

**Never** expose these without the key, and never let the server auto-approve.
The claim being made on stage is that no player is flagged by a rule a human did
not read and approve. That claim has to be literally true in the code.

## Step 7 — The patient bot

In `client/autoplay.js`, add a `patient` mode to `NaiveBrain`: the evasive
filter (`SPRITES.has(e.sprite)`), plus a pause of 110–130 ticks at semi-regular
intervals. Tight variance is the point — that is what `pause_stddev_ticks`
catches.

Register it in `FARM_FILTERS` if it fits the existing shape, or handle it beside
`HumanLikeBrain` if it needs its own class. Per `CLAUDE.md`, the live no-reconnect
toggle only has to work for naive ↔ evasive; `patient` may be start-mode only.

It must **hit `unreachable_bait`** (it still walks toward vault loot) and
**score under 0.4 on rules v1**. Verify both before building anything else on
top of it.

---

## Test procedure

Run in this order. Do not proceed past a failing step.

```bash
npm start
node bots/naive-bot.js --count 5                    # 1
node bots/naive-bot.js --count 5 --mode evasive     # 2
node bots/naive-bot.js --count 5 --mode patient     # 3
```

1. **Naive flags.** Dashboard shows non-zero scores and `flagged: YES` within
   ~30 s. If this fails, nothing downstream is testable — fix it first.
2. **Evasive does not flag** on `ghost_loot`, but *does* accumulate
   `unreachable_bait` hits and flags on those. If it never flags, step 2 of the
   build is not working.
3. **Patient hits traps but does not flag.** Trap hit count climbing, score
   staying under 0.4. If it flags, rules v1 are too broad — narrow them.
4. **Alarm fires** within ~60 s of the patient farm starting. Check the console
   and the admin channel.
5. **Agent proposes** a valid shadow rule with backtest numbers attached. Verify
   the numbers by hand against `traces/` once — do not trust them blind.
6. **Approve → patient flags** within the next few trap hits.
7. **Restart the server.** Approved rules must persist (they are in the JSON
   file). Shadow rules that were never approved must still be shadow.

Also confirm: a deliberately malformed `rules/active.json` does not crash the
server, and a `null` API response does not either.

---

## Scope discipline

Cut without discussion: `position_offset`, `phantom_player`, the category
bandit, any database, the AI trap-*maker* from doc 02. Rules live in a JSON
file, state lives in memory.

The model does not detect anything. It compares two distributions and proposes a
threshold. What is defensible here is the pipeline around it: schema validation,
backtest against labelled data, shadow status, human approval, audit trail. Do
not let the code drift into claiming more than that.
