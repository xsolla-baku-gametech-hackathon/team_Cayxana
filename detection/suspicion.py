"""Step 3 - accumulate trap events into a flag.

A single trap never bans anyone. A flag needs agreement between at least
three suspicious events drawn from three *different* trap categories: one
mechanism can be bad luck or lag, three agreeing mechanisms cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scoring import SUSPICIOUS, Score

REQUIRED_EVENTS = 3
REQUIRED_CATEGORIES = 3


@dataclass(frozen=True)
class SuspicionEvent:
    category: str
    score: float | None
    tick: int
    # Decided by that category's own rule, since what counts as machine-like
    # depends on how innocently the category can be tripped.
    suspicious: bool = True


@dataclass
class PlayerSuspicion:
    player_id: str
    events: list[SuspicionEvent] = field(default_factory=list)
    flagged: bool = False

    def suspicious_events(self) -> list[SuspicionEvent]:
        return [e for e in self.events if e.suspicious]


class SuspicionStore:
    """In-memory suspicion per player. One instance per game server."""

    def __init__(self, threshold: float = SUSPICIOUS, shadow_categories=(),
                 required_events: int = REQUIRED_EVENTS, required_categories: int = REQUIRED_CATEGORIES):
        if not 1 <= required_categories <= required_events:
            raise ValueError("Require 1 <= categories <= events")
        self.required_events = required_events
        self.required_categories = required_categories
        self.threshold = threshold
        # Shadow categories are recorded for their statistics but may never
        # contribute to a flag until they have been promoted.
        self.shadow_categories = set(shadow_categories)
        self._players: dict[str, PlayerSuspicion] = {}

    def get(self, player_id: str) -> PlayerSuspicion:
        return self._players.setdefault(player_id, PlayerSuspicion(player_id))

    def record(self, player_id: str, category: str, score: Score | float, tick: int,
               suspicious: bool | None = None) -> PlayerSuspicion:
        """Record one observation and re-evaluate the flag rule.

        ``suspicious`` comes from the category rule. Without one, the store
        falls back to its own threshold.
        """
        value = score.value if isinstance(score, Score) else score
        if suspicious is None:
            suspicious = value is not None and value > self.threshold
        if value is None and not suspicious:
            return self.get(player_id)  # insufficient data is not evidence
        player = self.get(player_id)
        player.events.append(SuspicionEvent(
            category, None if value is None else float(value), int(tick), suspicious,
        ))
        player.flagged = self._should_flag(player)
        return player

    def _should_flag(self, player: PlayerSuspicion) -> bool:
        hits = [
            e for e in player.suspicious_events()
            if e.category not in self.shadow_categories
        ]
        if len(hits) < self.required_events:
            return False
        return len({e.category for e in hits}) >= self.required_categories

    def clear(self, player_id: str) -> None:
        self._players.pop(player_id, None)
