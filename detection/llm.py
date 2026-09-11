"""The model behind the offline layers: a local one, via LM Studio.

There is no hosted provider and no fallback to one. Three things about that
matter to the rest of the system, and none of them are about the model being
cleverer:

* **It is local.** Aggregate statistics about how players move never leave
  the machine, and a round costs no money, which is what makes it sane to
  run one whenever an anomaly is actually reported.
* **It is normally not loaded.** The client is created when a round starts
  and closed when the round ends. LM Studio loads the model just in time,
  and ``ttl`` tells it to unload again after the round, so the idle cost of
  this whole layer is zero bytes of VRAM. See ``close``.
* **Its answer is constrained twice.** The request carries a JSON schema,
  so LM Studio's grammar-constrained sampling makes a malformed answer
  impossible rather than unlikely; and whatever comes back is still
  validated, clamped and replayed by ``tuning``/``hypothesis`` before it
  can change anything. An 8B model is enough here precisely because it is
  never trusted - it proposes, arithmetic decides.

Nothing here ever runs between a trap trip and a verdict.

    LM Studio -> Developer -> Enable "Serve on local network" not required;
    the default http://127.0.0.1:1234 is what this expects. Point it at
    another host with LMSTUDIO_URL, or another model with LOCAL_MODEL.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .jsonio import strip_fences

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen/qwen3-vl-8b"
DEFAULT_TIMEOUT = 120.0
# Seconds of idleness after which LM Studio unloads a just-in-time loaded
# model. A round takes seconds; this is the belt to ``close``'s braces, for
# the case where the process dies before it can close anything.
DEFAULT_TTL = 120
# Low, not zero: two samples at 0.15 give the gate two different candidates
# to choose between without the answers drifting into invention.
DEFAULT_TEMPERATURE = 0.15
DEFAULT_MAX_TOKENS = 700

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class LLMUnavailable(RuntimeError):
    """LM Studio is not running, the model is missing, or the answer was empty."""


# Kept in one place because both prompts need it and because it is the part
# an 8B model gets wrong on its own: it will happily explain that a player
# who moves at constant speed must be scripted, and in this game that
# describes everyone on a keyboard.
HUMAN_FACTORS = """Real players in this game produce machine-looking numbers for
reasons that are not cheating. Any change that punishes one of these is
refused by the replay gate, so do not propose one:
  - keyboard input is digital: speed is quantised and near-constant, and
    diagonal movement is one of only a few possible step lengths;
  - a corridor or a wall forces a perfectly straight path;
  - an analogue stick or a touchscreen produces smooth curves and almost
    no full stops;
  - an AFK player is frozen, which is neither human-like nor machine-like -
    it is no evidence at all;
  - lag and packet loss manufacture equal-length gaps that imitate a
    metronome, and teleports that imitate a script;
  - an expert takes the optimal line and reacts in a few ticks;
  - a player sprinting between two points may never pause inside a window.
Prefer a change that costs a bot detection over one that risks a player."""


@dataclass
class LocalModelClient:
    """Callable: ``client(prompt, schema=...)`` -> text, or LLMUnavailable.

    ``samples`` asks the same question more than once. Proposals are cheap
    to reject and the gate picks whichever one survives, so two samples buy
    a better round than one; the count stays small because this is not a
    retry loop.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = DEFAULT_TEMPERATURE
    samples: int = 2
    ttl: int = DEFAULT_TTL
    max_tokens: int = DEFAULT_MAX_TOKENS
    system: str = ("You are a calibration assistant for a bot detector. You never "
                   "write code and never decide anything: you propose numbers, "
                   "which are then replayed against recorded games and refused if "
                   "they harm a real player. Answer with JSON only.")
    _loaded: bool = field(default=False, repr=False)

    def __post_init__(self):
        self.base_url = os.environ.get("LMSTUDIO_URL", self.base_url).rstrip("/")
        self.model = os.environ.get("LOCAL_MODEL", self.model)

    # -- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        """Is LM Studio answering? Does not load the model."""
        try:
            self.models()
        except LLMUnavailable:
            return False
        return True

    def models(self) -> list[str]:
        request = urllib.request.Request(f"{self.base_url}/models", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                TimeoutError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(
                f"LM Studio not reachable at {self.base_url} ({type(exc).__name__})"
            ) from None
        return [entry.get("id", "") for entry in payload.get("data", [])]

    # -- one round ---------------------------------------------------------

    def __call__(self, prompt: str, schema: dict | None = None):
        answers = []
        for _ in range(max(1, self.samples)):
            try:
                answers.append(self._once(prompt, schema))
            except LLMUnavailable:
                if not answers:
                    raise
                break  # one usable answer is enough for this round
        return answers if len(answers) > 1 else answers[0]

    def _once(self, prompt: str, schema: dict | None) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system},
                         {"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # Idle unload, so the layer holds no VRAM between anomalies.
            "ttl": self.ttl,
        }
        if schema is not None:
            # Grammar-constrained decoding: the shape of the answer stops
            # being something a small model has to remember.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "proposal", "strict": True, "schema": schema},
            }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 404 no such model, 400 bad schema
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise LLMUnavailable(f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as exc:
            raise LLMUnavailable(type(exc).__name__) from None
        self._loaded = True
        return _text_of(payload)

    # -- shutting itself down ---------------------------------------------

    def close(self) -> bool:
        """Unload the model now, rather than waiting for ``ttl`` to expire.

        Best effort by design: the CLI may not be installed, and LM Studio
        may be serving a model somebody else is using. Either way the round
        has already finished, so a failure here costs nothing but VRAM for
        ``ttl`` seconds.
        """
        if not self._loaded:
            return False
        self._loaded = False
        lms = shutil.which("lms")
        if lms is None:
            return False
        try:
            subprocess.run([lms, "unload", self.model],
                           capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return True


def _text_of(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMUnavailable("no choices in answer")
    message = choices[0].get("message") or {}
    text = str(message.get("content") or "")
    # Qwen3 emits a reasoning block when thinking is on. It is not an error
    # and not an answer, so it is removed rather than refused.
    text = strip_fences(THINK_RE.sub("", text))
    if not text:
        raise LLMUnavailable(choices[0].get("finish_reason") or "empty answer")
    return text


def default_client(**kwargs):
    """A local client, or None when LM Studio is not answering.

    None means "this layer stays off", and every caller treats it that way.
    There is deliberately nothing to fall back to: a round that cannot run
    locally does not run at all, rather than quietly sending aggregate
    statistics about players to somebody else's server.
    """
    client = LocalModelClient(**kwargs)
    return client if client.available else None
