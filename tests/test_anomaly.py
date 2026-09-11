"""The layer that surfaces bots the detection path cannot catch.

Nothing here bans anyone, so the bar is different from the classifier's:
the review list must rank hidden bots above ordinary players, and it must
never fill up with humans.
"""

import random
import unittest

from detection import Detector, TrapSignal
from detection.anomaly import AnomalyMonitor
from detection.bandit import DEFAULT_CATEGORIES
from tests.fixtures import BOTS, HUMANS, TRAP

HOUR = 72_000
HUMAN_GAP = (3000, 9000)  # a person trips one every few minutes
BOT_GAP = (600, 1200)  # a sweeper hits one every 30-60 seconds


def populate(monitor, rng, players, generator, gap, prefix):
    for i in range(players):
        player_id = f"{prefix}{i:03}"
        detector = Detector()
        tick = rng.randrange(1000)
        while tick < HOUR:
            detector.on_trap_signal(TrapSignal(
                player_id, rng.choice(DEFAULT_CATEGORIES), tick=tick,
                x=TRAP["x"], y=TRAP["y"],
            ))
            samples = generator(rng.randrange(10_000), start_tick=tick)
            for start in range(0, len(samples), 20):
                for verdict in detector.on_movement(player_id, samples[start:start + 20]):
                    monitor.observe_verdict(verdict)
            tick += rng.randint(*gap)


class TestAnomalyMonitor(unittest.TestCase):
    def test_hidden_bot_is_surfaced_by_volume_alone(self):
        """humanized_bot defeats every movement feature, but it cannot
        farm traps at a human rate and still be worth running."""
        rng = random.Random(5)
        monitor = AnomalyMonitor()
        populate(monitor, rng, 40, lambda s, start_tick=0: HUMANS["human"](s, start_tick),
                 HUMAN_GAP, "human")
        populate(monitor, rng, 5, BOTS["humanized_bot"], BOT_GAP, "ghost")

        findings = monitor.review_list(limit=5)
        surfaced = {f.player_id for f in findings}
        self.assertEqual(surfaced, {f"ghost{i:03}" for i in range(5)},
                         "the top of the list must be exactly the hidden bots")

    def test_a_quiet_human_population_produces_an_empty_list(self):
        rng = random.Random(11)
        monitor = AnomalyMonitor()
        for name in ("human", "expert_human", "corridor_human", "mobile_human"):
            populate(monitor, rng, 10, HUMANS[name], HUMAN_GAP, name[:4])
        self.assertEqual(monitor.review_list(), [])

    def test_players_the_classifier_already_caught_are_excluded(self):
        monitor = AnomalyMonitor()
        for tick in range(0, 20_000, 2000):
            monitor.observe("caught", "position_offset", 0.9, True, tick)
            monitor.observe("quiet", "position_offset", 0.5, False, tick)
        self.assertNotIn("caught", [f.player_id for f in monitor.review_list()])

    def test_too_few_trips_is_never_a_finding(self):
        monitor = AnomalyMonitor()
        for tick in (0, 500):
            monitor.observe("newcomer", "position_offset", 0.5, False, tick)
        self.assertEqual(monitor.review_list(), [])

    def test_spread_alone_does_not_convict_an_expert_player(self):
        """Expert and corridor humans are very consistent. The spread floor
        sits below anything a human produced, so they stay off the list."""
        rng = random.Random(3)
        monitor = AnomalyMonitor()
        populate(monitor, rng, 12, HUMANS["expert_human"], HUMAN_GAP, "pro")
        populate(monitor, rng, 12, HUMANS["corridor_human"], HUMAN_GAP, "cor")
        self.assertEqual(monitor.review_list(), [])
