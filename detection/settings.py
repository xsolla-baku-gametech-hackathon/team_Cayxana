"""One place every tunable number is read from, so a layer can change them.

Until now the weights, saturation points and signature thresholds lived as
module constants. They still do - those constants are the *defaults* - but
every read now goes through a ``Settings`` value, which lets two things
happen that a constant cannot:

* the adaptive layer can evaluate a *candidate* set of numbers against
  recorded traces without touching what the live detector is using (the
  live path keeps running in another thread while that happens), and
* an accepted candidate can be swapped in atomically, by replacing one
  object reference, with the previous one kept for rollback.

Every knob has hard bounds. A value outside them is clamped, not applied,
and ``MAX_STEP`` limits how far one round may move a knob from where it
already is: a model that answers "threshold: 0.0" moves nothing far.

Nothing here decides anything. It holds numbers and refuses silly ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# Hard bounds per knob. Outside these a value is not a tuning decision, it
# is a bug or a bad answer: clamp it. The ranges are wide enough to be
# useful and narrow enough that no setting inside them disables detection.
BOUNDS: dict[str, tuple[float, float]] = {
    "weights.straightness": (0.05, 0.60),
    "weights.pause_variance": (0.05, 0.60),
    "weights.idle_share": (0.05, 0.60),
    "weights.reaction_ticks": (0.05, 0.60),
    "weights.revisits": (0.05, 0.60),
    "saturation.pause_variance": (1.0, 12.0),
    # Upper bound stays under the lowest human population idle share, so no
    # tuning round can widen this term into the human range.
    "saturation.idle_share": (0.01, 0.12),
    "saturation.reaction_ticks": (10.0, 120.0),
    "saturation.revisits": (4.0, 40.0),
    "suspicious": (0.60, 0.95),
    "signature.min_rails_share": (0.20, 0.95),
    "signature.min_periodicity": (0.40, 0.99),
    "signature.max_frozen_heading_var": (0.0001, 0.05),
    "signature.max_metronome_variance": (0.05, 5.0),
}
RULE_THRESHOLD_BOUNDS = (0.55, 0.95)

# How far one round may move a knob, as a share of its current value. A
# slow walk is recoverable and visible in the log; a jump is neither.
MAX_STEP = 0.50


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


@dataclass(frozen=True)
class Settings:
    """An immutable snapshot of every number the classifier reads."""

    weights: dict[str, float]
    saturation: dict[str, float]
    suspicious: float
    signature: dict[str, float]
    # Per-category score threshold overrides; a category missing here keeps
    # the threshold its rule was written with.
    rule_thresholds: dict[str, float] = field(default_factory=dict)

    def weight(self, name: str) -> float:
        return self.weights[name]

    def normalised_weights(self) -> dict[str, float]:
        total = sum(self.weights.values()) or 1.0
        return {name: value / total for name, value in self.weights.items()}

    def with_overrides(self, overrides: dict[str, float]) -> "Settings":
        """A copy with ``knob -> value`` applied, clamped and step-limited.

        Unknown knobs are ignored rather than raising: this is fed by a
        model, and one bad key must not lose the good ones beside it.
        """
        weights = dict(self.weights)
        saturation = dict(self.saturation)
        signature = dict(self.signature)
        rules = dict(self.rule_thresholds)
        suspicious = self.suspicious

        for knob, raw in overrides.items():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            group, _, rest = knob.partition(".")
            if group == "weights" and rest in weights:
                weights[rest] = _step(weights[rest], value, BOUNDS[knob])
            elif group == "saturation" and rest in saturation:
                saturation[rest] = _step(saturation[rest], value, BOUNDS[knob])
            elif group == "signature" and rest in signature:
                signature[rest] = _step(signature[rest], value, BOUNDS[knob])
            elif knob == "suspicious":
                suspicious = _step(suspicious, value, BOUNDS[knob])
            elif group == "rule" and rest.endswith(".threshold"):
                category = rest[: -len(".threshold")]
                current = rules.get(category, written_rule_threshold(category))
                rules[category] = _step(current, value, RULE_THRESHOLD_BOUNDS)
        return Settings(weights, saturation, suspicious, signature, rules)

    def diff(self, other: "Settings") -> dict[str, tuple[float, float]]:
        """``knob -> (before, after)`` for everything that actually moved."""
        moved = {}
        for group, mine, theirs in (
            ("weights", self.weights, other.weights),
            ("saturation", self.saturation, other.saturation),
            ("signature", self.signature, other.signature),
        ):
            for name, value in theirs.items():
                if mine.get(name) != value:
                    moved[f"{group}.{name}"] = (mine.get(name), value)
        if self.suspicious != other.suspicious:
            moved["suspicious"] = (self.suspicious, other.suspicious)
        for category, value in other.rule_thresholds.items():
            before = self.rule_thresholds.get(category, written_rule_threshold(category))
            if before != value:
                moved[f"rule.{category}.threshold"] = (before, value)
        return moved

    def as_dict(self) -> dict:
        return {"weights": dict(self.weights), "saturation": dict(self.saturation),
                "suspicious": self.suspicious, "signature": dict(self.signature),
                "rule_thresholds": dict(self.rule_thresholds)}

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        """Rebuild from a persisted dict, ignoring anything unrecognised."""
        base = defaults()
        overrides = {}
        for group in ("weights", "saturation", "signature"):
            for name, value in (raw.get(group) or {}).items():
                overrides[f"{group}.{name}"] = value
        if "suspicious" in raw:
            overrides["suspicious"] = raw["suspicious"]
        for category, value in (raw.get("rule_thresholds") or {}).items():
            overrides[f"rule.{category}.threshold"] = value
        # A persisted file is a resume, not a tuning round: allow it to sit
        # anywhere inside the bounds rather than one step from the default.
        return _unstepped(base, overrides)


DEFAULT_RULE_THRESHOLD = 0.75


def written_rule_threshold(category: str) -> float:
    """The threshold the category's rule was written with, before tuning."""
    from .rules import DEFAULT_RULES, UNKNOWN_CATEGORY_RULE

    rule = DEFAULT_RULES.get(category)
    return rule.threshold if rule else UNKNOWN_CATEGORY_RULE.threshold


