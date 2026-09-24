"""Portal usage: the browser's activity beat, and the admin Users page's reads.

  POST /api/usage/heartbeat        every signed-in page, once a minute while in use
  GET  /api/usage/summary          admin: adoption across everybody
  GET  /api/usage/users            admin: every account, its role and its use
  GET  /api/usage/users/detail     admin: one account in depth (?email=)

What counts as "active", and why the server does not simply trust the number the
browser sends, is written down in usage_tracking.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import streaks
import usage_tracking
from security import AuthUser, get_current_user, has_effective_admin_access_live

log = logging.getLogger(__name__)
router = APIRouter()


class BeatBody(BaseModel):
    # Generous on purpose: clean_page() files anything odd as "other", and a 422 here
    # would put a row on the Logs page for every beat from a page with a long name.
    page: str = Field("", max_length=256)
    active_seconds: int = Field(0, ge=0, le=3600)
    view: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(current_user: AuthUser) -> None:
    """has_effective_admin_access_live -- the same live check every admin page uses."""
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


@router.post("/heartbeat", status_code=204)
def heartbeat(body: BeatBody, current_user: AuthUser = Depends(get_current_user)) -> None:
    """Credit a minute of use. Answers 204 whatever happens.

    Telemetry must never be what breaks a page, so a failure here is logged and
    swallowed -- at WARNING, because the root logger is WARNING and a tracker that
    has silently stopped counting is exactly the failure an admin needs to see.
    """
    email = str(current_user.get("email") or "").strip().lower()
    if not email:
        return None
    try:
        usage_tracking.record_beat(email, body.page, body.active_seconds, body.view)
    except Exception as exc:
        log.warning("usage: beat not recorded for %s: %s: %s", email, type(exc).__name__, exc)
    # A day in the Hub counts toward the streak (streaks.py). Never raises.
    streaks.record_visit(email)
    return None


@router.get("/summary")
def usage_summary(
    days: int = Query(30, ge=0, le=400),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_admin(current_user)
    return {"success": True, "data": usage_tracking.summary(days), "timestamp": _now_iso()}


@router.get("/users")
def usage_users(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    _require_admin(current_user)
    rows = usage_tracking.users_overview(exclude_email=str(current_user.get("email") or ""))
    try:
        # Admins see everyone's streak (streaks.py); a failure empties that column only.
        by_email = streaks.overview()
        for row in rows:
            got = by_email.get(str(row.get("email") or "").strip().lower()) or {}
            row["streak_current"] = int(got.get("current") or 0)
            row["streak_longest"] = int(got.get("longest") or 0)
    except Exception as exc:
        log.warning("usage: streaks unavailable for the Users page: %s: %s", type(exc).__name__, exc)
    return {
        "success": True,
        "data": rows,
        "tracking_since": usage_tracking.tracking_since(),
        "timestamp": _now_iso(),
    }


@router.get("/users/detail")
def usage_user_detail(
    email: str = Query(..., min_length=3, max_length=255),
    days: int = Query(30, ge=0, le=400),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """One person in depth. The e-mail is a query parameter, not a path segment:
    an address with a "+" or a "/" in it is still one address."""
    _require_admin(current_user)
    data = usage_tracking.user_detail(email, days)
    if not data:
        raise HTTPException(status_code=404, detail="No such user. Accounts are created on first sign-in.")
    return {"success": True, "data": data, "timestamp": _now_iso()}
