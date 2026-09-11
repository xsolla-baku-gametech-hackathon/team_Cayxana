"""Signature detectors must fire on machines only.

A signature marks an observation suspicious on its own, so a single human
tripping one is a serious defect. These tests sweep every human population
and fail on the first hit.
"""

import unittest

from detection.features import _points, extract_features
from detection.signatures import DETECTORS, detect
from tests.fixtures import BOTS, HUMANS, TRAP

SEEDS = 200


def fired(generator, seed):
    trace = generator(seed)
    return detect(extract_features(trace, TRAP), _points(trace))


class TestSignatures(unittest.TestCase):
    def test_no_human_population_trips_any_signature(self):
        offenders = [
            (label, seed, hits)
            for label, gen in HUMANS.items()
            for seed in range(SEEDS)
            for hits in [fired(gen, seed)]
            if hits
        ]
        self.assertEqual(offenders, [], f"humans tripped signatures: {offenders[:5]}")

    def test_lagged_and_packet_loss_humans_survive_the_rhythm_detector(self):
        """Dropped packets manufacture equal-length pauses; the cadence
        guard is what stops that from reading as a metronome."""
        for label in ("lagged_human", "packet_loss_human"):
            for seed in range(SEEDS):
                self.assertNotIn("metronome", fired(HUMANS[label], seed), f"{label} {seed}")

    def test_each_bot_family_trips_at_least_one_signature(self):
        for label in ("naive_bot", "jitter_bot", "random_pause_bot", "waypoint_bot"):
            hits = [seed for seed in range(SEEDS) if fired(BOTS[label], seed)]
            self.assertEqual(len(hits), SEEDS, f"{label} missed on {SEEDS - len(hits)} traces")

    def test_signatures_are_refused_on_thin_traces(self):
        for seed in range(20):
            self.assertEqual(fired(HUMANS["short_session_human"], seed), ())

    def test_every_detector_is_documented(self):
        for name, fn in DETECTORS.items():
            self.assertTrue(fn.__doc__, f"{name} has no explanation")
