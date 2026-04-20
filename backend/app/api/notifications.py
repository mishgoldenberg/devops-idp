"""
In-app notifications for the approval workflow (and other async events).

Notifications are keyed by the recipient's SSO email so we don't need to
resolve users -> UUIDs on every tick of the frontend poll. The table is
created on startup by db.ensure_approval_workflow_tables().

Each row carries an optional ``link`` (so the bell dropdown can navigate
to the right page on click) and an optional ``group_key`` (so repeated
events — e.g. several pipeline runs for the same request — collapse
into a single bell entry).

Endpoints (all require an authenticated user and only ever expose/mutate
rows where user_email = the caller's email):

  GET  /api/notifications            -> latest 50 notifications (grouped)
  GET  /api/notifications/unread-count
  POST /api/notifications/{id}/read  -> mark single notification read
  POST /api/notifications/read-all   -> mark everything read
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status

from db import execute, query_all, query_one
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()

# Keep 60 days of notifications by default. Older rows are removed
# opportunistically whenever anyone lists their inbox.
_RETENTION_DAYS = 60


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


def _cleanup_old_notifications() -> None:
    """Best-effort retention: drop notifications older than _RETENTION_DAYS."""
    try:
        execute(
            "DELETE FROM notifications "
            "WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL '%s days')"
            % int(_RETENTION_DAYS)
        )
    except Exception as exc:
        log.debug("notifications cleanup failed: %s", exc)


def create_notification(
    user_email: str,
    message: str,
    notif_type: Optional[str] = None,
    related_id: Optional[str] = None,
    link: Optional[str] = None,
    group_key: Optional[str] = None,
) -> None:
    """Best-effort: never raises, never blocks the caller's main flow."""
    if not user_email or not message:
        return
    try:
        execute(
            """
            INSERT INTO notifications (user_email, message, notif_type, related_id, link, group_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                user_email.strip().lower(),
                message,
                notif_type,
                related_id,
                (link or None),
                (group_key or None),
            ],
        )
    except Exception as exc:
        # The link/group_key columns may not exist yet on a very old install;
        # fall back to the classic INSERT so the notification still lands.
        log.debug("create_notification extended insert failed: %s", exc)
        try:
            execute(
                """
                INSERT INTO notifications (user_email, message, notif_type, related_id)
                VALUES (%s, %s, %s, %s)
                """,
                [user_email.strip().lower(), message, notif_type, related_id],
            )
        except Exception as inner:
            log.debug("create_notification failed: %s", inner)


def _fetch_columns() -> str:
    """Return SELECT column list with safe defaults for optional columns."""
    return (
        "id, message, notif_type, related_id, is_read, created_at, "
        "COALESCE(link, NULL) AS link, "
        "COALESCE(group_key, NULL) AS group_key"
    )


def _load_user_rows(email: str, limit: int = 50):
    try:
        return query_all(
            f"""
            SELECT {_fetch_columns()}
            FROM notifications
            WHERE user_email = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            [email, limit],
        )
    except Exception as exc:
        # Retry without the optional columns for legacy installs.
        log.debug("notifications: primary SELECT failed, retrying bare: %s", exc)
        try:
            return query_all(
                """
                SELECT id, message, notif_type, related_id, is_read, created_at
                FROM notifications
                WHERE user_email = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                [email, limit],
            )
        except Exception as inner:
            log.warning("notifications list failed: %s", inner)
            return []


def _group_rows(rows: list) -> list:
    """Collapse notifications sharing the same group_key; newest wins."""
    grouped: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    out: list = []
    for row in rows:
        gk = row.get("group_key")
        if not gk:
            out.append(row)
            continue
        if gk in grouped:
            grouped[gk]["group_count"] = int(grouped[gk].get("group_count") or 1) + 1
            # Preserve unread state if any member is unread.
            if not row.get("is_read"):
                grouped[gk]["is_read"] = False
            continue
        entry = dict(row)
        entry["group_count"] = 1
        grouped[gk] = entry
        out.append(entry)
    # Keep original DESC order; grouped rows already refer to the newest message.
    return out


@router.get("")
def list_notifications(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    _cleanup_old_notifications()
    rows = _load_user_rows(email, limit=50)
    return {"success": True, "data": _group_rows(rows)}


@router.get("/unread-count")
def unread_count(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        row = query_one(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_email = %s AND is_read = FALSE",
            [email],
        )
    except Exception as exc:
        log.debug("unread_count failed: %s", exc)
        row = {"n": 0}
    return {"success": True, "data": {"count": int((row or {}).get("n") or 0)}}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str = Path(...),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        execute(
            "UPDATE notifications SET is_read = TRUE "
            "WHERE id = %s AND user_email = %s",
            [notification_id, email],
        )
    except Exception as exc:
        log.debug("mark_read failed: %s", exc)
    return {"success": True}


@router.post("/read-all")
def mark_all_read(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        execute(
            "UPDATE notifications SET is_read = TRUE "
            "WHERE user_email = %s AND is_read = FALSE",
            [email],
        )
    except Exception as exc:
        log.debug("mark_all_read failed: %s", exc)
    return {"success": True}
