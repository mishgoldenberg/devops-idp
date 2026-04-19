"""
In-app notifications for the approval workflow (and other async events).

Notifications are keyed by the recipient's SSO email so we don't need to
resolve users -> UUIDs on every tick of the frontend poll. The table is
created on startup by db.ensure_approval_workflow_tables().

Endpoints (all require an authenticated user and only ever expose/mutate
rows where user_email = the caller's email):

  GET  /api/notifications            -> latest 50 notifications
  GET  /api/notifications/unread-count
  POST /api/notifications/{id}/read  -> mark single notification read
  POST /api/notifications/read-all   -> mark everything read
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status

from db import execute, query_all, query_one
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


def create_notification(
    user_email: str,
    message: str,
    notif_type: Optional[str] = None,
    related_id: Optional[str] = None,
) -> None:
    """Best-effort: never raises, never blocks the caller's main flow."""
    if not user_email or not message:
        return
    try:
        execute(
            """
            INSERT INTO notifications (user_email, message, notif_type, related_id)
            VALUES (%s, %s, %s, %s)
            """,
            [user_email.strip().lower(), message, notif_type, related_id],
        )
    except Exception as exc:
        log.debug("create_notification failed: %s", exc)


@router.get("")
def list_notifications(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    rows = query_all(
        """
        SELECT id, message, notif_type, related_id, is_read, created_at
        FROM notifications
        WHERE user_email = %s
        ORDER BY created_at DESC
        LIMIT 50
        """,
        [email],
    )
    return {"success": True, "data": rows}


@router.get("/unread-count")
def unread_count(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    row = query_one(
        "SELECT COUNT(*) AS n FROM notifications WHERE user_email = %s AND is_read = FALSE",
        [email],
    )
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
