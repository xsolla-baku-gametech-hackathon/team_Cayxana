"""Looping movement: farms that fall into a cycle, never a person wandering."""

import random
import unittest

from bridge.__main__ import GameBridge
from detection.looping import (FLAG_WINDOWS, MIN_RETARGET_TRIPS, WINDOW_TICKS,
                               LoopMonitor, window_signatures)
from tests.fixtures import _engine_step

MINUTES = 4 * WINDOW_TICKS


def keyboard_wander(seed, ticks=MINUTES):
    """A keyboard player: holds a direction for a while, looks around, turns."""
    rng = random.Random(9000 + seed)
    x, y, dx, dy, until, idle = 800.0, 450.0, 1, 0, 0, 0
    for tick in range(ticks):
        if tick >= until:
            if rng.random() < 0.1:
                dx = dy = 0
                idle = tick + rng.randint(10, 40)
                until = idle
            else:
                dx, dy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
                if dx == dy == 0:
                    dx = 1
                until = tick + rng.randint(8, 30)
        x, y = _engine_step(x, y, dx, dy)
        if x in (0.0, 1600.0) or y in (0.0, 900.0):
            until = tick  # hit the edge: pick a new direction next tick
        yield {'tick': tick, 'x': x, 'y': y}


def ping_pong(ticks=MINUTES, period=6):
    """Two targets pulling opposite ways: step right, step back, forever."""
    x = 800.0
    for tick in range(ticks):
        x, _ = _engine_step(x, 450.0, 1 if (tick // period) % 2 == 0 else -1, 0)
        yield {'tick': tick, 'x': x, 'y': 450.0}


def circuit(ticks=MINUTES, side=30):
    """Running the same square round a wall, lap after lap."""
    x, y = 800.0, 450.0
    moves = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    for tick in range(ticks):
        dx, dy = moves[(tick // side) % 4]
        x, y = _engine_step(x, y, dx, dy)
        yield {'tick': tick, 'x': x, 'y': y}


def feed(monitor, player, samples):
    out = []
    for sample in samples:
        out += monitor.movement(player, sample)
    return out


class LoopingTests(unittest.TestCase):
    def test_keyboard_wanderers_never_flag(self):
        for seed in range(20):
            monitor = LoopMonitor()
            records = feed(monitor, 'h', keyboard_wander(seed))
            self.assertFalse(monitor.is_flagged('h'), f"seed {seed}: {records[-1:]}")

    def test_back_and_forth_flags(self):
        monitor = LoopMonitor()
        records = feed(monitor, 'b', ping_pong())
        self.assertTrue(monitor.is_flagged('b'))
        self.assertIn('back_and_forth', records[0]['signatures'])

    def test_repeated_circuit_flags(self):
        monitor = LoopMonitor()
        records = feed(monitor, 'b', circuit())
        self.assertTrue(monitor.is_flagged('b'))
        self.assertIn('route_repeat', records[0]['signatures'])

    def test_one_looping_window_is_not_a_flag(self):
        monitor = LoopMonitor()
        records = feed(monitor, 'b', ping_pong(ticks=WINDOW_TICKS + 1))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['loopingWindows'], 1)
        self.assertFalse(monitor.is_flagged('b'))
        self.assertEqual(FLAG_WINDOWS, 2)

    def test_standing_still_is_not_judged(self):
        still = [{'tick': t, 'x': 100.0, 'y': 100.0} for t in range(WINDOW_TICKS + 1)]
        self.assertIsNone(window_signatures([(s['tick'], s['x'], s['y']) for s in still]))

    def test_turning_at_every_trip_flags_after_enough_trips(self):
        monitor = LoopMonitor()
        x, dx, records = 800.0, 1, []
        for tick in range(20 * (MIN_RETARGET_TRIPS + 2)):
            if tick % 20 == 0:
                monitor.trip('b', tick)
            if tick % 20 == 4:
                dx = -dx  # new traps appeared elsewhere: swing round at once
            x, _ = _engine_step(x, 450.0, dx, 0)
            records += monitor.movement('b', {'tick': tick, 'x': x, 'y': 450.0})
        retarget = [r for r in records if 'trap_retarget' in r['signatures']]
        self.assertEqual(len(retarget), 1)
        self.assertTrue(retarget[0]['flagged'])
        self.assertGreaterEqual(retarget[0]['retargetTrips'], MIN_RETARGET_TRIPS)

    def test_bridge_marks_verdicts_and_forgets_on_leave(self):
        bridge = GameBridge()
        out = []
        for sample in ping_pong(ticks=3 * WINDOW_TICKS):
            if sample['tick'] == 2 * WINDOW_TICKS:
                out += bridge.process({'type': 'trap', 'playerId': 'p', 'category': 'ghost_loot',
                                       'tick': sample['tick'], 'x': 0, 'y': 0})
            out += bridge.process({'type': 'move', 'tick': sample['tick'],
                                   'players': [['p', sample['x'], sample['y']]]})
        self.assertTrue(any(r['type'] == 'loop' and r['flagged'] for r in out))
        verdicts = [r for r in out if r['type'] == 'verdict']
        self.assertTrue(verdicts)
        self.assertTrue(all(v['flagged'] and v['loopFlagged'] for v in verdicts))
        bridge.process({'type': 'leave', 'playerId': 'p', 'tick': 3 * WINDOW_TICKS})
        self.assertFalse(bridge.looping.is_flagged('p'))


if __name__ == '__main__':
    unittest.main()
