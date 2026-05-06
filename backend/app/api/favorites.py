"""
Per-user favorites (the dashboard ⭐ Favorites section).

Scope is intentionally narrow: toggle pinning an Azure DevOps project or a
ServiceNow ticket, and list everything the current user has favorited.
The row shape stores ``item_name`` denormalized so the dashboard section
can render without round-tripping through Azure DevOps / ServiceNow.

Endpoints (mounted at ``/api/favorites``):
  GET    /api/favorites          → all favorites for the current user
  POST   /api/favorites          → add (idempotent via UNIQUE constraint)
  DELETE /api/favorites          → remove
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import execute, query_all
from security import AuthUser, get_current_user

log = logging.getLogger(__name__)
router = APIRouter()

# Keep this explicit so callers can't drop arbitrary strings in and
# pollute the table. Additions here are cheap but intentional — every new
# type also needs frontend handling (icon, click target).
ALLOWED_ITEM_TYPES = {"ado_project", "ticket", "repo"}


class FavoriteBody(BaseModel):
    item_type: str = Field(..., min_length=1, max_length=32)
    item_id: str = Field(..., min_length=1, max_length=255)
    item_name: str = Field("", max_length=512)


class FavoriteRefBody(BaseModel):
    """Body shape for DELETE — only (type, id) are needed to address a row."""
    item_type: str = Field(..., min_length=1, max_length=32)
    item_id: str = Field(..., min_length=1, max_length=255)


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


def _validate_type(item_type: str) -> str:
    t = (item_type or "").strip().lower()
    if t not in ALLOWED_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported item_type '{item_type}'. "
                   f"Allowed: {sorted(ALLOWED_ITEM_TYPES)}",
        )
    return t


@router.get("")
def list_favorites(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        rows = query_all(
            """
            SELECT id::text AS id,
                   item_type,
                   item_id,
                   item_name,
                   created_at
              FROM favorites
             WHERE user_email = %s
             ORDER BY created_at DESC
             LIMIT 200
            """,
            [email],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("list_favorites failed: %s", exc)
        rows = []
    return {"success": True, "data": rows}


@router.post("")
def add_favorite(
    body: FavoriteBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    item_type = _validate_type(body.item_type)
    item_id = body.item_id.strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")
    # Fall back to the id as display name if the caller didn't pass one,
    # so the dashboard always has *something* human-readable to show.
    item_name = (body.item_name or "").strip() or item_id
    try:
        execute(
            """
            INSERT INTO favorites (user_email, item_type, item_id, item_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_email, item_type, item_id) DO UPDATE
              SET item_name = EXCLUDED.item_name
            """,
            [email, item_type, item_id, item_name],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("add_favorite failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save favorite")
    return {"success": True}


@router.delete("")
def remove_favorite(
    body: FavoriteRefBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    item_type = _validate_type(body.item_type)
    item_id = body.item_id.strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")
    try:
        execute(
            """
            DELETE FROM favorites
             WHERE user_email = %s
               AND item_type = %s
               AND item_id = %s
            """,
            [email, item_type, item_id],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("remove_favorite failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to remove favorite")
    return {"success": True}
