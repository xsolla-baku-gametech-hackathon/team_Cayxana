"""Replay a session or follow its live movement stream: python -m bridge."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from detection import Detector, TrapSignal
from detection.clock import TICK_HZ
from detection.longterm import LongTermMonitor
from detection.looping import LoopMonitor
from detection.rules import canonical_category, rule_for
from detection.scoring import score_details
from detection.suspicion import REQUIRED_EVENTS
from detection.traprate import TrapRateMonitor
from .reader import GameLogReader, latest_session


class GameBridge:
    def __init__(self, required_categories=1, ai=None):
        self.required_categories = required_categories
        # The adaptive layer survives a session restart: it is the only
        # thing here that is supposed to carry knowledge across sessions.
        self.ai = ai
        self.reset()

    def reset(self):
        """Drop every per-session monitor. Keeps ``ai``, which outlives one."""
        self.detector = Detector(required_categories=self.required_categories)
        self.long_term = LongTermMonitor()
        self.trap_rate = TrapRateMonitor()
        self.looping = LoopMonitor()
        self.players = set()
        self.session = None

    def process(self, record):
        """One log record in, the verdicts and monitoring records it produced out."""
        tick = record.get('tick', 0)
        handler = getattr(self, f"_on_{record['type']}", None)
        verdicts, monitoring = handler(record, tick) if handler else ([], [])
        if self.ai:
            for verdict in verdicts:
                self.ai.observe(verdict)
            self.ai.maybe_run()  # returns at once; a round runs off-thread
        return ([self.serialize(v, tick) for v in verdicts]
                + [dict(r, session=self.session) for r in monitoring])

    # -- one handler per record type; each returns (verdicts, monitoring) ----

    def _on_session(self, record, tick):
        if record.get('tickHz') != TICK_HZ:
            raise ValueError(f'Detector requires a {TICK_HZ} Hz game stream')
        self.reset()
        self.session = record.get('startedAt')
        return [], []

    def _on_join(self, record, tick):
        self.players.add(record['playerId'])
        if self.ai:
            # Self-declared kind. It reaches the offline gate only; the
            # classifier below never sees it.
            self.ai.note_player(record['playerId'], record.get('kind'))
        return [], []

    def _on_trap(self, record, tick):
        player = record['playerId']
        self.players.add(player)
        monitoring = []
        rate = self.trap_rate.trip(player, tick)
        if rate:
            monitoring.append(rate)
        self.looping.trip(player, tick)
        # Same invisible-loot mechanism; retain original category for audit.
        category = canonical_category(record['category'])
        self.detector.on_trap_signal(TrapSignal(
            player, category, tick, record.get('x'), record.get('y'),
            record.get('trapId'), {'sourceCategory': record['category']}))
        if not rule_for(category).shadow:
            self.long_term.start(player, tick)
        return [], monitoring

    def _on_move(self, record, tick):
        verdicts, monitoring = [], []
        for player, x, y in record['players']:
            sample = {'tick': tick, 'x': x, 'y': y}
            verdicts.extend(self.detector.on_movement(player, [sample]))
            monitoring.extend(self.long_term.movement(player, sample))
            monitoring.extend(self.looping.movement(player, sample))
            if self.ai:
                self.ai.movement(player, sample)
        return verdicts, monitoring

    def _on_leave(self, record, tick):
        player = record['playerId']
        verdicts = self.detector.on_disconnect(player)
        self.players.discard(player)
        self.long_term.forget(player)
        self.trap_rate.forget(player)
        self.looping.forget(player)
        if self.ai:
            self.ai.forget(player)
        return verdicts, []

    def serialize(self, verdict, tick):
        return dict(type='verdict', session=self.session, playerId=verdict.player_id,
                    tick=tick, signalTick=verdict.signal.tick, category=verdict.category,
                    sourceCategory=verdict.signal.metadata.get('sourceCategory'),
                    trapId=verdict.signal.trap_id, score=verdict.score,
                    suspicious=verdict.suspicious,
                    flagged=(verdict.flagged or self.trap_rate.is_flagged(verdict.player_id)
                             or self.looping.is_flagged(verdict.player_id)),
                    trapRateFlagged=self.trap_rate.is_flagged(verdict.player_id),
                    loopFlagged=self.looping.is_flagged(verdict.player_id),
                    shadow=verdict.rule.shadow, insufficientData=verdict.insufficient_data,
                    signatures=list(verdict.signatures), features=asdict(verdict.features),
                    rule=asdict(verdict.rule),
                    policy={'requiredEvents': REQUIRED_EVENTS,
                            'requiredCategories': self.required_categories},
                    **score_details(verdict.features))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--follow', action='store_true')
    parser.add_argument('--required-categories', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--ai', action='store_true',
                        help='arm the adaptive layer: it stays asleep until an '
                             'anomaly is reported, then asks the local model')
    parser.add_argument('--ai-interval', type=float, default=None,
                        help='seconds between adaptive rounds (default 300)')
    parser.add_argument('--ai-dry-run', action='store_true',
                        help='report accepted tuning but keep the current numbers')
    args = parser.parse_args()
    trace = args.trace or latest_session()
    if trace is None:
        parser.error('No game session found. Start python run_game.py first.')
    output = args.output or trace.with_name(trace.stem + '-verdicts.jsonl')
    if output.resolve() == trace.resolve():
        parser.error('Output must differ from input')
    output.parent.mkdir(parents=True, exist_ok=True)
    ai = None
    if args.ai:
        from detection.adaptive import AdaptiveLayer
        ai = AdaptiveLayer(out_dir=output.parent, apply_accepted=not args.ai_dry_run,
                           **({'interval': args.ai_interval} if args.ai_interval else {}))
        if not ai.enabled:
            print('[ai] no adaptive layer configured; staying off', flush=True)
            ai = None
        elif ai.probe():
            print('[ai] armed and asleep; a round starts only when an anomaly '
                  'is reported, and the model is unloaded again after it',
                  flush=True)
        else:
            print('[ai] armed, but no local model is reachable (is LM Studio '
                  'serving?); rounds will be skipped until one is', flush=True)
    bridge = GameBridge(required_categories=args.required_categories, ai=ai)
    count = 0
    with output.open('w', encoding='utf-8') as handle:
        try:
            for record in GameLogReader(trace, follow=args.follow):
                for verdict in bridge.process(record):
                    handle.write(json.dumps(verdict, allow_nan=False) + '\n')
                    handle.flush()
                    count += 1
                    print(json.dumps(verdict), flush=True)
        except KeyboardInterrupt:
            pass
    print(f'{count} verdicts written to {output}', flush=True)


if __name__ == '__main__':
    main()
