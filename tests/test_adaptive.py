"""The adaptive layer: what a model is allowed to change, and what it is not.

The point of these tests is not that the tuning helps. It is that a bad
answer cannot hurt: out-of-range numbers are clamped, a change that costs a
labelled human a new suspicion is refused, and an unreachable model leaves
the detector exactly as it was.
"""

import json
import unittest

from detection import settings
from detection.adaptive import (MIN_DISAGREEMENTS, AdaptiveLayer, _disagrees,
                                synthetic_observations)
from detection.llm import LLMUnavailable
from detection.tuning import (FALSE_POSITIVE_COST, MIN_RECALL, Observation,
                              assess, evaluate, parse_adjustments, per_player,
                              summarise, tune)
from tests.fixtures import BOTS, HUMANS, TRAP


def observations(humans=6, bots=6):
    rows = []
    for name, generator in HUMANS.items():
        rows += [Observation.from_trace(generator(s), label="human",
                                        player_id=f"h:{name}", trap_event=TRAP)
                 for s in range(humans)]
    for name, generator in BOTS.items():
        rows += [Observation.from_trace(generator(s), label="bot",
                                        player_id=f"b:{name}", trap_event=TRAP)
                 for s in range(bots)]
    return rows


def answer(**adjustments):
    return json.dumps({"adjustments": adjustments, "reasoning": "test"})


class Bounds(unittest.TestCase):
    def setUp(self):
        self.base = settings.defaults()

    def test_value_outside_the_bounds_is_clamped(self):
        candidate = self.base.with_overrides({"signature.min_periodicity": 99.0})
        self.assertLessEqual(candidate.signature["min_periodicity"],
                             settings.BOUNDS["signature.min_periodicity"][1])

    def test_one_round_cannot_jump_further_than_max_step(self):
        candidate = self.base.with_overrides({"weights.pause_variance": 0.0})
        moved = self.base.weights["pause_variance"] - candidate.weights["pause_variance"]
        self.assertAlmostEqual(moved, self.base.weights["pause_variance"] * settings.MAX_STEP)

    def test_unknown_knob_is_ignored_without_losing_the_good_one(self):
        candidate = self.base.with_overrides({"nonsense.knob": 1.0,
                                              "signature.min_rails_share": 0.4})
        self.assertEqual(self.base.diff(candidate),
                         {"signature.min_rails_share": (0.30, 0.4)})

    def test_nan_and_text_are_dropped(self):
        candidate = self.base.with_overrides({"suspicious": float("nan"),
                                              "signature.min_periodicity": "high"})
        self.assertEqual(self.base.diff(candidate), {})

    def test_rule_threshold_moves_from_the_rule_it_was_written_with(self):
        written = settings.written_rule_threshold("invisible_entity")
        candidate = self.base.with_overrides({"rule.invisible_entity.threshold": 0.9})
        self.assertEqual(self.base.diff(candidate),
                         {"rule.invisible_entity.threshold": (written, 0.9)})

    def test_a_persisted_file_round_trips(self):
        candidate = self.base.with_overrides({"signature.min_rails_share": 0.42,
                                              "rule.phantom_player.threshold": 0.8})
        restored = settings.Settings.from_dict(json.loads(json.dumps(candidate.as_dict())))
        self.assertEqual(candidate.as_dict(), restored.as_dict())

    def test_weights_are_used_normalised_so_the_evidence_floor_still_means_something(self):
        candidate = self.base.with_overrides({"weights.straightness": 0.05})
        self.assertAlmostEqual(sum(candidate.normalised_weights().values()), 1.0)


