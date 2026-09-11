"""Synthetic labelled traces: a population per behaviour, not per label.

No real capture exists yet, so calibration runs against deterministic
generators. The set is deliberately adversarial in both directions:

* humans whose movement looks mechanical (a corridor, an AFK player, a
  packet-loss burst, an expert taking the optimal line)
* bots written by someone who has read this detector and is mimicking a
  human (random pauses, positional jitter, waypoints, a delayed reaction)

A generator that only produces easy cases proves nothing.
"""

from __future__ import annotations

import random
from math import cos, sin

TICKS = 320  # long enough to cover the longest observation window
TRAP = {"tick": 20, "x": 340.0, "y": 120.0}
START = (300.0, 80.0)


def _toward_trap(x: float, y: float) -> tuple[float, float]:
    dx, dy = TRAP["x"] - x, TRAP["y"] - y
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    return dx / length, dy / length


def _trace(points, start_tick: int = 0) -> list[dict]:
    return [
        {"tick": start_tick + i, "x": round(x, 4), "y": round(y, 4)}
        for i, (x, y) in enumerate(points)
    ]


# --------------------------------------------------------------------------
# Bots
# --------------------------------------------------------------------------

def naive_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Straight march, identical 2-tick pauses. The textbook script."""
    x, y = START[0] + seed % 5, START[1] - seed % 5
    dx, dy = _toward_trap(x, y)
    points = []
    for i in range(TICKS):
        if i % 20 < 18:
            x, y = x + dx * 1.2, y + dy * 1.2
        points.append((x, y))
    return _trace(points, start_tick)


def jitter_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Straight march with cosmetic noise sprinkled on every position."""
    rng = random.Random(100 + seed)
    x, y = START
    dx, dy = _toward_trap(x, y)
    points = []
    for i in range(TICKS):
        if i % 20 < 18:
            x, y = x + dx * 1.2, y + dy * 1.2
        points.append((x + rng.gauss(0, 0.7), y + rng.gauss(0, 0.7)))
    return _trace(points, start_tick)


