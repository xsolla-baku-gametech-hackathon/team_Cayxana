"""The local model client, and the two ways a round is allowed to start.

Nothing here talks to LM Studio: the transport is exercised with recorded
payloads, and the layer is driven with a stand-in model. What is being
tested is the policy - asleep unless something is wrong, one round, model
unloaded - not the model's opinions.
"""

from __future__ import annotations

import unittest

from detection import settings
from detection.adaptive import MIN_DISAGREEMENTS, AdaptiveLayer
from detection.anomaly import BUSY_MULTIPLE, MIN_COHORT, MIN_TRIPS, AnomalyMonitor
from detection.hypothesis import schema, separation, summarise
from detection.llm import LLMUnavailable, LocalModelClient, _text_of
from detection.tuning import Observation
from tests.fixtures import BOTS, HUMANS, TRAP


def payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class Transport(unittest.TestCase):
    def test_a_reasoning_block_is_not_an_answer_and_not_an_error(self):
        text = _text_of(payload('<think>the human range is wide</think>{"a": 1}'))
        self.assertEqual(text, '{"a": 1}')

    def test_a_fenced_answer_is_unwrapped(self):
        self.assertEqual(_text_of(payload('```json\n{"a": 1}\n```')), '{"a": 1}')

    def test_an_empty_answer_is_an_unavailable_model(self):
        with self.assertRaises(LLMUnavailable):
            _text_of(payload("<think>...</think>"))
        with self.assertRaises(LLMUnavailable):
            _text_of({"choices": []})

    def test_a_model_that_was_never_loaded_is_not_unloaded(self):
        self.assertFalse(LocalModelClient().close())


class Separation(unittest.TestCase):
    """The arithmetic the model is no longer asked to do for itself."""

    def test_a_gap_above_the_humans_is_found_with_its_direction(self):
        gap = separation({"cohort_p5": 0.9, "cohort_p95": 1.0,
                          "human_min": 0.1, "human_max": 0.5})
        self.assertEqual(gap["direction"], "above")
        self.assertEqual(gap["window"], (0.5, 0.9))
        self.assertTrue(0.5 < gap["suggest"] < 0.9)

    def test_a_gap_below_the_humans_is_found(self):
        gap = separation({"cohort_p5": 0.01, "cohort_p95": 0.05,
                          "human_min": 0.2, "human_max": 0.9})
        self.assertEqual(gap["direction"], "below")

    def test_an_overlap_is_not_a_gap(self):
        self.assertIsNone(separation({"cohort_p5": 0.1, "cohort_p95": 0.6,
                                      "human_min": 0.2, "human_max": 0.9}))

    def test_the_schema_offers_only_measurements_that_separate(self):
        cohort = [BOTS["smooth_rails_bot"](s) for s in range(6)]
        humans = [g(s) for name, g in HUMANS.items() for s in range(2)
                  if name != "short_session_human"]
        summary = summarise(cohort, humans)
        branches = schema(summary)["anyOf"]
        self.assertTrue(branches)
        for branch in branches:
            metric = branch["properties"]["metric"]["const"]
            gap = summary[metric]["separation"]
            # The direction cannot be read backwards, and the threshold
            # cannot land on the human side of the gap: neither is a valid
            # token under the grammar this schema compiles to.
            self.assertEqual(branch["properties"]["direction"]["const"],
                             gap["direction"])
            low, high = sorted(gap["window"])
            self.assertEqual(branch["properties"]["threshold"]["minimum"], low)
            self.assertEqual(branch["properties"]["threshold"]["maximum"], high)

    def test_no_separation_falls_back_to_the_open_schema(self):
        self.assertIn("properties", schema({}))

    def test_no_separation_costs_no_model_call(self):
        """An overlapping table has no answer the gate would accept."""
        from detection.hypothesis import propose

        calls = []

        def model(prompt, schema=None):
            calls.append(prompt)
            return "{}"

        overlapping = {"speed_cv": {"cohort_p5": 0.1, "cohort_p95": 0.6,
                                    "human_min": 0.2, "human_max": 0.9,
                                    "separation": None}}
        results = propose(model, overlapping, [], [])
        self.assertEqual(calls, [])
        self.assertIn("no model call", results[0].reason)


