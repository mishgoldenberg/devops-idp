"""
Which models a person may use, and which of them can read live data.

The list comes from the gateway with the person's own key, so the dropdown shows
exactly what that key may use and nothing it may not. It is cached for ten minutes
per key: every request made to list models is one less for answering questions.

Whether a model can call tools is a property of the model's SERVER (vLLM needs
--enable-auto-tool-choice and a --tool-call-parser), not of the person asking, so it
is remembered once for everybody, for a week. It is learned for free wherever
possible: the first real question to a model finds out, and a refusal naming the tool
flags is recorded as "cannot". An explicit check (one small request) is only made when
somebody asks for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from cache import get_cached
from redis_client import get_redis

from . import config
from . import llm

log = logging.getLogger(__name__)

_CAP_TTL = 7 * 24 * 3600
_CAP_PREFIX = "devbot:cap:"
_MODELS_TTL = 600

# Beside Redis, not instead of it: a pod whose Redis is down still remembers what it
# learned itself, rather than asking the model again on every question.
_local_caps: Dict[str, Dict[str, Any]] = {}
_local_lock = threading.Lock()


def key_fingerprint(key: str) -> str:
    """Names a key in a cache without being the key."""
    return hashlib.sha256(("devbot:" + (key or "")).encode("utf-8")).hexdigest()[:24]


def label(model_id: str) -> str:
    """What the dropdown calls a model: the name without the publisher prefix."""
    return (model_id or "").rsplit("/", 1)[-1] or model_id


def available(key: str) -> List[Dict[str, Any]]:
    """The chat models this key may use: id, label, context window, tool ability."""
    rows = get_cached(
        f"devbot:models:{key_fingerprint(key)}",
        _MODELS_TTL,
        lambda: llm.list_models(key),
    )
    out = []
    for row in rows or []:
        model_id = str(row.get("id") or "")
        if not model_id:
            continue
        cap = capability(model_id)
        out.append({
            "id": model_id,
            "label": label(model_id),
            "context": int(row.get("context") or config.context_tokens()),
            "context_known": bool(row.get("context")),
            "max_output": row.get("max_output"),
            "tools": cap.get("tools"),
            "tools_detail": cap.get("detail") or "",
        })
    out.sort(key=lambda m: m["label"].lower())
    return out


def forget_models(key: str) -> None:
    try:
        get_redis().delete(f"devbot:models:{key_fingerprint(key)}")
    except Exception:
        pass


def choose(models: List[Dict[str, Any]], *wanted: str) -> Optional[Dict[str, Any]]:
    """The first of ``wanted`` this key may use; then the configured default; then any
    model that can read live data; then any model at all."""
    by_id = {m["id"]: m for m in models}
    for model_id in list(wanted) + [config.default_model()]:
        if model_id and model_id in by_id:
            return by_id[model_id]
    for model in models:
        if model.get("tools"):
            return model
    return models[0] if models else None


# ── tool ability ─────────────────────────────────────────────────────────────

def capability(model_id: str) -> Dict[str, Any]:
    """{"tools": True|False|None, "detail", "checked_at"}. None means nobody has found out yet."""
    try:
        raw = get_redis().get(_CAP_PREFIX + model_id)
        if raw:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
    except Exception:
        pass
    with _local_lock:
        return dict(_local_caps.get(model_id) or {"tools": None})


def remember(model_id: str, tools: Optional[bool], detail: str = "") -> None:
    value = {"tools": tools, "detail": detail, "checked_at": int(time.time())}
    with _local_lock:
        _local_caps[model_id] = value
    try:
        get_redis().setex(_CAP_PREFIX + model_id, _CAP_TTL, json.dumps(value))
    except Exception:
        pass
    if tools is False:
        log.warning("devbot: model %s cannot call tools: %s", model_id, detail)
