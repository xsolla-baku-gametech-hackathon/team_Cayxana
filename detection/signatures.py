"""Narrow machine signatures: things no human movement produces.

The weighted score in ``scoring`` asks "how human-like was this, overall?".
That question averages, and averaging hides a specialist: a bot that fakes
human pauses but moves at a mathematically constant speed scores mid-range
forever and is never flagged.

A signature asks a different question: "did this trace contain something a
human physically cannot do?" Each one is narrow, independent, and tuned so
that it fires on *no* human population at all. Any single one firing is
enough to mark the observation suspicious - the three-distinct-category
rule is what still protects the player from a ban.

Every detector returns None when it cannot judge, and refusing is always
preferred to guessing.
"""

from __future__ import annotations

import statistics as st
from collections import Counter
from math import atan2, dist, pi

from . import settings as _settings
from .features import PAUSE_EPSILON, Features, enough_evidence

# Thresholds sit far outside every human population measured, not next to
# them; see tests/test_signatures.py, which fails if a human ever trips one.
# These are the defaults; the numbers actually used come from ``settings``,
# so the adaptive layer can tighten one after it has proved the tighter
# value on recorded traces. A detector reads its threshold per call.
MAX_CONSTANT_SPEED_CV = 0.05
MIN_RAILS_SHARE = 0.30
# Calibrated on real play (2026-09-11): 6 keyboard humans, 85 windows, and
# the headless farm in all three modes. At 0.80: humans 1/85, the
# human-imitating farm 41/50. At the old 0.65 humans were 11/85.
MIN_PERIODICITY = 0.80
MAX_FROZEN_HEADING_VAR = 0.001
MIN_QUANTISED_SHARE = 0.60
MAX_METRONOME_VARIANCE = 0.5
MIN_PAUSES_FOR_RHYTHM = 4
MAX_REGULAR_GAP = 2

# Each detector's threshold, by the name ``settings`` knows it under.
# ``_threshold`` falls back to these when a set of settings predates the
# knob - which is how a detector that is not currently deployed (see
# DISABLED_FOR_DIGITAL_INPUT) stays callable instead of raising KeyError.
DEFAULT_THRESHOLDS = {
    "max_constant_speed_cv": MAX_CONSTANT_SPEED_CV,
    "min_rails_share": MIN_RAILS_SHARE,
    "min_periodicity": MIN_PERIODICITY,
    "max_frozen_heading_var": MAX_FROZEN_HEADING_VAR,
    "min_quantised_share": MIN_QUANTISED_SHARE,
    "max_metronome_variance": MAX_METRONOME_VARIANCE,
}


def _steps(points):
    """(distance, tick gap) for each consecutive pair of samples."""
    return [(dist(a[1:], b[1:]), max(b[0] - a[0], 1)) for a, b in zip(points, points[1:])]


def _moving_headings(points):
    moving = [(a, b) for a, b in zip(points, points[1:]) if dist(a[1:], b[1:]) >= PAUSE_EPSILON]
    return [atan2(b[2] - a[2], b[1] - a[1]) for a, b in moving]


def _turns(headings):
    return [(cur - prev + pi) % (2 * pi) - pi for prev, cur in zip(headings, headings[1:])]


def regular_cadence(points) -> bool:
    """False when samples arrive unevenly, as under lag or packet loss.

    Rhythm cannot be measured on a stream with holes in it: dropped packets
    manufacture pauses of equal length that look exactly like a metronome.
    This guard is the reason lagging players survive the rhythm detector.
    """
    gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
    return len(gaps) >= 10 and max(gaps) <= MAX_REGULAR_GAP


def pause_count(points) -> int:
    count, run = 0, 0
    for a, b in zip(points, points[1:]):
        if dist(a[1:], b[1:]) < PAUSE_EPSILON:
            run += 1
        elif run:
            count, run = count + 1, 0
    return count + (1 if run else 0)


# -- the measurements --------------------------------------------------------

def speed_cv(points) -> float | None:
    """Coefficient of variation of speed while moving.

    Human speed never repeats exactly - analogue input, acceleration, and
    collision all perturb it. A scripted mover advances by a fixed amount.
    """
    speeds = [d / g for d, g in _steps(points) if d >= PAUSE_EPSILON]
    if len(speeds) < 5 or st.mean(speeds) == 0:
        return None
    return st.pstdev(speeds) / st.mean(speeds)


def step_mode_share(points) -> float | None:
    """Share of moving steps landing on the single most common length."""
    lengths = [round(d / g, 2) for d, g in _steps(points) if d >= PAUSE_EPSILON]
    if len(lengths) < 5:
        return None
    return Counter(lengths).most_common(1)[0][1] / len(lengths)


def heading_change_variance(points) -> float | None:
    """Variance of per-step turn angle. Humans wobble; rails do not."""
    turns = _turns(_moving_headings(points))
    return st.pvariance(turns) if len(turns) >= 5 else None


