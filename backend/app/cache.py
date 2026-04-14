"""
Thin Redis-backed cache helper.

Usage:
    from cache import get_cached, invalidate

    # Inside an endpoint:
    result = get_cached(f"snow:tickets:{user_email}", ttl=30, producer=lambda: fetch_from_snow())

Redis errors are always swallowed — a missing/degraded cache never breaks the
API; it just means every request hits the upstream service directly.
"""

import json
from typing import Any, Callable, TypeVar

from redis_client import get_redis

T = TypeVar("T")


def get_cached(key: str, ttl: int, producer: Callable[[], T]) -> T:
    """Return cached value or call producer(), cache its result, and return it."""
    try:
        raw = get_redis().get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass

    result = producer()

    try:
        get_redis().setex(key, ttl, json.dumps(result, default=str))
    except Exception:
        pass

    return result


def invalidate(key: str) -> None:
    """Delete a cache key (call after write operations)."""
    try:
        get_redis().delete(key)
    except Exception:
        pass


def invalidate_prefix(prefix: str) -> None:
    """Delete all keys that start with *prefix* (use sparingly — scans keyspace)."""
    try:
        r = get_redis()
        keys = r.keys(f"{prefix}*")
        if keys:
            r.delete(*keys)
    except Exception:
        pass
