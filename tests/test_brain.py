"""Steps 2-5: scoring, suspicion, bandit, burn detection."""

import unittest

from detection.features import Features
from detection.scoring import SUSPICIOUS, classify, score_features
from tests.fixtures import (BOTS, HUMANS, TRAP, bot_trace, human_trace,
                            lagged_human_trace, simulate_session)

SAMPLES = 50


class TestScoring(unittest.TestCase):
    # A smooth analogue-stick human who walks a fairly direct line with
    # regular short pauses is, in these four features, identical to a bot.
    # No weighting fixes that; only a fifth feature (speed variability) or a
    # model could, and both are ruled out. The trap-level rate is therefore
    # capped rather than zeroed, and zero is enforced where a ban is decided:
    # see TestSessionLevel.
    MAX_TRAP_FALSE_POSITIVE_RATE = 0.005

    def test_human_false_positive_rate_stays_under_the_cap(self):
        flagged = [
            (label, seed, round(s.value, 3))
            for label, gen in HUMANS.items()
            for seed in range(SAMPLES)
            for s in [classify(gen(seed), TRAP)]
            if s.is_suspicious()
        ]
        rate = len(flagged) / (SAMPLES * len(HUMANS))
        self.assertLessEqual(rate, self.MAX_TRAP_FALSE_POSITIVE_RATE, f"false positives: {flagged}")

    def test_the_ordinary_human_populations_are_never_flagged(self):
        """Everything except the smooth-analogue edge case must be exactly 0."""
        for label, gen in HUMANS.items():
            if label == "mobile_human":
                continue
            hits = [s for s in range(SAMPLES) if classify(gen(s), TRAP).is_suspicious()]
            self.assertEqual(hits, [], f"{label} flagged at seeds {hits}")

    def test_short_and_frozen_traces_are_refused_not_scored(self):
        """Thin evidence must return "insufficient data", never a confident 1.0."""
        for label in ("short_session_human",):
            for seed in range(SAMPLES):
                self.assertTrue(classify(HUMANS[label](seed), TRAP).insufficient_data, label)

    def test_bots_are_caught(self):
        caught = sum(classify(bot_trace(s), TRAP).is_suspicious() for s in range(SAMPLES))
        self.assertEqual(caught, SAMPLES)

    def test_populations_are_separated(self):
        worst_bot = min(classify(bot_trace(s), TRAP).value for s in range(SAMPLES))
        best_human = max(
            classify(gen(s), TRAP).value
            for gen in (human_trace, lagged_human_trace)
            for s in range(SAMPLES)
        )
        self.assertGreater(worst_bot - best_human, 0.2, "margin too thin to trust")
        self.assertTrue(best_human < SUSPICIOUS < worst_bot)

    def test_lagged_human_is_not_flagged(self):
        """Lag produces late, bursty, duplicated samples - never a flag."""
        for seed in range(SAMPLES):
            score = classify(lagged_human_trace(seed), TRAP)
            self.assertFalse(score.is_suspicious(), f"seed {seed}: {score.value}")

    def test_all_features_null_is_insufficient_data_not_nan(self):
        score = score_features(Features(None, None, None, None, None))
        self.assertIsNone(score.value)
        self.assertTrue(score.insufficient_data)
        self.assertFalse(score.is_suspicious())

    def test_null_feature_renormalises_instead_of_scoring_zero(self):
        both = score_features(Features(1.0, 0.0, 0.0, 0.0, 0.0, samples=200, span_ticks=200))
        partial = score_features(Features(1.0, 0.0, 0.0, None, 0.0, samples=200, span_ticks=200))
        self.assertAlmostEqual(both.value, 1.0)
        self.assertAlmostEqual(partial.value, 1.0)  # not dragged by a phantom term
        self.assertNotIn("reaction_ticks", partial.used)

    def test_empty_trace_never_crashes(self):
        self.assertTrue(classify([], TRAP).insufficient_data)


if __name__ == "__main__":
    unittest.main()


