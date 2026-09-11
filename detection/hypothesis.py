"""Offline layer 3: ask a model which measurement would separate a cohort.

``anomaly`` surfaces players who trip traps constantly and never cross a
limit. It cannot say *why* they look normal - that needs a hypothesis, and
inventing hypotheses is the one part of this system a model is good at.

Three constraints make that safe:

1. **The model never writes code.** It picks a measurement from the
   registry below and proposes a direction and a threshold. There is
   nothing to execute, so a bad answer is a bad number, not a bad program.
2. **The model never sees a trace.** It gets aggregate statistics only,
   so no player's movement leaves the server and the prompt stays small.
3. **The model cannot promote its own idea.** Every proposal is replayed
   against the labelled human populations, and a single human hit rejects
   it. What survives goes to shadow mode, where it records but cannot flag.

This runs every few minutes, offline. Nothing here touches the detection
path, and if the model is unreachable the system carries on unchanged.
"""

from __future__ import annotations

import json
import re
import statistics as st
from dataclasses import dataclass

from .features import _points, extract_features
from .llm import HUMAN_FACTORS
from .signatures import (heading_change_variance, speed_cv, speed_periodicity,
                         step_mode_share, straight_run_share)

FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.MULTILINE)
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
REQUIRED_FIELDS = ("name", "metric", "direction", "threshold", "rationale", "excuse")
DIRECTIONS = ("above", "below")

# Everything the model is allowed to reason about. Adding a measurement here
# is a deliberate act by a developer; the model can only combine what exists.
METRICS = {
    "straightness": lambda f, p: f.straightness,
    "pause_variance": lambda f, p: f.pause_variance,
    "idle_share": lambda f, p: f.idle_share,
    "reaction_ticks": lambda f, p: f.reaction_ticks,
    "revisits": lambda f, p: None if f.revisits is None else float(f.revisits),
    "speed_cv": lambda f, p: speed_cv(p),
    "step_mode_share": lambda f, p: step_mode_share(p),
    "heading_change_variance": lambda f, p: heading_change_variance(p),
    "straight_run_share": lambda f, p: straight_run_share(p),
    "speed_periodicity": lambda f, p: speed_periodicity(p),
}

# A proposal that fires on this share of the cohort is not worth shadowing.
MIN_COHORT_COVERAGE = 0.30


def schema(summary: dict | None = None) -> dict:
    """The shape a proposal must have, enforced by the server's decoder.

    Without a summary this is only "the fields exist and the metric is one
    we own". With one it is much stronger: a branch per measurement that
    actually separates the two groups, each branch pinning the direction
    the arithmetic found and bounding the threshold to the gap between the
    groups. Under grammar-constrained decoding those are not requests, they
    are the only tokens the model can emit - so the two mistakes a small
    model makes here, picking an overlapping measurement and reading the
    comparison backwards, stop being possible rather than being caught
    later by the gate.
    """
    fields = {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,39}$"},
        "metric": {"type": "string", "enum": sorted(METRICS)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "threshold": {"type": "number"},
        "rationale": {"type": "string", "maxLength": 300},
        "excuse": {"type": "string", "maxLength": 300},
    }
    base = {"type": "object", "properties": fields,
            "required": list(REQUIRED_FIELDS), "additionalProperties": False}
    branches = []
    for name, stats in (summary or {}).items():
        gap = stats.get("separation")
        if not gap:
            continue
        low, high = sorted(gap["window"])
        branches.append({
            "type": "object",
            "properties": dict(fields, metric={"const": name},
                               direction={"const": gap["direction"]},
                               threshold={"type": "number", "minimum": low,
                                          "maximum": high}),
            "required": list(REQUIRED_FIELDS),
            "additionalProperties": False,
        })
    return {"anyOf": branches} if branches else base


@dataclass(frozen=True)
class Proposal:
    name: str
    metric: str
    direction: str
    threshold: float
    rationale: str
    excuse: str

    def fires(self, features, points) -> bool:
        value = METRICS[self.metric](features, points)
        if value is None:
            return False
        return value > self.threshold if self.direction == "above" else value < self.threshold

    def describe(self) -> str:
        arrow = ">" if self.direction == "above" else "<"
        return f"{self.name}: {self.metric} {arrow} {self.threshold}"


@dataclass(frozen=True)
class Assessment:
    proposal: Proposal | None
    accepted: bool
    reason: str
    human_hits: int = 0
    cohort_coverage: float = 0.0


def measure(trace) -> tuple:
    """Every registry metric for one trace, as (features, points, values)."""
    points = _points(trace)
    features = extract_features(trace)
    return features, points, {n: fn(features, points) for n, fn in METRICS.items()}


