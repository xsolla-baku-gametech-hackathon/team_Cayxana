"""The tick domain, in one place.

The game server runs an authoritative loop at a fixed rate and every module
here measures time in its ticks. That rate was written out longhand in six
places - ``72_000``, ``60 * 20``, a bare ``/ 20``, ``TICK_HZ = 20`` - so a
server running at a different rate would have had to be found six times.

Nothing here decides anything. It is the unit, and the conversions between
it and the seconds a person reads on a dashboard.
"""

from __future__ import annotations

# The server's tick loop. Every window length below is derived from it.
TICK_HZ = 20

TICKS_PER_SECOND = TICK_HZ
TICKS_PER_MINUTE = 60 * TICK_HZ
TICKS_PER_HOUR = 3600 * TICK_HZ  # 72_000


def seconds(ticks: float) -> float:
    """Ticks as seconds, for anything a person reads."""
    return ticks / TICK_HZ


def ticks(seconds: float) -> int:
    """Seconds as whole ticks, for a window length written in seconds."""
    return int(seconds * TICK_HZ)
