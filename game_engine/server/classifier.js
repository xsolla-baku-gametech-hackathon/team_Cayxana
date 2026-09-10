// server/classifier.js — OWNER: Detection workstream.
// This is a STUB. Replace this file's contents; keep the export names.
// Must be synchronous and should never throw (the server wraps it anyway).

import { SCORE_THRESHOLD } from '../constants.js';

/**
 * REQUIRED. Called when checkTraps returns an event.
 * @param {{tick:number, x:number, y:number}[]} trace  oldest → newest, up to TRACE_LEN entries,
 *        ending with the tick on which the trap fired
 * @param {object} trapEvent  exactly as returned by checkTraps
 * @returns {{score:number, features:object}}  score in [0, 1], higher = more machine-like
 */
export function classify(trace, trapEvent) {
  return { score: 0, features: {} };
}

/**
 * PROPOSED (optional). Turns a player's recorded results into the dashboard's
 * current score and flag. If you drop this export, the server falls back to
 * "latest score, never flagged".
 *
 * Reference implementation of the agreed rule: flagged when there are
 * 3+ events above threshold from at least 2 different categories.
 * Flagging is review-only — the server never acts on it.
 *
 * @param {{tick:number, trapEvent:object, score:number, features:object}[]} results
 * @returns {{score:number, flagged:boolean}}
 */
export function assessPlayer(results) {
  if (results.length === 0) return { score: 0, flagged: false };
  const above = results.filter((r) => r.score >= SCORE_THRESHOLD);
  const categories = new Set(above.map((r) => r.trapEvent.category));
  return {
    score: results[results.length - 1].score,
    flagged: above.length >= 3 && categories.size >= 2,
  };
}
