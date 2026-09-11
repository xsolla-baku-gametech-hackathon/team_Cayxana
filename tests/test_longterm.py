import unittest
from types import SimpleNamespace
from unittest.mock import patch

from detection.longterm import LongTermMonitor, WINDOW, HORIZON, REPORT


class LongTermTests(unittest.TestCase):
    def setUp(self):
        self.monitor = LongTermMonitor()
        self.monitor.start('p', 0)

    def feed(self, start, end, score):
        records = []
        with patch('detection.longterm.classify', return_value=SimpleNamespace(value=score)):
            for tick in range(start+1, end+1):
                records += self.monitor.movement('p', {'tick': tick, 'x': tick, 'y': 0})
        return records

    def test_seven_minute_report_and_nine_minute_flag(self):
        records = self.feed(0, HORIZON, .8)
        reports = [r for r in records if r['reportUpdated']]
        self.assertEqual([r['tick'] for r in reports], [REPORT])
        self.assertAlmostEqual(reports[0]['report7m']['averageScore'], .8)
        self.assertFalse(any(r['flagged'] for r in records[:-1]))
        self.assertTrue(records[-1]['highRisk'])
        later = self.feed(HORIZON, 2*REPORT, .2)
        self.assertTrue(later[-1]['reportUpdated'])
        self.assertEqual(later[-1]['report7m']['expectedWindows'], REPORT // WINDOW)
        self.assertTrue(later[-1]['flagged'])

    def test_missing_scores_not_zero_or_false_confidence(self):
        self.feed(0, WINDOW, .9)
        record = self.feed(WINDOW, HORIZON, None)[-1]
        self.assertAlmostEqual(record['rolling9m']['averageScore'], .9)
        self.assertFalse(record['eligible'])
        self.assertFalse(record['flagged'])

    def test_threshold_is_strict(self):
        self.assertFalse(self.feed(0, HORIZON, .75)[-1]['highRisk'])

    def test_gap_and_duplicate_ticks_do_not_create_coverage(self):
        self.feed(0, WINDOW, .9)
        self.monitor.movement('p', {'tick': WINDOW, 'x': 0, 'y': 0})
        record = self.monitor.movement('p', {'tick': HORIZON, 'x': 0, 'y': 0})[-1]
        self.assertEqual(record['rolling9m']['validWindows'], 1)
        self.assertFalse(record['flagged'])

    def test_untrapped_and_disconnected_players_not_monitored(self):
        self.assertEqual(self.monitor.movement('other', {'tick': 100}), [])
        self.monitor.forget('p')
        self.assertEqual(self.monitor.movement('p', {'tick': 100}), [])

    def test_retrip_does_not_restart_clock(self):
        self.monitor.start('p', 500)
        self.assertEqual(self.monitor.players['p'].started, 0)