def summarise(cohort_traces, baseline_traces) -> dict:
    """Aggregate statistics for the prompt. No raw movement leaves here."""
    def stats(traces):
        collected = {name: [] for name in METRICS}
        for trace in traces:
            _, _, values = measure(trace)
            for name, value in values.items():
                if value is not None:
                    collected[name].append(value)
        return collected

    cohort, baseline = stats(cohort_traces), stats(baseline_traces)
    summary = {}
    for name in METRICS:
        c, b = sorted(cohort[name]), sorted(baseline[name])
        if not c or not b:
            continue
        summary[name] = {
            "cohort_median": round(st.median(c), 4),
            "cohort_p5": round(c[int(0.05 * (len(c) - 1))], 4),
            "cohort_p95": round(c[int(0.95 * (len(c) - 1))], 4),
            "human_median": round(st.median(b), 4),
            # The gate rejects on a single human hit, so the model needs the
            # full observed range, not percentiles: a threshold outside p95
            # still lands on the humans in the tail.
            "human_min": round(b[0], 4),
            "human_max": round(b[-1], 4),
        }
        summary[name]["separation"] = separation(summary[name])
    return summary


def separation(stats: dict) -> dict | None:
    """Does this measurement separate the two groups, and where?

    Whether a range sits outside another range is arithmetic, and asking a
    model to do arithmetic over nine rows of numbers is how you get a
    threshold copied out of the wrong column. So it is computed here, and
    the model is left with the part that is actually a judgement: which of
    the separating measurements describes something a machine does on
    purpose, what to call it, and what innocent explanation it must survive.

    The cohort side uses p5..p95, because the gate only needs the proposal
    to cover 30% of the cohort; the human side uses the full range, because
    the gate refuses on a single human hit.
    """
    low, high = stats["cohort_p5"], stats["cohort_p95"]
    if low > stats["human_max"]:
        edge = (stats["human_max"] + low) / 2
        return {"direction": "above", "suggest": round(edge, 4),
                "window": (stats["human_max"], low)}
    if high < stats["human_min"]:
        edge = (high + stats["human_min"]) / 2
        return {"direction": "below", "suggest": round(edge, 4),
                "window": (high, stats["human_min"])}
    return None


def build_prompt(summary: dict, existing: tuple[str, ...]) -> str:
    # Separating measurements first, and each one labelled with the gap it
    # leaves. An overlapping row is kept, marked unusable, so the model can
    # see that the alternatives really were checked.
    ordered = sorted(summary.items(), key=lambda row: row[1]["separation"] is None)
    lines = []
    for name, stats in ordered:
        gap = stats["separation"]
        verdict = ("   <-- SEPARATES CLEANLY: every value in this group is "
                   f"{gap['direction']} every human value; a threshold near "
                   f"{gap['suggest']} (between {gap['window'][0]} and "
                   f"{gap['window'][1]}) catches the group and no human"
                   if gap else "   (overlaps the humans: unusable)")
        lines.append(
            f"  {name:24} cohort {stats['cohort_p5']}..{stats['cohort_p95']} "
            f"(median {stats['cohort_median']})"
            f"   |  every human seen: {stats['human_min']}..{stats['human_max']} "
            f"(median {stats['human_median']}){verdict}")
    usable = [name for name, stats in ordered if stats["separation"] is not None]
    headline = ("Measurements that separate the group from every human: "
                + ", ".join(usable) + ". Choose from those and no others."
                if usable else
                "No measurement separates the group cleanly. Pick the one with "
                "the narrowest overlap and put the threshold outside the human "
                "range anyway; a detector that fires on nobody is refused, but "
                "it is not harmful.")
    return f"""A group of players trips traps far more often than anyone else
and never once looks machine-like to the classifier. Below is every
measurement we take, aggregated over that group and over known humans.

{chr(10).join(lines)}

{headline}

Detectors that already exist (do not repeat them):
  {", ".join(existing) or "none"}

{HUMAN_FACTORS}

Pick ONE of the measurements marked SEPARATES CLEANLY - the one that
describes something a machine would do on purpose rather than by
coincidence - and give a threshold inside the gap that row names. Copy
the direction from that row. Do not invent a number from another row and
do not go outside the gap: a threshold on the human side of it is
refused, and one far past the group catches nobody.

"excuse" is the innocent reason a real player could produce this value;
write the strongest one you can think of, not the weakest.

Reply with JSON only, no prose:
{{"name": "snake_case_name", "metric": "<one of the measurements above>",
 "direction": "above" | "below", "threshold": <number>,
 "rationale": "why a machine produces this",
 "excuse": "the innocent explanation this must survive"}}"""


