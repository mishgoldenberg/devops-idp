"""
Fitting a conversation into what the model and the key allow.

Two different ceilings apply, and the smaller one wins:

  * the model's context window -- how much one request may carry at all;
  * the key's tokens per minute -- how much ALL of this minute's requests may carry.
    A question that uses tools makes two or three requests, so one request may only
    spend part of the minute, or the answer is refused half-way through.

There is no tokenizer on this network, so tokens are ESTIMATED from characters, with
Hebrew and other non-Latin text counted at a higher rate (it takes more tokens per
character), and the estimate is corrected against what the gateway actually reports
after each answer. The trimming order is chosen to lose the least: old tool results
first (a summary of each stays), then whole old turns, then the size of this turn's
own tool results. The question being asked is never cut.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import config

_ratio: Dict[str, float] = {}
_ratio_lock = threading.Lock()

# What an omitted earlier tool result is replaced with. The model still sees that a
# lookup happened and what it was about, just not its rows.
OMITTED = "[earlier result omitted to save space]"


def estimate(text: str, model: str = "") -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    raw = ascii_chars / 3.6 + other / 1.5
    with _ratio_lock:
        factor = _ratio.get(model, 1.0)
    return int(raw * factor) + 1


def message_tokens(message: Dict[str, Any], model: str = "") -> int:
    total = 6 + estimate(str(message.get("content") or ""), model)
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        total += 8 + estimate(str(fn.get("name") or "") + str(fn.get("arguments") or ""), model)
    return total


def tools_tokens(tools: Optional[List[Dict[str, Any]]], model: str = "") -> int:
    return estimate(json.dumps(tools, separators=(",", ":")), model) if tools else 0


def total(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, model: str = "") -> int:
    return sum(message_tokens(m, model) for m in messages) + tools_tokens(tools, model)


def learn(model: str, estimated: int, actual: int) -> None:
    """Correct future estimates for this model against what the gateway counted."""
    if estimated <= 50 or actual <= 0:
        return
    observed = max(0.5, min(2.5, actual / (estimated / max(_ratio.get(model, 1.0), 0.01))))
    with _ratio_lock:
        previous = _ratio.get(model)
        _ratio[model] = observed if previous is None else previous * 0.7 + observed * 0.3


@dataclass
class Plan:
    prompt_cap: int
    answer_tokens: int
    rounds: int
    notes: List[str] = field(default_factory=list)


def plan(context: int, max_output: Optional[int], limits: Dict[str, Any]) -> Plan:
    """How big one request may be, how long the answer, and how many model calls."""
    answer = min(config.max_answer_tokens(), max(256, context // 4))
    if max_output:
        answer = min(answer, int(max_output))
    cap = min(context - answer - 256, config.max_prompt_tokens())
    rounds = config.max_rounds()
    notes: List[str] = []

    tpm = _positive(limits.get("tpm"))
    if tpm:
        share = int(tpm * 0.45)
        if share < cap:
            cap = share
            notes.append(f"Your key allows {tpm:,} tokens a minute, so each request is kept under {share:,}.")
        answer = min(answer, max(300, int(tpm * 0.2)))
    rpm = _positive(limits.get("rpm"))
    if rpm:
        # At least two: one call to look things up and one to answer. Below that a
        # question needing live data cannot be answered at all.
        allowed = max(2, rpm // 2)
        if allowed < rounds:
            rounds = allowed
            notes.append(f"Your key allows {rpm} requests a minute, so a question makes at most {rounds}.")
    return Plan(prompt_cap=max(1500, cap), answer_tokens=answer, rounds=rounds, notes=notes)


def _positive(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _turn_starts(messages: List[Dict[str, Any]]) -> List[int]:
    return [i for i, m in enumerate(messages) if m.get("role") == "user"]


def fit(
    messages: List[Dict[str, Any]],
    cap: int,
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    """Messages that fit ``cap``, and how many earlier messages had to be left out.

    ``messages[0]`` is the system prompt and the last user message starts the current
    turn; both are always kept.
    """
    msgs = [dict(m) for m in messages]
    if total(msgs, tools, model) <= cap:
        return msgs, 0
    starts = _turn_starts(msgs)
    current = starts[-1] if starts else len(msgs)

    # 1. Earlier tool results shrink to a line.
    for i in range(1, current):
        if msgs[i].get("role") == "tool" and msgs[i].get("content") != OMITTED:
            msgs[i]["content"] = OMITTED
    if total(msgs, tools, model) <= cap:
        return msgs, 0

    # 2. Whole earlier turns go, oldest first.
    dropped = 0
    while total(msgs, tools, model) > cap:
        starts = _turn_starts(msgs)
        if len(starts) < 2:
            break
        first, second = starts[0], starts[1]
        dropped += second - first
        del msgs[first:second]

    # 3. This turn's own tool results get shorter, biggest first.
    limit = 2400
    while total(msgs, tools, model) > cap and limit >= 400:
        for m in msgs:
            if m.get("role") == "tool" and len(str(m.get("content") or "")) > limit:
                m["content"] = str(m["content"])[:limit] + " ...[cut to fit]"
        limit //= 2
    return msgs, dropped
