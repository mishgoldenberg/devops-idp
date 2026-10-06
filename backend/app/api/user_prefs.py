"""
The signed-in person's own preferences, under /api/me: GET for everything, PUT
/theme and /density, POST and DELETE /avatar. The display name is changed through
the profile form (POST /ui/profile).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from common import safe_image
from db import execute, query_one
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()


# 600KB ceiling on raw string length for the avatar payload. Base64 inflates
# payload by ~33%, so this maps to ~450KB of image bytes — more than enough
# for a sidebar avatar and small enough to keep the `users` row readable.
_AVATAR_MAX_CHARS = 600_000
# The decoded picture; the profile page sends a 256px JPEG, a few tens of KB.
_AVATAR_MAX_BYTES = 400 * 1024
# "system" is a PREFERENCE, not a theme: it is stored as-is and resolved to light or
# night in the browser, against the OS setting, on every load. Storing the resolved
# value instead would freeze whichever mode the user happened to be in when they chose
# it, which is the one thing "follow the system" must not do.
_ALLOWED_THEMES = {"light", "night", "system"}
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
    try:
        raw = safe_image(raw, _AVATAR_MAX_BYTES)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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


