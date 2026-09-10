// bots/trace-schema.js — the trace file format.
//
// docs/00 (the format spec) is still missing from docs/. Per CLAUDE.md this is
// not invented from scratch: server/classifier.js's own JSDoc already commits
// to the runtime contract —
//   trace:     {tick:number, x:number, y:number}[]  oldest → newest, ending on
//              the tick the trap fired
//   trapEvent: exactly what server/traps.js checkTraps() returns
// — so offline files and the server's live ring buffer share one schema, as
// CLAUDE.md requires. `kind`/`mode` are added as training labels only; they
// are never sent to the server and must never be confused with fields the
// classifier itself consumes.

import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

export const TRACE_SCHEMA_VERSION = 1;

/**
 * @param {object} p
 * @param {string} p.playerId
 * @param {'bot'|'human'} p.kind           ground truth for training, demo-only
 * @param {'naive'|'evasive'|'human-like'|null} p.mode  bot-farm label only
 * @param {{trapId:string, category:string, tick:number, x:number, y:number,
 *          playerId:string, spawnTick:number}} p.trapEvent
 * @param {{tick:number, x:number, y:number}[]} p.trace
 * @param {number} [p.stubScore]  the (currently stubbed) server-side classify() score, diagnostic only
 */
export function makeEpisode({ playerId, kind, mode, trapEvent, trace, stubScore }) {
  return {
    schema: TRACE_SCHEMA_VERSION,
    recordedAt: new Date().toISOString(),
    playerId,
    kind,
    mode,
    trapEvent,
    trace,
    ...(stubScore !== undefined ? { stubScore } : {}),
  };
}

export async function writeEpisode(dir, episode) {
  await mkdir(dir, { recursive: true });
  const file = path.join(dir, `${episode.trapEvent.trapId}.json`);
  await writeFile(file, JSON.stringify(episode, null, 2));
  return file;
}
