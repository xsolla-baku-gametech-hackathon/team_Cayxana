"""Offline layer 4: ask a model to move the weights and thresholds.

``hypothesis`` invents a *new* measurement. This module changes the numbers
the existing ones already use - the feature weights, the saturation points,
the per-category score thresholds and the signature cut-offs - because the
starting values in ``rules`` and ``signatures`` were guesses written before
any live trip existed, and a guess that fires on real keyboard players is
exactly what the dashboard shows.

The same three constraints as ``hypothesis`` apply:

1. **The model never writes code.** It returns ``knob -> number`` for knobs
   that already exist. Every value is clamped to the knob's bounds and to
   one ``MAX_STEP`` from where it is now.
2. **The model never sees a trace.** It gets counts and quantiles.
3. **The model cannot apply its own answer.** The candidate numbers are
   replayed over recorded observations and are only accepted if they cost
   no labelled human a new suspicion, and gain something measurable.

The labels used by the gate are the players' self-declared ``kind`` from
the game log plus the synthetic labelled populations. Self-declared is good
enough to *reject* a change; it is not an accuracy measurement, and nothing
here bans anyone.
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field

from . import settings as _settings
from .features import MIN_SAMPLES, Features, _points, extract_features
from .jsonio import decode
from .llm import HUMAN_FACTORS
from .rules import rule_for
from .scoring import score_features
from .signatures import DETECTORS, KNOBS, detect

# A round needs both sides present, or "no human was harmed" is vacuous.
MIN_LABELLED_HUMANS = 8
MIN_LABELLED_BOTS = 8

# What a change is allowed to trade. Suspecting a real player is worse than
# missing a bot - the bot comes back and trips another trap, the player does
# not get their afternoon back - so a point of false-positive rate is priced
# at FALSE_POSITIVE_COST points of bot coverage. Without a price like this
# the gate would simply freeze whatever the detector does today, false
# positives included, because every fix costs some coverage.
FALSE_POSITIVE_COST = 3.0
# A floor under that trade: no sequence of accepted rounds may tune the
# detector down to something that catches nothing. Below this the change is
# refused however good the false-positive story looks.
MIN_RECALL = 0.50


@dataclass(frozen=True)
class Observation:
    """One closed window, kept for replay. ``label`` may be None (unknown)."""

    player_id: str
    category: str
    features: Features
    points: tuple
    label: str | None = None

    @classmethod
    def from_trace(cls, trace, category="invisible_entity", label=None,
                   player_id="synthetic", trap_event=None) -> "Observation":
        return cls(player_id, category, extract_features(trace, trap_event),
                   tuple(_points(trace)), label)

    def judge(self, settings) -> tuple[bool, float | None, tuple[str, ...]]:
        """Would this window look suspicious under ``settings``?

        The same two-part question the detector asks: a signature is
        conclusive on its own, otherwise the category threshold decides.
        """
        found = detect(self.features, self.points, settings)
        score = score_features(self.features, found, settings)
        threshold = rule_for(self.category, settings=settings).threshold
        suspicious = bool(found) or (score.value is not None and score.value > threshold)
        return suspicious, score.value, found


@dataclass(frozen=True)
class Report:
    """What a set of numbers does to the observations we have recorded."""

    human_hits: int
    humans: int
    bot_hits: int
    bots: int
    signature_hits: dict = field(default_factory=dict)

    @property
    def false_positive_rate(self) -> float:
        return self.human_hits / self.humans if self.humans else 0.0

    @property
    def recall(self) -> float:
        return self.bot_hits / self.bots if self.bots else 0.0

    def as_dict(self) -> dict:
        return {"humanHits": self.human_hits, "humans": self.humans,
                "botHits": self.bot_hits, "bots": self.bots,
                "falsePositiveRate": round(self.false_positive_rate, 4),
                "recall": round(self.recall, 4),
                "signatureHits": self.signature_hits}


@dataclass(frozen=True)
class TuningResult:
    accepted: bool
    reason: str
    overrides: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    before: Report | None = None
    after: Report | None = None
    candidate: object = None
    note: str = ""

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason,
                "changes": {k: list(v) for k, v in self.changes.items()},
                "before": self.before.as_dict() if self.before else None,
                "after": self.after.as_dict() if self.after else None,
                "note": self.note[:300]}


def per_player(observations, settings, label="human") -> list[dict]:
    """Hit rate per labelled player, worst first.

    A "human" who is suspicious in every single window is usually not a
    tuning problem: it is a label problem (an autoplay client, or a bot
    that connected without declaring itself). The gate cannot tell those
    apart, so it shows them and lets a person decide.
    """
    counts: dict[str, list[int]] = {}
    for observation in observations:
        if observation.label != label:
            continue
        row = counts.setdefault(observation.player_id, [0, 0])
        row[0] += observation.judge(settings)[0]
        row[1] += 1
    return sorted(({"playerId": player, "hits": hits, "windows": total,
                    "rate": round(hits / total, 3)}
                   for player, (hits, total) in counts.items()),
                  key=lambda row: (-row["rate"], -row["windows"]))


def evaluate(observations, settings) -> Report:
    """Replay every recorded window under one set of numbers."""
    human_hits = humans = bot_hits = bots = 0
    per_signature: dict[str, dict[str, int]] = {
        name: {"human": 0, "bot": 0} for name in DETECTORS}
    for observation in observations:
        suspicious, _, found = observation.judge(settings)
        if observation.label == "human":
            humans += 1
            human_hits += suspicious
        elif observation.label == "bot":
            bots += 1
            bot_hits += suspicious
        for name in found:
            if observation.label in ("human", "bot"):
                per_signature[name][observation.label] += 1
    hits = {name: counts for name, counts in per_signature.items()
            if counts["human"] or counts["bot"]}
    return Report(human_hits, humans, bot_hits, bots, hits)


# -- the prompt --------------------------------------------------------------

def _quantiles(values):
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    return {"min": round(ordered[0], 4),
            "median": round(st.median(ordered), 4),
            "max": round(ordered[-1], 4),
            "n": len(ordered)}


def summarise(observations, settings) -> dict:
    """Counts and quantiles per label. No player's movement is included."""
    scores = {"human": [], "bot": []}
    per_category: dict[str, dict[str, int]] = {}
    for observation in observations:
        label = observation.label
        if label not in scores:
            continue
        suspicious, value, _ = observation.judge(settings)
        scores[label].append(value)
        bucket = per_category.setdefault(observation.category,
                                         {"human": 0, "humanHits": 0, "bot": 0, "botHits": 0})
        bucket[label] += 1
        bucket[f"{label}Hits"] += suspicious
    report = evaluate(observations, settings)
    return {"report": report.as_dict(),
            "scores": {label: _quantiles(values) for label, values in scores.items()},
            "categories": per_category,
            "settings": settings.as_dict()}


