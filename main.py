"""Detection-brain report: per-trap scores, session outcomes, adaptation demo.

Run: python main.py
"""

from __future__ import annotations

import random
import statistics as st

from detection import Detector, TrapSignal, bandit
from detection.anomaly import AnomalyMonitor
from detection.bandit import DEFAULT_CATEGORIES
from detection.burn import BurnMonitor
from detection.features import _points, extract_features
from detection.rules import DEFAULT_RULES, rule_for
from detection.signatures import DETECTORS, detect
from detection.scoring import SUSPICIOUS, classify
from tests.fixtures import BOTS, HUMANS, TICKS, TRAP, simulate_session

TRACES = 300
SESSIONS = 200


def _cache():
    """Score every population once; the tables below only read from here."""
    return {
        name: [classify(gen(s), TRAP) for s in range(TRACES)]
        for group in (HUMANS, BOTS)
        for name, gen in group.items()
    }


def trap_level(scores):
    print(f"Per-trap scores, {TRACES} traces per population (threshold {SUSPICIOUS})")
    print(f"  {'population':22}{'scored':>7}{'p50':>8}{'p95':>8}{'max':>8}{'flagged':>9}{'no data':>9}")
    for group, kind in ((HUMANS, "human"), (BOTS, "bot")):
        for name in group:
            rows = scores[name]
            values = sorted(s.value for s in rows if s.value is not None)
            flagged = sum(s.is_suspicious() for s in rows)
            if not values:
                print(f"  {name:22}{0:>7}{'  all refused as insufficient data':>40}")
                continue
            p95 = values[int(0.95 * (len(values) - 1))]
            print(f"  {name:22}{len(values):>7}{st.median(values):>8.3f}{p95:>8.3f}"
                  f"{values[-1]:>8.3f}{flagged:>9}{len(rows) - len(values):>9}")
        print(f"  {'-' * 60}" if kind == "human" else "")


def session_level():
    """The metric that decides a ban: 12 randomly placed traps per session."""
    print(f"Session outcomes, {SESSIONS} sessions per population (random placement)")
    for group in (HUMANS, BOTS):
        for name, gen in group.items():
            hits = sum(simulate_session(gen, s).flagged for s in range(SESSIONS))
            bar = "#" * round(40 * hits / SESSIONS)
            print(f"  {name:22}{hits:>5}/{SESSIONS}  {hits / SESSIONS:6.1%}  {bar}")
        print()


def hidden_population_demo():
    """A bot that defeats every movement feature, hidden in a busy server.

    It is caught by volume, not by gait: it never crosses a threshold, but
    it cannot farm traps at a human rate and still be worth running. The
    output is a review list - nobody is banned by this.
    """
    hour, human_gap, bot_gap = 72_000, (3000, 9000), (600, 1200)
    rng = random.Random(5)
    monitor = AnomalyMonitor()
    truth = {}

    def play(player_id, generator, gap):
        detector, tick = Detector(), rng.randrange(1000)
        while tick < hour:
            detector.on_trap_signal(TrapSignal(player_id, rng.choice(DEFAULT_CATEGORIES),
                                               tick=tick, x=TRAP["x"], y=TRAP["y"]))
            samples = generator(rng.randrange(10_000), start_tick=tick)
            for start in range(0, len(samples), 20):
                for verdict in detector.on_movement(player_id, samples[start:start + 20]):
                    monitor.observe_verdict(verdict)
            tick += rng.randint(*gap)

    generators = list(HUMANS.values())
    for i in range(60):
        truth[f"human{i:03}"] = "human"
        play(f"human{i:03}", rng.choice(generators), human_gap)
    for i in range(8):
        truth[f"ghost{i:02}"] = "humanized_bot"
        play(f"ghost{i:02}", BOTS["humanized_bot"], bot_gap)

    findings = monitor.review_list(limit=10)
    print(f"  {'player':12}{'trips':>7}{'trips/hr':>10}{'spread':>9}{'oddness':>9}   truth")
    for f in findings:
        print(f"  {f.player_id:12}{f.trips:>7}{f.trips_per_hour:>10.1f}{f.spread:>9.3f}"
              f"{f.oddness:>9.2f}   {truth[f.player_id]}")
    bots = sum(truth[f.player_id] != "human" for f in findings)
    print(f"  -> {bots}/{len(findings)} of the list are bots, "
          f"{bots}/8 hidden bots surfaced, 0 banned")
    print()