class Gate(unittest.TestCase):
    def setUp(self):
        settings.reset()
        self.rows = observations()

    def tearDown(self):
        settings.reset()

    def test_defaults_keep_every_labelled_human_clean(self):
        report = evaluate(self.rows, settings.live())
        self.assertEqual(report.human_hits, 0)
        self.assertGreater(report.recall, MIN_RECALL)

    def test_a_change_that_suspects_a_human_is_refused(self):
        # Loosening the periodicity signature catches more bots *and* reaches
        # the expert and corridor humans. More catches is not a defence.
        result = assess(answer(**{"signature.min_periodicity": 0.4}), self.rows)
        self.assertFalse(result.accepted)
        self.assertGreater(result.after.human_hits, 0)
        self.assertGreater(result.after.bot_hits, result.before.bot_hits)

    def test_a_change_that_only_costs_bot_coverage_is_refused(self):
        result = assess(answer(**{"signature.min_periodicity": 0.95,
                                  "signature.min_quantised_share": 0.9}), self.rows)
        self.assertFalse(result.accepted)

    def test_a_round_without_enough_labelled_evidence_does_nothing(self):
        result = assess(answer(**{"signature.min_rails_share": 0.4}), self.rows[:4])
        self.assertFalse(result.accepted)
        self.assertIn("not enough", result.reason)

    def test_unparseable_and_empty_answers_are_refused(self):
        for raw in ("not json", "[]", json.dumps({"adjustments": {}}),
                    json.dumps({"adjustments": {"no.such.knob": 1}})):
            self.assertFalse(assess(raw, self.rows).accepted, raw)

    def test_the_gate_prices_a_false_positive_above_a_missed_bot(self):
        self.assertGreater(FALSE_POSITIVE_COST, 1.0)

    def test_adjustments_may_arrive_as_a_list(self):
        overrides, _, reason = parse_adjustments(json.dumps(
            {"adjustments": [{"knob": "signature.min_rails_share", "value": 0.4}]}))
        self.assertEqual(reason, "ok")
        self.assertEqual(overrides, {"signature.min_rails_share": 0.4})

    def test_an_unreachable_model_leaves_the_numbers_alone(self):
        def dead(prompt):
            raise LLMUnavailable("HTTP 403")

        before = settings.live()
        result = tune(dead, self.rows)
        self.assertFalse(result.accepted)
        self.assertIs(settings.live(), before)

    def test_the_prompt_carries_no_player_movement(self):
        summary = summarise(self.rows, settings.live())
        text = json.dumps(summary)
        self.assertNotIn("\"x\"", text)
        self.assertNotIn("h:corridor_human", text)

    def test_per_player_surfaces_a_mislabelled_human(self):
        rows = self.rows + [Observation.from_trace(BOTS["naive_bot"](s), label="human",
                                                   player_id="autoplay", trap_event=TRAP)
                            for s in range(5)]
        worst = per_player(rows, settings.live())[0]
        self.assertEqual(worst["playerId"], "autoplay")
        self.assertEqual(worst["rate"], 1.0)


class Layer(unittest.TestCase):
    def setUp(self):
        settings.reset()

    def tearDown(self):
        settings.reset()

    def test_no_model_means_the_layer_is_inert(self):
        layer = AdaptiveLayer(client=None, client_factory=lambda: None)
        self.assertFalse(layer.maybe_run(now=1e9))
        record = layer.run_round(force=True)
        self.assertEqual(record["skipped"], "no model available")

    def test_an_accepted_round_is_applied_and_a_dry_run_is_not(self):
        """Start from numbers that suspect real humans; the round pulls back."""
        def model(prompt, schema=None):
            if "tuning a bot detector" in prompt:
                return answer(**{"signature.min_periodicity": 0.65})
            raise LLMUnavailable("discovery not under test")

        loose = settings.defaults().with_overrides({"signature.min_periodicity": 0.4})
        for apply_accepted in (False, True):
            settings.set_live(loose)
            layer = AdaptiveLayer(client=model, apply_accepted=apply_accepted,
                                  log=lambda line: None)
            layer._observations.extend(observations())
            record = layer.run_round(force=True)
            self.assertTrue(record["tuning"]["accepted"], record["tuning"]["reason"])
            self.assertEqual(record["tuning"]["after"]["humanHits"], 0)
            moved = settings.live().signature["min_periodicity"] != 0.4
            self.assertEqual(moved, apply_accepted)

    def test_a_model_that_raises_never_reaches_the_caller(self):
        def broken(prompt, schema=None):
            raise RuntimeError("boom")

        layer = AdaptiveLayer(client=broken, log=lambda line: None)
        layer._observations.extend(observations())
        record = layer.run_round(force=True)
        self.assertFalse(record["tuning"]["accepted"])
        self.assertEqual(settings.live().as_dict(), settings.defaults().as_dict())

    def test_synthetic_regression_suite_is_labelled_on_both_sides(self):
        counts = {"human": 0, "bot": 0}
        for observation in synthetic_observations(2):
            counts[observation.label] += 1
        self.assertTrue(counts["human"] and counts["bot"])


