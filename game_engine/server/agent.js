// server/agent.js — the model's whole job, and its whole leash.
//
// What it does: it is handed two tables of numbers — feature means and stddevs
// for a cohort of players who keep tripping traps without scoring, and for a
// cohort labelled human — and asked which threshold separates them. It answers
// with JSON.
//
// What it does NOT do: it never writes JavaScript, never sees a coordinate,
// never sees a player id, never sees a label, and never changes a score. Every
// candidate it returns is schema-checked, clamped, backtested against labelled
// episodes, and written to rules/active.json with status "shadow" — worth zero
// until a person reads it and clicks Approve.
//
// The model does not detect anything. It compares two distributions and
// proposes a threshold. The pipeline around it is the part that is defensible.
//
//   node server/agent.js --replay     propose from a canned response (no model call)
//   node server/agent.js              propose from traces/, calling the Claude API
//   node server/agent.js --local      same, against a local OpenAI-compatible server

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as C from '../constants.js';
import { FEATURE_SCHEMA, FEATURE_BY_NAME, extractFeatures } from './features.js';
import * as engine from './ruleEngine.js';
import * as episodes from './episodes.js';

const RULES_DIR = path.resolve(fileURLToPath(new URL('../rules', import.meta.url)));
const SAMPLE_FILE = path.join(RULES_DIR, 'proposal-sample.json');
const ROUNDS_FILE = path.join(episodes.TRACE_DIR, 'agent-rounds.jsonl');

// ── Which model ────────────────────────────────────────────────────────
//
// Two backends, because the model is the SWAPPABLE part of this layer and it
// is worth being able to prove that. Everything that makes agent output safe
// to act on — the feature schema, the range clamps, the backtest against
// labelled episodes, shadow status, the human approval click — sits outside
// the model and does not change when the model does.
//
//   AGENT_PROVIDER=claude   (default)  the API, model pinned by the spec
//   AGENT_PROVIDER=local               any OpenAI-compatible server on this
//                                      machine: LM Studio, llama.cpp, Ollama
//
// The local path is not a downgrade to apologise for. Nothing leaves the
// machine, there is no key to forget, and there is no network call between the
// alarm firing and a rule appearing in the review queue.
const PROVIDER = process.argv.includes('--local') ? 'local' : (process.env.AGENT_PROVIDER || 'claude');
const LOCAL_BASE_URL = process.env.AGENT_BASE_URL || 'http://localhost:1234/v1';
const DEFAULT_MODEL = { claude: 'claude-sonnet-4-6', local: 'qwen/qwen3.6-35b-a3b' };
const MODEL = process.env.AGENT_MODEL || DEFAULT_MODEL[PROVIDER] || DEFAULT_MODEL.claude;
const LOCAL_TIMEOUT_MS = 300000; // a cold 22 GB model can take minutes to page in
// Reasoning models spend most of their budget thinking before they write a
// character of the answer. Measured on qwen3.6-35b-a3b: ~200 reasoning tokens
// to emit a nine-token JSON array, and LM Studio honours neither
// `enable_thinking: false` nor the `/no_think` suffix — so the budget has to be
// generous or `content` comes back empty and the round is wasted.
const LOCAL_MAX_TOKENS = Number(process.env.AGENT_MAX_TOKENS) || 6000;
const MAX_CANDIDATES = 3;

let lastInvocation = 0;
let inFlight = false;

// ── Cohort tables ──────────────────────────────────────────────────────

const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

function stddev(xs) {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / xs.length);
}

const r2 = (v) => Math.round(v * 100) / 100;

/** Features are recomputed from raw traces, so a feature change never stales the corpus. */
export function featuresOf(episode) {
  return extractFeatures(episode.trace, episode.trapEvent);
}

/** mean/stddev of every schema feature over a set of episodes. */
function summarise(rows) {
  const out = {};
  for (const f of FEATURE_SCHEMA) {
    const xs = rows.map((r) => r[f.name]).filter(Number.isFinite);
    out[f.name] = { mean: r2(mean(xs)), stddev: r2(stddev(xs)) };
  }
  return out;
}

/** The diff table, rendered small. This is the only data the model receives. */
function renderTable(suspect, human) {
  const rows = FEATURE_SCHEMA.map((f) => {
    const s = suspect[f.name];
    const h = human[f.name];
    return `${f.name} | ${f.min}-${f.max} | ${s.mean} ± ${s.stddev} | ${h.mean} ± ${h.stddev} | ${f.desc}`;
  });
  return ['feature | range | suspect mean ± sd | human mean ± sd | meaning', ...rows].join('\n');
}