def discovery_demo():
    """A bot family that appears after the detectors were written.

    smooth_rails_bot varies speed, pauses raggedly and wanders, so every
    signature misses it and it never scores above 0.42. The offline layer
    is shown aggregates only, proposes one measurement, and the proposal
    is replayed against the labelled humans before it may run at all.
    """
    import json

    from detection.hypothesis import ShadowDetectors, measure, propose, summarise

    humans = [gen(s) for gen in HUMANS.values() for s in range(20)]
    cohort = [BOTS["smooth_rails_bot"](s) for s in range(50)]
    existing = ("metronome", "constant_speed", "rails", "periodic")

    def stand_in_model(prompt):
        """Two answers of the kind a model actually returns: one that
        separates, one that sounds right and would ban real players."""
        return [
            json.dumps({"name": "tight_leg_cycle", "metric": "speed_periodicity",
                        "direction": "above", "threshold": 0.50,
                        "rationale": "fixed-length legs make the speed series repeat",
                        "excuse": "a player pacing a route repeats, but never this exactly"}),
            json.dumps({"name": "too_consistent", "metric": "speed_cv",
                        "direction": "below", "threshold": 0.40,
                        "rationale": "machines vary less than people",
                        "excuse": "a calm player also varies little"}),
        ]

    registry = ShadowDetectors()
    for assessment in propose(stand_in_model, summarise(cohort, humans),
                              humans, cohort, existing):
        described = assessment.proposal.describe() if assessment.proposal else "-"
        verdict = "ACCEPTED" if assessment.accepted else "rejected"
        print(f"  {verdict:9}{described:44}{assessment.reason}"
              f"  (humans hit {assessment.human_hits}, cohort {assessment.cohort_coverage:.0%})")
        if assessment.accepted:
            registry.add(assessment.proposal)

    for trace in cohort:
        features, points, _ = measure(trace)
        registry.observe(features, points)
    for trace in humans:
        features, points, _ = measure(trace)
        registry.observe(features, points, known_human=True)
    for name, hits, human_hits in registry.report():
        print(f"  shadow mode: {name} fired {hits} times, {human_hits} on known humans "
              f"- promotion stays a human decision")
    print()


def adaptation_demo():
    """One category gets patched out; the bandit must move its traffic."""
    rng = random.Random(11)
    stats = bandit.new_stats()
    monitor = BurnMonitor()
    catch_rates = dict(zip(DEFAULT_CATEGORIES, (0.45, 0.30, 0.20, 0.15)))
    now = 0.0

    for hour in range(7):
        if hour == 5:  # cheat developers ship a patch
            catch_rates["invisible_entity"] = 0.0
        deployed = {}
        for _ in range(1200):
            now += 3.0
            category = bandit.pick_category(stats, rng)
            deployed[category] = deployed.get(category, 0) + 1
            caught = rng.random() < catch_rates[category]
            monitor.record(category, f"sess{rng.randrange(200)}", caught, now)
            (bandit.record_catch if caught else bandit.record_miss)(stats, category)
        burned = monitor.sweep(stats, now)
        total = sum(deployed.values())
        share = {k: f"{100 * v // total}%" for k, v in sorted(deployed.items())}
        print(f"  hour {hour}: {share}" + (f"  -> BURNED {burned}" if burned else ""))
    print()


def signature_table():
    print(f"Machine signatures, {TRACES} traces per population (any one is conclusive)")
    header = f"  {'population':22}" + "".join(f"{k[:13]:>15}" for k in DETECTORS) + f"{'ANY':>8}"
    print(header)
    for group in (HUMANS, BOTS):
        for name, gen in group.items():
            hits = [detect(extract_features(t, TRAP), _points(t))
                    for t in (gen(s) for s in range(TRACES))]
            cells = "".join(f"{sum(k in h for h in hits) / len(hits):>15.1%}" for k in DETECTORS)
            print(f"  {name:22}{cells}{sum(bool(h) for h in hits) / len(hits):>8.1%}")
        print()


def category_rules():
    print("Per-category rules (a trip only starts an observation)")
    for rule in DEFAULT_RULES.values():
        print(f"  {rule.describe()}")
    print(f"  {rule_for('anything_new').describe()}")
    print()


if __name__ == "__main__":
    category_rules()
    signature_table()
    scores = _cache()
    trap_level(scores)
    print()
    session_level()
    print("Hidden population: the bot no movement feature can catch")
    hidden_population_demo()
    print("Offline discovery: a bot family that appeared after deployment")
    discovery_demo()
    print("Feedback for the trap randomiser: bandit + burn over 7 simulated hours")
    adaptation_demo()