class TestSuspicion(unittest.TestCase):
    def setUp(self):
        from detection.suspicion import SuspicionStore

        self.store = SuspicionStore()

    def test_three_hits_one_category_is_not_a_flag(self):
        for tick in range(3):
            p = self.store.record("p1", "invisible_entity", 0.99, tick)
        self.assertFalse(p.flagged)

    def test_three_hits_three_categories_flags(self):
        for tick, cat in enumerate(("invisible_entity", "position_offset", "phantom_player")):
            p = self.store.record("p1", cat, 0.99, tick)
        self.assertTrue(p.flagged)

    def test_two_high_one_low_is_not_a_flag(self):
        self.store.record("p1", "invisible_entity", 0.99, 0)
        self.store.record("p1", "position_offset", 0.99, 1)
        p = self.store.record("p1", "phantom_player", 0.10, 2)
        self.assertFalse(p.flagged)

    def test_insufficient_data_is_not_evidence(self):
        from detection.scoring import Score
        from detection.features import Features

        empty = Score(None, Features(None, None, None, None, None), ())
        for tick, cat in enumerate(("a", "b", "c")):
            p = self.store.record("p1", cat, empty, tick)
        self.assertEqual(p.events, [])
        self.assertFalse(p.flagged)

    def test_lagged_human_full_session_is_never_flagged(self):
        """Every trap category fires on one laggy player for a whole session."""
        categories = ("invisible_entity", "position_offset", "phantom_player", "unreachable_bait")
        for seed in range(20):
            for i, category in enumerate(categories * 3):
                score = classify(lagged_human_trace(seed + i), TRAP)
                player = self.store.record(f"lagger{seed}", category, score, i)
            self.assertFalse(player.flagged, f"lagged human flagged (seed {seed})")

    def test_bot_across_categories_is_flagged(self):
        for i, category in enumerate(("invisible_entity", "position_offset", "phantom_player")):
            player = self.store.record("bot1", category, classify(bot_trace(i), TRAP), i)
        self.assertTrue(player.flagged)


class TestBandit(unittest.TestCase):
    def setUp(self):
        import random

        from detection import bandit

        self.bandit = bandit
        self.rng = random.Random(7)

    def counts(self, stats, draws=4000):
        picks = {}
        for _ in range(draws):
            name = self.bandit.pick_category(stats, self.rng)
            picks[name] = picks.get(name, 0) + 1
        return picks

    def test_strong_arm_dominates_but_weak_arm_survives(self):
        stats = {
            "strong": self.bandit.CategoryStats(caught=20, missed=2),
            "weak": self.bandit.CategoryStats(caught=0, missed=20),
        }
        picks = self.counts(stats)
        self.assertGreater(picks["strong"], picks.get("weak", 0) * 5)
        self.assertGreater(picks.get("weak", 0), 0, "exploration collapsed: arm can never recover")

    def test_no_data_is_roughly_uniform(self):
        stats = self.bandit.new_stats()
        picks = self.counts(stats)
        share = [c / sum(picks.values()) for c in picks.values()]
        self.assertEqual(len(picks), 4)
        self.assertTrue(all(0.15 < s < 0.35 for s in share), picks)

    def test_false_positive_costs_ten_misses(self):
        stats = self.bandit.new_stats()
        self.bandit.record_false_positive(stats, "position_offset")
        self.bandit.record_miss(stats, "phantom_player")
        self.assertEqual(stats["position_offset"].missed, 10)
        self.assertEqual(stats["phantom_player"].missed, 1)

    def test_burned_categories_are_not_deployed(self):
        stats = self.bandit.new_stats()
        stats["invisible_entity"].burned = True
        picks = self.counts(stats, draws=1000)
        self.assertNotIn("invisible_entity", picks)