function buildPrompt({ suspect, human, suspectCount, humanCount, alarm }) {
  const active = engine.all().filter((r) => r.status === 'active');
  return [
    'A cohort of players keeps tripping bot traps, but the rules in force score them near zero.',
    'The traps are ground truth that something inhuman is happening; the rules cannot see it.',
    '',
    `Suspect cohort: ${suspectCount} trap episodes. Known-human cohort: ${humanCount} episodes.`,
    alarm ? `Alarm: ${alarm.hits} trap hits in the last 60 s at mean score ${r2(alarm.meanScore)}.` : '',
    '',
    'Feature distributions:',
    renderTable(suspect, human),
    '',
    'Rules currently active (a rule fires when every condition holds; weights sum, capped at 1):',
    active.length ? active.map((r) => `${r.id}: ${engine.describe(r)}  -> weight ${r.weight}`).join('\n') : '(none)',
    '',
    `Propose 1-${MAX_CANDIDATES} new rules that separate the suspect cohort from the human cohort.`,
    'Prefer features where the two distributions barely overlap. Do not restate a rule that is already active.',
    `Weights are between ${C.RULE_WEIGHT_MIN} and ${C.RULE_WEIGHT_MAX}; a rule that only corroborates should be small.`,
    '',
    'Answer with a JSON array only. No prose, no markdown fences. Each element:',
    '{"id":"r_snake_case","when":[{"feat":"<feature name>","op":">=|>|<=|<","v":<number>}],',
    ' "weight":<number>,"rationale":"<one short sentence a reviewer can check>"}',
  ].filter(Boolean).join('\n');
}

// ── Validation ─────────────────────────────────────────────────────────

/**
 * Strip everything a model wraps around its answer: reasoning blocks (any
 * reasoning-capable model emits these — Qwen3 and friends use <think>), and
 * markdown fences, which every model adds sooner or later however firmly it
 * was told not to. Then take the outermost JSON array or object, so a stray
 * sentence before or after the payload costs us nothing.
 */
