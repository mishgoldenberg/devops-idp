"""
HTTP router for the user-facing activity feed.

Backed by the ``activity_log`` table. The only public route is a read
endpoint for the authenticated caller; writes happen server-side from
the places where the three tracked actions actually succeed (ADO project
creation, ServiceNow ticket creation, self-service submission), not from
untrusted clients.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status

from activity import clear_for_user, recent_for_user
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


@router.get("")
def list_activity(
    limit: int = Query(20, ge=1, le=50),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Return the caller's last ``limit`` activity entries (max 50), newest
    first. Shape mirrors the rest of the portal's JSON APIs so the widget
    can reuse the same response handling.
    """
    email = _caller_email(current_user)
    rows = recent_for_user(email, limit=limit)
    return {"success": True, "data": rows}


@router.delete("")
def clear_activity(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Clear the caller's own activity feed.

    Deliberately available to every signed-in user, not just admins: the feed is
    private to the person it describes, so clearing it affects nobody else. The
    caller's email comes from the auth token and is never accepted from the
    request — that is what stops this becoming a way to wipe someone else's feed.
    """
    email = _caller_email(current_user)
    try:
        removed = clear_for_user(email)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("activity clear failed for %s: %s", email, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not clear your activity — please try again",
        )
    return {"success": True, "data": {"removed": removed}}
