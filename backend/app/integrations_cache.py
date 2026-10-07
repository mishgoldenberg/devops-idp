"""
Thin wrapper around ``cache.get_cached`` that's scoped to external-integration
reads (Azure DevOps, ServiceNow, SonarQube, Artifactory).

Why a dedicated helper?
  * One place to set the default TTL (60s per the polish spec).
  * Consistent cache-key namespacing (``ext:<integration>:<owner>:<key>``) so
    ops can ``redis-cli keys 'ext:*'`` when debugging.
  * ``invalidate_owner(integration, owner)`` is used after write paths (create
    ticket, create project) so the next read reflects reality immediately
    instead of waiting for the TTL.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Tuple, TypeVar

from cache import get_cached, invalidate_prefix
from redis_client import get_redis


_log = logging.getLogger(__name__)

DEFAULT_EXTERNAL_TTL_S = 60

T = TypeVar("T")


def _key(integration: str, owner: str, suffix: str) -> str:
    integration = (integration or "ext").strip().lower()[:32]
    owner = (owner or "anon").strip().lower()[:255]
    suffix = (suffix or "").strip()[:255]
    return f"ext:{integration}:{owner}:{suffix}" if suffix else f"ext:{integration}:{owner}"


def cached_external(
    integration: str,
    owner: str,
    suffix: str,
    producer: Callable[[], T],
    *,
    ttl: int = DEFAULT_EXTERNAL_TTL_S,
) -> T:
    """Cache the result of an external read for ``ttl`` seconds."""
    return get_cached(_key(integration, owner, suffix), ttl, producer)


def invalidate_owner(integration: str, owner: str) -> None:
    """Drop every cached entry for this (integration, owner) pair."""
    integration = (integration or "ext").strip().lower()[:32]
    owner = (owner or "anon").strip().lower()[:255]
    invalidate_prefix(f"ext:{integration}:{owner}")


_flight_guard = threading.Lock()
_flights: Dict[str, threading.Lock] = {}


def _flight(key: str) -> threading.Lock:
    with _flight_guard:
        return _flights.setdefault(key, threading.Lock())


def _read_stamped(key: str):
    """(value, stored_at) from the cache, or None. Redis errors are a miss."""
    try:
        raw = get_redis().get(key)
        if raw:
            entry = json.loads(raw)
            if isinstance(entry, dict) and "at" in entry and "v" in entry:
                return entry["v"], float(entry["at"])
    except Exception:
        pass
    return None


def _write_stamped(key: str, value, keep: int) -> None:
    try:
        get_redis().setex(key, keep, json.dumps({"at": time.time(), "v": value}, default=str))
    except Exception:
        pass


def cached_external_swr(
    integration: str,
    owner: str,
    suffix: str,
    producer: Callable[[], T],
    *,
    fresh: int,
    keep: int,
    refresh: bool = False,
) -> Tuple[T, int, bool]:
    """Answer from the cache at once, and bring the answer up to date behind it.

    Returns ``(value, age_seconds, refreshing)``.

      * younger than ``fresh``  -- returned as it is;
      * older, but within ``keep`` -- returned AT ONCE, while one background
        thread fetches a new one (``refreshing`` says so, and the widget asks again
        a few seconds later);
      * absent, or ``refresh`` -- fetched now, with one caller per key doing the
        work and everybody else waiting for its answer rather than repeating it.

    For reads that are slow and change slowly: nobody waits for a crawl to find out
    that nothing has changed. A failed read raises and is never stored, and a failed
    BACKGROUND read leaves the previous answer in place and says so at WARNING.
    Stored as {"at", "v"}, so give it a suffix no plain cached_external() uses.
    """
    key = _key(integration, owner, suffix)
    asked = time.time()
    if not refresh:
        hit = _read_stamped(key)
        if hit is not None:
            value, at = hit
            age = max(0, int(asked - at))
            if age < fresh:
                return value, age, False
            return value, age, _refresh_in_background(key, producer, keep)

    lock = _flight(key)
    with lock:
        # Whoever held the lock may have just fetched exactly what this caller wants.
        hit = _read_stamped(key)
        if hit is not None and (hit[1] >= asked or (not refresh and time.time() - hit[1] < fresh)):
            return hit[0], max(0, int(time.time() - hit[1])), False
        value = producer()
        _write_stamped(key, value, keep)
        return value, 0, False


def _refresh_in_background(key: str, producer: Callable[[], T], keep: int) -> bool:
    """Start one refresh of ``key`` unless one is already running. True either way
    a refresh is under way."""
    lock = _flight(key)
    if not lock.acquire(blocking=False):
        return True

    def run() -> None:
        try:
            _write_stamped(key, producer(), keep)
        except Exception as exc:
            _log.warning("cache: background refresh of %s failed; the previous answer stays: %s: %s",
                         key, type(exc).__name__, exc)
        finally:
            lock.release()

    try:
        threading.Thread(target=run, name="cache-refresh", daemon=True).start()
    except Exception as exc:
        lock.release()
        _log.warning("cache: could not start a background refresh of %s: %s", key, exc)
        return False
    return True


def forget_external(integration: str, owner: str, suffix: str) -> None:
    """Drop ONE cached entry, by the same key cached_external stored it under.

    For the answer a caller has just decided not to keep -- most often a read that
    failed. A cached failure is worse than no cache at all: it holds the failure for
    the whole TTL, so the thing a person does about it (press Refresh) is the one
    thing guaranteed not to help.
    """
    invalidate_prefix(_key(integration, owner, suffix))
