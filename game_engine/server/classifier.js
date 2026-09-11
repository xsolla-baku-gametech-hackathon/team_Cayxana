// server/classifier.js — OWNER: Detection workstream.
//
// Three lines of actual logic: extract features from the trace, evaluate the
// ruleset against them, return the score. All the judgement lives in
// features.js (code, fixed) and rules/active.json (data, hot-reloaded,
// human-approved). This file is only the seam between them.
//
// Must be synchronous and should never throw — the server wraps it anyway.

import { SCORE_THRESHOLD } from '../constants.js';
import { extractFeatures } from './features.js';
import * as engine from './ruleEngine.js';

/**
 * REQUIRED. Called when checkTraps returns an event.
 * @param {{tick:number, x:number, y:number}[]} trace  oldest → newest, up to TRACE_LEN entries,
 *        ending with the tick on which the trap fired
 * @param {object} trapEvent  exactly as returned by checkTraps
 * @returns {{score:number, features:object}}  score in [0, 1], higher = more machine-like
 */
export function classify(trace, trapEvent) {
  const features = extractFeatures(trace, trapEvent);
  const { score, fired, shadowFired } = engine.evaluate(features);
  // Why this player scored what they scored, carried alongside the numbers so
  // the dashboard never has to re-derive it. Underscored: not a feature.
  features._fired = fired;
  features._shadow = shadowFired;
  return { score, features };
}

/** A single category has to say it this many times to stand on its own. */
const CORROBORATION_IN_ONE_CATEGORY = 4;

/**
 * Turns a player's recorded results into the dashboard's current score and flag.
 *
 * The agreed rule was: 3+ events above threshold from at least 2 different
 * categories. That clause was unsatisfiable while traps.js had one category,
 * and `unreachable_bait` is the second - but it also exposed a hole. An
 * evasive bot filters ghost loot out entirely, so every piece of evidence it
 * ever produces is unreachable_bait. Under a strict two-category rule the one
 * bot this layer exists to catch is the one bot that can never be flagged.
 *
 * So corroboration is still required, it just has two shapes: agreement across
 * categories, or the same category saying it four separate times. Both mean
 * "more than one independent trip", which is the property that matters - a
 * single unlucky walk past a trap still cannot flag anyone.
 *
 * Flagging is review-only - the server never acts on it.
 *
 * @param {{tick:number, trapEvent:object, score:number, features:object}[]} results
 * @returns {{score:number, flagged:boolean}}
 */
export function assessPlayer(results) {
  if (results.length === 0) return { score: 0, flagged: false };
  const above = results.filter((r) => r.score >= SCORE_THRESHOLD);
  const categories = new Set(above.map((r) => r.trapEvent.category));
  const corroborated = categories.size >= 2 || above.length >= CORROBORATION_IN_ONE_CATEGORY;
  return {
    score: results[results.length - 1].score,
    flagged: above.length >= 3 && corroborated,
  };
}