# How many knobs one round may move. Two is enough to trade a weight
# against a threshold, and few enough that the replay gate can attribute
# the result to something.
MAX_KNOBS_PER_ROUND = 2


def schema_for(summary: dict) -> dict:
    """The JSON shape an answer must have, as a schema the server enforces.

    With this attached, LM Studio decodes under a grammar: the model cannot
    emit prose, a knob that does not exist, or a string where a number
    belongs. That removes the whole class of failure a small model is most
    prone to, and leaves only the question of whether the *number* is any
    good - which is what replay is for.
    """
    knobs = sorted(_settings.BOUNDS) + [
        f"rule.{name}.threshold" for name in sorted(summary.get("categories") or {})]
    return {
        "type": "object",
        "properties": {
            "adjustments": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_KNOBS_PER_ROUND,
                "items": {
                    "type": "object",
                    "properties": {
                        "knob": {"type": "string", "enum": knobs},
                        "value": {"type": "number"},
                    },
                    "required": ["knob", "value"],
                    "additionalProperties": False,
                },
            },
            "reasoning": {"type": "string", "maxLength": 300},
        },
        "required": ["adjustments", "reasoning"],
        "additionalProperties": False,
    }


def build_prompt(summary: dict, extra_note: str = "") -> str:
    report = summary["report"]
    # A small model reads a table of six similar-looking rows and picks the
    # wrong one. So the row that is actually costing us a real player says
    # so, in words, next to the name of the knob that controls it.
    signature_lines = "\n".join(
        f"  {name:18} fired on {counts['bot']} bot windows and "
        f"{counts['human']} human windows"
        + (f"   <-- COSTS A REAL PLAYER: {_fix_for(name)}"
           if counts["human"] else "   (clean; leave it alone)")
        for name, counts in sorted(summary["report"]["signatureHits"].items())
    ) or "  (no signature fired)"
    offenders = sorted(name for name, counts in
                       summary["report"]["signatureHits"].items() if counts["human"])
    verdict_line = (
        "The signatures firing on real players are: "
        + ", ".join(f"{name} ({_fix_for(name)})" for name in offenders)
        + ". Fix one of those first."
        if offenders else
        "No signature is firing on a real player, so the score side is what "
        "is left to move: a weight, a saturation point, or a category threshold."
    )
    category_lines = "\n".join(
        f"  {name:18} humans {c['humanHits']}/{c['human']} suspicious, "
        f"bots {c['botHits']}/{c['bot']} suspicious"
        for name, c in sorted(summary["categories"].items())
    ) or "  (no category data)"
    scores = summary["scores"]
    bounds = "\n".join(f"  {knob:38} now {_current(summary['settings'], knob)}"
                       f"   allowed {low}..{high}"
                       for knob, (low, high) in sorted(_settings.BOUNDS.items()))
    rule_lines = "\n".join(
        f"  rule.{name}.threshold{'':<18} now {value}   allowed "
        f"{_settings.RULE_THRESHOLD_BOUNDS[0]}..{_settings.RULE_THRESHOLD_BOUNDS[1]}"
        for name, value in sorted(_ruleview(summary).items())) or ""

    return f"""You are tuning a bot detector for a game. It watches how a player
moves for ~10 seconds after they trip a trap, turns that into a
0..1 machine-likeness score, and separately looks for "signatures":
behaviours a human body and a keyboard are not supposed to produce.

Current results over recorded windows (labels are the players'
self-declared kind, so treat them as a sanity check, not truth):

  human windows: {report['humans']}, of which {report['humanHits']} looked suspicious
  bot windows:   {report['bots']}, of which {report['botHits']} looked suspicious
  human scores:  {scores.get('human')}
  bot scores:    {scores.get('bot')}

Signatures (any one of them firing marks the window on its own):
{signature_lines}

{verdict_line}

Per trap category:
{category_lines}

Knobs you may move, with the value in force and the hard limits:
{bounds}
{rule_lines}

A window is suspicious if ANY signature fires, OR the weighted score
is above that category's threshold. So a signature that fires on human
windows is the most expensive mistake here: it alone marks a real
player.

{HUMAN_FACTORS}

Goal, in this order:
1. no human window suspicious,
2. keep as many bot windows suspicious as possible.

Your answer is replayed over these windows before anything is applied,
and it is refused unless: no additional human window becomes
suspicious; bot coverage stays above {int(MIN_RECALL * 100)}%; and the false-positive
rate it removes, counted {FALSE_POSITIVE_COST:.0f}x, outweighs the bot coverage it costs.
So aim for the largest cut in human hits that keeps most of the bots -
not for a threshold so tight that nothing fires at all.

Move at most {MAX_KNOBS_PER_ROUND} knobs, and only knobs from the list above. Each knob
may move at most {int(_settings.MAX_STEP * 100)}% from its current value per round, so a change
that needs to go further is applied over several rounds: propose the
next step, not the destination. If the numbers above give you no reason
to prefer one knob, say so in the reasoning and move the single knob
with the clearest evidence behind it - do not move a knob at random.

Reply with JSON only, no prose, in exactly this shape:
{{"adjustments": [{{"knob": "<one from the list>", "value": <number>}}],
  "reasoning": "one sentence on what you expect this to change"}}
{extra_note}"""


