import unittest

from bridge.__main__ import GameBridge
from detection.traprate import MAX_TRIPS, WINDOW_TICKS, TrapRateMonitor


class TrapRateTests(unittest.TestCase):
    def test_worst_measured_human_rate_does_not_flag(self):
        monitor = TrapRateMonitor()
        # 6 trips in a minute was the most any real player managed, sustained here for 10 minutes.
        results = [monitor.trip('p', tick) for tick in range(0, 12000, 200)]
        self.assertEqual([r for r in results if r], [])

    def test_farm_rate_flags_once(self):
        monitor = TrapRateMonitor()
        results = [monitor.trip('p', tick) for tick in range(0, 1200, 20)]  # 60 trips a minute
        flags = [r for r in results if r]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['trips'], MAX_TRIPS)

    def test_trips_older_than_the_window_do_not_count(self):
        monitor = TrapRateMonitor()
        for i in range(MAX_TRIPS - 1):
            monitor.trip('p', i)
        self.assertIsNone(monitor.trip('p', WINDOW_TICKS + MAX_TRIPS))

    def test_bridge_marks_later_verdicts_flagged(self):
        bridge = GameBridge()
        out = []
        for tick in range(0, 20 * MAX_TRIPS, 20):
            out += bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot', 'tick': tick})
        self.assertEqual([r['type'] for r in out], ['trap_rate'])
        verdicts = []
        for tick in range(1, 500):
            verdicts += [r for r in bridge.process({'type': 'move', 'tick': tick, 'players': [['p', 100 + tick % 7, 100]]})
                         if r['type'] == 'verdict']
        self.assertTrue(verdicts)
        self.assertTrue(all(v['flagged'] and v['trapRateFlagged'] for v in verdicts))

    def test_leave_forgets_the_rate(self):
        bridge = GameBridge()
        for tick in range(MAX_TRIPS):
            bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot', 'tick': tick})
        bridge.process({'type': 'leave', 'playerId': 'p', 'tick': MAX_TRIPS})
        self.assertFalse(bridge.trap_rate.is_flagged('p'))
