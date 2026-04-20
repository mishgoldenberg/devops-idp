"""
Portal audit log.

A small, append-only record of the user-facing events operators care about:
self-service submissions, approvals, Terraform lifecycle, ticket creation,
Safe-Mode toggles, etc.

Why a new table (``audit_events``) instead of reusing ``audit_logs``?
  * ``audit_logs.action`` is a Postgres ENUM limited to a fixed set of
    values defined in the base schema. Adding new action labels requires a
    coordinated ALTER TYPE, which is fragile in closed-network installs.
  * The spec calls for ``user_email`` (not user_id) and a free-form action
    string, which doesn't map cleanly onto the existing table.

``audit_logs`` is left untouched so legacy writers and downstream tooling
continue to work.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db import execute, query_all, query_one


_log = logging.getLogger(__name__)


class Action:
    """Common action labels. Free-form strings are also accepted."""

    # Self-service
    SELF_SERVICE_REQUEST_CREATED = "self_service.request_created"
    REQUEST_APPROVED = "self_service.request_approved"
    REQUEST_REJECTED = "self_service.request_rejected"

    # Terraform lifecycle
    TERRAFORM_STARTED = "terraform.started"
    TERRAFORM_COMPLETED = "terraform.completed"
    TERRAFORM_FAILED = "terraform.failed"

    # ServiceNow
    TICKET_CREATED = "servicenow.ticket_created"

    # Safe Mode
    SAFE_MODE_ENABLED = "admin.safe_mode_enabled"
    SAFE_MODE_DISABLED = "admin.safe_mode_disabled"


def ensure_table() -> None:
    """Idempotent DDL for the audit_events table."""
    try:
        execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id          BIGSERIAL PRIMARY KEY,
                user_email  VARCHAR(255),
                action      VARCHAR(128) NOT NULL,
                metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created "
            "ON audit_events (created_at DESC)"
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_user_created "
            "ON audit_events (user_email, created_at DESC)"
        )
        execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_action_created "
            "ON audit_events (action, created_at DESC)"
        )
    except Exception as exc:
        _log.warning("audit: ensure_table failed: %s", exc)


def log_event(
    action: str,
    *,
    user_email: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit write. Never raises."""
    if not action:
        return
    try:
        execute(
            """
            INSERT INTO audit_events (user_email, action, metadata)
            VALUES (%s, %s, %s)
            """,
            [
                (user_email or "").strip().lower()[:255] or None,
                str(action)[:128],
                metadata or {},
            ],
        )
    except Exception as exc:
        _log.debug("audit.log_event failed: %s", exc)


# Convenience alias so callers can write ``audit.log(action, ...)``.
log = log_event


def list_events(
    *,
    user_email: Optional[str] = None,
    action: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Return {data, total, limit, offset}.  ``date_from`` / ``date_to`` accept
    ISO timestamps and are filtered server-side; invalid values are ignored.
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    clauses: List[str] = []
    params: List[Any] = []

    if user_email:
        clauses.append("LOWER(user_email) = %s")
        params.append(str(user_email).strip().lower())
    if action:
        clauses.append("action = %s")
        params.append(str(action).strip()[:128])
    if date_from:
        clauses.append("created_at >= %s")
        params.append(str(date_from))
    if date_to:
        clauses.append("created_at <= %s")
        params.append(str(date_to))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    try:
        total_row = query_one(
            f"SELECT COUNT(*)::bigint AS n FROM audit_events{where}",
            params,
        )
        total = int((total_row or {}).get("n") or 0)

        rows = query_all(
            f"""
            SELECT id, user_email, action, metadata, created_at
              FROM audit_events
              {where}
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
    except Exception as exc:
        _log.debug("audit.list_events failed: %s", exc)
        return {"data": [], "total": 0, "limit": limit, "offset": offset}

    return {"data": rows, "total": total, "limit": limit, "offset": offset}


def distinct_actions() -> List[str]:
    """Return every action string present in the table (for filter dropdowns)."""
    try:
        rows = query_all(
            "SELECT DISTINCT action FROM audit_events ORDER BY action ASC"
        )
        return [str(r["action"]) for r in rows if r.get("action")]
    except Exception:
        return []


# ── Retention ──────────────────────────────────────────────────────────────
# Audit events are meant to answer "what happened in the last few days" during
# incident triage. Keeping them longer bloats the DB and muddies the UI, so we
# hard-cap retention and expose a tiny helper that the app's startup wires to
# a background thread (see ``main.py``'s ``_audit_retention_loop``).

AUDIT_RETENTION_DAYS = 7


def delete_older_than(days: int = AUDIT_RETENTION_DAYS) -> int:
    """
    Delete rows in ``audit_events`` whose ``created_at`` is older than ``days``.

    Returns the number of rows deleted. Safe to call repeatedly; never raises
    (deletion is best-effort — if the DB hiccups, the next run picks it up).
    Only accepts a positive integer day count as a guard against accidental
    mass deletion (``days <= 0`` is rejected).
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        _log.warning("audit.delete_older_than: refusing days=%s (must be > 0)", days)
        return 0
    try:
        row = query_one(
            """
            WITH deleted AS (
                DELETE FROM audit_events
                 WHERE created_at < NOW() - (%s || ' days')::interval
                 RETURNING 1
            )
            SELECT COUNT(*)::bigint AS n FROM deleted
            """,
            [days],
        )
        deleted = int((row or {}).get("n") or 0)
        if deleted:
            _log.info("audit.delete_older_than(%s): purged %d row(s)", days, deleted)
        return deleted
    except Exception as exc:
        _log.warning("audit.delete_older_than(%s) failed: %s", days, exc)
        return 0
