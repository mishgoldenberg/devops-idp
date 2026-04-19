"""
Safe Mode admin API.

  GET  /api/safe-mode/status   — any authenticated user (so the UI can show
                                 an always-visible banner when active)
  POST /api/safe-mode/toggle   — admin-only; writes an audit event
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

import audit
import safe_mode
from security import AuthUser, get_current_user, has_effective_admin_access_live


log = logging.getLogger(__name__)
router = APIRouter()


class SafeModePayload(BaseModel):
    enabled: bool


@router.get("/status")
def get_status(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    return {
        "success": True,
        "data": {"enabled": bool(safe_mode.is_enabled())},
    }


@router.post("/toggle")
def toggle_safe_mode(
    payload: SafeModePayload = Body(...),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to change Safe Mode.",
        )
    new_val = safe_mode.set_enabled(
        payload.enabled,
        actor=str(current_user.get("email") or current_user.get("username") or "admin"),
    )
    audit.log(
        audit.Action.SAFE_MODE_ENABLED if new_val else audit.Action.SAFE_MODE_DISABLED,
        user_email=str(current_user.get("email") or ""),
        metadata={"new_value": new_val},
    )
    return {"success": True, "data": {"enabled": new_val}}
