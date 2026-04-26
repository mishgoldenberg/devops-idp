"""
Per-user UI preferences: avatar + preferred theme.

Kept as a tiny, dedicated router instead of folding into ``auth`` because these
endpoints are written to from both the Settings page (theme) and the Profile
page (avatar), and it's nice to keep their request shape discoverable at
``/api/me/...`` instead of hiding them inside the auth namespace.

Endpoints:
  GET  /api/me                 -> current user's display info + preferences
  PUT  /api/me/theme           -> persist preferred_theme ("light" | "night")
  POST /api/me/avatar          -> set avatar_url (data URL or absolute URL)
  DELETE /api/me/avatar        -> clear avatar
  PUT  /api/me/display-name    -> update full_name (keeps legacy POST /ui/profile too)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import execute, query_one
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()


# 600KB ceiling on raw string length for the avatar payload. Base64 inflates
# payload by ~33%, so this maps to ~450KB of image bytes — more than enough
# for a sidebar avatar and small enough to keep the `users` row readable.
_AVATAR_MAX_CHARS = 600_000
_ALLOWED_THEMES = {"light", "night"}
_ALLOWED_DENSITIES = {"comfortable", "compact"}


def _caller_id(user: AuthUser) -> str:
    user_id = user.get("id") or user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing user id",
        )
    return str(user_id)


@router.get("")
def get_me(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the caller's display info + persisted preferences."""
    user_id = _caller_id(current_user)
    row: Dict[str, Any] = {}
    try:
        row = query_one(
            """
            SELECT id::text AS id, username, email, full_name,
                   avatar_url, preferred_theme, preferred_density
            FROM users
            WHERE id = %s
            """,
            [user_id],
        ) or {}
    except Exception as exc:
        log.debug("get_me query failed: %s", exc)
        row = {}

    return {
        "success": True,
        "data": {
            "id": row.get("id") or user_id,
            "username": row.get("username") or current_user.get("username"),
            "email": row.get("email") or current_user.get("email"),
            "display_name": row.get("full_name")
                or current_user.get("display_name")
                or current_user.get("username"),
            "role": current_user.get("role") or "User",
            "avatar_url": row.get("avatar_url"),
            "preferred_theme": row.get("preferred_theme"),
            "preferred_density": row.get("preferred_density"),
        },
    }


class ThemeBody(BaseModel):
    theme: str = Field(..., description="DaisyUI theme name: 'light' or 'night'")


@router.put("/theme")
def set_theme(
    body: ThemeBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    theme = (body.theme or "").strip().lower()
    if theme not in _ALLOWED_THEMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"theme must be one of {sorted(_ALLOWED_THEMES)}",
        )
    user_id = _caller_id(current_user)
    try:
        execute(
            "UPDATE users SET preferred_theme = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [theme, user_id],
        )
    except Exception as exc:
        log.debug("set_theme failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save theme")
    return {"success": True, "data": {"preferred_theme": theme}}


class DensityBody(BaseModel):
    density: str = Field(..., description="UI density: 'comfortable' or 'compact'")


@router.put("/density")
def set_density(
    body: DensityBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    density = (body.density or "").strip().lower()
    if density not in _ALLOWED_DENSITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"density must be one of {sorted(_ALLOWED_DENSITIES)}",
        )
    user_id = _caller_id(current_user)
    try:
        execute(
            "UPDATE users SET preferred_density = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [density, user_id],
        )
    except Exception as exc:
        log.debug("set_density failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save density")
    return {"success": True, "data": {"preferred_density": density}}


class AvatarBody(BaseModel):
    avatar_url: Optional[str] = Field(None, description="data: URL or absolute URL")


@router.post("/avatar")
def set_avatar(
    body: AvatarBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    raw = (body.avatar_url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="avatar_url is required")
    if len(raw) > _AVATAR_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"avatar payload too large (max {_AVATAR_MAX_CHARS} characters)",
        )
    # Only allow data URLs we can render inline, or http(s) URLs. This keeps
    # hostile schemes (javascript:, vbscript:, file:) out of the DB.
    lowered = raw.lower()
    if not (lowered.startswith("data:image/") or lowered.startswith("http://") or lowered.startswith("https://")):
        raise HTTPException(status_code=400, detail="Unsupported avatar URL scheme")

    user_id = _caller_id(current_user)
    try:
        execute(
            "UPDATE users SET avatar_url = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [raw, user_id],
        )
    except Exception as exc:
        log.debug("set_avatar failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save avatar")
    return {"success": True, "data": {"avatar_url": raw}}


@router.delete("/avatar")
def clear_avatar(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    user_id = _caller_id(current_user)
    try:
        execute(
            "UPDATE users SET avatar_url = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [user_id],
        )
    except Exception as exc:
        log.debug("clear_avatar failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to clear avatar")
    return {"success": True}


class DisplayNameBody(BaseModel):
    display_name: str = Field("", max_length=255)


@router.put("/display-name")
def set_display_name(
    body: DisplayNameBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    user_id = _caller_id(current_user)
    new_name = (body.display_name or "").strip() or None
    try:
        execute(
            "UPDATE users SET full_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [new_name, user_id],
        )
    except Exception as exc:
        log.debug("set_display_name failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save name")
    return {"success": True, "data": {"display_name": new_name}}
