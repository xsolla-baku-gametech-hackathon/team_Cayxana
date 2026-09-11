"""Decoding what a model answered, before anything believes it.

Three modules take JSON from a model and have to survive whatever comes
back: ``hypothesis`` (a proposed measurement), ``invention`` (a proposed
trap category) and ``tuning`` (knob adjustments). All three faced the same
four ways an answer arrives wrong - wrapped in a markdown fence, not JSON
at all, not an object, or an object with the wrong keys - and all three
had grown their own copy of the same regex and the same checks.

They are here once instead. Every function returns ``(value, reason)`` and
never raises: a bad answer is a rejection with a sentence attached, which
is what the callers log and what the retry prompt hands back to the model.

Nothing here judges whether an answer is any *good*. That is replay's job.
"""

from __future__ import annotations

import json
import re

# LM Studio's grammar-constrained decoding makes a fence unlikely, but a
# model that ignores the schema still wraps its answer in one.
FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.MULTILINE)
# What a proposed detector or category may be called: snake_case, 3-40
# characters, so a name can be logged and grepped without quoting.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


def strip_fences(text: str) -> str:
    """Drop a markdown code fence around an answer, if there is one."""
    return FENCE_RE.sub("", text).strip()


def decode(raw) -> tuple[dict | None, str]:
    """A model answer as an object. ``raw`` may already be decoded."""
    if isinstance(raw, str):
        try:
            raw = json.loads(strip_fences(raw))
        except (json.JSONDecodeError, ValueError):
            return None, "unparseable JSON"
    if not isinstance(raw, dict):
        return None, "not an object"
    return raw, "ok"


def check_fields(raw: dict, required) -> str:
    """"" when the object carries exactly ``required``, else why not.

    Unknown keys are refused rather than ignored: an answer with a field we
    do not know about is an answer to a different question, and quietly
    dropping it hides that.
    """
    missing = [name for name in required if name not in raw]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    unknown = [name for name in raw if name not in required]
    if unknown:
        return f"unknown fields: {', '.join(sorted(unknown))}"
    return ""


def check_name(raw_name, existing=(), noun: str = "detector") -> tuple[str | None, str]:
    """The proposed name, lowercased, or why it cannot be used."""
    name = str(raw_name).strip().lower()
    if not NAME_RE.match(name):
        return None, "invalid name"
    if name in set(existing):
        return None, f"duplicate of an existing {noun}"
    return name, "ok"