def straight_run_share(points) -> float | None:
    """Longest perfectly straight run as a share of all moving steps."""
    headings = _moving_headings(points)
    if len(headings) < 6:
        return None
    best = run = 1
    for delta in _turns(headings):
        run = run + 1 if abs(delta) < 0.01 else 1
        best = max(best, run)
    return best / len(headings)


def speed_periodicity(points) -> float | None:
    """Strongest autocorrelation of the speed series over lags 2..30.

    "Move N ticks, pause M ticks, repeat" scores high. Humans do not repeat.
    """
    speeds = [d / g for d, g in _steps(points)]
    if len(speeds) < 40:
        return None
    mean = st.mean(speeds)
    centred = [s - mean for s in speeds]
    denominator = sum(c * c for c in centred)
    if denominator == 0:
        return None
    return max(
        sum(centred[i] * centred[i + lag] for i in range(len(centred) - lag)) / denominator
        for lag in range(2, min(31, len(centred) // 2))
    )


# -- the detectors -----------------------------------------------------------

def _threshold(name: str, settings) -> float:
    return _settings.resolve(settings).signature.get(name, DEFAULT_THRESHOLDS[name])


def metronome(features: Features, points, settings=None) -> bool | None:
    """Pauses of identical length: a scripted loop with a fixed dwell."""
    if features.pause_variance is None or pause_count(points) < MIN_PAUSES_FOR_RHYTHM:
        return None
    if not regular_cadence(points):
        return None
    return features.pause_variance < _threshold("max_metronome_variance", settings)


def constant_speed(features: Features, points, settings=None) -> bool | None:
    """Speed does not vary at all: a fixed advance per tick."""
    cv = speed_cv(points)
    return None if cv is None else cv < _threshold("max_constant_speed_cv", settings)


def rails(features: Features, points, settings=None) -> bool | None:
    """Long perfectly straight runs: a path followed, not steered."""
    share = straight_run_share(points)
    return None if share is None else share > _threshold("min_rails_share", settings)


def periodic(features: Features, points, settings=None) -> bool | None:
    """The speed series repeats on a fixed cycle."""
    value = speed_periodicity(points)
    return None if value is None else value > _threshold("min_periodicity", settings)


def frozen_heading(features: Features, points, settings=None) -> bool | None:
    """Heading never changes while moving."""
    variance = heading_change_variance(points)
    return None if variance is None else variance < _threshold("max_frozen_heading_var", settings)


def quantised_steps(features: Features, points, settings=None) -> bool | None:
    """Nearly every step lands on the exact same length."""
    share = step_mode_share(points)
    return None if share is None else share > _threshold("min_quantised_share", settings)


# Which knob controls which detector, and which way to turn it so the
# detector fires *less*. Written next to the detectors so the two cannot
# drift apart, and rendered into the tuning prompt: "this signature is
# firing on humans" is only useful next to "and this is the knob for it".
KNOBS = {
    "metronome": ("signature.max_metronome_variance", "lower"),
    "rails": ("signature.min_rails_share", "raise"),
    "periodic": ("signature.min_periodicity", "raise"),
    "frozen_heading": ("signature.max_frozen_heading_var", "lower"),
}

# Every signature that exists, deployed or not.
ALL_DETECTORS = {
    "metronome": metronome,
    "constant_speed": constant_speed,
    "rails": rails,
    "periodic": periodic,
    "frozen_heading": frozen_heading,
    "quantised_steps": quantised_steps,
}

# ``constant_speed`` and ``quantised_steps`` are written above but not run.
# In this game input is -1/0/+1 per axis at a fixed PLAYER_SPEED, so every
# keyboard player moves at constant speed in identical steps. Measured on
# real play they fired on 98-100% of humans AND of every bot mode: they
# separate nothing and flagged every real person. They stay defined and
# callable so the finding can be re-measured on an engine with analogue
# input, where they would separate again.
DISABLED_FOR_DIGITAL_INPUT = ("constant_speed", "quantised_steps")

# What ``detect`` actually runs. Derived, so the exclusion above is the
# single statement of which signatures are deployed - it used to be stated
# twice, once in prose and once in a hand-written dict.
DETECTORS = {name: fn for name, fn in ALL_DETECTORS.items()
             if name not in DISABLED_FOR_DIGITAL_INPUT}


def detect(features: Features, points, settings=None) -> tuple[str, ...]:
    """Names of every signature present. Empty means nothing conclusive."""
    # A signature is conclusive, so it needs at least as much evidence as a
    # score does - the same floor, shared with ``scoring``. Without it a
    # five-sample trace "proves" constant speed.
    if not enough_evidence(features):
        return ()
    return tuple(name for name, fn in DETECTORS.items() if fn(features, points, settings))
