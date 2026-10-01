"""
DevBot, as admins see it: who uses it, how questions end, what the lookups do, and what
people said about the answers. The page is /ui/devbot-monitor.

  GET /api/devbot/admin/overview?days=30   totals, per day, per model, failures, lookups
  GET /api/devbot/admin/people?days=30     everyone with a key or a question in the period
  GET /api/devbot/admin/feedback?rating=   thumbs up / down, with what people chose to send

No question or answer text is recorded for this page except what a person sent with
their feedback (monitor.py). Every route re-checks admin access itself.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from devbot import monitor
from devbot.tools import base as tools
from security import AuthUser, get_current_user, has_effective_admin_access_live

router = APIRouter()
log = logging.getLogger(__name__)


def _require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return user


def _answer(request: Request, produce) -> Dict[str, Any]:
    try:
        return {"success": True, "data": produce()}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("devbot admin: a monitoring query failed", exc_info=True)
        detail = f"The DevBot figures could not be read: {type(exc).__name__}: {exc}"
        try:
            request.state.audit_detail = detail
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=detail)


@router.get("/overview")
def overview(request: Request, days: int = Query(30, ge=0, le=730),
             _admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    def produce() -> Dict[str, Any]:
        data = monitor.overview(days)
        # Each lookup by the name the chat shows while it runs, not its function name.
        for row in data["tools"] + data["tool_errors"]:
            row["label"] = tools.label_of(str(row.get("tool") or ""))
        return data

    return _answer(request, produce)


@router.get("/people")
def people(request: Request, days: int = Query(30, ge=0, le=730),
           _admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    return _answer(request, lambda: monitor.people(days))


@router.get("/feedback")
def feedback(request: Request, rating: str = Query("", pattern="^(up|down|)$"), days: int = Query(30, ge=0, le=730),
             _admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    return _answer(request, lambda: monitor.feedback(rating, days))
