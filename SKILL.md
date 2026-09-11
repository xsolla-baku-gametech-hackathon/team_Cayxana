---
name: trap-detection-brain
description: Build the detection brain for a trap-based game bot detection system — behavioural classifier, Thompson-sampling bandit, burn detection, and an optional LLM layer that invents new trap categories. Use when implementing or extending the classifier/bandit/burn-detection modules of the server-side bot detection project.
---

# Trap Detection Brain

## What you are building

Two independent brains for a bot-detection system:

1. **The classifier** — measures HOW a player moved and scores human vs machine. Pure arithmetic. Runs on every trap event. **Mandatory.**
2. **The AI category inventor** — when cheat developers defeat a whole trap category, an LLM proposes a new one. Runs offline, every few minutes. **Optional, cut first.**

Between them sits the **bandit + burn detection**, which decides which trap category to use and detects when one has been defeated.

## Non-negotiable rules

Read these before writing any code. Violating them invalidates the whole system.

- **NO AI/ML in the detection path.** The classifier is five arithmetic features and a weighted sum. An LLM call takes 1–3 seconds and costs money; the classifier must run in microseconds and be deterministic. Do not "improve" it with a model.
- **A single trap NEVER produces a ban.** One trap raises suspicion. A flag requires agreement across ≥3 traps from **different categories**.
- **Optimise for zero false positives, not maximum catches.** Success metric is "how many humans did we wrongly flag" — target zero. Catching fewer bots is acceptable; flagging one human is not.
- **Never let unvalidated LLM output reach the game.** Validate → clamp → shadow-mode → only then live.
- **Network lag is not bot behaviour.** Lag produces late arrivals. Bots produce mechanical movement. Never flag on timing alone.

---

## STEP 1 — Feature extractor

Do this first and nothing else. Do not start the bandit, do not touch the LLM.

### Input contract

```js
trace = [
  { tick: 100, x: 50, y: 20 },
  { tick: 101, x: 52, y: 21 },
  { tick: 102, x: 52, y: 21 },   // stationary
  // ... ~10 seconds at 20 ticks/sec = ~200 samples
]
trapEvent = { tick: 98, x: 340, y: 120 }
```

### Implement `extractFeatures(trace, trapEvent) -> { straightness, pauseVariance, reactionTicks, revisits }`

**1. Path straightness**
```
straightness = euclidean(first, last) / sum(euclidean(p[i], p[i+1]))
```
Bot ≈ 0.95. Human ≈ 0.5. Guard against division by zero when the player never moved.

**2. Pause-duration variance**
- A "pause" = consecutive ticks where distance moved < epsilon (use 0.5 units)
- Collect the LENGTH of every pause into an array
- Return the variance of that array
- Human: `[1.2, 4.7, 0.8, 3.1]` → high variance. Bot: `[0.5, 0.5, 0.5]` → ~0
- **This is the strongest single feature.** Get it right.
- If there are fewer than 2 pauses, return `null` — not 0. A null feature must be excluded from scoring, not treated as bot-like.

**3. Reaction delay**
```
reactionTicks = (first tick where the player starts moving toward trapEvent) - trapEvent.tick
```
"Moving toward" = the distance to the trap decreases for 3 consecutive ticks. Bot: 1–2 ticks. Human: 15–40.

**4. Revisit count**
- Bucket positions into a grid (cell size ≈ 10 units)
- Count how many times the player enters a cell they have already visited
- Human: 8–15. Bot: 0–1.

### TEST THIS STEP HARD

Write `test/features.test.js` before moving on:

- **Perfect straight line** → straightness ≈ 1.0
- **A square path returning to start** → straightness ≈ 0 (handle it, don't crash)
- **Never moved at all** → no NaN, no Infinity, no crash
- **All pauses identical** → pauseVariance ≈ 0
- **Only 1 pause** → returns null, not 0
- **Trap never approached** → reactionTicks is null, not 0
- **Trace with 2 samples** → does not crash
- **Empty trace** → does not crash

Every one of these edge cases WILL occur in the live demo. A NaN reaching the score function silently produces garbage scores that look plausible.

**Do not proceed until all edge tests pass.**

---

## STEP 2 — Scoring

### Implement `classify(trace, trapEvent) -> { score, features }`

```js
score = 0.3 * straightness
      + 0.3 * (1 - norm(pauseVariance))
      + 0.2 * (1 - norm(reactionTicks))
      + 0.2 * (1 - norm(revisits))
// 0..1, closer to 1 = more machine-like
```

Normalisation: clamp each raw feature into a sensible range, then map to 0..1. Example: `norm(reactionTicks)` clamps to `[0, 40]` then divides by 40.

**Handling null features:** if a feature is null, drop its term and renormalise the remaining weights so they still sum to 1. Never substitute 0 — that silently reads as "bot-like".

**But check what "null" is hiding.** `pause_variance` needs two pauses to have a variance, so a bot that never stops produced *no* pause rhythm — and the strongest fact about it was being filed as "unknown", which cost it 0.40 of weight and left it permanently unscoreable. The fix is not to weaken the null rule; it is a second measurement of the same behaviour that cannot go null: `idle_share`, the share of the window spent standing still. Zero pauses is a measurement. Whenever a feature is null for exactly the population you are trying to catch, that is the shape of the bug.

### How to pick weights

Do not guess. Do not optimise.

1. Take the labelled trace files (human / bot)
2. Print all five raw features for every trace, grouped by label
3. Look at where the two populations separate
4. Adjust weights so separation is clean
5. Print a confusion table

### TEST THIS STEP HARD

- Run on all labelled traces. **Report false-positive rate explicitly.** It must be 0.
- **Lagged human trace** (irregular tick gaps, duplicated positions, jitter) → must NOT be flagged. Build this test fixture deliberately; it is the single most important test in the project.
- Feed a trace with every feature null → score must be a defined number or an explicit "insufficient data" result, never NaN.

---

## STEP 3 — Suspicion accumulation

Only now connect scoring to flagging.

### Implement a per-player suspicion store

```js
{
  playerId,
  events: [ { category, score, tick } ],
  flagged: boolean
}
```

### Flag rule

```
flag IF count(events where score > THRESHOLD) >= 3
   AND those events span >= 3 DISTINCT categories
```

The distinct-category requirement is not optional. A player who trips three invisible-entity traps may just be unlucky or lagging. Three different mechanisms agreeing is much harder to explain innocently.

### TEST

- 3 high scores, all same category → NOT flagged
- 3 high scores, 3 different categories → flagged
- 2 high + 1 low → NOT flagged
- Lagged human across a full session → NOT flagged

---

## STEP 4 — Bandit

### Implement Thompson sampling over trap categories

```js
stats = {
  invisible_entity:  { caught: 0,  missed: 47 },
  position_offset:   { caught: 14, missed: 12 },
  phantom_player:    { caught: 9,  missed: 20 },
  unreachable_bait:  { caught: 6,  missed: 18 }
}

function pickCategory(stats) {
  let best = null, bestSample = -1;
  for (const [name, s] of Object.entries(stats)) {
    const sample = betaSample(s.caught + 1, s.missed + 1);
    if (sample > bestSample) { bestSample = sample; best = name; }
  }
  return best;
}
```

Implement `betaSample(a, b)` via two gamma draws, or use a simple approximation. Roughly 20 lines. If this is taking more than 30 minutes, substitute a weighted moving average — the demo will not show the difference.

### Reward shaping

- Bot caught: `caught += 1`
- Trap triggered, nothing caught: `missed += 1`
- **Human wrongly flagged: `missed += 10`** — the penalty must be asymmetric and heavy

### TEST

- One category with 20 catches and one with 0 → the strong one is picked far more often, but **the weak one is still picked sometimes**. If a category reaches probability zero, exploration is broken and the system can never notice that a category started working again.
- All categories at zero data → roughly uniform selection

---

## STEP 5 — Burn detection

### The trigger that eventually calls the LLM

```
for each category:
  currentRate = caught / triggered   (last 1 hour)
  baseline    = rolling average of previous rates

  IF currentRate < 0.2 * baseline
  AND the drop appears across MANY DISTINCT SESSIONS simultaneously
  THEN category is BURNED → weight to ~0
```

### The critical part

**The "many distinct sessions simultaneously" condition is mandatory.** A drop within one player's session is noise — that player might just not be a bot. A simultaneous drop across hundreds of sessions is a cheat developer shipping a patch.

Without this condition the system panics at its own noise and disables working categories. This is the most common way this component fails.

Require a minimum sample size (e.g. at least 50 triggers in the window) before burn detection is allowed to fire at all.

### TEST

- Simulate: one category's catch rate drops in a single session → must NOT burn
- Simulate: same drop across 100 sessions at once → MUST burn
- Simulate: low traffic, 3 triggers, 0 catches → must NOT burn (below minimum sample)
- After burning, the bandit must reallocate to other categories within a few selections

---

## STEP 6 — AI category invention (OPTIONAL — cut this first)

Do not start this step unless steps 1–5 are complete and tested.

### Write the validator BEFORE the prompt

```js
validateCategory(raw) -> { ok, category } | { ok: false, reason }
```

- Strip markdown fences, parse JSON, reject on parse failure
- Reject unknown or missing schema fields
- Clamp all numbers to map bounds and sane ranges
- Reject any proposal that duplicates an existing category
- If the entire batch fails → **silently fall back to the hardcoded pool.** No error to the user, no crash.

### The prompt

```
Game entity fields:
  id, x, y, type, sprite, health, owner, spawnTick

Working categories:
  position_offset   (catch rate: 14/hr)
  unreachable_bait  (catch rate: 9/hr)

BURNED category:
  invisible_entity  (catch rate: 0/hr)

Rule: a trap is any difference between the data the server
sends and the picture the client draws. Humans see the
picture; bots read the data.

Propose a new category. JSON only, no prose.
```

Expect roughly 1 usable proposal in 3. The rest will be duplicates or nonsense. This is normal — do not add retry loops that burn tokens chasing a perfect answer.

### Shadow mode is mandatory

A newly invented category **must not flag anyone** at first. Run it live, record its catch rate, and only after it has demonstrated a healthy catch rate with zero human flags may it be promoted to flagging.

### TEST

- Malformed JSON input → falls back cleanly, no crash
- Out-of-bounds coordinates → clamped, not rejected outright
- Duplicate of an existing category → rejected
- LLM endpoint unreachable → hardcoded pool used, system keeps running

---

## Critical points summary

| Risk | Guard |
|---|---|
| NaN from degenerate traces | Edge tests in Step 1, null handling in Step 2 |
| Lagged human flagged as bot | Dedicated lag fixture, tested at Steps 2 and 3 |
| Single trap causing a ban | Distinct-category requirement in Step 3 |
| Bandit collapsing to one arm | Exploration test in Step 4 |
| Burn detection firing on noise | Multi-session + minimum-sample conditions in Step 5 |
| LLM output corrupting the world | Validator written before prompt, shadow mode, Step 6 |

## Order of work

Strictly sequential. Do not parallelise these steps or start a later one "while waiting".

```
1 Features  →  2 Scoring  →  3 Suspicion  →  4 Bandit  →  5 Burn  →  6 AI
   ^^^ tests pass before advancing at every arrow ^^^
```

Steps 1–5 constitute a complete, shippable system. Step 6 is an enhancement. If time runs out, stopping after Step 5 loses nothing that matters.
