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

from activity import recent_for_user
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
