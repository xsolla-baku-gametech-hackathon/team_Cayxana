// server/episodes.js — the labelled dataset, written and read.
//
// An EPISODE is one trap trip: the trace that led to it, the trapEvent that
// fired, and the demo-only `kind` label of whoever tripped it. The format is
// bots/trace-schema.js — the same one bots/generate-traces.js writes offline
// and bots/trace-emitter.js writes from the live farm — so all three sources
// land in one directory and the backtest reads them without caring which is
// which.
//
// Why the server writes them too, when trace-emitter.js already exists: the
// emitter runs inside the bot process, so it only ever sees bots. A person in
// a browser has no emitter. Without this file the "human" half of every
// backtest would be bots pretending, which is exactly the mistake that makes
// a false-positive rate look better than it is.
//
// The label is read ONLY by the backtest and by the agent's cohort tables. It
// never reaches classify(), never reaches a rule, and never reaches the model.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { makeEpisode } from '../bots/trace-schema.js';

export const TRACE_DIR = path.resolve(fileURLToPath(new URL('../traces', import.meta.url)));

let writeFailed = false;

/**
 * Append one episode, best-effort and synchronous.
 *
 * One file per trap trip, named by trapId, exactly like writeEpisode() in
 * trace-schema.js — but written with the sync API because this is called from
 * inside the tick and an unawaited promise storm during a bot farm is not
 * worth the microseconds.
 */
export function record({ playerId, kind, mode = null, trapEvent, trace, stubScore }) {
  if (writeFailed) return;
  try {
    fs.mkdirSync(TRACE_DIR, { recursive: true });
    const episode = makeEpisode({ playerId, kind, mode, trapEvent, trace, stubScore });
    const name = `${trapEvent.trapId}-${trapEvent.tick}.json`; // a trapId can fire more than once
    fs.writeFileSync(path.join(TRACE_DIR, name), JSON.stringify(episode));
    return name;
  } catch (err) {
    writeFailed = true;
    console.warn('[episodes] logging disabled after write error:', err.message);
    return null;
  }
}

/** Re-label episodes already on disk, e.g. when a "human" turns out not to be one. */
export function relabel(names, kind) {
  for (const name of names) {
    const file = path.join(TRACE_DIR, name);
    try {
      const rec = JSON.parse(fs.readFileSync(file, 'utf8'));
      rec.kind = kind;
      fs.writeFileSync(file, JSON.stringify(rec));
    } catch { /* moved or half-written: nothing to fix */ }
  }
}

/**
 * Every labelled episode on disk, as {kind, mode, trapEvent, trace}.
 *
 * Deliberately returns raw traces rather than computed features: features are
 * code and they change, and an episode that stores only yesterday's features
 * is worthless the moment one is redefined. Recomputing is cheap and keeps the
 * whole corpus valid across a feature change.
 *
 * @param {{dir?:string}} [opts]
 */
export function load({ dir = TRACE_DIR } = {}) {
  let files;
  try {
    files = fs.readdirSync(dir).filter((f) => f.endsWith('.json'));
  } catch {
    return [];
  }
  const out = [];
  for (const f of files) {
    try {
      const rec = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
      if (!rec || !rec.trapEvent || !Array.isArray(rec.trace)) continue;
      if (rec.kind !== 'bot' && rec.kind !== 'human') continue; // unlabelled: not evidence
      out.push(rec);
    } catch { /* half-written or not an episode: skip it */ }
  }
  return out;
}

/** Split loaded episodes by label. */
export function cohorts(episodes) {
  return {
    bot: episodes.filter((e) => e.kind === 'bot'),
    human: episodes.filter((e) => e.kind === 'human'),
  };
}
