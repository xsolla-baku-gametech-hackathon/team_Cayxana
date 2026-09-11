import unittest

from bridge.__main__ import GameBridge
from detection import Detector, TrapSignal
from detection.suspicion import SuspicionStore


class BridgeTests(unittest.TestCase):
    def test_demo_requires_three_separate_observations(self):
        bridge = GameBridge()
        flags = []
        for start in (0, 300, 600):
            bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot', 'tick': start})
            # Repeated trips within an open window do not multiply evidence.
            bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot', 'tick': start+1})
            for tick in range(start+1, start+201):
                results = bridge.process({'type': 'move', 'tick': tick, 'players': [['p', tick*3, 0]]})
                flags.extend(r['flagged'] for r in results if r['type'] == 'verdict')
        self.assertEqual(flags, [False, False, True])

    def test_two_categories_can_be_selected_when_path_trap_is_ready(self):
        store = SuspicionStore(required_categories=2)
        for tick in range(3):
            self.assertFalse(store.record('p', 'invisible_entity', 1.0, tick).flagged)
        self.assertTrue(store.record('p', 'unreachable_bait', 1.0, 4).flagged)

    def test_unknown_categories_cannot_flag_in_demo(self):
        bridge = GameBridge()
        for start in (0, 300, 600):
            bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'unfinished_path', 'tick': start})
            for tick in range(start+1, start+201):
                results = bridge.process({'type': 'move', 'tick': tick, 'players': [['p', tick*3, 0]]})
                for result in results:
                    if result['type'] != 'verdict':
                        continue
                    self.assertTrue(result['shadow'])
                    self.assertFalse(result['flagged'])

    def test_session_reset_preserves_selected_policy(self):
        bridge = GameBridge(required_categories=2)
        bridge.process({'type': 'session', 'tickHz': 20, 'startedAt': 123})
        self.assertEqual(bridge.required_categories, 2)

    def test_non_suspicious_observations_do_not_flag(self):
        store = SuspicionStore(required_categories=1)
        for tick in range(20):
            self.assertFalse(store.record('p', 'invisible_entity', 0.2, tick).flagged)

    def test_real_game_category_and_post_trip_window(self):
        bridge = GameBridge()
        bridge.process({'type': 'session', 'tickHz': 20, 'startedAt': 123})
        bridge.process({'type': 'join', 'playerId': 'p', 'kind': 'human'})
        self.assertEqual(bridge.process({'type': 'trap', 'playerId': 'p',
                                        'category': 'ghost_loot', 'tick': 10}), [])
        result = []
        for tick in range(11, 211):
            result += bridge.process({'type': 'move', 'tick': tick, 'players': [['p', tick*3, 0]]})
        result = [r for r in result if r['type'] == 'verdict']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['category'], 'invisible_entity')
        self.assertEqual(result[0]['sourceCategory'], 'ghost_loot')
        self.assertFalse(result[0]['shadow'])
        self.assertFalse(result[0]['flagged'])
        self.assertEqual(result[0]['features']['samples'], 200)

    def test_disconnect_without_evidence(self):
        bridge = GameBridge()
        bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot', 'tick': 10})
        result = bridge.process({'type': 'leave', 'playerId': 'p', 'tick': 11})
        self.assertTrue(result[0]['insufficientData'])
        self.assertFalse(result[0]['flagged'])
        self.assertEqual(bridge.detector.watching('p'), [])

    def test_batch_does_not_extend_observation_window(self):
        detector = Detector()
        detector.on_trap_signal(TrapSignal('p', 'invisible_entity', 0))
        result = detector.on_movement('p', [{'tick': t, 'x': t*3, 'y': 0} for t in range(401)])
        self.assertEqual(result[0].features.span_ticks, 200)
        self.assertEqual(result[0].features.samples, 201)

    def test_incompatible_tick_rate_rejected(self):
        with self.assertRaises(ValueError):
            GameBridge().process({'type': 'session', 'tickHz': 60})
