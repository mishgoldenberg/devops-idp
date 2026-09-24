"""DevBot settings.

Read on every call rather than cached at import: they are a handful of getenv calls,
and a value that is only read at import is one a test or an operator cannot change
without restarting the process. Each one is a literal ``os.getenv("NAME", default)``
so that scripts/gen_docs.py finds it for docs/env.md.

Only DEVBOT_LLM_BASE_URL is needed to switch DevBot on; everything else has a default
sized for a small per-person rate limit.
"""

from __future__ import annotations

import os
from typing import Optional


def _clean(value: Optional[str], default: str = "") -> str:
    value = (value or "").strip()
    # A pipeline variable that was never defined reaches the pod as its own macro,
    # "$(DEVBOT_LLM_BASE_URL)". That is not a URL; it is "not set".
    if not value or "$(" in value:
        return default
    return value


def _int(value: Optional[str], default: int, low: int, high: int) -> int:
    try:
        number = int(_clean(value, str(default)))
    except ValueError:
        number = default
    return max(low, min(high, number))


def llm_base_url() -> str:
    """The gateway's OpenAI-compatible base, e.g. https://models.example/v1."""
    url = _clean(os.getenv("DEVBOT_LLM_BASE_URL", "")).rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def enabled() -> bool:
    return bool(llm_base_url())


def default_model() -> str:
    """The model a conversation starts on, when the person's key may use it."""
    return _clean(os.getenv("DEVBOT_DEFAULT_MODEL", ""))


def key_help_url() -> str:
    """Where people create their own model key. Optional: the guide works without it."""
    return _clean(os.getenv("DEVBOT_KEY_HELP_URL", ""))


def context_tokens() -> int:
    """Context window assumed for a model the gateway does not describe."""
    return _int(os.getenv("DEVBOT_CONTEXT_TOKENS", "16384"), 16384, 2048, 1_000_000)


def max_prompt_tokens() -> int:
    """Ceiling on what one request sends, whatever the model could take.

    Every request is paid for against the key's tokens-per-minute. A turn that uses
    tools makes two or three requests, so each one is kept well under the minute's
    allowance rather than filling the model's context window.
    """
    return _int(os.getenv("DEVBOT_MAX_PROMPT_TOKENS", "12000"), 12000, 1500, 1_000_000)


def max_answer_tokens() -> int:
    return _int(os.getenv("DEVBOT_MAX_ANSWER_TOKENS", "2000"), 2000, 256, 32000)


def max_rounds() -> int:
    """Model calls one question may make. Each is one request against the key's RPM."""
    return _int(os.getenv("DEVBOT_MAX_TOOL_ROUNDS", "4"), 4, 1, 8)


def history_days() -> int:
    """How long a conversation is kept after it was last used."""
    return _int(os.getenv("DEVBOT_HISTORY_DAYS", "30"), 30, 1, 365)


def stream() -> bool:
    """Ask the model to stream. On by default: the answer appears as it is written.

    A gateway that gets tool calls right only when NOT streaming would leave DevBot
    unable to read anything; scripts/check_llm_endpoint.py tells, and false then asks
    for whole answers instead (they appear at once, when finished).
    """
    return _clean(os.getenv("DEVBOT_STREAM", "true"), "true").lower() not in ("0", "false", "no")


def max_streams() -> int:
    """Answers one backend pod streams at once.

    Each stream holds an open connection for as long as the model takes. The cap keeps
    a burst of questions from crowding out the rest of the Hub on the same pod.
    """
    return _int(os.getenv("DEVBOT_MAX_STREAMS", "8"), 8, 1, 64)
