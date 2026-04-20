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

import logging
from typing import Callable, TypeVar

from cache import get_cached, invalidate_prefix


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
