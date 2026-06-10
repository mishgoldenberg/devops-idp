from typing import Optional

import redis

from config import get_settings


_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Get a singleton Redis client."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
            # Cache must fail fast: an unreachable Redis otherwise blocks
            # request threads forever (no default socket timeout in redis-py).
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
    return _client


def health_check() -> bool:
    """Simple Redis health check."""
    try:
        client = get_redis()
        client.ping()
        return True
    except Exception:
        return False


