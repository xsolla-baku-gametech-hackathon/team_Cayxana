// bots/trace-emitter.js — always-on trace emitter for the live farm.
//
// The game socket (`/game`) never carries trap metadata — a bot only learns
// ITS OWN trapEvents by also holding a normal, keyed connection to the same
// telemetry feed the dashboard uses (server/index.js requires the key for
// exactly this reason: "anyone who can connect could read them — including a
// bot"). Telemetry only says WHEN a trap fired, though; it broadcasts a
// snapshot of the CURRENT trace at 2 Hz, not the trace as it stood at the
// exact fired tick. So the trace payload itself comes from each bot's own
// TraceBuffer, built tick-by-tick from its own state messages — deterministic
// and identical in shape to the server's per-player ring buffer, since both
// are fed the same inputs at the same rate.

import { WebSocket } from 'ws';
import * as C from '../constants.js';
import { makeEpisode, writeEpisode } from './trace-schema.js';

export class TraceBuffer {
  constructor(capacity = C.TRACE_LEN) {
    this.capacity = capacity;
    this.buf = [];
  }
  record(tick, x, y) {
    this.buf.push({ tick, x, y });
    if (this.buf.length > this.capacity) this.buf.shift();
  }
  /** Trace ending at `tick` inclusive, or null if that tick isn't (or no longer is) in the buffer. */
  sliceTo(tick) {
    const idx = this.buf.findIndex((s) => s.tick === tick);
    return idx === -1 ? null : this.buf.slice(0, idx + 1);
  }
}

/**
 * One shared telemetry connection per process, fanning out to every
 * registered player. Bots register their own TraceBuffer + a label function
 * (returns { kind, mode }) once their playerId is known from the first state
 * message; unregister on disconnect.
 */
export function startTraceEmitter({ host, telemetryKey, outDir, onEpisode }) {
  const players = new Map(); // playerId -> { buffer, labelFn }
  const seen = new Set();    // trapId, so a player reconnecting mid-history can't duplicate an episode
  let ws;

  function handleMessage(data) {
    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }
    if (msg.type !== 'telemetry') return;
    for (const p of msg.players) {
      const reg = players.get(p.id);
      if (!reg) continue;
      for (const ev of p.events) {
        if (seen.has(ev.trapId)) continue;
        const trace = reg.buffer.sliceTo(ev.tick);
        if (!trace) continue; // fired before our buffer had this tick, or it has since scrolled past it
        seen.add(ev.trapId);
        const { score, ...trapEvent } = ev; // trapEvent must stay exactly what checkTraps returns
        const { kind, mode } = reg.labelFn();
        const episode = makeEpisode({ playerId: p.id, kind, mode, trapEvent, trace, stubScore: score });
        writeEpisode(outDir, episode)
          .then((file) => onEpisode?.(file, episode))
          .catch((err) => console.error('[trace] write failed:', err.message));
      }
    }
  }

  function connect() {
    ws = new WebSocket(`ws://${host}${C.PATHS.TELEMETRY}?key=${encodeURIComponent(telemetryKey)}`);
    ws.on('message', handleMessage);
    ws.on('close', () => setTimeout(connect, 2000));
    ws.on('error', () => {}); // 'close' follows and handles the retry
  }
  connect();

  return {
    register(playerId, buffer, labelFn) { players.set(playerId, { buffer, labelFn }); },
    unregister(playerId) { players.delete(playerId); },
  };
}