if __name__ == "__main__":
    unittest.main()


class Trigger(unittest.TestCase):
    """A round costs a model call, so agreement must cost nothing."""

    def setUp(self):
        settings.reset()
        self.calls = []
        self.layer = AdaptiveLayer(client=self._model, log=lambda line: None,
                                   interval=0.0)
        self.layer.note_player("b", "bot")
        # A declared human counts for nothing until a person confirms it
        # (labels.json); see AdaptiveLayer.label_for. This one is confirmed,
        # so a window of theirs that looks machine-like is a real
        # contradiction rather than a farm asking for less detection.
        self.layer.note_player("h", "human")
        self.layer._overrides["h"] = "human"

    def tearDown(self):
        settings.reset()

    def _model(self, prompt, schema=None):
        self.calls.append(prompt)
        return answer(**{"signature.min_rails_share": 0.4})

    def _feed(self, player, generator, count, suspicious):
        for seed in range(count):
            self.layer._observations.append(Observation.from_trace(
                generator(seed), label=self.layer.label_for(player),
                player_id=player, trap_event=TRAP))
            self.layer._seen_since_run += 1
            self.layer._disagreements += _disagrees(
                self.layer._observations[-1], suspicious)

    def test_agreement_never_starts_a_round(self):
        # A bot that looks like a bot and a human that looks human: nothing
        # to explain, so nothing is asked.
        self._feed("b", BOTS["naive_bot"], 10, suspicious=True)
        self._feed("h", HUMANS["human"], 10, suspicious=False)
        self.assertFalse(self.layer.maybe_run(now=1e9))
        self.assertEqual(self.calls, [])

    def test_a_human_that_looks_machine_like_starts_a_round(self):
        self._feed("h", HUMANS["human"], MIN_DISAGREEMENTS, suspicious=True)
        self.assertTrue(self.layer.maybe_run(now=1e9))
        self.layer._thread.join(60)
        self.assertTrue(self.calls)

    def test_a_bot_nobody_caught_starts_a_round(self):
        self._feed("b", BOTS["humanized_bot"], MIN_DISAGREEMENTS, suspicious=False)
        self.assertTrue(self.layer.maybe_run(now=1e9))
        self.layer._thread.join(60)
        self.assertTrue(self.calls)

    def test_one_disagreement_is_not_enough(self):
        self._feed("h", HUMANS["human"], 1, suspicious=True)
        self.assertFalse(self.layer.maybe_run(now=1e9))

    def test_the_counter_resets_after_a_round(self):
        self._feed("h", HUMANS["human"], MIN_DISAGREEMENTS, suspicious=True)
        self.layer.run_round(force=True)
        self.assertEqual(self.layer._disagreements, 0)
        self.assertFalse(self.layer.maybe_run(now=1e9))

    def test_a_round_with_nothing_to_buy_makes_no_model_call(self):
        # Every labelled human clean, every labelled bot caught: even a
        # perfect answer would be refused, so nothing is asked.
        self.layer.include_synthetic = False
        self.layer._observations.extend(
            Observation.from_trace(BOTS["naive_bot"](s), label="bot",
                                   player_id="b", trap_event=TRAP)
            for s in range(10))
        self.layer._observations.extend(
            Observation.from_trace(HUMANS["human"](s), label="human",
                                   player_id="h", trap_event=TRAP)
            for s in range(10))
        record = self.layer.run_round(force=True)
        self.assertIn("no model call", record["tuning"]["reason"])
        self.assertEqual(self.calls, [])

    def test_unlabelled_windows_cannot_trigger_a_round(self):
        for seed in range(20):
            self.layer._observations.append(Observation.from_trace(
                BOTS["naive_bot"](seed), label=None, player_id="unknown",
                trap_event=TRAP))
            self.layer._disagreements += _disagrees(
                self.layer._observations[-1], suspicious=False)
        self.assertEqual(self.layer._disagreements, 0)
