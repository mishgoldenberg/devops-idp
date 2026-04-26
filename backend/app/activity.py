"""
User-facing activity feed.

Distinct from ``audit`` on purpose:
  * ``audit.log()`` writes admin-facing records (every API call, every
    state transition, noisy) and is retention-trimmed after 7 days.
  * ``activity.log()`` writes *user-visible* events: things the person who
    triggered them will want to see later in a "what did I just do?"
    feed. Only three event kinds today (create_project, create_ticket,
    self_service) so the feed stays short and meaningful.

All calls are best-effort — failures are swallowed so logging never takes
down the caller's happy path.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from db import execute, query_all

log = logging.getLogger(__name__)

# Canonical action types understood by the widget renderer. The values are
# stable on-disk because the Recent Activity widget literally switches on
# them to format labels.
ACTION_CREATE_PROJECT = "create_project"
ACTION_CREATE_TICKET = "create_ticket"
ACTION_SELF_SERVICE = "self_service"


def log_activity(
    user_email: Optional[str],
    action_type: str,
    item_name: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Record a user-visible action. Silent on failure.

    ``user_email`` is required — activity is strictly per-user. Calls with
    a blank/None email are dropped (we don't want anonymous rows cluttering
    the feed).
    """
    email = (user_email or "").strip().lower()
    if not email or not action_type or not item_name:
        return
    try:
        execute(
            """
            INSERT INTO activity_log
                (user_email, action_type, item_name, metadata)
            VALUES (%s, %s, %s, %s)
            """,
            [email, action_type, item_name[:512], metadata or {}],
        )
    except Exception as exc:  # noqa: BLE001 — best-effort logging
        log.debug("activity.log_activity failed: %s", exc)


def recent_for_user(user_email: str, limit: int = 20) -> list[Dict[str, Any]]:
    """Return the caller's last ``limit`` activity rows, newest first."""
    email = (user_email or "").strip().lower()
    if not email:
        return []
    safe_limit = max(1, min(int(limit or 20), 50))
    try:
        return query_all(
            """
            SELECT id, action_type, item_name, metadata,
                   created_at
              FROM activity_log
             WHERE user_email = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [email, safe_limit],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("activity.recent_for_user failed: %s", exc)
        return []
