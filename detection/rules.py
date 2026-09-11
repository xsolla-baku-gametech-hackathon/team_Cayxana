"""Per-category rules: what a trip means and how long to watch afterwards.

The classifier never inspects the trap. A trap signal only says "this player
tripped category X at tick T", which puts them under observation; the verdict
comes from how they move *after* that moment.

So a rule is not geometry. Per category it answers three questions:

* how long do we watch (a slow-burn trap needs a longer window than a
  reflex one)
* how innocently could a human have tripped this (the excuse), which sets
  how strict the score has to be before the event counts
* is this category trusted to contribute to a flag at all

Thresholds here are starting values, not measurements: no live trips exist
yet. ``calibrate`` documents how to replace them once they do.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import settings as _settings

# 20 ticks/sec, so 200 ticks is the ~10 s of movement the features expect.
DEFAULT_OBSERVATION_TICKS = 200


@dataclass(frozen=True)
class CategoryRule:
    category: str
    observation_ticks: int
    threshold: float
    shadow: bool = False
    excuse: str = ""

    def describe(self) -> str:
        mode = "shadow" if self.shadow else "live"
        return (f"{self.category}: watch {self.observation_ticks} ticks, "
                f"flag above {self.threshold} ({mode}) - excuse: {self.excuse}")


# A category a human can stumble into needs a *stricter* score before the
# event counts. A category a human cannot reach at all can afford to be
# more forgiving of imperfect movement evidence. The trip never adds
# suspicion by itself - it only decides how hard the movement has to argue.
#
# invisible_entity and unreachable_bait were replaced with ``calibrate`` on
# 2026-09-11 real play: 6 keyboard humans, worst scores 0.917 / 0.931, so
# 0.97 / 0.98. In this game the weighted score runs the wrong way (humans
# score higher than every bot farm); at the old 0.80 / 0.70 it put 19 human
# windows over the line and 3 bot windows. Recalibrate when more humans play.
DEFAULT_RULES: dict[str, CategoryRule] = {
    "invisible_entity": CategoryRule(
        "invisible_entity", 200, 0.97,
        excuse="a player can walk over something they cannot see, by accident",
    ),
    "position_offset": CategoryRule(
        "position_offset", 200, 0.75,
        excuse="lag can drag a player toward a stale position without cheating",
    ),
    "phantom_player": CategoryRule(
        "phantom_player", 260, 0.75,
        excuse="a player may run the same way a phantom happens to move",
    ),
    "unreachable_bait": CategoryRule(
        "unreachable_bait", 160, 0.98,
        excuse="hard to trip innocently, so the movement evidence can be weaker",
    ),
}

# Anything the trap system invents later is watched but cannot flag until it
# has been calibrated. An unknown category must never ban anyone.
UNKNOWN_CATEGORY_RULE = CategoryRule(
    "unknown", DEFAULT_OBSERVATION_TICKS, 0.90, shadow=True,
    excuse="not calibrated yet; observed only",
)


def rule_for(category: str, rules: dict[str, CategoryRule] | None = None,
             settings=None) -> CategoryRule:
    """The rule in force for a category, with any tuned threshold applied.

    The threshold can be moved by the adaptive layer; the observation window
    and the shadow flag cannot, because those are policy, not calibration.
    """
    known = DEFAULT_RULES if rules is None else rules
    rule = known.get(category)
    if rule is None:
        rule = CategoryRule(
            category,
            UNKNOWN_CATEGORY_RULE.observation_ticks,
            UNKNOWN_CATEGORY_RULE.threshold,
            shadow=True,
            excuse=UNKNOWN_CATEGORY_RULE.excuse,
        )
    tuned = _settings.rule_threshold(category, rule.threshold, settings)
    return rule if tuned == rule.threshold else replace(rule, threshold=tuned)


def calibrate(scores_by_category: dict[str, list[float]], margin: float = 0.05) -> dict[str, float]:
    """Suggested thresholds from *confirmed human* trips, once they exist.

    Feed the scores of trips known to be human (reviewed and cleared) per
    category; the suggestion sits a margin above the worst of them. This is
    the procedure that replaces the guessed defaults above - never tune a
    threshold against bot scores, only against humans you refused to ban.
    """
    return {
        category: round(min(1.0, max(scores) + margin), 3)
        for category, scores in scores_by_category.items()
        if scores
    }
