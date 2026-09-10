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
npm install
npm start
```

| Open | What it is |
|---|---|
| http://localhost:8080/ | the game (WASD / arrow keys) |
| http://localhost:8080/split/?key=demo | **demo view** — player view next to admin view |
| http://localhost:8080/admin/?key=demo | admin view: server truth, traps visible |
| http://localhost:8080/dashboard/?key=demo | detection telemetry |

Launch a bot farm in a second terminal:

```bash
node bots/naive-bot.js --count 10
```

Run the protocol conformance check against a running server:

```bash
npm run smoke
```

Env vars: `PORT` (default 8080), `TELEMETRY_KEY` (default `demo`).

## How it works

1. **Authoritative server** (Node + `ws`, 20 Hz). The server decides everything and sends each client full state every tick.
2. **Traps** (`server/traps.js`). Each player gets their own traps. The first category is *ghost loot*: a coin or chest whose sprite name is missing from the client's asset table, placed in odd spots such as map corners, map edges, and against walls. It fires when a player stays within 24 units of it for 2 consecutive ticks.
3. **The asymmetry** (`client/sprites.js`). The client draws only sprite names in its asset table and silently skips everything else. A headless bot has no asset table, so it can't filter traps without reverse-engineering the client's art.
4. **Detection** (`server/classifier.js`). Every trap hit is scored from the last 10 s of movement. A player is flagged after 3+ high-score hits from 2+ trap categories. Flagged players are reviewed by a human, not banned.

## Repository layout

```
constants.js          shared constants — server, client, bots and dashboard import it
server/
  index.js            HTTP + WebSocket server, tick loop, telemetry & admin feeds
  world.js            world state, movement, collision, pickups
  traps.js            trap placement, lifecycle and firing
  classifier.js       movement-trace scoring
client/               the game page and its asset table (sprites.js)
admin/                admin view (keyed)
split/                side-by-side demo page
dashboard/            detection telemetry UI
bots/                 naive farming bot and farm launcher
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
