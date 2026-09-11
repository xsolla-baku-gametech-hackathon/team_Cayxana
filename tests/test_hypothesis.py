"""The offline discovery layer.

The model may propose anything; nothing it proposes may reach a player
without surviving replay against the labelled humans.
"""

import json
import unittest

from detection.hypothesis import (METRICS, Proposal, ShadowDetectors, assess,
                                  build_prompt, measure, propose, summarise)
from tests.fixtures import BOTS, HUMANS

HUMAN_TRACES = [gen(s) for gen in HUMANS.values() for s in range(15)]
COHORT = [BOTS["smooth_rails_bot"](s) for s in range(40)]

GOOD = {
    "name": "tight_leg_cycle", "metric": "speed_periodicity", "direction": "above",
    "threshold": 0.50, "rationale": "fixed-length legs repeat the speed series",
    "excuse": "a player pacing a route repeats too, but never this exactly",
}


class TestValidation(unittest.TestCase):
    def bad(self, **overrides):
        return assess({**GOOD, **overrides}, HUMAN_TRACES, COHORT)

    def test_a_separating_proposal_is_accepted(self):
        result = assess(GOOD, HUMAN_TRACES, COHORT)
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.human_hits, 0)
        self.assertGreaterEqual(result.cohort_coverage, 0.30)

    def test_a_proposal_that_touches_one_human_is_rejected(self):
        result = self.bad(threshold=0.40)  # inside the human range
        self.assertFalse(result.accepted)
        self.assertGreater(result.human_hits, 0)
        self.assertIn("humans", result.reason)

    def test_a_proposal_that_misses_the_cohort_is_rejected(self):
        result = self.bad(threshold=0.99)
        self.assertFalse(result.accepted)
        self.assertIn("separate", result.reason)

    def test_unknown_metric_is_rejected(self):
        self.assertFalse(self.bad(metric="mouse_entropy").accepted)

    def test_malformed_output_is_rejected_not_raised(self):
        for raw in ("```json\n{oops", "", "null", "[1,2]", 42, {"name": "x"}):
            self.assertFalse(assess(raw, HUMAN_TRACES, COHORT).accepted)

    def test_markdown_fences_are_stripped(self):
        raw = "```json\n" + json.dumps(GOOD) + "\n```"
        self.assertTrue(assess(raw, HUMAN_TRACES, COHORT).accepted)

    def test_duplicate_name_is_rejected(self):
        self.assertFalse(assess(GOOD, HUMAN_TRACES, COHORT, ("tight_leg_cycle",)).accepted)

    def test_unreachable_model_degrades_quietly(self):
        def down(prompt, schema=None):
            raise ConnectionError("timeout")

        # A summary with something worth asking about, so the round really
        # reaches the model rather than stopping short of it.
        summary = {"speed_cv": {"cohort_p5": 0.01, "cohort_p95": 0.05,
                                "cohort_median": 0.03, "human_min": 0.2,
                                "human_max": 0.9, "human_median": 0.4,
                                "separation": {"direction": "below",
                                               "suggest": 0.125,
                                               "window": (0.05, 0.2)}}}
        results = propose(down, summary, HUMAN_TRACES, COHORT)
        self.assertEqual([r.accepted for r in results], [False])
        self.assertIn("unavailable", results[0].reason)


class TestPrompt(unittest.TestCase):
    def test_prompt_carries_the_full_human_range_not_percentiles(self):
        """The gate rejects on one human hit, so a threshold chosen from
        p95 lands in the tail and always fails."""
        summary = summarise(COHORT, HUMAN_TRACES)
        prompt = build_prompt(summary, ("metronome",))
        self.assertIn("every human seen", prompt)
        self.assertIn("JSON only", prompt)
        for stats in summary.values():
            self.assertLessEqual(stats["human_min"], stats["human_median"])
            self.assertLessEqual(stats["human_median"], stats["human_max"])

    def test_summary_contains_no_raw_movement(self):
        summary = summarise(COHORT, HUMAN_TRACES)
        text = json.dumps(summary)
        self.assertNotIn("tick", text)
        self.assertTrue(set(summary) <= set(METRICS))


class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self.registry = ShadowDetectors()
        self.registry.add(Proposal(**GOOD))

    def test_shadow_detector_records_but_never_promotes_itself(self):
        for trace in COHORT[:10]:
            features, points, _ = measure(trace)
            self.registry.observe(features, points)
        name, hits, human_hits = self.registry.report()[0]
        self.assertEqual(name, "tight_leg_cycle")
        self.assertGreater(hits, 0)
        self.assertEqual(human_hits, 0)
        self.assertEqual(self.registry.promoted, {})

    def test_promotion_is_refused_after_a_known_human_hit(self):
        features, points, _ = measure(HUMAN_TRACES[0])
        self.registry._human_fires["tight_leg_cycle"] = 1
        with self.assertRaises(ValueError):
            self.registry.promote("tight_leg_cycle")

    def test_promotion_is_explicit(self):
        self.registry.promote("tight_leg_cycle")
        self.assertIn("tight_leg_cycle", self.registry.promoted)
        self.assertEqual(self.registry.shadow, {})
