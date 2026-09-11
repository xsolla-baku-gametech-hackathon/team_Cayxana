import unittest

from detection.features import Features
from detection.scoring import score_details, score_features


def f(straightness=None, pause_variance=None, idle_share=None,
      reaction_ticks=None, revisits=None, samples=0, span_ticks=0):
    return Features(straightness, pause_variance, idle_share, reaction_ticks,
                    revisits, samples, span_ticks)


class PartialScoreTests(unittest.TestCase):
    def test_missing_pauses_alone_still_scores(self):
        """A window with no pause in it is evidence, not a missing value.

        This is the never-resting farming bot: it has no pause rhythm to
        measure, and before ``idle_share`` existed that cost it 0.40 of
        weight and left it permanently unscoreable.
        """
        features = f(straightness=1.0, idle_share=0.0, reaction_ticks=0.0,
                     revisits=0, samples=200, span_ticks=199)
        details = score_details(features)
        self.assertEqual(details['scoreStatus'], 'complete')
        self.assertIsNone(details['partialScore'])
        self.assertEqual(details['missingFeatures'], ['pause_variance'])
        score = score_features(features)
        self.assertAlmostEqual(score.value, 1.0)
        self.assertTrue(score.is_suspicious())

    def test_long_window_without_trap_event_scores(self):
        """The 60-second monitoring window has no trap, so no reaction time."""
        features = f(straightness=.5, idle_share=0.0, revisits=3,
                     samples=1200, span_ticks=1199)
        self.assertIsNone(score_details(features)['partialScore'])
        self.assertIsNotNone(score_features(features).value)

    def test_partial_is_for_a_window_with_no_rhythm_evidence_at_all(self):
        features = f(straightness=.5, reaction_ticks=20.0, revisits=3,
                     samples=200, span_ticks=199)
        details = score_details(features)
        self.assertEqual(details['scoreStatus'], 'missing_features')
        self.assertIsNotNone(details['partialScore'])
        self.assertIsNone(score_features(features).value)

    def test_short_and_stationary_traces_stay_unknown(self):
        cases = [(f(straightness=1, revisits=0, samples=5, span_ticks=4), 'short_trace'),
                 (f(idle_share=1.0, revisits=0, samples=200, span_ticks=199), 'no_movement')]
        for features, status in cases:
            self.assertIsNone(score_details(features)['partialScore'])
            self.assertEqual(score_details(features)['scoreStatus'], status)
            self.assertIsNone(score_features(features).value)

    def test_complete_score_unchanged(self):
        details = score_details(f(.5, 2, .2, 20, 3, 200, 199))
        self.assertEqual(details['scoreStatus'], 'complete')
        self.assertIsNone(details['partialScore'])


if __name__ == "__main__":
    unittest.main()
