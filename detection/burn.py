"""Step 5 - detect that a trap category has been defeated.

A category is "burned" when cheat developers ship a patch that recognises it.
The signature is not a low catch rate on its own - it is a low catch rate
appearing across many independent sessions at the same time. A drop inside a
single session is noise: that player may simply not be a bot.

Guarding on the multi-session condition, plus a minimum sample size, is what
stops the detector from panicking at its own noise and disabling traps that
still work.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

WINDOW = 3600.0  # one hour of live traffic per window
HISTORY_WINDOWS = 5  # how many earlier windows form the baseline
MIN_TRIGGERS = 50  # below this the window is too small to judge
MIN_SESSIONS = 20  # a patch shows up across many players at once
MIN_SESSION_TRIGGERS = 3  # per-session evidence before it counts as affected
AFFECTED_SHARE = 0.5  # fraction of sessions that must have gone silent
BURN_RATIO = 0.2  # burned if current rate < 20% of baseline


@dataclass(frozen=True)
class BurnVerdict:
    burned: bool
    reason: str
    current_rate: float | None = None
    baseline: float | None = None
    sessions: int = 0
    affected_sessions: int = 0


@dataclass(frozen=True)
class _Trigger:
    at: float
    category: str
    session: str
    caught: bool


class BurnMonitor:
    """Rolling per-category trap outcomes, with a burn verdict on demand."""

    def __init__(self, window: float = WINDOW, history_windows: int = HISTORY_WINDOWS):
        self.window = window
        self.retention = window * (history_windows + 1)
        self._events: deque[_Trigger] = deque()

    def record(self, category: str, session: str, caught: bool, at: float) -> None:
        self._events.append(_Trigger(at, category, session, caught))
        cutoff = at - self.retention
        while self._events and self._events[0].at < cutoff:
            self._events.popleft()

    def _slice(self, category: str, start: float, end: float) -> list[_Trigger]:
        return [e for e in self._events if e.category == category and start < e.at <= end]

    def evaluate(self, category: str, now: float) -> BurnVerdict:
        current = self._slice(category, now - self.window, now)
        if len(current) < MIN_TRIGGERS:
            return BurnVerdict(False, "insufficient sample in current window")

        earlier = self._slice(category, now - self.retention, now - self.window)
        if len(earlier) < MIN_TRIGGERS:
            return BurnVerdict(False, "no trustworthy baseline yet")

        current_rate = sum(e.caught for e in current) / len(current)
        baseline = sum(e.caught for e in earlier) / len(earlier)
        if baseline == 0.0:
            return BurnVerdict(False, "category never worked", current_rate, baseline)
        if current_rate >= BURN_RATIO * baseline:
            return BurnVerdict(False, "catch rate holding", current_rate, baseline)

        per_session: dict[str, list[bool]] = {}
        for e in current:
            per_session.setdefault(e.session, []).append(e.caught)
        judged = {s: hits for s, hits in per_session.items() if len(hits) >= MIN_SESSION_TRIGGERS}
        affected = [s for s, hits in judged.items() if not any(hits)]

        stats = dict(
            current_rate=current_rate,
            baseline=baseline,
            sessions=len(judged),
            affected_sessions=len(affected),
        )
        if len(affected) < MIN_SESSIONS:
            # One or a few quiet sessions is normal variation, not a patch.
            return BurnVerdict(False, "drop not widespread across sessions", **stats)
        if len(affected) < AFFECTED_SHARE * len(judged):
            return BurnVerdict(False, "most sessions still catching", **stats)

        return BurnVerdict(True, "simultaneous drop across sessions", **stats)

    def sweep(self, stats: dict, now: float) -> list[str]:
        """Burn every defeated category; returns the names newly burned."""
        newly = []
        for name, arm in stats.items():
            if not arm.burned and self.evaluate(name, now).burned:
                arm.burned = True  # weight goes to ~0; the bandit stops deploying it
                newly.append(name)
        return newly
