"""
Admin audit logs API.

Backs the Admin → Audit Logs page. All endpoints require admin access
(hierarchy_level ≤ 5, enforced via ``has_effective_admin_access_live``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder

import audit
from security import AuthUser, get_current_user, has_effective_admin_access_live


log = logging.getLogger(__name__)
router = APIRouter()


def _require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to view audit logs.",
        )
    return user


@router.get("")
def list_audit_events(
    user_email: Optional[str] = Query(
        None, description="Filter by user email (exact match)", alias="user_email"
    ),
    user: Optional[str] = Query(
        None, description="Alias for user_email (kept for backwards compatibility)"
    ),
    action: Optional[str] = Query(None, description="Filter by action label"),
    date_from: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    date_to: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    result = audit.list_events(
        user_email=user_email or user,
        action=action,
        date_from=date_from or date_from_alias,
        date_to=date_to or date_to_alias,
        limit=limit,
        offset=offset,
    )
    # Normalize shape so frontend can use either `items` or `data`.
    encoded = jsonable_encoder(result)
    data_rows = encoded.get("data") or encoded.get("items") or []
    return {
        "success": True,
        "items": data_rows,
        "data": data_rows,
        "total": encoded.get("total", 0),
        "limit": encoded.get("limit", limit),
        "offset": encoded.get("offset", offset),
    }


@router.get("/actions")
def list_actions(_admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    return {"success": True, "data": audit.distinct_actions()}
