"""Step 6 (optional) - LLM-invented trap categories.

Runs offline, every few minutes, never in the detection path. Nothing the
model produces reaches the game before it has been parsed, schema-checked,
clamped and run in shadow mode. If any of that fails the system silently
falls back to the hardcoded pool: an unreachable model must never be visible
to players.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .jsonio import check_fields, check_name, decode

MAP_BOUNDS = (0.0, 0.0, 1000.0, 1000.0)  # min_x, min_y, max_x, max_y
ENTITY_FIELDS = ("id", "x", "y", "type", "sprite", "health", "owner", "spawnTick")
REQUIRED_FIELDS = ("name", "description", "entity_field", "server_value", "client_value", "x", "y")

# Used whenever invention is unavailable or produces nothing usable.
HARDCODED_POOL = ("invisible_entity", "position_offset", "phantom_player", "unreachable_bait")

# Promotion gates: a shadow category must prove itself before it may flag.
MIN_SHADOW_TRIGGERS = 200
MIN_SHADOW_CATCH_RATE = 0.05


@dataclass(frozen=True)
class TrapCategory:
    name: str
    description: str
    entity_field: str
    server_value: str
    client_value: str
    x: float
    y: float


def _clamp(value, low, high) -> float:
    return float(min(max(float(value), low), high))


def validate_category(raw, existing=()) -> tuple[TrapCategory | None, str]:
    """Parse one proposal. Returns (category, reason); category is None on reject."""
    raw, reason = decode(raw)
    if raw is None:
        return None, reason

    wrong_fields = check_fields(raw, REQUIRED_FIELDS)
    if wrong_fields:
        return None, wrong_fields

    name, reason = check_name(raw["name"], existing, noun="category")
    if name is None:
        return None, reason
    if raw["entity_field"] not in ENTITY_FIELDS:
        return None, "unknown entity field"
    if str(raw["server_value"]) == str(raw["client_value"]):
        return None, "no divergence between server data and client picture"

    min_x, min_y, max_x, max_y = MAP_BOUNDS
    try:  # out-of-bounds coordinates are clamped, not a reason to reject
        x, y = _clamp(raw["x"], min_x, max_x), _clamp(raw["y"], min_y, max_y)
    except (TypeError, ValueError):
        return None, "coordinates are not numbers"

    return TrapCategory(
        name=name,
        description=str(raw["description"])[:200],
        entity_field=raw["entity_field"],
        server_value=str(raw["server_value"])[:100],
        client_value=str(raw["client_value"])[:100],
        x=x,
        y=y,
    ), "ok"


def build_prompt(catch_rates: dict[str, float], burned: list[str]) -> str:
    working = "\n".join(
        f"  {name:18}(catch rate: {rate:.0f}/hr)"
        for name, rate in catch_rates.items()
        if name not in burned
    )
    return f"""Game entity fields:
  {", ".join(ENTITY_FIELDS)}

Working categories:
{working}

BURNED category:
{chr(10).join(f"  {name:18}(catch rate: 0/hr)" for name in burned) or "  none"}

Rule: a trap is any difference between the data the server
sends and the picture the client draws. Humans see the
picture; bots read the data.

Propose a new category. JSON only, no prose.
Fields: {", ".join(REQUIRED_FIELDS)}."""


def invent(call_llm, catch_rates, burned, existing) -> tuple[list[TrapCategory], list[str]]:
    """Ask for proposals once. Roughly 1 in 3 is usable; never retry-loop.

    ``call_llm`` takes a prompt and returns the raw text (e.g. a claude-opus-5
    call). Any failure at all degrades to the hardcoded pool.
    """
    try:
        raw = call_llm(build_prompt(catch_rates, burned))
    except Exception as exc:  # unreachable endpoint, timeout, bad credentials
        return [], [f"llm unavailable: {type(exc).__name__}"]

    proposals = raw if isinstance(raw, list) else [raw]
    accepted, rejected, seen = [], [], set(existing)
    for proposal in proposals:
        category, reason = validate_category(proposal, seen)
        if category is None:
            rejected.append(reason)
        else:
            accepted.append(category)
            seen.add(category.name)
    return accepted, rejected


@dataclass
class ShadowRegistry:
    """Invented categories run live but silent until they have earned trust."""

    shadow: dict[str, TrapCategory] = field(default_factory=dict)
    live: set[str] = field(default_factory=lambda: set(HARDCODED_POOL))
    _triggers: dict[str, int] = field(default_factory=dict)
    _caught: dict[str, int] = field(default_factory=dict)
    _human_flags: dict[str, int] = field(default_factory=dict)

    def add(self, category: TrapCategory) -> None:
        if category.name not in self.live:
            self.shadow[category.name] = category

    def flagging_categories(self) -> set[str]:
        return set(self.live)

    def observe(self, name: str, caught: bool = False, human_flag: bool = False) -> None:
        self._triggers[name] = self._triggers.get(name, 0) + 1
        self._caught[name] = self._caught.get(name, 0) + caught
        self._human_flags[name] = self._human_flags.get(name, 0) + human_flag

    def promote_ready(self) -> list[str]:
        """Promote shadow categories with a healthy rate and zero human flags."""
        promoted = []
        for name in list(self.shadow):
            triggers = self._triggers.get(name, 0)
            if triggers < MIN_SHADOW_TRIGGERS or self._human_flags.get(name, 0) > 0:
                continue
            if self._caught.get(name, 0) / triggers >= MIN_SHADOW_CATCH_RATE:
                self.live.add(name)
                del self.shadow[name]
                promoted.append(name)
        return promoted


def available_categories(registry: ShadowRegistry | None) -> tuple[str, ...]:
    if registry is None:
        return HARDCODED_POOL
    return tuple(sorted(registry.live | set(registry.shadow))) or HARDCODED_POOL