function stripFences(text) {
  let s = String(text ?? '')
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/<\/?(?:think|thinking|reasoning)>/gi, '')
    .replace(/```(?:json)?/gi, '')
    .trim();
  const starts = ['[', '{'].map((c) => s.indexOf(c)).filter((i) => i !== -1);
  const first = starts.length ? Math.min(...starts) : -1;
  const last = Math.max(s.lastIndexOf(']'), s.lastIndexOf('}'));
  if (first !== -1 && last > first) s = s.slice(first, last + 1);
  return s.trim();
}

/**
 * Turns whatever came back into rules the engine will accept, or [].
 * Clamps rather than rejects wherever clamping is meaningful: a threshold
 * outside a feature's range is a misunderstanding of the range, not an attack,
 * and the backtest will show whether the clamped rule is any good.
 */
export function validate(text, { existingIds = engine.ids() } = {}) {
  let parsed;
  try {
    parsed = JSON.parse(stripFences(text));
  } catch {
    return { ok: false, error: 'not JSON', rules: [] };
  }
  // A bare array is what we asked for, but smaller models very often wrap it
  // in an object. That is a formatting habit, not a wrong answer — unwrap it.
  if (!Array.isArray(parsed) && parsed && typeof parsed === 'object') {
    parsed = parsed.rules ?? parsed.candidates ?? parsed.proposals ?? parsed.output ?? parsed;
  }
  if (!Array.isArray(parsed)) return { ok: false, error: 'not an array', rules: [] };

  const taken = new Set(existingIds);
  const rules = [];
  for (const raw of parsed.slice(0, MAX_CANDIDATES)) {
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.when) || raw.when.length === 0) continue;

    const when = [];
    let bad = false;
    for (const c of raw.when) {
      const f = FEATURE_BY_NAME.get(c?.feat);
      if (!f || !engine.LEGAL_OPS.includes(c?.op) || !Number.isFinite(Number(c?.v))) { bad = true; break; }
      when.push({ feat: f.name, op: c.op, v: Math.min(f.max, Math.max(f.min, Number(c.v))) });
    }
    if (bad || when.length === 0) continue;

    const weight = Number(raw.weight);
    let id = typeof raw.id === 'string' && /^[a-z0-9_]{3,48}$/.test(raw.id) ? raw.id : 'r_agent';
    while (taken.has(id)) id = `${id.replace(/_\d+$/, '')}_${Math.floor(Math.random() * 900 + 100)}`;
    taken.add(id);

    rules.push({
      id,
      when,
      weight: Number.isFinite(weight)
        ? Math.min(C.RULE_WEIGHT_MAX, Math.max(C.RULE_WEIGHT_MIN, weight))
        : 0.3,
      rationale: String(raw.rationale ?? '').slice(0, 200),
      author: 'agent',
      status: 'shadow',
      createdAt: new Date().toISOString(),
      backtest: null,
      approvedBy: null,
    });
  }
  return { ok: rules.length > 0, error: rules.length ? null : 'no usable rules', rules };
}

// ── Backtest ───────────────────────────────────────────────────────────

/**
 * How this rule would have behaved on labelled episodes. `humanFlagged` is the
 * number that matters: a rule that catches every bot and one human in ten is a
 * rule that mislabels real players, and the reviewer has to see that before
 * they approve it, not after.
 *
 * @param {object} rule
 * @param {{kind:string, features:object}[]} rows  pre-computed, so a batch of
 *        candidates does not re-extract features once per rule
 */
export function backtest(rule, rows) {
  const hit = (kind) => rows.filter((r) => r.kind === kind && engine.matches(rule, r.features)).length;
  const total = (kind) => rows.filter((r) => r.kind === kind).length;
  return {
    botCaught: hit('bot'),
    botTotal: total('bot'),
    humanFlagged: hit('human'),
    humanTotal: total('human'),
  };
}

/** The UI shows this rule as risky. It still shows it — the human decides. */
export function isRisky(bt) {
  return Boolean(bt && bt.humanTotal > 0 && bt.humanFlagged / bt.humanTotal > 0.1);
}

/** Load every labelled episode and compute its features once. */
export function corpus() {
  return episodes.load().map((e) => ({
    kind: e.kind,
    playerId: e.playerId,
    category: e.trapEvent?.category ?? 'unknown',
    features: featuresOf(e),
  }));
}

// ── The call ───────────────────────────────────────────────────────────

const SYSTEM = [
  'You tune a bot-detection ruleset for a game.',
  'You answer with a JSON array and nothing else: no prose, no explanation, no markdown fences.',
  'Only use feature names from the table you are given. Only use the operators >=, >, <=, <.',
  'Every threshold must sit inside the stated range for that feature.',
].join(' ');

async function askClaude(prompt) {
  const { default: Anthropic } = await import('@anthropic-ai/sdk');
  const client = new Anthropic();
  const res = await client.messages.create({
    model: MODEL,
    max_tokens: 1500,
    system: SYSTEM,
    messages: [{ role: 'user', content: prompt }],
  });
  return res.content.filter((b) => b.type === 'text').map((b) => b.text).join('');
}

/**
 * Any OpenAI-compatible local server. Deliberately plain fetch and no SDK: the
 * whole point of this path is that it depends on nothing but a port being open.
 *
 * Temperature is low because this is not a creative task — it is "read two
 * columns and name a threshold", and a smaller model does that better when it
 * is not being encouraged to invent.
 */
async function askLocal(prompt) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LOCAL_TIMEOUT_MS);
  try {
    const res = await fetch(`${LOCAL_BASE_URL}/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer local' },
      signal: controller.signal,
      body: JSON.stringify({
        model: MODEL,
        temperature: 0.2,
        max_tokens: LOCAL_MAX_TOKENS,
        messages: [{ role: 'system', content: SYSTEM }, { role: 'user', content: prompt }],
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
    const choice = (await res.json())?.choices?.[0]?.message;
    const content = choice?.content?.trim() ?? '';
    if (!content) {
      // Empty content with reasoning present means the token budget ran out
      // mid-thought. Say so — never hand the thinking text to the parser and
      // then call the resulting failure "the model returned invalid JSON".
      throw new Error((choice?.reasoning_content ?? '').length
        ? `answer truncated: all ${LOCAL_MAX_TOKENS} tokens went to reasoning (raise AGENT_MAX_TOKENS)`
        : 'empty response');
    }
    return content;
  } finally {
    clearTimeout(timer);
  }
}

const askModel = (prompt) => (PROVIDER === 'local' ? askLocal(prompt) : askClaude(prompt));

function logRound(record) {
  try {
    fs.mkdirSync(episodes.TRACE_DIR, { recursive: true });
    fs.appendFileSync(ROUNDS_FILE, `${JSON.stringify(record)}\n`);
  } catch (err) {
    console.warn('[agent] could not write the audit line:', err.message);
  }
}

/**
 * One round. Never throws, never blocks a tick for long, never surfaces a
 * broken rule to the reviewer.
 *
 * @param {object} opts
 * @param {string[]} [opts.suspectIds]  players the evasion monitor is alarming on
 * @param {object}   [opts.alarm]       the alarm payload, for the prompt and the log
 * @param {boolean}  [opts.replay]      use rules/proposal-sample.json instead of a model
 * @returns {Promise<{added:object[], skipped?:string}>}
 */
export async function propose({ suspectIds = [], alarm = null, replay = false } = {}) {
  if (inFlight) return { added: [], skipped: 'a round is already running' };
  inFlight = true;
  lastInvocation = Date.now();
  const startedAt = new Date().toISOString();

  try {
    const rows = corpus();
    const suspectSet = new Set(suspectIds);
    const suspectRows = suspectSet.size
      ? rows.filter((r) => suspectSet.has(r.playerId))
      : rows.filter((r) => r.kind === 'bot');
    const humanRows = rows.filter((r) => r.kind === 'human' && !suspectSet.has(r.playerId));

    if (suspectRows.length === 0) {
      const skipped = 'no suspect episodes on disk yet';
      logRound({ type: 'agent_round', at: startedAt, replay, skipped });
      return { added: [], skipped };
    }

    const prompt = buildPrompt({
      suspect: summarise(suspectRows.map((r) => r.features)),
      human: summarise(humanRows.map((r) => r.features)),
      suspectCount: suspectRows.length,
      humanCount: humanRows.length,
      alarm,
    });

    let text = null;
    let source = replay ? 'replay' : `${PROVIDER}:${MODEL}`;
    if (replay) {
      text = fs.readFileSync(SAMPLE_FILE, 'utf8');
    } else {
      // One retry, then give up quietly. A slow or broken model must not be
      // visible anywhere except this log line.
      for (let attempt = 0; attempt < 2 && text === null; attempt++) {
        try {
          text = await askModel(prompt);
        } catch (err) {
          console.warn(`[agent] model call failed (attempt ${attempt + 1}):`, err.message);
        }
      }
      if (text === null) {
        const skipped = 'model unavailable';
        logRound({ type: 'agent_round', at: startedAt, replay, source, skipped });
        return { added: [], skipped };
      }
    }

    let { ok, error, rules } = validate(text);
    if (!ok && !replay) {
      // Malformed → retry once, exactly as specified, then give up silently.
      try {
        const retry = await askModel(`${prompt}\n\nYour previous answer was not a JSON array. Answer with a JSON array only.`);
        ({ ok, error, rules } = validate(retry));
      } catch (err) {
        console.warn('[agent] retry failed:', err.message);
      }
    }
    if (!ok) {
      logRound({ type: 'agent_round', at: startedAt, replay, source, skipped: `invalid proposal: ${error}` });
      console.warn('[agent] discarded an invalid proposal:', error);
      return { added: [], skipped: `invalid proposal: ${error}` };
    }

    for (const rule of rules) {
      rule.backtest = backtest(rule, rows);
      rule.risky = isRisky(rule.backtest);
      rule.alarm = alarm ? { hits: alarm.hits, meanScore: r2(alarm.meanScore) } : null;
    }

    const added = engine.append(rules);
    logRound({
      type: 'agent_round',
      at: startedAt,
      replay,
      source,
      suspectEpisodes: suspectRows.length,
      humanEpisodes: humanRows.length,
      alarm,
      proposed: rules.map((r) => ({ id: r.id, when: r.when, weight: r.weight, rationale: r.rationale, backtest: r.backtest })),
      added: added.map((r) => r.id),
    });
    for (const r of added) {
      const bt = r.backtest;
      console.log(`[agent] shadow rule ${r.id}: ${engine.describe(r)} ` +
        `(bots ${bt.botCaught}/${bt.botTotal}, humans ${bt.humanFlagged}/${bt.humanTotal})`);
    }
    return { added };
  } catch (err) {
    console.warn('[agent] round failed:', err.message); // never crash the server
    return { added: [], skipped: err.message };
  } finally {
    inFlight = false;
  }
}

/** Global rate limit: one invocation per AGENT_COOLDOWN_MS, however many alarms fire. */
export const readyAt = () => lastInvocation + C.AGENT_COOLDOWN_MS;
export const ready = () => !inFlight && Date.now() >= readyAt();

// ── CLI ────────────────────────────────────────────────────────────────

const invokedDirectly = process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (invokedDirectly) {
  engine.load();
  const replay = process.argv.includes('--replay');
  if (!replay) console.log(`[agent] provider=${PROVIDER} model=${MODEL}${PROVIDER === 'local' ? ` at ${LOCAL_BASE_URL}` : ''}`);
  const result = await propose({ replay });
  if (result.added.length === 0) console.log(`[agent] nothing proposed: ${result.skipped ?? 'no candidates'}`);
  else console.log(`[agent] ${result.added.length} shadow rule(s) written to rules/active.json`);
}