def _step(current: float, target: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    span = abs(current) * MAX_STEP or (high - low) * MAX_STEP
    return round(clamp(clamp(target, current - span, current + span), low, high), 5)


def _unstepped(base: "Settings", overrides: dict) -> "Settings":
    weights, saturation = dict(base.weights), dict(base.saturation)
    signature, rules = dict(base.signature), dict(base.rule_thresholds)
    suspicious = base.suspicious
    for knob, raw in overrides.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        group, _, rest = knob.partition(".")
        if group == "weights" and rest in weights:
            weights[rest] = clamp(value, *BOUNDS[knob])
        elif group == "saturation" and rest in saturation:
            saturation[rest] = clamp(value, *BOUNDS[knob])
        elif group == "signature" and rest in signature:
            signature[rest] = clamp(value, *BOUNDS[knob])
        elif knob == "suspicious":
            suspicious = clamp(value, *BOUNDS[knob])
        elif group == "rule" and rest.endswith(".threshold"):
            rules[rest[: -len(".threshold")]] = clamp(value, *RULE_THRESHOLD_BOUNDS)
    return Settings(weights, saturation, suspicious, signature, rules)


def defaults() -> Settings:
    """The hand-written numbers, read from the modules that document them."""
    from . import scoring, signatures

    return Settings(
        weights=dict(scoring.WEIGHTS),
        saturation=dict(scoring.SATURATION),
        suspicious=scoring.SUSPICIOUS,
        signature={
            "min_rails_share": signatures.MIN_RAILS_SHARE,
            "min_periodicity": signatures.MIN_PERIODICITY,
            "max_frozen_heading_var": signatures.MAX_FROZEN_HEADING_VAR,
            "max_metronome_variance": signatures.MAX_METRONOME_VARIANCE,
        },
        rule_thresholds={},
    )


_live: Settings | None = None


def live() -> Settings:
    """What the detection path is using right now."""
    global _live
    if _live is None:
        _live = defaults()
    return _live


def set_live(settings: Settings) -> Settings:
    """Swap in a new set. Returns the previous one, for rollback."""
    global _live
    previous = live()
    _live = settings
    return previous


def reset() -> Settings:
    """Back to the hand-written defaults."""
    return set_live(defaults())


def rule_threshold(category: str, written: float, settings: Settings | None = None) -> float:
    """The threshold in force for a category: an override, or the rule's own."""
    current = live() if settings is None else settings
    return current.rule_thresholds.get(category, written)


__all__ = ["Settings", "BOUNDS", "MAX_STEP", "defaults", "live", "set_live",
           "reset", "rule_threshold", "clamp", "replace"]
