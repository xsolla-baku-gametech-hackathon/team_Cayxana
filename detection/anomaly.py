"""Surface bots that never cross a limit.

The scoring path answers "is this observation machine-like?". A bot built
to sit just under every threshold answers "no" forever while tripping traps
all day. Nothing in the detection path will ever notice it, because each
observation on its own is genuinely unremarkable.

Two population-level facts give it away, and both are plain arithmetic:

1. **It trips far more traps than anyone else and is never suspicious.**
   A human who trips this often crosses a limit occasionally; a human who
   never crosses a limit does not trip this often.
2. **Its scores do not vary.** A script reproducing the same movement
   reproduces very nearly the same score.

Fact 2 turned out to be much weaker than it sounds, and the numbers are
worth keeping: measured per-player spread is 0.019-0.205 across the human
populations, and a fully mimicking bot sits at 0.017-0.059 - inside the
human range, and indistinguishable from an expert or a corridor player.
Only crude bots collapse to zero, and those are already caught by
``signatures``. So spread only ranks a player once volume has already
surfaced them - on its own it selects expert players, not bots.

Neither fact is evidence of cheating, so nothing here bans anyone. The
output is a ranked review list - the input to the offline analysis that
decides whether a new detector is worth writing.
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field

TICKS_PER_HOUR = 72_000  # 20 ticks/sec

# A player must be seen this often before their numbers mean anything.
MIN_TRIPS = 6
# The 5th percentile of per-player spread across every human population.
# Only a player *tighter* than any measured human counts as unnaturally
# consistent; the human median is 0.049, so a threshold near it would
# select almost everybody.
HUMAN_SPREAD_FLOOR = 0.02
# How much busier than the server median counts as an outlier trip rate.
# This is the signal that actually works, because it does not depend on
# how the player moves at all.
BUSY_MULTIPLE = 3.0
# One quiet repeat tripper is a curiosity: a player on a busy part of the
# map, or a label we got wrong. Several of them at once, all invisible to
# every rule we have, is a bot family - and that is the only thing worth
# waking a model up for.
MIN_COHORT = 3


@dataclass
class PlayerProfile:
    player_id: str
    trips: int = 0
    suspicious: int = 0
    scores: list[float] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    first_tick: int | None = None
    last_tick: int | None = None

    @property
    def spread(self) -> float | None:
        return st.pstdev(self.scores) if len(self.scores) >= 3 else None

    @property
    def median_score(self) -> float | None:
        return st.median(self.scores) if self.scores else None

    def trips_per_hour(self) -> float | None:
        if self.first_tick is None or self.last_tick is None:
            return None
        span = self.last_tick - self.first_tick
        if span <= 0:
            return None
        return self.trips * TICKS_PER_HOUR / span


@dataclass(frozen=True)
class Finding:
    player_id: str
    trips: int
    trips_per_hour: float
    spread: float
    median_score: float
    oddness: float
    reason: str


@dataclass(frozen=True)
class Cluster:
    """Several players reproducing the same score with no variation.

    One player scoring 0.60 every time is a curiosity. Forty unrelated
    accounts all scoring 0.60 every time is a shared script.
    """

    band: float
    players: tuple[str, ...]
    median_spread: float


@dataclass(frozen=True)
class AnomalyReport:
    """A cohort no rule explains. The only thing that starts an AI round."""

    players: tuple
    trips: int
    categories: tuple
    uncalibrated_categories: tuple
    busiest: float
    median_spread: float
    findings: tuple = ()

    @property
    def urgent(self) -> bool:
        """A trap category with no calibrated rule is catching this group."""
        return bool(self.uncalibrated_categories)

    def describe(self) -> str:
        note = (f", including uncalibrated {', '.join(self.uncalibrated_categories)}"
                if self.uncalibrated_categories else "")
        return (f"{len(self.players)} players, {self.trips} trips across "
                f"{len(self.categories)} categories{note}, none ever suspicious")

    def as_dict(self) -> dict:
        return {"players": list(self.players), "trips": self.trips,
                "categories": list(self.categories),
                "uncalibratedCategories": list(self.uncalibrated_categories),
                "busiestTripsPerHour": self.busiest,
                "medianSpread": self.median_spread,
                "urgent": self.urgent,
                "reasons": [f.reason for f in self.findings][:5]}


class AnomalyMonitor:
    """Population-level bookkeeping. Never decides anything on its own."""

    def __init__(self, min_trips: int = MIN_TRIPS):
        self.min_trips = min_trips
        self._players: dict[str, PlayerProfile] = {}

    def observe(self, player_id: str, category: str, score: float | None,
                suspicious: bool, tick: int) -> None:
        profile = self._players.setdefault(player_id, PlayerProfile(player_id))
        profile.trips += 1
        profile.suspicious += bool(suspicious)
        profile.categories.add(category)
        if score is not None:
            profile.scores.append(score)
        profile.first_tick = tick if profile.first_tick is None else min(profile.first_tick, tick)
        profile.last_tick = tick if profile.last_tick is None else max(profile.last_tick, tick)

    def observe_verdict(self, verdict) -> None:
        self.observe(verdict.player_id, verdict.category, verdict.score,
                     verdict.suspicious, verdict.signal.tick)

    # -- layer 1 + 2 ------------------------------------------------------

    def review_list(self, limit: int = 20) -> list[Finding]:
        """Quiet repeat trippers whose movement barely varies, ranked.

        Only players the detection path has *never* found suspicious are
        eligible: anyone it did catch is already handled.
        """
        rates = [p.trips_per_hour() for p in self._players.values()]
        rates = [r for r in rates if r is not None]
        if not rates:
            return []
        typical = st.median(rates) or 1.0

        findings = []
        for profile in self._players.values():
            rate, spread = profile.trips_per_hour(), profile.spread
            if profile.suspicious or profile.trips < self.min_trips:
                continue
            if rate is None or spread is None:
                continue

            busyness = rate / typical
            tightness = 1.0 - min(spread / HUMAN_SPREAD_FLOOR, 1.0)
            # Volume is the only thing allowed to put a player on the list.
            # Consistency merely ranks them: expert and corridor players are
            # tighter than the human 5th percentile, so a spread-triggered
            # list fills up with the best humans on the server.
            if busyness < BUSY_MULTIPLE:
                continue

            reasons = []
            if busyness >= BUSY_MULTIPLE:
                reasons.append(f"trips {busyness:.1f}x the server median, never suspicious")
            if tightness > 0.0:
                reasons.append(f"score spread {spread:.3f}, tighter than any measured human")
            findings.append(Finding(
                player_id=profile.player_id,
                trips=profile.trips,
                trips_per_hour=rate,
                spread=spread,
                median_score=profile.median_score,
                oddness=round(busyness * (1.0 + tightness), 3),
                reason="; ".join(reasons),
            ))

        findings.sort(key=lambda f: f.oddness, reverse=True)
        return findings[:limit]

    def clusters(self, band_width: float = 0.05, min_players: int = 5) -> list[Cluster]:
        """Bands where many low-variation players share the same score."""
        bands: dict[float, list[PlayerProfile]] = {}
        for profile in self._players.values():
            spread, median = profile.spread, profile.median_score
            if spread is None or median is None or spread >= HUMAN_SPREAD_FLOOR:
                continue
            bands.setdefault(round(median / band_width) * band_width, []).append(profile)

        return sorted(
            (
                Cluster(
                    band=round(band, 3),
                    players=tuple(sorted(p.player_id for p in members)),
                    median_spread=round(st.median([p.spread for p in members]), 4),
                )
                for band, members in bands.items()
                if len(members) >= min_players
            ),
            key=lambda c: len(c.players),
            reverse=True,
        )

    # -- the wake-up signal ------------------------------------------------

    def report(self, limit: int = 20) -> "AnomalyReport | None":
        """The one condition that justifies asking a model anything.

        Returns None - meaning "stay asleep" - unless a *group* of players
        is tripping traps far more often than the server median while no
        rule in the system has ever found any of them suspicious. That is
        the shape of a bot family the detector cannot see: plenty of
        evidence that something is wrong, and nothing in the rules that
        matches it.

        Categories the cohort trips whose rule is still uncalibrated are
        called out separately, because a brand new trap that catches this
        group constantly and flags nobody is the strongest version of the
        same signal - and the one most likely to be a rule problem rather
        than a player problem.
        """
        from .rules import rule_for

        findings = self.review_list(limit)
        if len(findings) < MIN_COHORT:
            return None

        players = tuple(f.player_id for f in findings)
        categories: set[str] = set()
        trips = 0
        for finding in findings:
            profile = self._players[finding.player_id]
            categories |= profile.categories
            trips += profile.trips
        uncalibrated = tuple(sorted(c for c in categories if rule_for(c).shadow))
        return AnomalyReport(
            players=players,
            trips=trips,
            categories=tuple(sorted(categories)),
            uncalibrated_categories=uncalibrated,
            busiest=round(max(f.trips_per_hour for f in findings), 1),
            median_spread=round(st.median([f.spread for f in findings]), 4),
            findings=tuple(findings),
        )

    def profile(self, player_id: str) -> PlayerProfile | None:
        return self._players.get(player_id)
