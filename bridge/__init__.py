"""Bridge between the game engine and the detection brain.

The game engine (Node, ``team_Cayxana-main/game_engine``) appends every join, position,
trap trip and leave to ``traces/session-*.jsonl``. This package reads that
stream and drives ``detection.Detector`` with it, then writes each verdict
back to ``traces/verdicts.jsonl``, which the game server tails and shows on
its admin and telemetry feeds.

Nothing here decides anything. It is transport: parse, forward, record.

    python -m bridge --follow
"""

from .reader import GameLogReader, latest_session

__all__ = ["GameLogReader", "latest_session"]