def _fix_for(name: str) -> str:
    """How to make one signature fire less, named in the knob's own terms."""
    knob, direction = KNOBS.get(name, ("", ""))
    return f"{direction} {knob}" if knob else "no knob for this one"


def _current(settings_dict: dict, knob: str):
    group, _, rest = knob.partition(".")
    if knob == "suspicious":
        return settings_dict["suspicious"]
    return (settings_dict.get(group) or {}).get(rest)


def _ruleview(summary: dict) -> dict:
    """Threshold in force per category we actually have observations for."""
    overrides = summary["settings"].get("rule_thresholds") or {}
    return {name: overrides.get(name, rule_for(name).threshold)
            for name in summary["categories"]}


# -- validation and the gate -------------------------------------------------

def parse_adjustments(raw) -> tuple[dict, str, str]:
    """(overrides, note, reason). Unknown knobs are dropped, not fatal."""
    raw, reason = decode(raw)
    if raw is None:
        return {}, "", reason

    adjustments = raw.get("adjustments", raw)
    if isinstance(adjustments, list):  # [{"knob": ..., "value": ...}, ...]
        adjustments = {item.get("knob"): item.get("value")
                       for item in adjustments if isinstance(item, dict)}
    if not isinstance(adjustments, dict):
        return {}, "", "adjustments is not an object"

    overrides = {}
    for knob, value in adjustments.items():
        knob = str(knob).strip()
        if knob not in _settings.BOUNDS and not (
                knob.startswith("rule.") and knob.endswith(".threshold")):
            continue  # a knob that does not exist is not an error, just noise
        try:
            overrides[knob] = float(value)
        except (TypeError, ValueError):
            continue
    if not overrides:
        return {}, "", "no usable knob in the answer"
    return overrides, str(raw.get("reasoning", ""))[:300], "ok"


