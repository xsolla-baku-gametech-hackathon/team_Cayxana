"""Step 1 - behavioural feature extraction.

Every feature may be ``None`` when the trace does not contain enough evidence
to measure it. ``None`` means "unknown" and must be dropped by the scorer;
substituting 0.0 would silently read as bot-like.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import dist

# A step shorter than this counts as standing still.
PAUSE_EPSILON = 0.5
# Grid cell size, in world units, used for revisit counting.
CELL_SIZE = 10.0
# How many consecutive closing steps count as "moving toward the trap".
APPROACH_STEPS = 3


@dataclass(frozen=True)
class Features:
    straightness: float | None
    pause_variance: float | None
    idle_share: float | None
    reaction_ticks: float | None
    revisits: int | None
    samples: int = 0
    span_ticks: int = 0


def _points(trace):
    """Normalise the raw trace into (tick, x, y) tuples, dropping bad samples."""
    points = []
    for sample in trace or ():
        try:
            points.append((int(sample["tick"]), float(sample["x"]), float(sample["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def straightness(points) -> float | None:
    """Displacement / path length. Bot ~0.95, human ~0.5, closed loop ~0."""
    if len(points) < 2:
        return None
    path = sum(dist(a[1:], b[1:]) for a, b in zip(points, points[1:]))
    if path == 0.0:  # never moved: nothing to measure
        return None
    return dist(points[0][1:], points[-1][1:]) / path


def pause_variance(points) -> float | None:
    """Variance of pause durations in ticks. The strongest single feature.

    Human pauses are ragged (high variance); a scripted loop pauses for the
    same number of ticks every time (~0).
    """
    if len(points) < 2:
        return None

    pauses, current = [], 0.0
    for a, b in zip(points, points[1:]):
        elapsed = b[0] - a[0]
        if dist(a[1:], b[1:]) < PAUSE_EPSILON:
            current += elapsed  # tick-based, so lag gaps do not distort it
        elif current:
            pauses.append(current)
            current = 0.0
    if current:
        pauses.append(current)

    if len(pauses) < 2:
        return None
    mean = sum(pauses) / len(pauses)
    return sum((p - mean) ** 2 for p in pauses) / len(pauses)


def idle_share(points) -> float | None:
    """Share of the observed span the player spent standing still.

    ``pause_variance`` answers "how ragged was the rhythm?" and has to
    refuse a trace with fewer than two pauses in it. That refusal is the
    hole a farming bot walks through: it never stops at all, so it has no
    rhythm to be ragged, and the strongest fact about it - that it never
    rests - was being recorded as "unknown".

    This measures the same behaviour as a proportion instead of a variance,
    so it survives where the variance cannot: zero pauses is a measurement,
    not a missing value. Every human population measured spends at least a
    tenth of a window still; a bot walking loot to loot spends none.
    """
    if len(points) < 2:
        return None
    still = 0.0
    for a, b in zip(points, points[1:]):
        if dist(a[1:], b[1:]) < PAUSE_EPSILON:
            still += b[0] - a[0]
    span = points[-1][0] - points[0][0]
    if span <= 0:
        return None
    return min(still / span, 1.0)


def reaction_ticks(points, trap_event) -> float | None:
    """Ticks between the trap firing and the player committing to it.

    "Committing" = the distance to the trap shrinks for APPROACH_STEPS
    consecutive samples. Bot 1-2 ticks, human 15-40.
    """
    if not trap_event:
        return None
    try:
        trap_tick = int(trap_event["tick"])
        trap_xy = (float(trap_event["x"]), float(trap_event["y"]))
    except (KeyError, TypeError, ValueError):
        return None

    after = [p for p in points if p[0] >= trap_tick]
    if len(after) < APPROACH_STEPS + 1:
        return None

    distances = [dist(p[1:], trap_xy) for p in after]
    closing = 0
    for i in range(len(distances) - 1):
        closing = closing + 1 if distances[i + 1] < distances[i] else 0
        if closing == APPROACH_STEPS:
            start = after[i + 1 - APPROACH_STEPS][0]
            return float(start - trap_tick)
    return None


def revisits(points) -> int | None:
    """How often the player re-enters a grid cell they already left."""
    if not points:
        return None

    seen, count, previous = set(), 0, None
    for _, x, y in points:
        cell = (int(x // CELL_SIZE), int(y // CELL_SIZE))
        if cell != previous:
            if cell in seen:
                count += 1
            seen.add(cell)
            previous = cell
    return count


def extract_features(trace, trap_event=None) -> Features:
    points = _points(trace)
    return Features(
        straightness=straightness(points),
        pause_variance=pause_variance(points),
        idle_share=idle_share(points),
        reaction_ticks=reaction_ticks(points, trap_event),
        revisits=revisits(points),
        samples=len(points),
        span_ticks=points[-1][0] - points[0][0] if len(points) > 1 else 0,
    )
