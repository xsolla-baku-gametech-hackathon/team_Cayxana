// server/sessionlog.js — the JSONL stream the Python bridge follows.
//
// One file per server start: traces/session-<time>.jsonl, plus traces/latest.txt
// naming it. Records: session, join, trap, move (every tick, all players), leave.
// Enabled with TRACE_LOG=1 (run_game.py sets it).

import fs from 'node:fs';
import path from 'node:path';

import * as C from '../constants.js';
import { TRACE_DIR } from './episodes.js';

const ENABLED = process.env.TRACE_LOG === '1';
const round1 = (v) => Math.round(v * 10) / 10;

let stream = null;
let fileName = null;

export function start() {
  if (!ENABLED) return null;
  try {
    fs.mkdirSync(TRACE_DIR, { recursive: true });
    fileName = `session-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`;
    // Open synchronously so the file exists before latest.txt points at it.
    const fd = fs.openSync(path.join(TRACE_DIR, fileName), 'a');
    stream = fs.createWriteStream(null, { fd });
    stream.on('error', (err) => {
      console.warn('[sessionlog] disabled after write error:', err.message);
      stream = null;
    });
    write({ type: 'session', tickHz: C.TICK_HZ, startedAt: Date.now() });
    fs.writeFileSync(path.join(TRACE_DIR, 'latest.txt'), fileName);
    return fileName;
  } catch (err) {
    console.warn('[sessionlog] could not start:', err.message);
    stream = null;
    return null;
  }
}

export const name = () => fileName;

export function write(record) {
  if (stream) stream.write(`${JSON.stringify(record)}\n`);
}

export function move(tick, players) {
  if (!stream || players.size === 0) return;
  const rows = [];
  for (const p of players.values()) rows.push([p.id, round1(p.x), round1(p.y)]);
  write({ type: 'move', tick, players: rows });
}
