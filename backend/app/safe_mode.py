"""
Safe Mode — admin-controlled toggle that turns every self-service into a
simulated success.

When Safe Mode is ON:
  * Azure DevOps project creation returns a synthetic job_id without talking
    to Azure DevOps or Kubernetes.
  * ServiceNow ticket creation returns a fake ticket without contacting the
    ServiceNow instance.
  * Any other integration that checks ``is_enabled()`` should short-circuit
    its side effects.

State resolution order on every read:
  1. ``portal_flags`` row if present (persists across restarts).
  2. ``SAFE_MODE`` env var ("1" / "true" / "yes" / "on").
  3. Default: False.

Writes (``set_enabled``) update both the in-memory flag and the DB row so the
change is visible immediately and survives restarts.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from db import execute, query_one


log = logging.getLogger(__name__)

_FLAG_KEY = "safe_mode"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

_state_lock = threading.Lock()
_cached_state: Optional[bool] = None


def ensure_table() -> None:
    """Idempotent DDL for the ``portal_flags`` key/value store."""
    try:
        execute(
            """
            CREATE TABLE IF NOT EXISTS portal_flags (
                key         VARCHAR(64) PRIMARY KEY,
                value       BOOLEAN     NOT NULL,
                updated_by  VARCHAR(255),
                updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    except Exception as exc:
        log.warning("safe_mode: ensure_table failed: %s", exc)


def _env_default() -> bool:
    raw = str(os.getenv("SAFE_MODE", "")).strip().lower()
    return raw in _TRUTHY


def _load_from_db() -> Optional[bool]:
    try:
        row = query_one("SELECT value FROM portal_flags WHERE key = %s", [_FLAG_KEY])
        if row is None:
            return None
        return bool(row.get("value"))
    except Exception as exc:
        log.debug("safe_mode: DB read failed, falling back to env: %s", exc)
        return None


def is_enabled() -> bool:
    """Return current Safe Mode state (DB override > env var > False)."""
    global _cached_state
    if _cached_state is not None:
        return _cached_state
    with _state_lock:
        if _cached_state is not None:
            return _cached_state
        db_val = _load_from_db()
        _cached_state = bool(db_val) if db_val is not None else _env_default()
        return _cached_state


def set_enabled(value: bool, *, actor: str = "system") -> bool:
    """
    Persist Safe Mode state and return the new value. Silent-fails to env-only
    behaviour if the DB write fails so a degraded DB doesn't lock the toggle.
    """
    global _cached_state
    new_val = bool(value)
    try:
        execute(
            """
            INSERT INTO portal_flags (key, value, updated_by, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET
              value = EXCLUDED.value,
              updated_by = EXCLUDED.updated_by,
              updated_at = CURRENT_TIMESTAMP
            """,
            [_FLAG_KEY, new_val, (actor or "system")[:255]],
        )
    except Exception as exc:
        log.warning("safe_mode: DB write failed, using in-memory only: %s", exc)
    with _state_lock:
        _cached_state = new_val
    return new_val


def invalidate_cache() -> None:
    """Force the next ``is_enabled()`` to re-read from the DB."""
    global _cached_state
    with _state_lock:
        _cached_state = None
