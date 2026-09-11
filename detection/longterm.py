"""Continuous post-trap monitoring; missing scores never become zeroes."""
from collections import deque
from dataclasses import dataclass, field
from math import isfinite

from .scoring import classify

WINDOW = 1200  # 60 seconds at 20 Hz; more opportunity to observe pauses
REPORT = 7 * 60 * 20
HORIZON = 9 * 60 * 20
MIN_COVERAGE = 0.80


@dataclass
class Timeline:
    started: int
    end: int
    report_at: int
    last_tick: int = -1
    samples: list = field(default_factory=list)
    scores: deque = field(default_factory=deque)
    last_report: dict | None = None
    flagged: bool = False


def aggregate(rows, expected):
    valid = [score for _, score in rows if score is not None and isfinite(score)]
    return dict(averageScore=sum(valid) / len(valid) if valid else None,
                validWindows=len(valid), expectedWindows=expected,
                coverage=len(valid) / expected if expected else 0)


class LongTermMonitor:
    def __init__(self):
        self.players = {}

    def start(self, player_id, tick):
        self.players.setdefault(player_id, Timeline(tick, tick + WINDOW, tick + REPORT))

    def forget(self, player_id):
        self.players.pop(player_id, None)

    def movement(self, player_id, sample):
        state = self.players.get(player_id)
        if state is None or sample['tick'] <= max(state.last_tick, state.started):
            return []
        tick = sample['tick']
        state.last_tick = tick
        records = []
        # Close missing intervals as unknown instead of carrying a previous score.
        while tick > state.end:
            records.append(self.close(player_id, state))
        state.samples.append(sample)
        if tick == state.end:
            records.append(self.close(player_id, state))
        return records

    def close(self, player_id, state):
        end = state.end
        # A numeric score must represent nearly the whole one-minute window.
        value = classify(state.samples).value if len(state.samples) >= WINDOW * 0.9 else None
        state.scores.append((end, value))
        while state.scores and state.scores[0][0] <= end - HORIZON:
            state.scores.popleft()
        elapsed = end - state.started
        rolling = aggregate(state.scores, min(elapsed, HORIZON) // WINDOW)
        eligible = elapsed >= HORIZON and rolling['coverage'] >= MIN_COVERAGE
        high = eligible and rolling['averageScore'] is not None and rolling['averageScore'] > 0.75
        state.flagged |= high
        report = end >= state.report_at
        if report:
            rows = [(t, s) for t, s in state.scores if end - REPORT < t <= end]
            state.last_report = dict(tick=end, **aggregate(rows, REPORT // WINDOW))
            state.report_at += REPORT
        record = dict(type='long_term', playerId=player_id, tick=end,
                      monitoringSeconds=elapsed / 20, rolling9m=rolling,
                      eligible=eligible, highRisk=high, flagged=state.flagged,
                      report7m=state.last_report, reportUpdated=report,
                      nextReportTick=state.report_at, threshold=0.75,
                      minimumCoverage=MIN_COVERAGE)
        state.samples = []
        state.end += WINDOW
        return record