class TestBurnDetection(unittest.TestCase):
    def setUp(self):
        from detection.burn import BurnMonitor

        self.monitor = BurnMonitor()
        self.now = 100_000.0

    def fill_baseline(self, sessions, catch_rate=0.5, per_session=10):
        """Five healthy windows before the current one."""
        for w in range(1, 6):
            at = self.now - self.monitor.window * w - 1.0
            for s in range(sessions):
                for i in range(per_session):
                    self.monitor.record("invisible_entity", f"s{w}_{s}", i / per_session < catch_rate, at)

    def fill_current(self, sessions, catch_rate=0.0, per_session=10):
        at = self.now - 10.0
        for s in range(sessions):
            for i in range(per_session):
                self.monitor.record("invisible_entity", f"cur{s}", i / per_session < catch_rate, at)

    def test_single_session_drop_does_not_burn(self):
        self.fill_baseline(sessions=20)
        self.fill_current(sessions=1, catch_rate=0.0, per_session=60)
        self.fill_current(sessions=19, catch_rate=0.5)
        verdict = self.monitor.evaluate("invisible_entity", self.now)
        self.assertFalse(verdict.burned, verdict)

    def test_simultaneous_drop_across_many_sessions_burns(self):
        self.fill_baseline(sessions=20)
        self.fill_current(sessions=100, catch_rate=0.0)
        verdict = self.monitor.evaluate("invisible_entity", self.now)
        self.assertTrue(verdict.burned, verdict)
        self.assertGreaterEqual(verdict.affected_sessions, 100)

    def test_low_traffic_never_burns(self):
        self.fill_baseline(sessions=20)
        for i in range(3):  # three triggers, no catches, whole window
            self.monitor.record("invisible_entity", f"q{i}", False, self.now - 5.0)
        verdict = self.monitor.evaluate("invisible_entity", self.now)
        self.assertFalse(verdict.burned)
        self.assertIn("insufficient sample", verdict.reason)

    def test_no_baseline_never_burns(self):
        self.fill_current(sessions=100, catch_rate=0.0)
        self.assertFalse(self.monitor.evaluate("invisible_entity", self.now).burned)

    def test_healthy_category_is_not_burned(self):
        self.fill_baseline(sessions=20)
        self.fill_current(sessions=100, catch_rate=0.5)
        self.assertFalse(self.monitor.evaluate("invisible_entity", self.now).burned)

    def test_bandit_reallocates_after_a_burn(self):
        import random

        from detection import bandit

        stats = bandit.new_stats()
        stats["invisible_entity"].caught = 200  # the current favourite
        self.fill_baseline(sessions=20)
        self.fill_current(sessions=100, catch_rate=0.0)

        self.assertEqual(self.monitor.sweep(stats, self.now), ["invisible_entity"])
        rng = random.Random(3)
        picks = {bandit.pick_category(stats, rng) for _ in range(20)}
        self.assertNotIn("invisible_entity", picks)
        self.assertGreater(len(picks), 1)


