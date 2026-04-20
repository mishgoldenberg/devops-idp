"""
User-submitted suggestions from the Settings page.

Deliberately minimal — admins can triage later through a follow-up UI. All
submissions are email-keyed (same convention as notifications) so anonymous
replay from a compromised token is limited to the token's own user.

Endpoints:
  POST /api/suggestions        -> submit (any authenticated user)
  GET  /api/suggestions        -> list all (admin-only)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import execute, query_all
from security import AuthUser, get_current_user, has_effective_admin_access_live


log = logging.getLogger(__name__)
router = APIRouter()


class SuggestionBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=5000)


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


@router.post("")
def create_suggestion(
    body: SuggestionBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    try:
        execute(
            """
            INSERT INTO suggestions (user_email, title, description)
            VALUES (%s, %s, %s)
            """,
            [email, title, (body.description or "").strip()],
        )
    except Exception as exc:
        log.debug("create_suggestion failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save suggestion")
    return {"success": True}


@router.get("")
def list_suggestions(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        rows = query_all(
            """
            SELECT id, user_email, title, description, status, created_at
            FROM suggestions
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
    except Exception as exc:
        log.debug("list_suggestions failed: %s", exc)
        rows = []
    return {"success": True, "data": rows}
