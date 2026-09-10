# Team Cayxana — Trap-Based Bot Detection

Built for the **Xsolla Baku GameTech Hackathon** (Sept 9–11, 2026).

Farming bots read game state straight off the network. Humans only see what the client draws.
We exploit that gap: the server plants **trap loot** that looks completely ordinary on the wire
but that the client never renders. A human has no reason to walk to an empty corner and wait there.
A bot does. Every such visit becomes evidence, which a heuristic classifier scores from the
player's movement trace. Suspicious accounts go to a review queue — **we never ban automatically.**

## Run it

Requires Node 20+.

```bash
cd game_engine
npm install
npm start
```

| Open | What it is |
|---|---|
| http://localhost:8080/ | the game (WASD / arrow keys) |
| http://localhost:8080/split/?key=demo | **demo view** — player view next to admin view |
| http://localhost:8080/split/?key=demo&auto=bot | demo view, left pane driven by the naive bot autopilot |
| http://localhost:8080/split/?key=demo&auto=human | demo view, left pane driven by the human-like autopilot |
| http://localhost:8080/admin/?key=demo | admin view: server truth, traps visible |
| http://localhost:8080/dashboard/?key=demo | detection telemetry |

Launch a bot farm in a second terminal:

```bash
cd game_engine
node bots/naive-bot.js --count 10                  # naive farm
node bots/naive-bot.js --count 10 --mode evasive   # farm that filters by the asset table
```

Switch a running farm between naive and evasive without reconnecting:
`POST http://localhost:8090/mode` with `{"mode":"evasive"}` (status: `GET /status`).

Run the protocol conformance check against a running server:

```bash
npm run smoke
```

Env vars: `PORT` (default 8080), `TELEMETRY_KEY` (default `demo`).

## How it works

1. **Authoritative server** (Node + `ws`, 20 Hz). The server decides everything and sends each client full state every tick.
2. **Traps** (`server/traps.js`). Each player gets their own traps. The first category is *ghost loot*: a coin or chest whose sprite name is missing from the client's asset table, placed in odd spots such as map corners, map edges, and against walls. It fires when a player stays within 24 units of it for 2 consecutive ticks. **Chain reaction:** every hit spawns 2 more traps around the spot (never in the cone ahead of the player), capped at 6 extra per player. A bot keeps feeding the chain; a human who hits one by accident walks on and the extras expire.
3. **The asymmetry** (`client/sprites.js`). The client draws only sprite names in its asset table and silently skips everything else. A headless bot has no asset table, so it can't filter traps without reverse-engineering the client's art.
4. **Detection** (`server/classifier.js`). Every trap hit is scored from the last 10 s of movement. A player is flagged after 3+ high-score hits from 2+ trap categories. Flagged players are reviewed by a human, not banned.

The map has one sealed room, the **vault**. Everyone can see into it, but nobody can reach it. The server flood-fills reachability at startup, never spawns real loot there, and exposes `isReachable()` for the `unreachable_bait` trap category.

## Repository layout

```
docs/                 role documents
game_engine/
  constants.js          shared constants — server, client, bots and dashboard import it
  server/
    index.js            HTTP + WebSocket server, tick loop, telemetry & admin feeds
    world.js            world state, movement, collision, pickups
    traps.js            trap placement, lifecycle and firing
    classifier.js       movement-trace scoring
  client/               the game page, asset table (sprites.js), autopilots (autoplay.js)
  admin/                admin view (keyed)
  split/                side-by-side demo page
  dashboard/            detection telemetry UI
  bots/                 bot farm (naive / evasive / human), trace emitter and generator
  tools/smoke-test.js   wire-protocol conformance check
```

## Security properties

- Entities on the wire carry only `id, type, x, y, sprite, color, value`. The server strips every other field, so trap metadata can't leak.
- Entity order is shuffled every tick, and ids are random. Traps can't be spotted by their position in the list or their id format.
- The admin and telemetry feeds require a key. Server code is never served over HTTP.
- The server never acts on a score. It records the score and forwards it for review.

## Team

| Member | Role |
|---|---|
| _name_ | Game server & client |
| _name_ | Traps |
| _name_ | Detection |
| _name_ | Bots |
| _name_ | Dashboard & pitch |

See [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).
