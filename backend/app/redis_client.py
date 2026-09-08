import threading
import time
from typing import Optional, Tuple

import redis

from config import get_settings


_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Get a singleton Redis client."""
    global _client
    if _client is None:
        settings = get_settings()
        kwargs = dict(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
            # Cache must fail fast: an unreachable Redis otherwise blocks
            # request threads forever (no default socket timeout in redis-py).
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        # The socket timeouts above are NOT the whole story: redis-py retries
        # internally, and a 2-second connect timeout measured 25 SECONDS against
        # an unreachable host because the client tried again, with backoff, three
        # times. Every one of those seconds is a blocked worker thread. The retry
        # policy is therefore stated explicitly rather than inherited, so the
        # figure above is the real ceiling on whichever redis-py the image ships.
        try:
            from redis.backoff import NoBackoff
            from redis.retry import Retry

            kwargs["retry"] = Retry(NoBackoff(), 0)
            kwargs["retry_on_timeout"] = False
            kwargs["retry_on_error"] = []
        except Exception:  # pragma: no cover - older/newer client without these knobs
            pass
        _client = redis.Redis(**kwargs)
    return _client


_health_lock = threading.Lock()
_health_cache: Tuple[float, bool] = (0.0, False)
_HEALTH_TTL = 2.0


def health_check() -> bool:
    """
    Redis health, bounded and non-overlapping — same reasoning as the DB one in
    db.py: this is called by a probe, so it must never queue behind itself.
    """
    global _health_cache
    ts, last = _health_cache
    now = time.monotonic()
    if now - ts < _HEALTH_TTL:
        return last
    if not _health_lock.acquire(blocking=False):
        return last
    try:
        try:
            get_redis().ping()
            ok = True
        except Exception:
            ok = False
        _health_cache = (time.monotonic(), ok)
        return ok
    finally:
        _health_lock.release()