class AnomalyTrigger(unittest.TestCase):
    """A cohort no rule explains is the thing that wakes the layer."""

    def setUp(self):
        self.monitor = AnomalyMonitor()

    def _quiet_repeat_tripper(self, player, trips=MIN_TRIPS * 2, spacing=40):
        for i in range(trips):
            self.monitor.observe(player, "invisible_entity", 0.2, False, i * spacing)

    def _ordinary_player(self, player):
        for i in range(MIN_TRIPS):
            self.monitor.observe(player, "invisible_entity", 0.2, False,
                                 i * int(40 * BUSY_MULTIPLE * 3))

    def test_an_ordinary_server_reports_nothing(self):
        for n in range(6):
            self._ordinary_player(f"p{n}")
        self.assertIsNone(self.monitor.report())

    def test_one_odd_player_is_not_a_cohort(self):
        for n in range(6):
            self._ordinary_player(f"p{n}")
        self._quiet_repeat_tripper("odd")
        self.assertIsNone(self.monitor.report())

    def test_a_cohort_of_quiet_repeat_trippers_is_reported(self):
        for n in range(6):
            self._ordinary_player(f"p{n}")
        for n in range(MIN_COHORT):
            self._quiet_repeat_tripper(f"bot{n}")
        report = self.monitor.report()
        self.assertIsNotNone(report)
        self.assertEqual(len(report.players), MIN_COHORT)
        self.assertIn("invisible_entity", report.categories)

    def test_a_player_the_rules_already_catch_is_not_in_the_cohort(self):
        for n in range(6):
            self._ordinary_player(f"p{n}")
        for n in range(MIN_COHORT):
            for i in range(MIN_TRIPS * 2):
                self.monitor.observe(f"caught{n}", "invisible_entity", 0.9,
                                     True, i * 40)
        self.assertIsNone(self.monitor.report())

    def test_an_uncalibrated_category_makes_the_report_urgent(self):
        for n in range(6):
            self._ordinary_player(f"p{n}")
        for n in range(MIN_COHORT):
            for i in range(MIN_TRIPS * 2):
                self.monitor.observe(f"bot{n}", "brand_new_trap", 0.2, False, i * 40)
        report = self.monitor.report()
        self.assertTrue(report.urgent)
        self.assertEqual(report.uncalibrated_categories, ("brand_new_trap",))


class Sleeping(unittest.TestCase):
    """No anomaly, no round - and no model loaded either."""

    def setUp(self):
        settings.reset()
        self.opened = 0
        self.closed = 0

    def tearDown(self):
        settings.reset()

    def _factory(self):
        outer = self

        class Stub:
            model = "stub"

            def __call__(self, prompt, schema=None):
                raise LLMUnavailable("not under test")

            def close(self):
                outer.closed += 1
                return True

        self.opened += 1
        return Stub()

    def _layer(self):
        return AdaptiveLayer(client_factory=self._factory, interval=0.0,
                             log=lambda line: None)

    def test_a_quiet_server_never_opens_the_model(self):
        layer = self._layer()
        for seed in range(20):
            layer._observations.append(Observation.from_trace(
                HUMANS["human"](seed), label="human", player_id="h",
                trap_event=TRAP))
        self.assertFalse(layer.maybe_run(now=1e9))
        self.assertEqual(self.opened, 0)

    def test_label_disagreement_opens_and_closes_the_model(self):
        layer = self._layer()
        layer._disagreements = MIN_DISAGREEMENTS
        layer._observations.extend(
            Observation.from_trace(BOTS["naive_bot"](s), label="bot",
                                   player_id="b", trap_event=TRAP)
            for s in range(10))
        record = layer.run_round()
        self.assertEqual(record["model"], "stub")
        self.assertEqual((self.opened, self.closed), (1, 1))
        # Discovery was not asked for: nothing reported a cohort.
        self.assertIn("no anomaly reported", record["discovery"][0]["reason"])

    def test_a_round_that_throws_still_unloads_the_model(self):
        layer = self._layer()
        layer._disagreements = MIN_DISAGREEMENTS
        def explode(*args, **kwargs):
            raise RuntimeError("the round fell over")

        layer._tune = explode
        with self.assertRaises(RuntimeError):
            layer.run_round()
        self.assertEqual((self.opened, self.closed), (1, 1))

    def test_an_injected_client_is_never_closed(self):
        closed = []

        class Injected:
            model = "injected"

            def __call__(self, prompt, schema=None):
                raise LLMUnavailable("not under test")

            def close(self):
                closed.append(True)
                return True

        layer = AdaptiveLayer(client=Injected(), interval=0.0,
                              log=lambda line: None)
        layer._disagreements = MIN_DISAGREEMENTS
        layer.run_round()
        self.assertEqual(closed, [])


if __name__ == "__main__":
    unittest.main()
