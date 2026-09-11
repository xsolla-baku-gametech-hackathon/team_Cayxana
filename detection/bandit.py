"""Step 4 - Thompson sampling over trap categories.

Each category is a Beta(caught + 1, missed + 1) arm. Sampling keeps a weak
category alive with small probability, which is what lets the system notice
later that a category started working again.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# A wrongly flagged human costs this many "misses". The penalty is deliberately
# asymmetric: we would rather retire a working trap than keep flagging humans.
FALSE_POSITIVE_PENALTY = 10

# With enough evidence a Beta posterior gets so sharp that a beaten arm is
# never sampled again, and the system can never notice that the arm started
# working. This floor forces a uniform draw now and then to keep it observable.
EXPLORATION_FLOOR = 0.05

DEFAULT_CATEGORIES = (
    "invisible_entity",
    "position_offset",
    "phantom_player",
    "unreachable_bait",
)


@dataclass
class CategoryStats:
    caught: int = 0
    missed: int = 0
    burned: bool = False

    @property
    def triggered(self) -> int:
        return self.caught + self.missed


def new_stats(categories=DEFAULT_CATEGORIES) -> dict[str, CategoryStats]:
    return {name: CategoryStats() for name in categories}


def beta_sample(a: float, b: float, rng: random.Random | None = None) -> float:
    """Draw from Beta(a, b) as the ratio of two Gamma draws."""
    rng = rng or random
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    total = x + y
    return 0.5 if total == 0.0 else x / total


def pick_category(stats: dict[str, CategoryStats], rng: random.Random | None = None) -> str | None:
    """Sample one posterior per arm and deploy the best draw."""
    live = {n: s for n, s in stats.items() if not s.burned} or stats
    if not live:
        return None

    if (rng or random).random() < EXPLORATION_FLOOR:
        return (rng or random).choice(sorted(live))

    best, best_sample = None, -1.0
    for name, s in live.items():
        sample = beta_sample(s.caught + 1, s.missed + 1, rng)
        if sample > best_sample:
            best, best_sample = name, sample
    return best


def record_catch(stats: dict[str, CategoryStats], category: str) -> None:
    stats[category].caught += 1


def record_miss(stats: dict[str, CategoryStats], category: str) -> None:
    stats[category].missed += 1


def record_false_positive(stats: dict[str, CategoryStats], category: str) -> None:
    """A human was wrongly flagged: punish the category hard."""
    stats[category].missed += FALSE_POSITIVE_PENALTY
