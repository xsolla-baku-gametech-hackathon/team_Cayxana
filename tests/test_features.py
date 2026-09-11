"""Step 1 edge cases. Every one of these occurs in a live session."""

import math
import unittest

from detection.features import Features, extract_features


def line(n=10, dx=1.0):
    return [{"tick": i, "x": i * dx, "y": 0.0} for i in range(n)]


def finite_or_none(value):
    return value is None or (isinstance(value, (int, float)) and math.isfinite(value))


class TestFeatures(unittest.TestCase):
    def assert_clean(self, f: Features):
        for name in ("straightness", "pause_variance", "reaction_ticks", "revisits"):
            self.assertTrue(finite_or_none(getattr(f, name)), f"{name} is not finite")

    def test_perfect_straight_line(self):
        self.assertAlmostEqual(extract_features(line()).straightness, 1.0, places=6)

    def test_square_returning_to_start(self):
        square = []
        tick = 0
        for x, y in [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]:
            for _ in range(3):
                square.append({"tick": tick, "x": float(x), "y": float(y)})
                tick += 1
        f = extract_features(square)
        self.assert_clean(f)
        self.assertAlmostEqual(f.straightness, 0.0, places=6)

    def test_never_moved(self):
        still = [{"tick": i, "x": 5.0, "y": 5.0} for i in range(50)]
        f = extract_features(still, {"tick": 0, "x": 1.0, "y": 1.0})
        self.assert_clean(f)
        self.assertIsNone(f.straightness)  # unknown, not 0

    def test_identical_pauses_give_zero_variance(self):
        """A metronome bot: move, pause exactly 3 ticks, repeat."""
        trace, tick, x = [], 0, 0.0
        for _ in range(5):
            for _ in range(3):
                x += 5.0
                trace.append({"tick": tick, "x": x, "y": 0.0})
                tick += 1
            for _ in range(3):  # hold the same position for 3 ticks, every cycle
                trace.append({"tick": tick, "x": x, "y": 0.0})
                tick += 1
        self.assertAlmostEqual(extract_features(trace).pause_variance, 0.0, places=6)

    def test_single_pause_returns_none(self):
        trace = [{"tick": 0, "x": 0.0, "y": 0.0}, {"tick": 1, "x": 0.0, "y": 0.0}]
        trace += [{"tick": 2 + i, "x": 5.0 * (i + 1), "y": 0.0} for i in range(5)]
        self.assertIsNone(extract_features(trace).pause_variance)  # not 0.0

    def test_trap_never_approached(self):
        away = [{"tick": i, "x": -float(i) * 5, "y": 0.0} for i in range(30)]
        self.assertIsNone(extract_features(away, {"tick": 0, "x": 500.0, "y": 0.0}).reaction_ticks)

    def test_reaction_measured_from_trap_tick(self):
        trace = [{"tick": i, "x": 0.0, "y": 0.0} for i in range(10)]
        trace += [{"tick": 10 + i, "x": float(i) * 5, "y": 0.0} for i in range(10)]
        f = extract_features(trace, {"tick": 5, "x": 100.0, "y": 0.0})
        self.assertEqual(f.reaction_ticks, 5.0)

    def test_two_sample_trace(self):
        self.assert_clean(extract_features(line(2), {"tick": 0, "x": 9.0, "y": 9.0}))

    def test_empty_trace(self):
        f = extract_features([], {"tick": 0, "x": 1.0, "y": 1.0})
        self.assert_clean(f)
        self.assertEqual(f, Features(None, None, None, None, None))

    def test_a_trace_with_no_pause_has_an_idle_share_of_zero(self):
        """The never-resting bot: no rhythm to measure, but a measurement."""
        f = extract_features(line(120))
        self.assertIsNone(f.pause_variance)  # fewer than two pauses
        self.assertEqual(f.idle_share, 0.0)  # and that is not "unknown"

    def test_idle_share_counts_the_ticks_spent_still(self):
        trace = [{"tick": i, "x": 0.0 if i < 50 else float(i - 50), "y": 0.0}
                 for i in range(100)]
        self.assertAlmostEqual(extract_features(trace).idle_share, 50 / 99, places=2)

    def test_a_frozen_trace_is_entirely_idle(self):
        self.assertEqual(extract_features(
            [{"tick": i, "x": 1.0, "y": 1.0} for i in range(50)]).idle_share, 1.0)

    def test_malformed_samples_are_dropped(self):
        self.assert_clean(extract_features([{"tick": 0}, None, {"x": 1, "y": 2, "tick": "a"}]))

    def test_revisits_counts_reentry(self):
        out = line(6, dx=15.0)
        there_and_back = out + out[::-1] + out
        self.assertGreater(extract_features(there_and_back).revisits, 0)
        self.assertEqual(extract_features(out).revisits, 0)  # never re-enters a cell


if __name__ == "__main__":
    unittest.main()
