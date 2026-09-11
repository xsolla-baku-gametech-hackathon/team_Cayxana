// server/ruleEngine.js — rules are data, and this is the only thing that reads them.
//
// A rule is a conjunction of threshold conditions over the features in
// server/features.js, plus a weight. A player's score is the sum of the
// weights of the ACTIVE rules whose conditions all hold, capped at 1.
//
// The one idea worth defending here is `status`:
//
//   active    counts toward the score
//   shadow    evaluated and logged, contributes exactly 0
//   rejected  never evaluated
//
// Everything an agent writes lands as `shadow`. Nothing an agent writes can
// change a player's score until a person opens the admin page and approves it.
// That is the whole point of the layer, so the shadow branch below is the line
// of code the claim rests on — do not "simplify" it away.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as C from '../constants.js';
import { FEATURE_BY_NAME } from './features.js';

const RULES_DIR = path.resolve(fileURLToPath(new URL('../rules', import.meta.url)));
const RULES_FILE = path.join(RULES_DIR, 'active.json');
const RELOAD_DEBOUNCE_MS = 150;

const OPS = {
  '>=': (a, b) => a >= b,
  '>': (a, b) => a > b,
  '<=': (a, b) => a <= b,
  '<': (a, b) => a < b,
};
export const LEGAL_OPS = Object.keys(OPS);
export const LEGAL_STATUS = new Set(['active', 'shadow', 'rejected']);

/** @type {object[]} the live ruleset. Replaced wholesale on a good reload. */
let rules = [];
let loadedAt = 0;
let lastError = null;
let watcher = null;
let debounce = null;

/** A rule the engine can safely evaluate. Anything else is dropped on load. */
function sane(rule) {
  if (!rule || typeof rule !== 'object') return null;
  if (typeof rule.id !== 'string' || !rule.id) return null;
  if (!Array.isArray(rule.when) || rule.when.length === 0) return null;
  const when = [];
  for (const c of rule.when) {
    if (!c || !FEATURE_BY_NAME.has(c.feat) || !OPS[c.op] || !Number.isFinite(Number(c.v))) return null;
    const f = FEATURE_BY_NAME.get(c.feat);
    when.push({ feat: c.feat, op: c.op, v: Math.min(f.max, Math.max(f.min, Number(c.v))) });
  }
  const weight = Number(rule.weight);
  return {
    ...rule,
    when,
    weight: Number.isFinite(weight)
      ? Math.min(C.RULE_WEIGHT_MAX, Math.max(C.RULE_WEIGHT_MIN, weight))
      : C.RULE_WEIGHT_MIN,
    status: LEGAL_STATUS.has(rule.status) ? rule.status : 'shadow',
  };
}

function parse(text) {
  const raw = JSON.parse(text);
  if (!Array.isArray(raw)) throw new Error('rules/active.json must be a JSON array');
  const out = [];
  const seen = new Set();
  for (const r of raw) {
    const s = sane(r);
    if (!s) { console.warn('[rules] dropped an unusable rule:', r?.id ?? '(no id)'); continue; }
    if (seen.has(s.id)) { console.warn('[rules] dropped a duplicate id:', s.id); continue; }
    seen.add(s.id);
    out.push(s);
  }
  return out;
}

/**
 * Reads rules/active.json. On any failure the PREVIOUS ruleset stays in force:
 * a half-written file or a typo during the demo must not take scoring down,
 * and it must certainly not silently score everyone 0.
 * @returns {boolean} true if the file was read and applied
 */
export function load() {
  try {
    const next = parse(fs.readFileSync(RULES_FILE, 'utf8'));
    rules = next;
    loadedAt = Date.now();
    lastError = null;
    const active = rules.filter((r) => r.status === 'active').length;
    const shadow = rules.filter((r) => r.status === 'shadow').length;
    console.log(`[rules] loaded ${rules.length} rule(s): ${active} active, ${shadow} shadow`);
    return true;
  } catch (err) {
    lastError = err.message;
    console.warn(`[rules] reload failed, keeping ${rules.length} rule(s) in force:`, err.message);
    return false;
  }
}

