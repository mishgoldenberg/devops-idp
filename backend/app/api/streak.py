"""The signed-in person's streak, for the banner flame (rules in streaks.py).

  GET  /api/streaks/me              the streak, today's activities, freezes, last two weeks
  POST /api/streaks/me/notice-seen  the weekend-bonus message has been shown

Only the person sees their own streak here; admins see everyone's on the Users page.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Response

import streaks
from security import AuthUser, get_current_user

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/me")
def my_streak(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = str(current_user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="No e-mail on this account.")
    data = streaks.status(email)
    try:
        # Azure DevOps history is read in the background, at most hourly; the flame
        # answers from what is already known and says when more is on its way.
        running = streaks.maybe_sync({
            "id": current_user.get("id"),
            "email": email,
            "username": current_user.get("username"),
        })
        data["sync"]["running"] = bool(running)
    except Exception as exc:
        log.warning("streaks: background read not started for %s: %s: %s", email, type(exc).__name__, exc)
    return {"success": True, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/me/notice-seen", status_code=204)
def notice_seen(current_user: AuthUser = Depends(get_current_user)) -> Response:
    streaks.acknowledge_notice(str(current_user.get("email") or ""))
    return Response(status_code=204)
