// server/pyverdicts.js — what the Python classifier decided, for the dashboard.
//
// The bridge (python -m bridge) writes traces/verdicts.jsonl. This tails it
// incrementally and keeps a per-player summary. The server still never acts
// on it: it is shown, not enforced. A shorter file than last time means a new
// run truncated it, so the summary starts over.

import fs from 'node:fs';
import path from 'node:path';

import { TRACE_DIR } from './episodes.js';

const FILE = process.env.VERDICTS_FILE || path.join(TRACE_DIR, 'verdicts.jsonl');
const MAX_READ = 4 * 1024 * 1024;

let offset = 0;
let partial = '';
let players = new Map();

function blank() {
  return { windows: 0, suspicious: 0, flagged: false, trapRate: null, loop: null, signatures: {}, lastScore: null };
}

function ingest(rec) {
  const id = rec?.playerId;
  if (!id) return;
  const p = players.get(id) ?? blank();
  players.set(id, p);
  if (rec.type === 'verdict') {
    p.windows++;
    if (rec.suspicious) p.suspicious++;
    if (rec.flagged) p.flagged = true;
    if (rec.score != null) p.lastScore = Math.round(rec.score * 100) / 100;
    for (const s of rec.signatures ?? []) p.signatures[s] = (p.signatures[s] ?? 0) + 1;
  } else if (rec.type === 'trap_rate') {
    p.flagged = true;
    p.trapRate = { trips: rec.trips, windowSeconds: rec.windowSeconds, tick: rec.tick };
  } else if (rec.type === 'loop') {
    // detection/looping.py: one record per looping window (or trap_retarget).
    if (rec.flagged) p.flagged = true;
    p.loop = { windows: rec.loopingWindows, flagAt: rec.flagWindows, flagged: rec.flagged,
               counts: rec.signatureCounts ?? {}, tick: rec.tick };
  }
}

function poll() {
  let size;
  try {
    size = fs.statSync(FILE).size;
  } catch {
    return; // no bridge running yet
  }
  if (size < offset) { offset = 0; partial = ''; players = new Map(); }
  if (size === offset) return;
  const length = Math.min(size - offset, MAX_READ);
  const buf = Buffer.alloc(length);
  let fd;
  try {
    fd = fs.openSync(FILE, 'r');
    fs.readSync(fd, buf, 0, length, offset);
  } catch {
    return;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
  offset += length;
  const lines = (partial + buf.toString('utf8')).split('\n');
  partial = lines.pop();
  for (const line of lines) {
    if (!line.trim()) continue;
    try { ingest(JSON.parse(line)); } catch { /* torn line */ }
  }
}

/** playerId -> summary, fresh from disk. */
export function snapshot() {
  poll();
  return players;
}