def assess(raw, observations, settings=None) -> TuningResult:
    """Parse, clamp, replay. A change that costs a human hit is refused.

    This is the gate that makes the layer safe: the model can answer
    anything, and only a candidate that measurably improves the recorded
    windows - without making a labelled human newly suspicious - survives.
    """
    current = _settings.resolve(settings)
    before = evaluate(observations, current)
    if before.humans < MIN_LABELLED_HUMANS or before.bots < MIN_LABELLED_BOTS:
        return TuningResult(False, "not enough labelled windows to judge a change",
                            before=before)

    overrides, note, reason = parse_adjustments(raw)
    if not overrides:
        return TuningResult(False, reason, before=before, note=note)

    candidate = current.with_overrides(overrides)
    changes = current.diff(candidate)
    if not changes:
        return TuningResult(False, "answer changed nothing after clamping",
                            overrides, before=before, note=note)

    after = evaluate(observations, candidate)
    refusal = _refuse(before, after)
    if refusal:
        return TuningResult(False, refusal, overrides, changes, before, after,
                            candidate, note)
    return TuningResult(True, "accepted", overrides, changes, before, after,
                        candidate, note)


def _refuse(before: Report, after: Report) -> str:
    """Why this candidate must not be applied, or "" if it may be."""
    if after.human_hits > before.human_hits:
        return "makes more labelled humans suspicious"
    if after.recall < MIN_RECALL <= before.recall:
        return f"would drop bot coverage below {MIN_RECALL:.0%}"
    gained = before.false_positive_rate - after.false_positive_rate
    lost = before.recall - after.recall
    if gained <= 0 and lost >= 0 and after.bot_hits <= before.bot_hits:
        return "no measured improvement"
    if FALSE_POSITIVE_COST * gained - lost <= 0:
        return (f"a bad trade: {gained:+.1%} false positives for "
                f"{-lost:+.1%} bot coverage")
    return ""


def tune(call_llm, observations, settings=None, extra_note="",
         attempts: int = 2) -> TuningResult:
    """One tuning round. Never raises into the caller.

    ``call_llm`` takes ``(prompt, schema=...)``. A client that cannot
    constrain its output ignores the schema; the prompt asks for the same
    shape in words, and ``parse_adjustments`` accepts either form.

    A refused answer is worth one more question, because the refusal says
    exactly what was wrong with it and a small model corrects well when
    told. The second attempt carries the first answer and its refusal; a
    local model makes this free, so the alternative - accepting a worse
    round - buys nothing.
    """
    current = _settings.resolve(settings)
    usable = [o for o in observations if o.features.samples >= MIN_SAMPLES]
    summary = summarise(usable, current)
    note, best = extra_note, None
    for attempt in range(max(1, attempts)):
        try:
            raw = call_llm(build_prompt(summary, note), schema=schema_for(summary))
        except Exception as exc:  # unreachable endpoint, timeout, bad answer
            return best or TuningResult(False, f"llm unavailable: {exc}",
                                        before=evaluate(usable, current))
        for answer in (raw if isinstance(raw, list) else [raw]):
            result = assess(answer, usable, current)
            if result.accepted:
                return result
            best = best or result
        if attempt + 1 < max(1, attempts) and best is not None:
            note = extra_note + _retry_note(best)
    return best or TuningResult(False, "no answer")


def _retry_note(refused: TuningResult) -> str:
    """Hand the refusal back, so the second answer is not the first again."""
    moved = ", ".join(f"{knob}={after}" for knob, (_, after)
                      in sorted(refused.changes.items())) or "nothing"
    return (f"\n\nYour previous answer moved {moved} and was REFUSED: "
            f"{refused.reason}. Do not propose that knob again with a similar "
            "value. Read the signature table once more and move the knob that "
            "the table says is costing a real player, or a knob on the score "
            "side if none is.")
