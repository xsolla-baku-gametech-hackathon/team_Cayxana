"""Step 2 - turn features into a machine-likeness score in 0..1.

A weighted sum of five clamped features. No model, no I/O, deterministic,
microseconds per call. Closer to 1 means more machine-like.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from . import settings as _settings
from .features import Features, _points, extract_features
from .signatures import detect

# Weights follow the separation measured across all labelled populations,
# not intuition. Pause rhythm carries most of the signal; revisit count
# barely separates at all (an open-field human revisits as little as a bot),
# so it is kept only as a tie-breaker.
WEIGHTS = {
    "straightness": 0.22,
    "pause_variance": 0.30,
    # Rhythm split in two: how ragged the pauses were (refusable) and how
    # much of the window was spent still at all (always measurable). The
    # second one is what keeps a never-resting bot scoreable; see
    # features.idle_share.
    "idle_share": 0.18,
    "reaction_ticks": 0.15,
    "revisits": 0.15,
}

# Saturation points: at or beyond these values a feature is fully human-like
# and stops pushing the score up. Set from the lowest human medians, not from
# the widest gap - a smooth-analogue or expert player sits close to the bots.
SATURATION = {
    "pause_variance": 3.0,
    # Deliberately well under every measured human idle share (the lowest
    # population sits near 0.16), so only a player who essentially never
    # stops reads as machine-like on this term. A bot that fakes pauses is
    # left to pause_variance and the signatures.
    "idle_share": 0.06,
    "reaction_ticks": 40.0,
    "revisits": 12.0,
}

# Scoring a trace on one or two surviving features produces confident
# nonsense: a player who joined seconds ago has a straight path and no
# rhythm yet, which reads as a perfect bot. Demand real evidence instead.
MIN_FEATURES = 3
# The floor a trace has to clear to be scored at all. It sits under the
# weight of the three features that survive a window with no trap event
# and no pause in it (straightness + idle share + revisits), because that
# combination *is* evidence - it was only ever refused as a side effect of
# pause rhythm being the one heavy feature that can vanish. A trace with no
# movement in it still loses straightness and is refused.
MIN_WEIGHT = 0.50
# Whatever the weights say, a score needs at least one rhythm measurement
# behind it: how ragged the pauses were, or how much of the window was
# spent still. Straightness and revisits alone describe a shape, not a
# behaviour, and a corridor produces both.
RHYTHM_FEATURES = ("pause_variance", "idle_share")
# A trace also has to be long enough to contain a rhythm at all.
MIN_SAMPLES = 40
MIN_SPAN_TICKS = 40

# Above this a single event counts as suspicious. Chosen for zero false
# positives across every labelled human population, not for maximum catches.
SUSPICIOUS = 0.75


@dataclass(frozen=True)
class Score:
    """``value`` is None when the trace carried no usable feature at all.

    ``signatures`` lists machine behaviours found in the trace. They are
    independent of the score: a trace can be refused as insufficient for
    scoring and still carry a conclusive signature.
    """

    value: float | None
    features: Features
    used: tuple[str, ...]
    signatures: tuple[str, ...] = ()

    @property
    def insufficient_data(self) -> bool:
        return self.value is None

    def is_suspicious(self, threshold: float | None = None) -> bool:
        """Either a machine signature, or an overall score above threshold."""
        if self.signatures:
            return True
        if threshold is None:
            threshold = _settings.live().suspicious
        return self.value is not None and self.value > threshold


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _machine_likeness(name: str, raw: float, saturation=SATURATION) -> float:
    """Map one raw feature to 0..1 where 1 = machine-like."""
    if name == "straightness":
        return _clamp01(raw)  # straight already means machine-like
    # The others are human-like when large, so they are inverted.
    return 1.0 - _clamp01(raw / saturation[name])


def _has_rhythm(names) -> bool:
    return any(name in names for name in RHYTHM_FEATURES)


def score_features(features: Features, signatures: tuple[str, ...] = (),
                   settings=None) -> Score:
    """``settings`` is the number set to score with; None means the live one."""
    current = _settings.live() if settings is None else settings
    weights, saturation = current.normalised_weights(), current.saturation
    if features.samples < MIN_SAMPLES or features.span_ticks < MIN_SPAN_TICKS:
        return Score(None, features, (), signatures)  # too little observed

    contributions, weight_sum, used = 0.0, 0.0, []
    for name, weight in weights.items():
        raw = getattr(features, name)
        if raw is None:
            continue  # unknown: drop the term rather than read it as bot-like
        contributions += weight * _machine_likeness(name, float(raw), saturation)
        weight_sum += weight
        used.append(name)

    if len(used) < MIN_FEATURES or weight_sum < MIN_WEIGHT or not _has_rhythm(used):
        return Score(None, features, tuple(used), signatures)  # not enough evidence
    return Score(contributions / weight_sum, features, tuple(used), signatures)


def classify(trace, trap_event=None, settings=None) -> Score:
    points = _points(trace)
    features = extract_features(trace, trap_event)
    return score_features(features, detect(features, points, settings), settings)


def score_details(features: Features, settings=None) -> dict:
    """Display-only partial estimate. Never replaces Score.value in decisions."""
    current = _settings.live() if settings is None else settings
    weights, saturation = current.normalised_weights(), current.saturation
    available = {name: getattr(features, name) for name in weights
                 if getattr(features, name) is not None and isfinite(getattr(features, name))}
    missing = [name for name in weights if name not in available]
    weight = sum(weights[name] for name in available)
    reason = 'complete'
    partial = None
    if features.samples < MIN_SAMPLES or features.span_ticks < MIN_SPAN_TICKS:
        reason = 'short_trace'
    elif features.straightness is None:
        reason = 'no_movement'
    elif (len(available) < MIN_FEATURES or weight < MIN_WEIGHT
          or not _has_rhythm(available)):
        reason = 'missing_features'
        if len(available) >= 2 and weight >= 0.44:
            partial = sum(weights[name] * _machine_likeness(name, raw, saturation)
                          for name, raw in available.items()) / weight
    return dict(partialScore=partial, featureCoverage=round(weight, 2),
                missingFeatures=missing, scoreStatus=reason)
