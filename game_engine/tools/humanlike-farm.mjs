// tools/humanlike-farm.mjs — the labelled control group.
//
//   node tools/humanlike-farm.mjs --count 3 --host localhost:8080
//
// Connects the way a browser does — no `?kind=bot`, so the server labels these
// players `human` — and drives HumanLikeBrain, the same brain the game page
// runs under ?auto=human. Every trap they trip is written to traces/ as a
// human-labelled episode by server/episodes.js.
//
// WHY THIS EXISTS: a backtest that says "flags 0 of 0 humans" asserts nothing.
// Something has to fill the denominator before real people are available.
//
// WHY YOU SHOULD NOT TRUST IT: it is a stand-in, and a measurably wrong one.
// Recording real players once showed the gap plainly — a person holding a
// movement key produces near-constant speed and near-straight lines, while
// this brain wobbles and pauses. Tuned against this alone, a ruleset looks
// clean and then flags the first real player who walks a corridor. Use it to
// keep the pipeline honest between sessions; replace it with recorded people
// before believing any false-positive rate.

import { WebSocket } from 'ws';
import * as C from '../constants.js';
import { HumanLikeBrain } from '../client/autoplay.js';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const HOST = arg('host', `localhost:${C.PORT}`);
const COUNT = Math.max(1, Number(arg('count', 3)) || 3);

function run(n) {
  const ws = new WebSocket(`ws://${HOST}${C.PATHS.GAME}`); // no kind → the server says human
  const brain = new HumanLikeBrain();
  let sent = { dx: 0, dy: 0 };
  let sinceSend = 0;
  let identified = false;

  ws.on('open', () => console.log(`[humanlike ${n}] connected`));
  ws.on('close', () => setTimeout(() => run(n), 2000));
  ws.on('error', () => {}); // 'close' follows and handles the retry

  ws.on('message', (data) => {
    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }
    if (msg.type !== 'state') return;
    if (!identified) {
      console.log(`[humanlike ${n}] playerId=${msg.you.id}, label=human`);
      identified = true;
    }
    const { dx, dy } = brain.decide(msg);
    if (dx !== sent.dx || dy !== sent.dy || ++sinceSend >= C.INPUT_KEEPALIVE_TICKS) {
      ws.send(JSON.stringify({ type: 'input', dx, dy }));
      sent = { dx, dy };
      sinceSend = 0;
    }
  });
}

console.log(`Launching ${COUNT} human-labelled stand-in(s) against ws://${HOST}${C.PATHS.GAME}`);
for (let i = 0; i < COUNT; i++) setTimeout(() => run(i + 1), i * 120);
