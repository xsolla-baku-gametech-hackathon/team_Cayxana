# CLAUDE.md — P4: Bots and the Attack

## Read first

Before writing code, read `docs/` (the five role documents). This file is the
working summary plus decisions made outside those documents. Where this file and
the documents disagree, this file is newer — but say so rather than silently
picking one.

**Plan the architecture before writing code.** For any non-trivial piece, lay out
the components and interfaces first, get agreement, then implement.

## What this project is

A five-person anticheat demo. A minimal top-down 2D webapp game with an
authoritative server plants traps that are present in the game data but invisible
or misleading on screen. A human never notices them. A bot reading the data walks
straight into them. That divergence is the detection signal.

I own **P4: the adversary.** I build the bots that get caught, and the evasive bot
that patches around one trap category to show the system adapting live on stage.

Other roles, for context only — I do not implement these:

- **P1** — game server (Node + `ws`) and canvas client
- **P2** — the four trap categories and randomised placement
- **P3** — the classifier that scores traces
- **P5** — dashboard

## The game, as it affects the bot

- Authoritative server, tick loop at **20 Hz**
- **Full state every tick, never deltas**
- Entity coordinates arrive in the state message over the WebSocket
- Client render loop contains `if (entity.sprite === null) continue;`
- Traps are injected server-side via `injectTraps(entities, playerId)` before
  state is sent to each client — the client never sees trap metadata
- A second WebSocket path carries telemetry for the dashboard

**There is no memory reading in this project.** No heap snapshots, no WASM
scanning, no CDP, no browser automation. The bot is a WebSocket client that
parses state messages. Anything else violates the "no privileges a real cheat
would not have" rule and invalidates the demo.

## The four trap categories (from doc 02, verbatim semantics)

| Category | Mechanism |
|---|---|
| `invisible_entity` | `sprite: null`. Exists in the data, never drawn. A human can never see it. |
| `position_offset` | Sprite is normal, but `realX`/`realY` differ from `x`/`y` by several units. A human walks to what is drawn; a bot walks to the stored coordinates. |
| `phantom_player` | A player entry with `sprite: null`. A bot reacts to someone who does not exist. |
| `unreachable_bait` | High-tier loot placed where no walkable path leads. |

Traps also carry a `ttl` (40–120 ticks) and a `tier` (low/medium/high).
Randomisation protects *instances*, not *categories* — one filter kills every
instance of a category at once. That is why there are four.

## OPEN DECISIONS — ask before assuming

Two conflicts between doc 04 and doc 02 are unresolved. Do not resolve them
silently; if code depends on one, ask.

**1. The sprite filter defeats two categories, not one.**
Doc 04 says the evasive bot's `sprite === null` filter defeats category A. But
`phantom_player` is also sprite-null, so one filter kills **A and C together**.
`position_offset` and `unreachable_bait` have normal sprites and still fire — so
the evasive bot does **not** survive cleanly.

- Option 1 — evasive survives everything. Matches doc 04's stage narrative, but
  requires suppressing B and D during that beat. Artificial.
- Option 2 *(recommended)* — evasive gets caught by B and D. Stronger claim, and
  it justifies why four categories exist. Stage line becomes "they patched
  category A, and B caught them ten seconds later."

Bot code is identical either way; only which traps are active differs.

**2. A nearest-entity bot never triggers `phantom_player`.**
Category C catches a bot that reacts to a player who does not exist. A bot that
only walks to the nearest *entity* ignores the player list entirely, so C has no
adversary demonstrating it.

Fix: give the bot one player-reactive behaviour. Contest-avoidance — skip a
target if another player is closer to it than the bot is. A phantom next to a
collectible then visibly deflects the bot, and that deflection is the detectable
event. ~10 lines, and it makes the bot more plausible as a real farm cheat.

## Expected trap coverage (assuming both fixes land)

| | `invisible_entity` | `position_offset` | `phantom_player` | `unreachable_bait` |
|---|---|---|---|---|
| Naive | caught | caught | caught | caught |
| Evasive | evades | caught | evades | caught |

This holds only if the bot reads `realX ?? x`. Build it that way deliberately.

## Hard constraints

- **Connect as an ordinary client.** No special server access, no back door, no
  reaching into the page. If the bot has privileges a real cheat would not have,
  the demo proves nothing.
- **Target extraction always uses `realX ?? x` / `realY ?? y`.** This is the
  bot's core tell and is not a mode setting — it is always on.
- **The mode toggle must flip the filter on live connections, without
  reconnecting.** If 40 bots drop and re-handshake on stage it looks like a
  restart rather than a patch, and the reconnect burst is itself a detection
  artifact that pollutes P3's signal.
- **Do not make the bot too good.** A convincingly human-like bot defeats our own
  detection and there is no demo. The human-imitating mode stays separate and
  optional.
- **Traces before bots.** P3 is idle until trace files exist. This is called out
  in doc 04 as the single most common way the project fails.

## Bot architecture

One codebase, one process, mode as configuration. Not three programs.

- **Client** — WebSocket connection, session identity, parse state message into a
  snapshot. Shared by all modes.
- **Target extraction** — coordinates as `realX ?? x`. Always on.
- **Filter predicate** — the *only* mode-dependent piece. Naive accepts
  everything; evasive rejects `sprite === null`. Swappable at runtime on a live
  connection.
- **Player reaction** — contest-avoidance against the player list, so category C
  has something to catch.
- **Movement** — nearest target, straight line, fixed tick interval. Stalls when
  blocked, which is what makes `unreachable_bait` legible.
- **Trace emitter** — always on, every mode.
- **Farm supervisor** — 20–50 client instances as async tasks in one process,
  each with its own identity. Not 50 browsers. Exposes `GET /status` and
  `POST /mode` for P5's dashboard.

## Build order

1. Trace schema module + generator script → 20 files to P3 **(blocking)**
2. WebSocket client + parser, verified against the running playground
3. Naive bot, single instance, trace output confirmed valid
4. Farm supervisor, scale to 20–50
5. Evasive predicate + control endpoint for the toggle
6. Human-imitating mode (jitter, uneven pauses) — only if time remains

Steps 1 and 2 are the only ones on anyone else's critical path.

## BLOCKER: document 00 is missing

Doc 00 defines the trace file format and the trap template. The generator writes
directly against the trace schema and P3's parser is built against whatever ships
first, so a mismatch there is expensive.

**Do not invent a trace format.** If doc 00 is not in `docs/`, ask.

Related: doc 01 has the server keeping its own ~200-position ring buffer per
player and calling `classify(trace, trapEvent)` on it. So P3 consumes server-side
buffers at runtime, while my trace files are the offline training format. Confirm
these are the same schema before building the generator.

## Notes for other roles (raise, do not implement)

- At 50 bots × 20 Hz, `injectTraps` runs ~1000×/second with 50 per-player trap
  records live. Fine at this scale, but it should be a lookup, not a rescan of
  the world each call. — for P1/P2
- The trap works because the server sends invisible entities to clients that
  should not see them. That is also exactly what makes the evasive bot's one-line
  filter work. A judge may ask why the server transmits bait at all. The answer
  is that a honeypot without transmission is not a honeypot — but that should be
  in someone's demo script deliberately, not improvised. — for P5