def validate(raw, existing=()) -> tuple[Proposal | None, str]:
    """Schema and sanity only. Behaviour is checked separately, by replay."""
    if isinstance(raw, str):
        try:
            raw = json.loads(FENCE_RE.sub("", raw).strip())
        except (json.JSONDecodeError, ValueError):
            return None, "unparseable JSON"
    if not isinstance(raw, dict):
        return None, "not an object"

    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        return None, f"missing fields: {', '.join(missing)}"
    unknown = [k for k in raw if k not in REQUIRED_FIELDS]
    if unknown:
        return None, f"unknown fields: {', '.join(sorted(unknown))}"

    name = str(raw["name"]).strip().lower()
    if not NAME_RE.match(name):
        return None, "invalid name"
    if name in set(existing):
        return None, "duplicate of an existing detector"
    if raw["metric"] not in METRICS:
        return None, f"unknown metric: {raw['metric']}"
    if raw["direction"] not in DIRECTIONS:
        return None, "direction must be above or below"
    try:
        threshold = float(raw["threshold"])
    except (TypeError, ValueError):
        return None, "threshold is not a number"
    if threshold != threshold or threshold in (float("inf"), float("-inf")):
        return None, "threshold is not finite"

    return Proposal(
        name=name,
        metric=raw["metric"],
        direction=raw["direction"],
        threshold=threshold,
        rationale=str(raw["rationale"])[:300],
        excuse=str(raw["excuse"])[:300],
    ), "ok"


def assess(raw, human_traces, cohort_traces, existing=()) -> Assessment:
    """Validate, then replay against real humans. One human hit rejects it.

    This is the gate that makes the whole layer safe: the model can propose
    anything at all, and a proposal that would ban a single labelled human
    never leaves this function.
    """
    proposal, reason = validate(raw, existing)
    if proposal is None:
        return Assessment(None, False, reason)

    human_hits = sum(proposal.fires(f, p) for f, p, _ in map(measure, human_traces))
    if human_hits:
        return Assessment(proposal, False, "fires on labelled humans", human_hits)

    fired = sum(proposal.fires(f, p) for f, p, _ in map(measure, cohort_traces))
    coverage = fired / len(cohort_traces) if cohort_traces else 0.0
    if coverage < MIN_COHORT_COVERAGE:
        return Assessment(proposal, False, "does not separate the cohort", 0, coverage)

    return Assessment(proposal, True, "accepted into shadow mode", 0, coverage)


def propose(call_llm, summary, human_traces, cohort_traces, existing=()) -> list[Assessment]:
    """One round of hypothesis generation. Never retries, never raises.

    ``call_llm`` takes ``(prompt, schema=...)`` and returns text or a list
    of texts. Any failure yields an empty round and the system keeps
    running on the detectors it already has.
    """
    # If no measurement separates the two groups, there is no answer worth
    # having: the gate refuses on a single human hit, so anything the model
    # picked out of an overlapping row would be rejected. Same rule as the
    # tuning side - when a round cannot buy anything, it does not run.
    if not any(stats.get("separation") for stats in summary.values()):
        return [Assessment(None, False, "no measurement separates the cohort "
                                        "from the humans; no model call made")]
    try:
        raw = call_llm(build_prompt(summary, tuple(existing)), schema=schema(summary))
    except Exception as exc:  # unreachable endpoint, timeout, bad credentials
        return [Assessment(None, False, f"llm unavailable: {type(exc).__name__}")]

    seen = set(existing)
    results = []
    for candidate in (raw if isinstance(raw, list) else [raw]):
        assessment = assess(candidate, human_traces, cohort_traces, seen)
        if assessment.accepted:
            seen.add(assessment.proposal.name)
        results.append(assessment)
    return results


@dataclass
class ShadowDetectors:
    """Accepted proposals run silently until a person promotes them.

    A proposal survived replay against the humans we happen to have, which
    is not the same as surviving the players we do not. So it records its
    hits and its human hits, and only a developer decides it is real.
    """

    shadow: dict = None
    promoted: dict = None
    _fires: dict = None
    _human_fires: dict = None

    def __post_init__(self):
        self.shadow = self.shadow or {}
        self.promoted = self.promoted or {}
        self._fires = self._fires or {}
        self._human_fires = self._human_fires or {}

    def add(self, proposal: Proposal) -> None:
        if proposal.name not in self.promoted:
            self.shadow[proposal.name] = proposal

    def observe(self, features, points, known_human: bool = False) -> tuple[str, ...]:
        """Record what the shadow detectors would have said. Flags nothing."""
        fired = []
        for name, proposal in self.shadow.items():
            if proposal.fires(features, points):
                self._fires[name] = self._fires.get(name, 0) + 1
                self._human_fires[name] = self._human_fires.get(name, 0) + known_human
                fired.append(name)
        return tuple(fired)

    def report(self) -> list[tuple[str, int, int]]:
        """(name, hits, human hits) - the evidence a person reviews."""
        return sorted(
            ((name, self._fires.get(name, 0), self._human_fires.get(name, 0))
             for name in self.shadow),
            key=lambda row: row[1], reverse=True,
        )

    def promote(self, name: str) -> Proposal:
        """Explicit, manual, and the only way a proposal starts flagging."""
        if self._human_fires.get(name, 0):
            raise ValueError(f"{name} has flagged a known human; refusing to promote")
        proposal = self.shadow.pop(name)
        self.promoted[name] = proposal
        return proposal