def random_pause_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Straight path, but pause lengths are randomised to fake human rhythm."""
    rng = random.Random(200 + seed)
    x, y = START
    dx, dy = _toward_trap(x, y)
    points = []
    while len(points) < TICKS:
        for _ in range(rng.randint(4, 14)):
            x, y = x + dx * 1.2, y + dy * 1.2
            points.append((x, y))
        for _ in range(rng.randint(1, 11)):  # ragged pause, like a human
            points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def waypoint_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Navmesh bot: straight segments between waypoints, constant speed."""
    rng = random.Random(300 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        tx = x + rng.uniform(-40, 60)
        ty = y + rng.uniform(-40, 60)
        steps = rng.randint(10, 30)
        for i in range(steps):
            points.append((x + (tx - x) * i / steps, y + (ty - y) * i / steps))
        x, y = tx, ty
    return _trace(points[:TICKS], start_tick)


def humanized_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A cheat author who has read this detector: every feature is mimicked.

    Random pauses, positional jitter, waypoint detours and a delayed
    reaction. Catching this one on a single trap is not expected; the
    suspicion layer across categories is what should accumulate on it.
    """
    rng = random.Random(400 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        if len(points) < 25:  # fake a human reaction delay before committing
            tx, ty = x + rng.uniform(-20, 20), y + rng.uniform(-20, 20)
        else:
            dx, dy = _toward_trap(x, y)
            tx, ty = x + dx * rng.uniform(15, 40), y + dy * rng.uniform(15, 40)
        steps = rng.randint(6, 18)
        for i in range(steps):
            points.append((
                x + (tx - x) * i / steps + rng.gauss(0, 0.8),
                y + (ty - y) * i / steps + rng.gauss(0, 0.8),
            ))
        x, y = tx, ty
        for _ in range(rng.randint(1, 9)):
            points.append((x + rng.gauss(0, 0.15), y + rng.gauss(0, 0.15)))
    return _trace(points[:TICKS], start_tick)


def smooth_rails_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A bot family that appears *after* the detectors were written.

    It varies its speed, pauses raggedly and wanders, so every existing
    signature misses it - but it still travels in unnaturally long
    straight segments, because it steers to a target instead of nudging
    toward one. It exists to prove the discovery layer end to end: nothing
    already deployed catches it, and the metric that does is measurable.
    """
    rng = random.Random(1300 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        heading = rng.uniform(0, 6.283)
        dx, dy = cos(heading), sin(heading)
        for _ in range(rng.randint(14, 26)):  # a long, exactly straight leg
            speed = rng.uniform(0.9, 1.9)  # human-looking speed variation
            x, y = x + dx * speed, y + dy * speed
            points.append((x, y))
        for _ in range(rng.randint(1, 11)):  # human-looking ragged pause
            points.append((x, y))
    return _trace(points[:TICKS], start_tick)


# --------------------------------------------------------------------------
# Humans
# --------------------------------------------------------------------------

def human_trace(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Wandering path, ragged pauses, late reaction."""
    rng = random.Random(seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        if rng.random() < 0.15:
            for _ in range(rng.randint(1, 12)):
                points.append((x, y))
        heading = rng.uniform(0, 6.283)
        for _ in range(rng.randint(2, 8)):
            x += 2.5 * rng.uniform(-1, 1) + 1.5 * (heading % 1 - 0.5)
            y += rng.uniform(-2.5, 2.5)
            points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def expert_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A good player taking the optimal line and reacting fast.

    Straightness approaches a bot's; only the rhythm separates them.
    """
    rng = random.Random(500 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        dx, dy = _toward_trap(x, y)
        for _ in range(rng.randint(8, 25)):
            speed = rng.uniform(0.8, 1.8)  # analogue input, never constant
            x += dx * speed + rng.gauss(0, 0.35)
            y += dy * speed + rng.gauss(0, 0.35)
            points.append((x, y))
        for _ in range(rng.randint(1, 6)):
            points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def corridor_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A human in a corridor: the map forces a straight line on them.

    The hardest false-positive case in the set - geometry, not scripting,
    produces the machine-like straightness.
    """
    rng = random.Random(600 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        for _ in range(rng.randint(5, 20)):
            x += rng.uniform(0.4, 2.2)  # variable speed; walls hold y steady
            y += rng.gauss(0, 0.25)
            points.append((x, y))
        for _ in range(rng.randint(1, 14)):  # reads chat, checks the map
            points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def afk_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Player is away: a nearly frozen trace with almost no evidence in it."""
    rng = random.Random(700 + seed)
    x, y = START
    points = []
    while len(points) < TICKS:
        for _ in range(rng.randint(20, 60)):
            points.append((x + rng.gauss(0, 0.05), y + rng.gauss(0, 0.05)))
        if rng.random() < 0.5:  # a small stretch, then still again
            for _ in range(rng.randint(2, 5)):
                x, y = x + rng.uniform(-2, 2), y + rng.uniform(-2, 2)
                points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def mobile_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Touch or analogue stick: smooth, curved, few hard stops."""
    rng = random.Random(800 + seed)
    x, y = START
    heading, points = rng.uniform(0, 6.283), []
    while len(points) < TICKS:
        heading += rng.gauss(0, 0.12)
        speed = rng.uniform(1.0, 2.2)
        x, y = x + cos(heading) * speed, y + sin(heading) * speed
        points.append((x, y))
        if rng.random() < 0.05:
            for _ in range(rng.randint(1, 8)):
                points.append((x, y))
    return _trace(points[:TICKS], start_tick)


def lagged_human_trace(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A human on a bad connection: tick gaps, duplicated frames, jitter."""
    rng = random.Random(1000 + seed)
    out, tick = [], start_tick
    for sample in human_trace(seed):
        tick += rng.choice([1, 1, 2, 3, 7])
        out.append({"tick": tick, "x": sample["x"], "y": sample["y"]})
        if rng.random() < 0.25:
            tick += rng.choice([1, 2])
            out.append({"tick": tick, "x": sample["x"], "y": sample["y"]})
    return out


def packet_loss_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Bursts of dropped samples: long gaps and apparent teleports."""
    rng = random.Random(1100 + seed)
    base = human_trace(seed, start_tick)
    out, i = [], 0
    while i < len(base):
        if rng.random() < 0.2:
            i += rng.randint(3, 15)  # a burst of packets never arrived
            continue
        out.append(base[i])
        i += 1
    return out


def short_session_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """Player joined seconds ago: barely any trace to judge."""
    return human_trace(seed, start_tick)[: 4 + seed % 6]


# --------------------------------------------------------------------------
# The engine's own movement model
# --------------------------------------------------------------------------
# Everything above was written before any of it ran against this game, and
# it assumes a player whose speed varies: an analogue stick, a mouse, a
# slightly different keypress every time. This game has none of that.
# ``server/world.js: movePlayers`` takes dx/dy in {-1, 0, 1} and moves every
# player by exactly PLAYER_SPEED units per tick, diagonals normalised. The
# only thing that changes a step length is a wall.
#
# So in *this* engine a person on a keyboard has near-zero speed variation
# and lands on the same step length over and over - the two things
# ``constant_speed`` and ``quantised_steps`` treat as proof of a script.
# Calibrating against the analogue humans above and never against these is
# how a detector passes its own tests and still suspects real players.
#
# These two populations are generated the way the server actually moves
# people, so a threshold proved against them is proved against this game.

PLAYER_SPEED = 7.0  # constants.js
WORLD = (1600.0, 900.0)


def _engine_step(x, y, dx, dy, blocked=None):
    """One tick of ``movePlayers``: fixed speed, normalised diagonal, walls."""
    if dx == 0 and dy == 0:
        return x, y
    length = (dx * dx + dy * dy) ** 0.5
    vx, vy = dx / length * PLAYER_SPEED, dy / length * PLAYER_SPEED
    if blocked == 0:
        vx = 0.0  # sliding along a wall: one axis is refused
    elif blocked == 1:
        vy = 0.0
    return (min(max(x + vx, 0.0), WORLD[0]), min(max(y + vy, 0.0), WORLD[1]))


def keyboard_human(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """A person playing this game on a keyboard. The missing ground truth.

    Human where a human can be - when to start, when to stop, how long to
    hold a direction, when to look around - and mechanical everywhere the
    server leaves them no choice.
    """
    rng = random.Random(1400 + seed)
    x, y = START
    points, dx, dy = [], 0, 0
    next_decide = idle_until = blocked_until = 0
    blocked_axis = 0
    for tick in range(TICKS):
        if tick >= idle_until and tick >= next_decide:
            if rng.random() < 0.07:  # stop, look around, read chat
                dx = dy = 0
                idle_until = tick + rng.randint(10, 30)
                next_decide = idle_until
            else:
                if rng.random() < 0.4:  # head for the trap area like anyone else
                    tx, ty = _toward_trap(x, y)
                    dx = 1 if tx > 0.35 else -1 if tx < -0.35 else 0
                    dy = 1 if ty > 0.35 else -1 if ty < -0.35 else 0
                else:
                    dx, dy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
                if dx == 0 and dy == 0:
                    dx = rng.choice([-1, 1])
                next_decide = tick + rng.randint(8, 24)
        if tick < idle_until:
            dx = dy = 0
        if rng.random() < 0.03:  # brushes a wall
            blocked_until = tick + rng.randint(2, 8)
            blocked_axis = rng.choice([0, 1])
        x, y = _engine_step(x, y, dx, dy,
                            blocked_axis if tick < blocked_until else None)
        points.append((x, y))
    return _trace(points, start_tick)


def keyboard_bot(seed: int = 0, start_tick: int = 0) -> list[dict]:
    """The farm that actually plays this game: ``client/autoplay.js`` NaiveBrain.

    Re-aims at the nearest loot every single tick and never stops. Its speed
    is identical to the keyboard human's above - the engine sees to that -
    so what separates them has to be *when* they move, not how fast.
    """
    rng = random.Random(1500 + seed)
    x, y = START
    points = []
    target = (TRAP["x"], TRAP["y"])
    for _ in range(TICKS):
        if ((target[0] - x) ** 2 + (target[1] - y) ** 2) ** 0.5 < 10:
            target = (rng.uniform(30, WORLD[0] - 30), rng.uniform(30, WORLD[1] - 30))
        deadzone = PLAYER_SPEED / 2
        dx = 1 if target[0] - x > deadzone else -1 if target[0] - x < -deadzone else 0
        dy = 1 if target[1] - y > deadzone else -1 if target[1] - y < -deadzone else 0
        x, y = _engine_step(x, y, dx, dy)
        points.append((x, y))
    return _trace(points, start_tick)


HUMANS = {
    "human": human_trace,
    "keyboard_human": keyboard_human,
    "lagged_human": lagged_human_trace,
    "packet_loss_human": packet_loss_human,
    "expert_human": expert_human,
    "corridor_human": corridor_human,
    "mobile_human": mobile_human,
    "afk_human": afk_human,
    "short_session_human": short_session_human,
}

BOTS = {
    "naive_bot": naive_bot,
    "keyboard_bot": keyboard_bot,
    "jitter_bot": jitter_bot,
    "random_pause_bot": random_pause_bot,
    "waypoint_bot": waypoint_bot,
    "humanized_bot": humanized_bot,
    "smooth_rails_bot": smooth_rails_bot,
}

# Kept for the tests written before the population registry existed.
bot_trace = naive_bot


def simulate_session(generator, seed: int, traps: int = 12, categories=None, rules=None):
    """Drive one player through a session against the streaming API.

    Traps arrive from the trap system in random order and at random times;
    the detector only ever sees a signal followed by movement.
    """
    import random

    from detection import Detector, TrapSignal
    from detection.bandit import DEFAULT_CATEGORIES

    categories = list(categories or DEFAULT_CATEGORIES)
    rng = random.Random(90_000 + seed)
    detector = Detector(rules)

    verdict = None
    for i in range(traps):
        trap_tick = i * (TICKS + 80)
        detector.on_trap_signal(TrapSignal(
            "p", rng.choice(categories), tick=trap_tick, x=TRAP["x"], y=TRAP["y"],
        ))
        samples = generator(seed * 1000 + i, start_tick=trap_tick)
        for start in range(0, len(samples), 20):  # movement arrives in chunks
            for v in detector.on_movement("p", samples[start:start + 20]):
                verdict = v
                if v.flagged:
                    return v
    return verdict
