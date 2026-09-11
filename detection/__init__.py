"""Server-side trap detection brain.

Scope: the trap system decides where traps go and tells us when one is
tripped. This package never inspects a trap. A trip only puts the player
under observation; the verdict comes from how they move *afterwards*.

    detector = Detector()

    # 1. the trap system reports a trip
    detector.on_trap_signal(TrapSignal("p7", "invisible_entity", tick=1040))

    # 2. movement streams in for every observed player
    for verdict in detector.on_movement("p7", samples):
        if verdict.flagged:
            queue_for_review(verdict.player_id)

    # 3. the session ends
    detector.on_disconnect("p7")

``on_movement`` returns a verdict only when an observation window closes, so
most calls return nothing. The detection path is pure arithmetic and
deterministic: no model call ever runs between a trap event and a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .features import Features
from .rules import CategoryRule, rule_for
from .scoring import Score, classify
from .suspicion import SuspicionStore

__all__ = ["Detector", "TrapSignal", "Verdict", "classify"]


@dataclass(frozen=True)
class TrapSignal:
    """What the trap system sends us. Only the first three fields are used.

    ``x``/``y`` are optional but worth sending: they let the reaction-delay
    feature measure how fast the player committed to the trap. Everything
    trap-specific belongs in ``metadata``, which this package deliberately
    ignores - it is carried through to the verdict for logging and review.
    """

    player_id: str
    category: str
    tick: int
    x: float | None = None
    y: float | None = None
    trap_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def as_trap_event(self) -> dict | None:
        if self.x is None or self.y is None:
            return None
        return {"tick": self.tick, "x": self.x, "y": self.y}


@dataclass(frozen=True)
class Verdict:
    """Produced when an observation window closes."""

    player_id: str
    category: str
    score: float | None
    features: Features
    suspicious: bool  # this observation looked machine-like for this category
    flagged: bool  # three categories now agree: hand to review
    signatures: tuple[str, ...]  # machine behaviours found, if any
    rule: CategoryRule
    signal: TrapSignal

    @property
    def insufficient_data(self) -> bool:
        return self.score is None


@dataclass
class _Observation:
    signal: TrapSignal
    rule: CategoryRule
    closes_at: int
    samples: list = field(default_factory=list)


class Detector:
    """Stateful across a session: one instance per game server."""

    def __init__(self, rules=None, *, required_events=3, required_categories=3):
        self.rules = rules
        self._store = SuspicionStore(required_events=required_events, required_categories=required_categories)
        self._open: dict[str, list[_Observation]] = {}

    # -- input ------------------------------------------------------------

    def on_trap_signal(self, signal: TrapSignal) -> None:
        """Put the player under observation. Never a verdict on its own."""
        rule = rule_for(signal.category, self.rules)
        observations = self._open.setdefault(signal.player_id, [])
        if any(o.signal.category == signal.category for o in observations):
            return  # already watching this category; a re-trip adds nothing
        observations.append(
            _Observation(signal, rule, signal.tick + rule.observation_ticks)
        )

    def on_movement(self, player_id: str, samples) -> list[Verdict]:
        """Feed movement; returns verdicts for windows that closed just now."""
        observations = self._open.get(player_id)
        if not observations:
            return []  # nobody is watching this player, so nothing to buffer

        # One scan for the whole batch: the newest tick in it does not
        # depend on which observation is being fed.
        latest = max((s.get("tick", -1) for s in samples), default=-1)
        verdicts, still_open = [], []
        for observation in observations:
            observation.samples.extend(
                s for s in samples
                if observation.signal.tick <= s.get("tick", -1) <= observation.closes_at
            )
            if latest >= observation.closes_at:
                verdicts.append(self._close(player_id, observation))
            else:
                still_open.append(observation)

        self._open[player_id] = still_open
        return verdicts

    def on_disconnect(self, player_id: str) -> list[Verdict]:
        """Close what is still open, then forget the player.

        Suspicion is per session. A window cut short usually holds too few
        samples and is refused rather than guessed at.
        """
        verdicts = [
            self._close(player_id, o) for o in self._open.pop(player_id, [])
        ]
        self._store.clear(player_id)
        return verdicts

    # -- internals --------------------------------------------------------

    def _close(self, player_id: str, observation: _Observation) -> Verdict:
        score: Score = classify(observation.samples, observation.signal.as_trap_event())
        rule = observation.rule
        # A signature is conclusive on its own; otherwise the category's
        # own threshold decides how hard the movement has to argue.
        suspicious = bool(score.signatures) or (
            score.value is not None and score.value > rule.threshold
        )
        player = self._store.record(
            player_id,
            rule.category,
            score,
            observation.signal.tick,
            suspicious=suspicious and not rule.shadow,
        )
        return Verdict(
            player_id=player_id,
            category=rule.category,
            score=score.value,
            features=score.features,
            suspicious=suspicious,
            flagged=player.flagged,
            signatures=score.signatures,
            rule=rule,
            signal=observation.signal,
        )

    # -- inspection -------------------------------------------------------

    def history(self, player_id: str):
        return self._store.get(player_id).events

    def watching(self, player_id: str) -> list[str]:
        return [o.signal.category for o in self._open.get(player_id, [])]
