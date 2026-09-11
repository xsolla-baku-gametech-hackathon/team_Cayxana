"""Trap-trip rate: how often a player walks into traps, not how they move.

Movement shape barely separates a keyboard player from a script in this
game (digital input, fixed speed). What a script does that a person does not
is keep walking into things: a farm reading raw state goes straight for
every ghost coin it is sent.

Measured 2026-09-11 over the most trips in any 60 s window:
    6 real humans (10 min each)      3-6
    naive farm (5 bots, 5 min)       67-100
    evasive farm                     0-12
    human-imitating farm             3-8
The threshold sits 2.5x above the worst human and far below the naive farm.
It says nothing about the evasive or human-imitating farms; ``periodic``
and the traps themselves have to carry those.

Every trip counts, including re-trips while an observation window is open:
the Detector ignores those as movement evidence, but they are exactly what
this rate measures.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

WINDOW_TICKS = 60 * 20
MAX_TRIPS = 15


@dataclass
class TrapRateMonitor:
    window_ticks: int = WINDOW_TICKS
    max_trips: int = MAX_TRIPS
    _trips: dict = field(default_factory=dict)
    flagged: dict = field(default_factory=dict)  # player -> the record that flagged them

    def trip(self, player_id: str, tick: int) -> dict | None:
        """Record one trip. Returns a record the first time a player crosses the line."""
        trips = self._trips.setdefault(player_id, deque())
        trips.append(tick)
        while trips and tick - trips[0] >= self.window_ticks:
            trips.popleft()
        if len(trips) < self.max_trips or player_id in self.flagged:
            return None
        record = {"type": "trap_rate", "playerId": player_id, "tick": tick,
                  "trips": len(trips), "windowSeconds": self.window_ticks / 20,
                  "maxTrips": self.max_trips, "flagged": True}
        self.flagged[player_id] = record
        return record

    def is_flagged(self, player_id: str) -> bool:
        return player_id in self.flagged

    def forget(self, player_id: str) -> None:
        self._trips.pop(player_id, None)
        self.flagged.pop(player_id, None)
