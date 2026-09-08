"""
Brute-force protection for the local (username + password) sign-in.

SSO logins are the IdP's problem. The local accounts are not: they are two long-lived
service credentials that live in a variable group, they never rotate on their own, and
until now the sign-in form would accept an unlimited number of guesses at them as fast
as the network allowed. That is the one door in the portal a password guesser can
actually knock on.

So: count failures per account and per source address, and once either crosses the
threshold, refuse further attempts for a cool-off window. Counting BOTH matters — the
account key stops one login being sprayed from many hosts, the address key stops one
host spraying many logins.

Redis is the store when it is reachable, so the lockout holds across every backend
replica; the process-local dictionary is the fallback, which degrades to per-pod
counting rather than to no counting at all. A guard that disappears the moment Redis
hiccups is not a guard.

Nothing here ever raises: a failure to record an attempt must not become a failure to
log in.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Tuple

log = logging.getLogger(__name__)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name) or default)))
    except (TypeError, ValueError):
        return default


# Five wrong passwords is a typo-and-a-half, not an attack; fifty is an attack. The
# window is long enough to make guessing pointless and short enough that a locked-out
# admin is not locked out for the afternoon.
MAX_FAILURES = _int_env("LOGIN_MAX_FAILURES", 5, 3, 50)
LOCKOUT_SECONDS = _int_env("LOGIN_LOCKOUT_SECONDS", 900, 60, 3600)

_PREFIX = "login:fail:"

# key -> (failure count, epoch seconds when the count expires)
_local: Dict[str, Tuple[int, float]] = {}
_local_lock = threading.Lock()


def _keys(username: str, client_ip: str):
    """The two independent buckets an attempt counts against."""
    out = []
    u = (username or "").strip().lower()
    if u:
        out.append(f"{_PREFIX}user:{u}")
    ip = (client_ip or "").strip()
    if ip:
        out.append(f"{_PREFIX}ip:{ip}")
    return out


def _redis():
    try:
        from redis_client import get_redis

        return get_redis()
    except Exception:
        return None


def _local_get(key: str) -> int:
    now = time.time()
    with _local_lock:
        count, expires = _local.get(key, (0, 0.0))
        if expires <= now:
            _local.pop(key, None)
            return 0
        return count


def _local_bump(key: str) -> int:
    now = time.time()
    with _local_lock:
        count, expires = _local.get(key, (0, 0.0))
        if expires <= now:
            count, expires = 0, now + LOCKOUT_SECONDS
        count += 1
        _local[key] = (count, expires)
        # Opportunistic sweep so a long-lived pod does not accumulate dead keys.
        if len(_local) > 4096:
            for k, (_, exp) in list(_local.items()):
                if exp <= now:
                    _local.pop(k, None)
        return count


def _local_clear(key: str) -> None:
    with _local_lock:
        _local.pop(key, None)


def is_locked(username: str, client_ip: str) -> bool:
    """True when this account or this address has spent its attempts."""
    client = _redis()
    for key in _keys(username, client_ip):
        count = 0
        if client is not None:
            try:
                count = int(client.get(key) or 0)
            except Exception as exc:
                log.debug("login guard read failed (%s), using local count: %s", key, exc)
                count = _local_get(key)
        else:
            count = _local_get(key)
        if count >= MAX_FAILURES:
            return True
    return False


def record_failure(username: str, client_ip: str) -> None:
    """Count one failed attempt against both buckets. Never raises."""
    client = _redis()
    for key in _keys(username, client_ip):
        bumped = False
        if client is not None:
            try:
                # The TTL is set on first increment and left alone afterwards, so the
                # window runs from the FIRST failure — otherwise a slow drip of guesses
                # would keep pushing the expiry out and the lock would never start.
                new = client.incr(key)
                if int(new) == 1:
                    client.expire(key, LOCKOUT_SECONDS)
                bumped = True
            except Exception as exc:
                log.debug("login guard incr failed (%s): %s", key, exc)
        if not bumped:
            _local_bump(key)


def record_success(username: str, client_ip: str) -> None:
    """Clear the counters after a correct password. Never raises."""
    client = _redis()
    for key in _keys(username, client_ip):
        if client is not None:
            try:
                client.delete(key)
            except Exception as exc:
                log.debug("login guard clear failed (%s): %s", key, exc)
        _local_clear(key)


def client_ip_of(request) -> str:
    """The browser's address, honouring the proxy hop.

    TLS and routing terminate at nginx, so ``request.client.host`` is the proxy for
    every single user — counting against it would lock the whole portal out on one
    person's typo. ``X-Forwarded-For``'s first entry is the original client.
    """
    try:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
        return (request.client.host if request.client else "") or ""
    except Exception:
        return ""
