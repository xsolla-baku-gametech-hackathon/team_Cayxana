// tools/backtest.mjs — check the numbers by hand.
//
//   node tools/backtest.mjs            every rule in rules/active.json
//   node tools/backtest.mjs --shadow   only the ones awaiting review
//
// The agent writes a `backtest` field into every rule it proposes. This prints
// the same numbers from the same labelled episodes, independently, plus what
// the ruleset as a whole would do — because a rule that looks harmless alone
// can still push a cohort over the flag threshold once it is summed with the
// rest.
//
// Verify a proposal against this at least once before trusting the field.

import * as engine from '../server/ruleEngine.js';
import { corpus } from '../server/agent.js';
import { SCORE_THRESHOLD } from '../constants.js';

engine.load();
const rows = corpus();
if (rows.length === 0) {
  console.log('No labelled episodes in traces/. Produce some first:');
  console.log('  node bots/generate-traces.js            synthetic, no server needed');
  console.log('  node bots/naive-bot.js --count 5        real, against a running server');
  process.exit(0);
}

const bots = rows.filter((r) => r.kind === 'bot').length;
const humans = rows.length - bots;
const shadowOnly = process.argv.includes('--shadow');
const rules = engine.all().filter((r) => (shadowOnly ? r.status === 'shadow' : r.status !== 'rejected'));

console.log(`${rows.length} episodes: ${bots} bot, ${humans} human\n`);
console.log('status    rule                      bot        human      conditions');
for (const r of rules) {
  const b = rows.filter((x) => x.kind === 'bot' && engine.matches(r, x.features)).length;
  const h = rows.filter((x) => x.kind === 'human' && engine.matches(r, x.features)).length;
  const risky = humans > 0 && h / humans > 0.1;
  console.log(
    `${r.status.padEnd(9)} ${r.id.padEnd(24)} ` +
    `${`${b}/${bots}`.padEnd(9)} ${`${h}/${humans}`.padEnd(6)}${risky ? ' !' : '  '} ` +
    `${r.conditions}${risky ? `   <- ${Math.round((100 * h) / humans)}% of humans` : ''}`,
  );
}

// What the ACTIVE set does end to end. Shadow rules score zero, so they do not
// appear here — which is the point of them.
const byPlayer = new Map();
for (const row of rows) {
  const p = byPlayer.get(row.playerId) ?? { kind: row.kind, above: [] };
  if (engine.evaluate(row.features).score >= SCORE_THRESHOLD) p.above.push(row.category ?? 'unknown');
  byPlayer.set(row.playerId, p);
}
const tally = { bot: [0, 0], human: [0, 0] };
for (const p of byPlayer.values()) {
  const flagged = p.above.length >= 3 && (new Set(p.above).size >= 2 || p.above.length >= 4);
  tally[p.kind][1]++;
  if (flagged) tally[p.kind][0]++;
}
console.log(`\nactive ruleset flags: bots ${tally.bot[0]}/${tally.bot[1]}, humans ${tally.human[0]}/${tally.human[1]}`);
