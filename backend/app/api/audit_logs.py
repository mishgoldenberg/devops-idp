"""
Admin logs API.

Backs the Admin → Logs page. Every endpoint requires Platform Admin
(``has_effective_admin_access_live``) — the log contains who did what and from
which IP, and that is not everybody's business.

This header used to claim the threshold was ``hierarchy_level <= 5``. It never
was: the function named right beside it tests for level 1, so the comment sent
every reader — including a security reviewer — to the wrong conclusion about who
can read the audit trail. State the guard, not a remembered intention.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

import audit
from security import AuthUser, get_current_user, has_effective_admin_access_live


log = logging.getLogger(__name__)
router = APIRouter()


def _require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to view logs.",
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
    level: Optional[str] = Query(
        None, description="Comma-separated levels, e.g. 'ERROR,CRITICAL'"
    ),
    source: Optional[str] = Query(None, description="app | http | system"),
    q: Optional[str] = Query(None, description="Substring match on action, user, or details"),
    date_from: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    date_to: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    resolved_from = date_from or date_from_alias
    resolved_to = date_to or date_to_alias
    resolved_user = user_email or user

    result = audit.list_events(
        user_email=resolved_user,
        action=action,
        level=level,
        source=source,
        q=q,
        date_from=resolved_from,
        date_to=resolved_to,
        limit=limit,
        offset=offset,
    )

    # The per-level tallies are computed WITHOUT the level filter, on purpose: they
    # are what tells you there are 3 errors to look at while you are looking at the
    # INFO rows. Counting only what you already selected would always say "all of
    # them", which answers nothing.
    counts = audit.level_counts(
        user_email=resolved_user,
        source=source,
        q=q,
        date_from=resolved_from,
        date_to=resolved_to,
    )

    encoded = jsonable_encoder(result)
    data_rows = encoded.get("data") or encoded.get("items") or []
    return {
        "success": True,
        "items": data_rows,
        "data": data_rows,
        "total": encoded.get("total", 0),
        "limit": encoded.get("limit", limit),
        "offset": encoded.get("offset", offset),
        "level_counts": counts,
    }


@router.delete("")
def clear_audit_events(
    user_email: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    confirm_all: bool = Query(
        False,
        description="Required when no filter is set — deleting the ENTIRE log has to be asked for.",
    ),
    admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    """Delete the rows the current filter matches.

    Scoped to the filter, not to the whole table, because that is what the operator
    is looking at: a page showing 400 provisioning errors from a fault that has been
    fixed is exactly the thing they want gone, and nothing else.

    Two things are deliberate here:

    * An UNFILTERED clear needs ``confirm_all=true``. A mis-click on an empty filter
      would otherwise erase the audit trail, and there is no undo for a delete —
      unlike almost everything else in this portal, the rows are simply gone.
    * The clear ITSELF is logged, after the delete, so it survives its own purge.
      A log that can be emptied without leaving a mark is not an audit trail.
    """
    filters = {
        "user_email": user_email,
        "action": action,
        "level": level,
        "source": source,
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
    }
    filtered = any(bool(v) for v in filters.values())
    if not filtered and not confirm_all:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "That would delete the entire log. Narrow the filter, or confirm "
                "clearing everything."
            ),
        )

    deleted = audit.delete_events(**filters)

    described = ", ".join(f"{k}={v}" for k, v in filters.items() if v) or "everything"
    audit.log_event(
        "audit.cleared",
        level=audit.Level.WARNING,
        source=audit.Source.APP,
        user_email=str(admin.get("email") or admin.get("username") or ""),
        metadata={
            "what": f"Cleared {deleted} log entr{'y' if deleted == 1 else 'ies'}",
            "filter": described,
            "deleted": deleted,
        },
    )
    # INFO, not WARNING: the DatabaseLogHandler mirrors WARNING+ back into this
    # same table, so a warning here would write a SECOND row about the clear.
    log.info(
        "audit log cleared by %s: %d row(s) matching %s",
        admin.get("email") or admin.get("username"), deleted, described,
    )
    return {"success": True, "deleted": deleted, "filter": described}


@router.get("/actions")
def list_actions(_admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    return {"success": True, "data": audit.distinct_actions()}


@router.get("/levels")
def list_levels(_admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    return {"success": True, "data": audit.ALL_LEVELS}


@router.get("/export")
def export_audit_events(
    user_email: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    _admin: AuthUser = Depends(_require_admin),
) -> StreamingResponse:
    """The current view, as CSV.

    Capped at 5000 rows. An admin who has narrowed the log down to something worth
    keeping needs to get it out of the browser — into a ticket, an incident report,
    an email to whoever owns the system that is failing. Screenshotting a table is
    what people do when you don't give them this.
    """
    result = audit.list_events(
        user_email=user_email,
        action=action,
        level=level,
        source=source,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=500,
        offset=0,
    )
    rows = result.get("data") or []

    # list_events is capped at 500 per call, so page through to the export limit
    # rather than silently handing back only the first page.
    offset = len(rows)
    while offset < 5000 and offset < int(result.get("total") or 0):
        page = audit.list_events(
            user_email=user_email,
            action=action,
            level=level,
            source=source,
            q=q,
            date_from=date_from,
            date_to=date_to,
            limit=500,
            offset=offset,
        )
        page_rows = page.get("data") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += len(page_rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "level", "source", "user", "action", "details"])
    for row in rows:
        writer.writerow([
            row.get("created_at"),
            row.get("level"),
            row.get("source"),
            row.get("user_email") or "",
            row.get("action"),
            row.get("metadata"),
        ])
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="devops-hub-logs.csv"'},
    )