/** Hot-reload on change, debounced (editors and our own writes fire twice). */
export function watch() {
  if (watcher) return;
  try {
    fs.mkdirSync(RULES_DIR, { recursive: true });
    watcher = fs.watch(RULES_DIR, (_event, filename) => {
      if (filename && filename !== 'active.json') return;
      clearTimeout(debounce);
      debounce = setTimeout(load, RELOAD_DEBOUNCE_MS);
    });
    watcher.on('error', (err) => console.warn('[rules] watcher error:', err.message));
  } catch (err) {
    console.warn('[rules] could not watch the rules directory:', err.message);
  }
}

/** Atomic write, so a reader (or the watcher) never sees a half-written file. */
export function save() {
  fs.mkdirSync(RULES_DIR, { recursive: true });
  const tmp = `${RULES_FILE}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(rules, null, 2)}\n`);
  fs.renameSync(tmp, RULES_FILE);
}

/** Do all of this rule's conditions hold for this feature record? */
export function matches(rule, features) {
  for (const c of rule.when) {
    const v = features[c.feat];
    if (!Number.isFinite(v) || !OPS[c.op](v, c.v)) return false;
  }
  return true;
}

/**
 * Score one feature record.
 * @returns {{score:number, fired:string[], shadowFired:string[]}}
 */
export function evaluate(features) {
  const fired = [];
  const shadowFired = [];
  let score = 0;
  for (const r of rules) {
    if (r.status === 'rejected') continue;
    if (!matches(r, features)) continue;
    if (r.status === 'shadow') {
      shadowFired.push(r.id); // logged, shown on the dashboard, worth exactly 0
      continue;
    }
    fired.push(r.id);
    score += r.weight;
  }
  return { score: Math.min(1, score), fired, shadowFired };
}

const round = (v) => (Number.isInteger(v) ? v : Math.round(v * 1000) / 1000);

/** Plain-English rendering of a rule's conditions, for the approval UI. */
export function describe(rule) {
  return rule.when.map((c) => `${c.feat} ${c.op} ${round(c.v)}`).join(' AND ');
}

export function all() {
  return rules.map((r) => ({ ...r, conditions: describe(r) }));
}

export function byId(id) {
  return rules.find((r) => r.id === id) ?? null;
}

export function ids() {
  return new Set(rules.map((r) => r.id));
}

/** Canonical form of a rule's conditions, for spotting re-proposals. */
function signature(rule) {
  return rule.when.map((c) => `${c.feat}${c.op}${c.v}`).sort().join('&');
}

/**
 * Appends agent candidates. They arrive as shadow and stay shadow.
 *
 * A rule whose conditions already exist is dropped whatever its id: the agent
 * is invoked on every alarm and will keep reaching the same conclusion from
 * the same distributions, and a review queue holding twelve copies of one rule
 * is a queue nobody reads.
 */
export function append(candidates) {
  const existing = ids();
  const seen = new Set(rules.map(signature));
  const added = [];
  for (const c of candidates) {
    const s = sane({ ...c, status: 'shadow' });
    if (!s || existing.has(s.id)) continue;
    const sig = signature(s);
    if (seen.has(sig)) { console.log(`[rules] ${s.id} re-proposes an existing rule; dropped`); continue; }
    seen.add(sig);
    existing.add(s.id);
    rules.push(s);
    added.push(s);
  }
  if (added.length) save();
  return added;
}

/**
 * The human-in-the-loop gate. Only ever called from the keyed approval
 * endpoints in server/index.js — the server itself never approves anything.
 * @returns {{ok:boolean, error?:string, rule?:object}}
 */
export function setStatus(id, status, { by = null, reason = null } = {}) {
  if (!LEGAL_STATUS.has(status)) return { ok: false, error: 'illegal status' };
  const rule = byId(id);
  if (!rule) return { ok: false, error: 'no such rule' };
  rule.status = status;
  rule.approvedBy = status === 'active' ? by : null;
  rule.decidedAt = new Date().toISOString();
  if (status === 'rejected') rule.rejectedReason = reason ?? '';
  save();
  console.log(`[rules] ${id} → ${status}${by ? ` by ${by}` : ''}`);
  return { ok: true, rule };
}

export function status() {
  return {
    file: RULES_FILE,
    count: rules.length,
    active: rules.filter((r) => r.status === 'active').length,
    shadow: rules.filter((r) => r.status === 'shadow').length,
    rejected: rules.filter((r) => r.status === 'rejected').length,
    loadedAt,
    lastError,
  };
}
