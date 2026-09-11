"""Reading the game engine's JSONL trace stream.

The file is append-only and written while the game runs, so the reader has
to cope with a line that is only half-written at the moment it reads: it
keeps the trailing fragment and prepends it to the next read. In follow mode
it polls, because a portable file-watch is not worth the dependency at 2 Hz.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRACE_DIR = Path(__file__).resolve().parent.parent / "game_engine" / "traces"
POLL_SECONDS = 0.25


def latest_session(trace_dir: Path | str = DEFAULT_TRACE_DIR) -> Path | None:
    """The session file the running server is writing to, if there is one.

    The server drops a pointer file when it starts; fall back to the newest
    session file when the pointer is missing or stale.
    """
    trace_dir = Path(trace_dir)
    pointer = trace_dir / "latest.txt"
    if pointer.is_file():
        candidate = trace_dir / pointer.read_text(encoding="utf-8").strip()
        if candidate.is_file():
            return candidate
    sessions = sorted(trace_dir.glob("session-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return sessions[-1] if sessions else None


@dataclass
class GameLogReader:
    """Yields records from one session file, optionally following it live."""

    path: Path
    follow: bool = False
    poll_seconds: float = POLL_SECONDS
    idle_timeout: float | None = None  # stop following after this much silence

    def __iter__(self):
        return self.records()

    def records(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            partial = ""
            idle = 0.0
            while True:
                chunk = handle.read()
                if chunk:
                    idle = 0.0
                    lines = (partial + chunk).split("\n")
                    partial = lines.pop()  # incomplete line, or ""
                    for line in lines:
                        record = _parse(line)
                        if record is not None:
                            yield record
                    continue
                if not self.follow:
                    return
                if self.idle_timeout is not None and idle >= self.idle_timeout:
                    return
                time.sleep(self.poll_seconds)
                idle += self.poll_seconds


def _parse(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None  # a torn line: the writer will not repeat it, drop it
    return record if isinstance(record, dict) and "type" in record else None
