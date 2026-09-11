"""The adaptive layer: watch the traps, then ask a model what to change.

This is the top of the stack and the only part that talks to a model. It
is asleep by default. No timer wakes it, no amount of ordinary traffic
wakes it, and while it is asleep no model is loaded at all - LM Studio
holds no VRAM and the detection path is untouched arithmetic.

Exactly two things wake it, and both mean "the rules and the evidence
disagree", which is the only situation where an opinion is worth having:

* **An anomaly report** (``anomaly.AnomalyMonitor.report``): a group of
  players tripping traps far more often than anyone else while no rule in
  the system has ever found any of them suspicious - loudly wrong on the
  new traps, invisible to every rule we own. This runs *discovery*.
* **Label disagreement**: enough windows where a player we have a label
  for moves like the opposite. Only two labels count: a self-declared bot,
  and a human a person confirmed in ``labels.json``. A self-declared
  *human* is not evidence - see ``AdaptiveLayer.label_for`` - because a
  bot farm connecting as humans is the one input that could talk this
  layer into detecting less.

A round therefore does one or both of two jobs, never a job nothing asked
for:

1. **Tuning** (``tuning``) - move the weights and thresholds the existing
   detectors already use. This is what fixes a signature that fires on real
   keyboard players.
2. **Discovery** (``hypothesis``) - when a group of players trips traps
   constantly and still never looks machine-like, propose a *new*
   measurement for them. Accepted proposals go to shadow mode: they record,
   they never flag, and a person promotes them.

Both jobs are gated by replay over recorded windows, which always include
the synthetic labelled populations as a regression suite. A change that
makes one labelled human newly suspicious is refused, whatever the model
said about it. Nothing here bans anyone, and with no local model reachable
the whole layer is inert and the classifier behaves exactly as before.

The model is opened at the start of a round and closed at the end of it -
proposal made, rule written, model unloaded.

Offline over a finished session:

    python -m detection.adaptive --trace <session.jsonl>
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import settings as _settings
from . import tuning

from .anomaly import AnomalyMonitor
from .llm import default_client
from .hypothesis import ShadowDetectors, measure, propose, summarise, validate
from .signatures import DETECTORS
from .tuning import Observation

# What counts as label disagreement worth a tuning round: a window whose
# label says human and whose movement says machine, or a window whose
# label says bot and whose movement says nothing at all. While the
# classifier and the labels agree there is nothing to tune, and the layer
# makes no call at all - not a cheaper one, none.
MIN_DISAGREEMENTS = 8
# A floor between rounds, so a noisy minute cannot spend a round per tick.
# This is a rate limit, not a schedule: silence never triggers anything.
DEFAULT_INTERVAL = 300.0
# ``maybe_run`` is called for every movement record - twenty times a second
# per player - and asking the anomaly monitor for a report means walking
# every profile on the server. So the question itself is rate limited: the
# trigger is checked a few times a minute, not a few hundred times.
CHECK_INTERVAL = 5.0

# Recorded windows kept for replay. Old evidence is dropped, so a change
# accepted today is judged against how players move today.
MAX_OBSERVATIONS = 600
# Movement kept per player, enough to cover the longest observation window.
MOVEMENT_BUFFER = 400
# Synthetic traces mixed into every gate as a regression suite.
SYNTHETIC_PER_POPULATION = 6

SETTINGS_FILE = "ai-settings.json"
PROPOSALS_FILE = "ai-proposals.jsonl"
SHADOW_FILE = "ai-shadow.json"
# Hand-written corrections to the self-declared labels, as {playerId: kind}.
# A bot connected as a human - to test whether it gets caught - would
# otherwise teach the gate that machine-like movement is human, which is
# the one way this layer could make the detector worse. Re-read every
# round, so a correction takes effect without a restart and is applied to
# the evidence already collected.
LABELS_FILE = "labels.json"


def synthetic_observations(per_population: int = SYNTHETIC_PER_POPULATION) -> list[Observation]:
    """The labelled populations, as observations the gate can replay.

    They are the floor under every accepted change: whatever a round does
    to live numbers, the corridor human, the AFK player and the lagging
    player must not become suspicious because of it.
    """
    try:
        from tests.fixtures import BOTS, HUMANS, TRAP
    except Exception:  # fixtures are not shipped in every deployment
        return []
    observations = []
    for group, label in ((HUMANS, "human"), (BOTS, "bot")):
        for name, generator in group.items():
            for seed in range(per_population):
                observations.append(Observation.from_trace(
                    generator(seed), label=label, player_id=f"synthetic:{name}",
                    trap_event=TRAP))
    return observations


@dataclass
class AdaptiveLayer:
    """Collects evidence from the live stream and runs rounds off the clock."""

    out_dir: Path | None = None
    # An injected client is kept open and reused (the tests do this). When
    # None, a client is built per round from ``client_factory`` and closed
    # again at the end of it, so nothing is loaded between anomalies.
    client: object = None
    client_factory: object = None
    interval: float = DEFAULT_INTERVAL
    apply_accepted: bool = True
    include_synthetic: bool = True
    log: object = None  # a callable taking one line of text
    anomaly: AnomalyMonitor = field(default_factory=AnomalyMonitor)

    _observations: deque = field(default_factory=lambda: deque(maxlen=MAX_OBSERVATIONS))
    _movement: dict = field(default_factory=dict)
    _labels: dict = field(default_factory=dict)
    _overrides: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _last_run: float = 0.0
    _last_check: float = 0.0
    _disagreements: int = 0
    _seen_since_run: int = 0
    shadow: ShadowDetectors = field(default_factory=ShadowDetectors)
    rounds: list = field(default_factory=list)

    def __post_init__(self):
        self.out_dir = Path(self.out_dir) if self.out_dir else None
        if self.client is None and self.client_factory is None:
            self.client_factory = default_client
        self._last_run = self._last_check = time.monotonic()
        self._restore()
        self.reload_labels()

    @property
    def enabled(self) -> bool:
        """Could a round happen at all? Not whether one is warranted."""
        return self.client is not None or self.client_factory is not None

    def probe(self) -> bool:
        """Is a model reachable right now? Asks without loading anything.

        Only for a start-up message: a round decides for itself, and a model
        that appears later - LM Studio started after the game - is picked up
        by the next round without a restart.
        """
        client = self._open_client()
        self._close_client(client)
        return client is not None

    def _open_client(self):
        """The model for this round. None means the round cannot run."""
        if self.client is not None:
            return self.client
        try:
            return self.client_factory()
        except Exception as exc:  # a factory that probes the network
            self._say(f"[ai] no model available: {type(exc).__name__}: {exc}")
            return None

    def _close_client(self, client) -> None:
        """Give the VRAM back. An injected client is left alone."""
        if client is None or client is self.client:
            return
        try:
            if client.close():
                self._say("[ai] model unloaded")
        except Exception:  # closing is best effort by definition
            pass

    # -- evidence from the live stream ------------------------------------

    def note_player(self, player_id: str, kind: str | None) -> None:
        """Record the self-declared kind. See ``label_for`` for what it buys.

        Never used by scoring: the classifier has never seen this field and
        must not start now.
        """
        if kind in ("human", "bot"):
            self._labels[player_id] = kind

    def label_for(self, player_id: str) -> str | None:
        """The label the gate is allowed to believe. Deliberately asymmetric.

        Nobody who cheats connects saying so, so a self-declared ``kind`` is
        a demo convenience, not evidence - and the two directions are not
        equally safe to believe:

        * "I am a bot" can only ever ask the gate for **more** detection: if
          a declared bot is not caught, that is a disagreement, and the fix
          is to catch it. Believing it cannot cost a real player anything.
        * "I am a human" asks the gate for **less** detection. A farm that
          connects as ten humans and moves like ten scripts would, believed,
          teach the gate that scripted movement is what humans look like -
          the one way this layer can make the detector worse.

        So a declared bot is believed, a declared human is treated as
        unknown, and the only thing that turns a player into a labelled
        human is a person writing them into ``labels.json``. Unknown
        windows are still kept as evidence; they simply cannot vote on
        whether a change was safe.
        """
        override = self._overrides.get(player_id)
        if override:
            return override
        declared = self._labels.get(player_id)
        return declared if declared == "bot" else None

    def movement(self, player_id: str, sample: dict) -> None:
        buffer = self._movement.get(player_id)
        if buffer is None:
            buffer = self._movement[player_id] = deque(maxlen=MOVEMENT_BUFFER)
        buffer.append(sample)

    def observe(self, verdict) -> None:
        """Keep one closed window for replay, with the movement behind it."""
        start = verdict.signal.tick
        end = start + verdict.rule.observation_ticks
        trace = [s for s in self._movement.get(verdict.player_id, ())
                 if start <= s.get("tick", -1) <= end]
        if len(trace) < 2:
            return
        observation = Observation.from_trace(
            trace, category=verdict.category,
            label=self.label_for(verdict.player_id),
            player_id=verdict.player_id,
            trap_event=verdict.signal.as_trap_event())
        self.anomaly.observe_verdict(verdict)
        with self._lock:
            self._observations.append(observation)
            self._seen_since_run += 1
            self._disagreements += _disagrees(observation, verdict.suspicious)

    def forget(self, player_id: str) -> None:
        self._movement.pop(player_id, None)

    # -- running a round ---------------------------------------------------

    def wake_reason(self):
        """``(anomaly report or None, disagreement count)``.

        This is the whole trigger, and it is deliberately readable: the two
        halves are independent, and a round with neither of them is not a
        cheap round - it is a round that does not happen.
        """
        with self._lock:
            disagreements = self._disagreements
        return self.anomaly.report(), disagreements

    def maybe_run(self, now: float | None = None) -> bool:
        """Start a round only if something is actually wrong. Returns at once."""
        now = time.monotonic() if now is None else now
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return False
        if now - self._last_check < CHECK_INTERVAL:
            return False  # asked recently enough; the answer cannot have moved far
        self._last_check = now
        # labels.json is written by the game while people play; a label that
        # only arrived at round time could never be what starts a round.
        self.reload_labels()
        if now - self._last_run < self.interval:
            return False  # a round may be warranted but one just ran
        report, disagreements = self.wake_reason()
        if report is None and disagreements < MIN_DISAGREEMENTS:
            return False  # nothing unexplained, nothing contradicted: stay asleep
        self._last_run = now
        self._thread = threading.Thread(
            target=self._guarded_round, args=(report,), daemon=True)
        self._thread.start()
        return True

    def _guarded_round(self, report=None) -> None:
        try:
            self.run_round(report=report)
        except Exception as exc:  # a failed round must never touch the game
            self._say(f"[ai] round failed: {type(exc).__name__}: {exc}")

    def reload_labels(self) -> dict:
        """Re-read the corrections file and apply it to evidence already held."""
        path = None if self.out_dir is None else self.out_dir / LABELS_FILE
        overrides = {}
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                overrides = {str(player): kind for player, kind in raw.items()
                             if kind in ("human", "bot")}
            except (ValueError, OSError, AttributeError):
                self._say(f"[ai] {LABELS_FILE} unreadable; keeping declared labels")
        if overrides == self._overrides:
            return overrides
        previous, self._overrides = self._overrides, overrides
        with self._lock:
            corrected = []
            for o in self._observations:
                # A label can also be withdrawn (the game revokes a "human"
                # that tripped traps at farm rate); fall back to what the
                # player declared, which for a human means unknown.
                if o.player_id in overrides or o.player_id in previous:
                    target = self.label_for(o.player_id)
                else:
                    target = o.label
                if target != o.label:
                    was = _disagrees(o, o.judge(_settings.live())[0])
                    o = replace(o, label=target)
                    # A window that only now has a label can now contradict it.
                    self._disagreements += _disagrees(o, o.judge(_settings.live())[0]) - was
                corrected.append(o)
            self._disagreements = max(0, self._disagreements)
            self._observations.clear()
            self._observations.extend(corrected)
        if overrides:
            self._say(f"[ai] label corrections in force: {overrides}")
        return overrides

    def run_round(self, report=None, force: bool = False) -> dict:
        """One round: load the model, do only the job that was asked for,
        unload the model again. Returns what happened.

        ``report`` is the anomaly that woke the round, if one did; ``force``
        runs both jobs regardless, which is what the offline CLI over a
        finished session wants.
        """
        self.reload_labels()
        if report is None and not force:
            report, _ = self.wake_reason()
        with self._lock:
            live_observations = list(self._observations)
        observations = live_observations + (
            synthetic_observations() if self.include_synthetic else [])
        record = {
            "type": "ai_round",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "liveWindows": len(live_observations),
            "labelledLive": _label_counts(live_observations),
            "labelCorrections": dict(self._overrides),
            "anomaly": report.as_dict() if report is not None else None,
        }
        with self._lock:
            record["disagreements"] = self._disagreements
            record["windowsSinceLastRound"] = self._seen_since_run
            self._disagreements = self._seen_since_run = 0
        # Each job answers a different question, so each one runs only when
        # its own question was asked. Tuning fixes rules that contradict the
        # labels; discovery writes a rule for a cohort no rule explains.
        tune_wanted = force or record["disagreements"] >= MIN_DISAGREEMENTS
        discover_wanted = force or report is not None
        if report is not None:
            self._say(f"[ai] woken by an anomaly: {report.describe()}")

        client = self._open_client() if (tune_wanted or discover_wanted) else None
        record["model"] = getattr(client, "model", None)
        try:
            if client is None:
                record["skipped"] = "no model available"
                self._say("[ai] a round was warranted but no model is reachable")
            else:
                record["humansByPlayer"] = tuning.per_player(
                    live_observations, _settings.live())[:8]
                record["tuning"] = (
                    self._tune(client, observations, report) if tune_wanted else
                    {"accepted": False, "reason": "no label disagreement; not asked"})
                record["discovery"] = (
                    self._discover(client, observations, report) if discover_wanted else
                    [{"accepted": False, "reason": "no anomaly reported; not asked"}])
        finally:
            # Proposal made, rule written, model unloaded - whatever happened
            # in between, including an exception.
            self._close_client(client)
        self.rounds.append(record)
        self._write(record)
        return record

    def _tune(self, client, observations, report=None) -> dict:
        # Ask nothing when there is nothing to buy: no labelled human is
        # suspected and the bots are being caught. This is the common case
        # once the numbers are right, and it must not cost a model call.
        before = tuning.evaluate(observations, _settings.live())
        if before.human_hits == 0 and before.bot_hits == before.bots:
            self._say("[ai] no model call: labels and movement agree "
                      f"(humans {before.human_hits}/{before.humans}, "
                      f"bots {before.bot_hits}/{before.bots})")
            return tuning.TuningResult(
                False, "nothing to fix; no model call made", before=before).as_dict()
        note = ("\nA cohort the rules do not explain was reported at the same "
                f"time: {report.describe()}." if report is not None else "")
        result = tuning.tune(client, observations, extra_note=note)
        if result.accepted and self.apply_accepted and result.candidate is not None:
            # One reference swap; every reader calls settings.live() per call,
            # so a window scored mid-swap uses one whole set or the other.
            _settings.set_live(result.candidate)
            self._persist_settings(result.candidate)
            self._say(f"[ai] applied: {_describe(result.changes)}"
                      f" (human hits {result.before.human_hits}->{result.after.human_hits},"
                      f" bot hits {result.before.bot_hits}->{result.after.bot_hits})")
        elif result.accepted:
            self._say(f"[ai] proposed (not applied): {_describe(result.changes)}")
        else:
            self._say(f"[ai] no tuning change: {result.reason}")
        return result.as_dict()

    def _discover(self, client, observations, report=None) -> list[dict]:
        """Ask for a new measurement for the windows we still cannot explain.

        When an anomaly report woke the round, the cohort *is* the players
        it named: those are the ones tripping traps constantly with no rule
        to show for it, so a measurement separating them from the labelled
        humans is the missing rule. Without a report the cohort falls back
        to every unexplained window we hold.
        """
        humans = [_trace_of(o) for o in observations if o.label == "human"]
        named = set(report.players) if report is not None else set()
        unexplained = [o for o in observations
                       if o.label != "human" and not o.judge(_settings.live())[0]]
        reported = [o for o in unexplained if o.player_id in named]
        cohort = [_trace_of(o) for o in (reported if len(reported) >= 5 else unexplained)]
        if len(cohort) < 5 or len(humans) < 5:
            return [{"accepted": False, "reason": "nothing unexplained to work on"}]

        existing = tuple(DETECTORS) + tuple(self.shadow.shadow) + tuple(self.shadow.promoted)
        results = []
        for assessment in propose(client, summarise(cohort, humans),
                                  humans, cohort, existing):
            if assessment.accepted:
                self.shadow.add(assessment.proposal)
                self._say(f"[ai] new shadow detector: {assessment.proposal.describe()}")
            results.append({
                "accepted": assessment.accepted,
                "reason": assessment.reason,
                "proposal": assessment.proposal.describe() if assessment.proposal else None,
                "humanHits": assessment.human_hits,
                "cohortCoverage": round(assessment.cohort_coverage, 3),
            })
        # Shadow detectors only ever record; promotion stays a human decision.
        for features, points, _ in (measure(t) for t in cohort):
            self.shadow.observe(features, points)
        for features, points, _ in (measure(t) for t in humans):
            self.shadow.observe(features, points, known_human=True)
        self._persist_shadow()
        return results

    # -- reporting and persistence -----------------------------------------

    def shadow_report(self) -> list[tuple[str, int, int]]:
        return self.shadow.report()

    def _say(self, line: str) -> None:
        (self.log or print)(line)

    def _write(self, record: dict) -> None:
        if self.out_dir is None:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with (self.out_dir / PROPOSALS_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, allow_nan=False, default=str) + "\n")

    def _persist_settings(self, current) -> None:
        if self.out_dir is None:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / SETTINGS_FILE).write_text(
            json.dumps(current.as_dict(), indent=2), encoding="utf-8")

    def _persist_shadow(self) -> None:
        if self.out_dir is None:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        payload = [{"name": p.name, "metric": p.metric, "direction": p.direction,
                    "threshold": p.threshold, "rationale": p.rationale,
                    "excuse": p.excuse} for p in self.shadow.shadow.values()]
        (self.out_dir / SHADOW_FILE).write_text(
            json.dumps(payload, indent=2), encoding="utf-8")

    def _restore(self) -> None:
        """Resume tuned numbers and shadow detectors from a previous run."""
        if self.out_dir is None:
            return
        path = self.out_dir / SETTINGS_FILE
        if path.is_file():
            try:
                _settings.set_live(_settings.Settings.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))))
                self._say(f"[ai] resumed tuned settings from {path.name}")
            except (ValueError, OSError, KeyError, TypeError):
                self._say("[ai] tuned settings file unreadable; using defaults")
        path = self.out_dir / SHADOW_FILE
        if path.is_file():
            try:
                for raw in json.loads(path.read_text(encoding="utf-8")):
                    proposal, _ = validate(raw)
                    if proposal is not None:
                        self.shadow.add(proposal)
            except (ValueError, OSError, TypeError):
                pass


def _trace_of(observation: Observation) -> list[dict]:
    return [{"tick": t, "x": x, "y": y} for t, x, y in observation.points]


def _disagrees(observation: Observation, suspicious: bool) -> bool:
    """Does this window contradict what its label claims?

    An unlabelled window cannot contradict anything, so it never triggers a
    round on its own - it is still kept as evidence for one.
    """
    if observation.label == "human":
        return suspicious
    if observation.label == "bot":
        return not suspicious
    return False


def _label_counts(observations) -> dict:
    counts = {"human": 0, "bot": 0, "unknown": 0}
    for observation in observations:
        counts[observation.label or "unknown"] += 1
    return counts


def _describe(changes: dict) -> str:
    return ", ".join(f"{knob} {before} -> {after}"
                     for knob, (before, after) in sorted(changes.items())) or "nothing"


# -- offline use over a finished session ------------------------------------

def load_label_corrections(directory: Path) -> dict:
    """``{playerId: "human"|"bot"}`` from labels.json, or empty."""
    path = Path(directory) / LABELS_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {str(player): kind for player, kind in raw.items()
            if kind in ("human", "bot")}


def observations_from_trace(path: Path, observation_ticks: int = 200) -> list[Observation]:
    """Rebuild the windows of a recorded session, labelled by declared kind.

    Used by the CLI so a round can be tried against a session that has
    already been played, without waiting for the timer in a live game.
    """
    from bridge.reader import GameLogReader

    labels: dict[str, str] = {}
    corrections = load_label_corrections(Path(path).parent)
    movement: dict[str, deque] = {}
    pending: list[tuple] = []
    observations: list[Observation] = []

    for record in GameLogReader(Path(path)):
        kind = record["type"]
        tick = record.get("tick", 0)
        if kind == "join":
            labels[record["playerId"]] = record.get("kind")
        elif kind == "trap":
            pending.append((record["playerId"], record.get("category"), tick,
                            record.get("x"), record.get("y")))
        elif kind == "move":
            for player, x, y in record["players"]:
                buffer = movement.get(player)
                if buffer is None:
                    buffer = movement[player] = deque(maxlen=MOVEMENT_BUFFER * 4)
                buffer.append({"tick": tick, "x": x, "y": y})
            done = [p for p in pending if tick >= p[2] + observation_ticks]
            for player, category, start, x, y in done:
                trace = [s for s in movement.get(player, ())
                         if start <= s["tick"] <= start + observation_ticks]
                if len(trace) >= 2:
                    category = {"ghost_loot": "invisible_entity"}.get(category, category)
                    trap = None if x is None else {"tick": start, "x": x, "y": y}
                    observations.append(Observation.from_trace(
                        trace, category=category,
                        label=corrections.get(player) or labels.get(player),
                        player_id=player, trap_event=trap))
            pending = [p for p in pending if p not in done]
    return observations


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="session JSONL to learn from")
    parser.add_argument("--out", type=Path, help="where to write proposals")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what a round would change, apply nothing")
    parser.add_argument("--no-synthetic", action="store_true",
                        help="gate on live windows only (not recommended)")
    args = parser.parse_args(argv)

    trace = args.trace
    if trace is None:
        from bridge.reader import latest_session
        trace = latest_session()
    if trace is None:
        parser.error("No session found; pass --trace")

    live = observations_from_trace(trace)
    print(f"{len(live)} windows from {Path(trace).name}: {_label_counts(live)}")
    layer = AdaptiveLayer(out_dir=args.out or Path(trace).parent,
                          apply_accepted=not args.dry_run,
                          include_synthetic=not args.no_synthetic)
    if not layer.enabled:
        print("No local model reachable (start LM Studio and load "
              "qwen/qwen3-vl-8b, or set LMSTUDIO_URL / LOCAL_MODEL).")
        print("Baseline over these windows, with no model involved:")
        report = tuning.evaluate(live + synthetic_observations(), _settings.live())
        print(json.dumps(report.as_dict(), indent=2))
        return 1
    with layer._lock:
        layer._observations.extend(live)
    # A finished session is being examined on purpose, so both jobs run
    # whether or not the live triggers would have fired.
    record = layer.run_round(force=True)
    print(json.dumps(record, indent=2, default=str))
    for name, hits, human_hits in layer.shadow_report():
        print(f"shadow: {name} fired {hits} times, {human_hits} on known humans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