class TestInvention(unittest.TestCase):
    def setUp(self):
        from detection import invention

        self.inv = invention
        self.valid = {
            "name": "stale_health",
            "description": "server reports health the client never renders",
            "entity_field": "health",
            "server_value": "0",
            "client_value": "100",
            "x": 120.0,
            "y": 340.0,
        }

    def test_valid_proposal_is_accepted(self):
        category, reason = self.inv.validate_category(self.valid)
        self.assertIsNotNone(category, reason)
        self.assertEqual(category.name, "stale_health")

    def test_markdown_fences_are_stripped(self):
        import json

        raw = "```json\n" + json.dumps(self.valid) + "\n```"
        self.assertIsNotNone(self.inv.validate_category(raw)[0])

    def test_malformed_json_is_rejected_not_raised(self):
        for raw in ("{not json", "", "null", "[1, 2]", 42):
            category, reason = self.inv.validate_category(raw)
            self.assertIsNone(category, raw)
            self.assertTrue(reason)

    def test_unknown_and_missing_fields_are_rejected(self):
        self.assertIsNone(self.inv.validate_category({**self.valid, "evil": 1})[0])
        self.assertIsNone(self.inv.validate_category({k: v for k, v in self.valid.items() if k != "x"})[0])
        self.assertIsNone(self.inv.validate_category({**self.valid, "entity_field": "hp"})[0])

    def test_out_of_bounds_coordinates_are_clamped_not_rejected(self):
        category, _ = self.inv.validate_category({**self.valid, "x": 99999, "y": -500})
        self.assertIsNotNone(category)
        self.assertEqual((category.x, category.y), (1000.0, 0.0))

    def test_duplicate_of_existing_category_is_rejected(self):
        proposal = {**self.valid, "name": "position_offset"}
        category, reason = self.inv.validate_category(proposal, self.inv.HARDCODED_POOL)
        self.assertIsNone(category)
        self.assertIn("duplicate", reason)

    def test_unreachable_llm_falls_back_to_hardcoded_pool(self):
        def down(prompt):
            raise ConnectionError("endpoint unreachable")

        accepted, rejected = self.inv.invent(down, {"position_offset": 14.0}, ["invisible_entity"], ())
        self.assertEqual(accepted, [])
        self.assertTrue(rejected)
        self.assertEqual(self.inv.available_categories(None), self.inv.HARDCODED_POOL)

    def test_mixed_batch_keeps_only_the_usable_proposal(self):
        import json

        batch = ["garbage", json.dumps(self.valid), json.dumps({**self.valid, "client_value": "0"})]
        accepted, rejected = self.inv.invent(lambda p: batch, {}, [], self.inv.HARDCODED_POOL)
        self.assertEqual([c.name for c in accepted], ["stale_health"])
        self.assertEqual(len(rejected), 2)

    def test_shadow_category_cannot_flag_before_promotion(self):
        from detection.suspicion import SuspicionStore

        registry = self.inv.ShadowRegistry()
        registry.add(self.inv.validate_category(self.valid)[0])
        store = SuspicionStore(shadow_categories=registry.shadow)

        for tick, cat in enumerate(("position_offset", "phantom_player", "stale_health")):
            player = store.record("p1", cat, 0.99, tick)
        self.assertFalse(player.flagged, "a shadow category contributed to a flag")

    def test_promotion_requires_volume_catches_and_zero_human_flags(self):
        registry = self.inv.ShadowRegistry()
        registry.add(self.inv.validate_category(self.valid)[0])

        for i in range(self.inv.MIN_SHADOW_TRIGGERS - 1):
            registry.observe("stale_health", caught=i % 5 == 0)
        self.assertEqual(registry.promote_ready(), [])  # not enough volume yet

        registry.observe("stale_health", caught=True)
        self.assertEqual(registry.promote_ready(), ["stale_health"])
        self.assertIn("stale_health", registry.flagging_categories())

    def test_a_human_flag_blocks_promotion(self):
        registry = self.inv.ShadowRegistry()
        registry.add(self.inv.validate_category(self.valid)[0])
        for _ in range(self.inv.MIN_SHADOW_TRIGGERS):
            registry.observe("stale_health", caught=True)
        registry.observe("stale_health", human_flag=True)
        self.assertEqual(registry.promote_ready(), [])

    def test_prompt_mentions_burned_category_and_schema(self):
        prompt = self.inv.build_prompt({"position_offset": 14.0}, ["invisible_entity"])
        self.assertIn("BURNED", prompt)
        self.assertIn("invisible_entity", prompt)
        self.assertIn("JSON only", prompt)


class TestSessionLevel(unittest.TestCase):
    """A ban follows from a whole session, so that is where the metric lives."""

    SESSIONS = 100

    def flag_rate(self, generator):
        hits = sum(simulate_session(generator, s).flagged for s in range(self.SESSIONS))
        return hits / self.SESSIONS

    def test_no_human_population_is_ever_flagged_over_a_session(self):
        offenders = {
            name: rate
            for name, gen in HUMANS.items()
            for rate in [self.flag_rate(gen)]
            if rate > 0
        }
        self.assertEqual(offenders, {}, f"humans flagged: {offenders}")

    def test_unsophisticated_bots_are_caught(self):
        """jitter_bot occasionally survives: invisible_entity demands 0.80,
        because that category is the one a human can trip by accident."""
        self.assertEqual(self.flag_rate(BOTS["naive_bot"]), 1.0)
        self.assertGreaterEqual(self.flag_rate(BOTS["jitter_bot"]), 0.95)

    def test_signature_bots_are_caught(self):
        """Randomised pauses and waypoint paths used to score mid-range
        forever and never flag. The narrow signature detectors close that
        gap: a bot may fake a rhythm, but not a varying speed."""
        for name in ("random_pause_bot", "waypoint_bot"):
            self.assertEqual(self.flag_rate(BOTS[name]), 1.0, name)

    def test_fully_mimicking_bot_still_escapes_and_that_is_recorded_here(self):
        """Documented blind spot, not an accident.

        humanized_bot varies speed, wanders, pauses raggedly and delays its
        reaction. Nothing in a movement trace separates it from a human, so
        no threshold or signature can catch it - only a new data source
        (input events, inter-trap timing) would. If this ever starts
        getting caught, check the false-positive numbers first.
        """
        self.assertEqual(self.flag_rate(BOTS["humanized_bot"]), 0.0)
