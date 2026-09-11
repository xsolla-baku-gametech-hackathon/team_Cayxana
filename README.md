# Team Cayxana — Trap-Based Bot Detection

Built for the **Xsolla Baku GameTech Hackathon** (Sept 9–11, 2026).

Farming bots read game state straight off the network. Humans only see what the client draws.
We exploit that gap: the server plants **trap loot** that looks completely ordinary on the wire
but that the client never renders. A human has no reason to walk to an empty corner and wait there.
A bot does. Every such visit becomes evidence, which a heuristic classifier scores from the
player's movement trace. Suspicious accounts go to a review queue — **we never ban automatically.**

## Run it

Requires **Node 20+** and **Python 3.10+**. Install the one Node dependency once:

```bash
cd game_engine && npm install && cd ..
```

Then start the whole stack — game server, Python detection brain, and the bot farm — with one
command:

```bash
python run_game.py --bots N     # N is number of bots
```

`run_game.py` owns every process and Ctrl+C stops all of them together. Leave `--bots` off (or at
`0`) while collecting human data. On startup it prints a LAN address so other laptops on the same
Wi-Fi can join.

| Open | What it is |
|---|---|
| http://localhost:8080/ | the game (WASD / arrow keys) |
| http://localhost:8080/?collect=insan | same game, but this player is **labelled human** for training data |
| http://localhost:8080/split/?key=demo | **demo view** — player view next to admin view |
| http://localhost:8080/split/?key=demo&auto=bot | demo view, left pane driven by the naive bot autopilot |
| http://localhost:8080/split/?key=demo&auto=human | demo view, left pane driven by the human-like autopilot |
| http://localhost:8080/admin/?key=demo | admin view: server truth, traps visible |
| http://localhost:8080/dashboard/?key=demo | detection telemetry |
| http://localhost:8080/rules?key=demo | the rule set the classifier is currently running |

### Flags

| Flag | What it does |
|---|---|
| `--bots N` | launch an N-bot naive farm alongside the game (default 0) |
| `--port 8080` | server port |
| `--no-ai` | turn off both AI layers. **The AI is on by default**; without LM Studio running the game still works and simply skips every AI round |
| `--ai-dry-run` | the AI reports what it would change, and changes nothing |
| `--model NAME` | LM Studio model for both AI layers (default `qwen/qwen3-vl-8b`) |
| `--collect-code WORD` | the secret in the human-data link (default `insan`) |
| `--collect-minutes N` | how long the human-data link asks people to play (default 10) |
| `--required-categories 1..3` | how many distinct trap categories the Python brain needs before it flags |

### Bot farms

`--bots N` starts a **naive** farm. For the other modes, run the farm yourself in a second terminal:

```bash
cd game_engine
node bots/naive-bot.js --count 10                  # naive farm
node bots/naive-bot.js --count 10 --mode evasive   # farm that filters by the asset table
node bots/naive-bot.js --count 10 --mode human     # headless human-imitating farm
```

Switch a running naive/evasive farm without reconnecting — this is the live "they shipped a patch"
demo: `POST http://localhost:8090/mode` with `{"mode":"evasive"}` (status: `GET /status`, port set
by `--control-port`). Human-imitating mode is fixed for the process's lifetime and cannot be
toggled.

### Checks

```bash
cd game_engine && npm run smoke             # wire-protocol conformance, against a running server
python -m unittest discover -s tests -t .   # the detection brain's test suite
python main.py                              # scores every labelled population and prints the tables
```

Env vars: `PORT` (default 8080), `TELEMETRY_KEY` (default `demo`), `COLLECT_CODE`, and
`LOCAL_MODEL` / `LMSTUDIO_URL` for the AI layers.

## How it works

