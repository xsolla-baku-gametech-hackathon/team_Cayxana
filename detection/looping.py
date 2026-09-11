"""Looping movement: a farm that has fallen into a cycle.

A naive farm re-aims at "the nearest thing" every tick. After a while that
target logic settles into a loop a person does not produce:

* ``back_and_forth`` - two targets pull in opposite directions, so the bot
  steps one way, then straight back, over and over
* ``stuck_circling`` - it travels a long way but stays inside a small box,
  running round a wall it cannot path past
* ``route_repeat`` - it walks exactly the same stretch, in the same
  direction, again and again
* ``trap_retarget`` - tripping a trap removes the player's traps and new ones
  appear elsewhere; a bot that reads raw state turns toward them at once,
  a person cannot see them and keeps going

The first three are measured on 60 s windows of the position stream, so they
see minutes of behaviour rather than the 10 s after one trap.

Measured 2026-09-11 on 60 s windows (sliding 30 s), worst window per player:
                          back_and_forth  stuck_circling  route_repeat
    8 real humans (146 w)   <= 0.029         0.000          <= 0.008
    naive farm (6 bots)     0.25 - 1.00      0.00 - 1.00    0.14 - 0.97
Thresholds sit 3x (back_and_forth) and 12x (route_repeat) above the worst
human window, and no human window tripped any of the three. A player is
flagged after ``FLAG_WINDOWS`` looping windows, not one.

``trap_retarget`` is weaker evidence: humans turned sharply after 7-50% of
their trips (never more than 19 trips in 10 minutes), naive bots after
58-68% of hundreds of trips. It needs ``MIN_RETARGET_TRIPS`` trips before it
can say anything, which in practice only a farm reaches.

Nothing here decides anything alone beyond a review flag; there is no ban.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import atan2, degrees, hypot, pi

TICK_HZ = 20
WINDOW_TICKS = 60 * TICK_HZ
EVALUATE_EVERY = 30 * TICK_HZ
MOVING_STEP = 0.5  # units per tick; below this the player is standing still
MIN_MOVING_STEPS = 15 * TICK_HZ  # a window needs 15 s of real movement to judge

MAX_REVERSAL_RATE = 0.10
MAX_STUCK_SHARE = 0.30
MAX_REPEAT_SHARE = 0.10
FLAG_WINDOWS = 2

STUCK_SPAN_TICKS = 10 * TICK_HZ
STUCK_MIN_PATH = 400.0  # travelled at least this far in the 10 s...
STUCK_MAX_BOX = 80.0  # ...without leaving a box this size
REPEAT_CELL = 4.0
REPEAT_MIN_GAP = 40  # ticks; closer than this is the same pass, not a repeat

RETARGET_AFTER_TICKS = 12
RETARGET_TURN = pi / 2
MIN_RETARGET_TRIPS = 20
MAX_RETARGET_SHARE = 0.60


def _moving_steps(samples):
    steps = []
    for i, (a, b) in enumerate(zip(samples, samples[1:])):
        dx, dy = b[1] - a[1], b[2] - a[2]
        if hypot(dx, dy) > MOVING_STEP:
            steps.append((i, dx, dy))
    return steps


def reversal_rate(steps) -> float:
    """Share of moving steps that go straight back the way the last one came."""
    reversals = 0
    for (i, ax, ay), (j, bx, by) in zip(steps, steps[1:]):
        if j - i <= 3 and ax * bx + ay * by < -0.9 * hypot(ax, ay) * hypot(bx, by):
            reversals += 1
    return reversals / len(steps)


def stuck_share(samples) -> float:
    """Share of busy 10 s spans that never leave a small box."""
    busy = stuck = 0
    for k in range(0, len(samples) - STUCK_SPAN_TICKS + 1, STUCK_SPAN_TICKS // 2):
        span = samples[k:k + STUCK_SPAN_TICKS]
        path = sum(hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(span, span[1:]))
        if path < STUCK_MIN_PATH:
            continue
        busy += 1
        xs = [s[1] for s in span]
        ys = [s[2] for s in span]
        if max(max(xs) - min(xs), max(ys) - min(ys)) < STUCK_MAX_BOX:
            stuck += 1
    return stuck / busy if busy else 0.0


def repeat_share(samples, steps) -> float:
    """Share of moving steps retracing an earlier position in the same direction."""
    first_seen = {}
    repeats = 0
    for i, dx, dy in steps:
        _, x, y = samples[i]
        key = (round(x / REPEAT_CELL), round(y / REPEAT_CELL), round(degrees(atan2(dy, dx)) / 45))
        if key in first_seen and i - first_seen[key] >= REPEAT_MIN_GAP:
            repeats += 1
        first_seen.setdefault(key, i)
    return repeats / len(steps)


def window_signatures(samples) -> dict | None:
    """Measurements and looping signatures for one window, or None if too little movement."""
    steps = _moving_steps(samples)
    if len(steps) < MIN_MOVING_STEPS:
        return None
    values = {
        "reversalRate": round(reversal_rate(steps), 4),
        "stuckShare": round(stuck_share(samples), 4),
        "repeatShare": round(repeat_share(samples, steps), 4),
    }
    signatures = []
    if values["reversalRate"] >= MAX_REVERSAL_RATE:
        signatures.append("back_and_forth")
    if values["stuckShare"] >= MAX_STUCK_SHARE:
        signatures.append("stuck_circling")
    if values["repeatShare"] >= MAX_REPEAT_SHARE:
        signatures.append("route_repeat")
    return dict(values, signatures=signatures)


def _heading(samples, i, span=3):
    a, b = samples[max(i - span, 0)], samples[i]
    dx, dy = b[1] - a[1], b[2] - a[2]
    return atan2(dy, dx) if hypot(dx, dy) > 3 else None


def turned_after(samples, i) -> bool | None:
    """Did the player swing more than 90 degrees within RETARGET_AFTER_TICKS of index i?"""
    before = _heading(samples, i)
    if before is None:
        return None  # standing still at the trip: nothing to turn from
    for j in range(i + 3, min(i + RETARGET_AFTER_TICKS, len(samples))):
        after = _heading(samples, j)
        if after is not None and abs((after - before + pi) % (2 * pi) - pi) > RETARGET_TURN:
            return True
    return False


@dataclass
class _Player:
    samples: deque = field(default_factory=lambda: deque(maxlen=WINDOW_TICKS + 1))
    next_eval: int | None = None
    looping_windows: int = 0
    signature_counts: dict = field(default_factory=dict)
    pending_trips: list = field(default_factory=list)
    retarget_turns: int = 0
    retarget_trips: int = 0


@dataclass
class LoopMonitor:
    flag_windows: int = FLAG_WINDOWS
    _players: dict = field(default_factory=dict)
    flagged: dict = field(default_factory=dict)  # player -> the record that flagged them

    def movement(self, player_id: str, sample: dict) -> list[dict]:
        """Feed one position sample; returns records for looping windows or a new flag."""
        p = self._players.setdefault(player_id, _Player())
        tick = sample["tick"]
        p.samples.append((tick, sample["x"], sample["y"]))
        out = []
        retarget = self._settle_trips(player_id, p, tick)
        if retarget:
            out.append(retarget)
        if p.next_eval is None:
            p.next_eval = tick + WINDOW_TICKS
        if tick >= p.next_eval:
            p.next_eval = tick + EVALUATE_EVERY
            window = [s for s in p.samples if s[0] > tick - WINDOW_TICKS]
            result = window_signatures(window)
            if result and result["signatures"]:
                p.looping_windows += 1
                for name in result["signatures"]:
                    p.signature_counts[name] = p.signature_counts.get(name, 0) + 1
                out.append(self._record(player_id, p, tick, dict(result, windowSeconds=WINDOW_TICKS / TICK_HZ)))
        return out

    def trip(self, player_id: str, tick: int) -> None:
        """A trap fired; judge the turn once enough movement after it has arrived."""
        self._players.setdefault(player_id, _Player()).pending_trips.append(tick)

    def _settle_trips(self, player_id, p, tick):
        due = [t for t in p.pending_trips if tick >= t + RETARGET_AFTER_TICKS]
        if not due:
            return None
        p.pending_trips = [t for t in p.pending_trips if tick < t + RETARGET_AFTER_TICKS]
        samples = list(p.samples)
        index = {s[0]: n for n, s in enumerate(samples)}
        for t in due:
            if t not in index:
                continue
            turned = turned_after(samples, index[t])
            if turned is None:
                continue
            p.retarget_trips += 1
            p.retarget_turns += turned
        if (p.retarget_trips >= MIN_RETARGET_TRIPS
                and p.retarget_turns / p.retarget_trips >= MAX_RETARGET_SHARE
                and "trap_retarget" not in p.signature_counts):
            p.signature_counts["trap_retarget"] = 1
            p.looping_windows = max(p.looping_windows, self.flag_windows - 1) + 1
            share = round(p.retarget_turns / p.retarget_trips, 3)
            return self._record(player_id, p, tick, {
                "signatures": ["trap_retarget"], "retargetShare": share,
                "retargetTrips": p.retarget_trips})
        return None

    def _record(self, player_id, p, tick, details):
        newly = p.looping_windows >= self.flag_windows and player_id not in self.flagged
        record = dict(type="loop", playerId=player_id, tick=tick, **details,
                      loopingWindows=p.looping_windows, flagWindows=self.flag_windows,
                      signatureCounts=dict(p.signature_counts),
                      flagged=p.looping_windows >= self.flag_windows)
        if newly:
            self.flagged[player_id] = record
        return record

    def is_flagged(self, player_id: str) -> bool:
        return player_id in self.flagged

    def forget(self, player_id: str) -> None:
        self._players.pop(player_id, None)
        self.flagged.pop(player_id, None)