1. **Authoritative server** (Node + `ws`, 20 Hz). The server decides everything and sends each client full state every tick.
2. **Traps** (`server/traps.js`). Each player gets their own traps. The category is *ghost loot*: a coin or chest whose sprite name is missing from the client's asset table, placed in odd spots such as map corners, map edges, and against walls. It fires when a player stays within 24 units of it for 2 consecutive ticks. **Chain reaction:** every hit spawns 2 more traps around the spot (never in the cone ahead of the player), capped at 6 extra per player. A bot keeps feeding the chain; a human who hits one by accident walks on and the extras expire.
3. **The asymmetry** (`client/sprites.js`). The client draws only sprite names in its asset table and silently skips everything else. A headless bot has no asset table, so it can't filter traps without reverse-engineering the client's art.
4. **In-engine detection** (`server/classifier.js`). Every trap hit is scored from the last 10 s of movement. A hit counts when it scores ≥ 0.75, and corroboration takes one of two shapes: hits from **2+ different trap categories**, or **4+ hits in the same category**. One unlucky walk past a trap can never flag anyone. Flagged players are reviewed by a human, not banned.
5. **The detection brain** (`detection/`, Python). The engine appends every join, move, trap trip and leave to `game_engine/traces/session-*.jsonl`; `bridge/` tails that file live, drives the Python classifier with it, and writes verdicts back to `traces/verdicts.jsonl` for the dashboard. The brain scores five movement features, looks for machine *signatures* no human produces, and separately tracks trap-trip rate and looping. Every feature may refuse to measure: "unknown" is never silently read as bot-like. The scoring path is pure arithmetic — no model call ever runs between a trap trip and a verdict.
6. **The adaptive layer** (`detection/adaptive.py`, optional). Asleep by default; no timer wakes it. An anomaly report or a disagreement with the labels wakes it, and it asks a **local** model — Qwen3 8B (`qwen/qwen3-vl-8b`) served by [LM Studio](https://lmstudio.ai) — to move a threshold or propose a new measurement. The model never writes code, never sees a player's movement, and cannot apply its own answer: every proposal is replayed against labelled human traces first, and anything that makes one labelled human newly suspicious is refused. With no model reachable the layer is inert and the classifier behaves exactly as before.

The map has one sealed room, the **vault**. Everyone can see into it, but nobody can reach it. The
server flood-fills reachability at startup, never spawns real loot there, and exposes
`isReachable()` so a trap is only ever placed somewhere a player could actually walk to.

## Repository layout

```
run_game.py           starts the whole stack: game server + detection brain + bot farm
main.py               offline report — scores every labelled population, prints the tables
docs/                 role documents, one per workstream

game_engine/          the game: authoritative Node server, browser client, bot farm
  constants.js          world size, tick rate, radii, keys — imported by server, client, bots and dashboard
  server/
    index.js            HTTP + WebSocket server, 20 Hz tick loop, telemetry & admin feeds
    world.js            world state, movement, collision, pickups, startup reachability flood-fill
    traps.js            trap placement, lifecycle (spawn → TTL → respawn), firing and chain reaction
    classifier.js       in-engine movement scoring and the flag rule
    features.js         trace + trap event → a flat record of finite numbers; coordinates stop here
    ruleEngine.js       the only thing that reads rules/: score = summed weights of the active rules that hold
    agent.js            the JS rule agent: hands the model two cohorts' feature stats, asks which threshold separates them
    episodes.js         the labelled dataset: one episode = one trap trip, its trace, and the demo-only kind label
    ringbuffer.js       fixed-capacity ring buffer — the per-player position history the classifier reads back
    sessionlog.js       appends every join/move/trap/leave to traces/session-*.jsonl
    humandata.js        the ?collect= link — the only thing that makes a player a labelled human
    pyverdicts.js       tails traces/verdicts.jsonl for the dashboard; shown, never enforced
  client/               the game page, asset table (sprites.js), autopilots (autoplay.js)
  admin/                admin view (keyed) — server truth, traps drawn
  split/                side-by-side demo page: player view next to admin view
  dashboard/            detection telemetry UI
  bots/                 bot farm (naive / evasive / human), trace emitter and offline generator
  rules/                active.json — the live rule set (data, not code), plus a sample proposal
  tools/smoke-test.js   wire-protocol conformance check
  traces/               session logs, verdicts, and collected human data (generated at runtime)

bridge/               transport: tails the engine's trace stream and drives the Python brain
  reader.py             follows session-*.jsonl while it is being written
  __main__.py           record → detector → verdict, written back as JSONL

detection/            the detection brain — pure arithmetic, no model in the hot path
  features.py           five movement features; each may refuse to measure
  scoring.py            features → a 0..1 machine-likeness score
  signatures.py         narrow "no human body does this" detectors; any one is conclusive
  suspicion.py          accumulates trap events into a flag across categories
  rules.py              per-category observation window, threshold and excuse
  settings.py           every tunable number in one place, with hard bounds
  clock.py              the 20 Hz tick domain and its conversions
  jsonio.py             decodes and validates whatever the model answers
  traprate.py           how often a player walks into traps, regardless of how they move
  looping.py            farm movement that has fallen into a cycle
  longterm.py           continuous post-trap monitoring over minutes
  anomaly.py            population-level review list — the only thing that wakes the AI
  bandit.py             Thompson sampling over trap categories
  burn.py               detects that a trap category has been defeated
  hypothesis.py         asks the model for a new measurement; replay-gated
  tuning.py             asks the model to move weights and thresholds; replay-gated
  invention.py          model-invented trap categories, shadow-run before they count
  llm.py                the local LM Studio client; loads on demand, unloads after
  adaptive.py           the AI layer — decides when a round is warranted, and applies nothing ungated

tests/                the brain's test suite, incl. synthetic labelled human and bot populations
```

## Security properties

- Entities on the wire carry only `id, type, x, y, sprite, color, value`. The server strips every other field, so trap metadata can't leak.
- Entity order is shuffled every tick, and ids are random. Traps can't be spotted by their position in the list or their id format.
- The admin and telemetry feeds require a key. Server code is never served over HTTP.
- The server never acts on a score. It records the score and forwards it for review.
- The AI is local-only. Aggregate statistics never leave the machine, and the model proposes numbers but can never apply them — replay against labelled humans decides.

## Team

| Member | Role |
|---|---|
| _name_ | Game server & client |
| _name_ | Traps |
| _name_ | Detection |
| _name_ | Bots |
| _name_ | Dashboard & pitch |

See [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).
