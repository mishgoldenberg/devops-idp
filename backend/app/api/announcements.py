"""
Announcements — the admin-to-everyone channel.

The portal had no way for the people who run it to say anything to the people who
use it. New feature, planned downtime, "the Artifactory certificate changes on
Sunday": all of that went out over chat or email, which reaches whoever happens to
be reading it and nobody else, and is gone the next day. Meanwhile the one page
every user opens every morning said nothing.

So: a billboard at the top of the dashboard, written here.

Design notes that are easy to get wrong
---------------------------------------
* **Dismissals are stamped, not permanent.** A dismissal records the
  announcement's ``updated_at``. Edit the announcement and it comes back for
  everyone, because the thing they dismissed is not the thing that is now
  posted. Without that, correcting a wrong date in an announcement means the
  correction is seen only by people who had not read the original.

* **The window is the schedule.** ``starts_at`` / ``ends_at`` are optional and
  either can be open. An announcement about Sunday's maintenance should be
  written on Thursday and stop showing itself on Monday without anyone having to
  remember to take it down — an announcement nobody retires is how a billboard
  becomes wallpaper.

* **Reads never 500.** The billboard is decoration on a page full of real work.
  If this table is missing or the query fails, the endpoint answers with an empty
  list and the dashboard renders exactly as it did before.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import audit
from db import execute, execute_returning, query_all, query_one
from security import AuthUser, get_current_user, has_effective_admin_access_live

log = logging.getLogger(__name__)
router = APIRouter()

KINDS = ("info", "success", "warning", "critical")

# The version marker a dismissal is recorded against: updated_at as epoch
# milliseconds, computed by POSTGRES on both sides of the comparison.
#
# The obvious implementation — store str(row["updated_at"]) from Python and
# compare it to `updated_at::text` in SQL — cannot ever match: psycopg2 renders a
# timestamptz as "2026-08-04 07:12:00.123456+00:00" and Postgres renders it as
# "…+00". Every dismissal would have been silently ineffective, with the banner
# reappearing on the next load and no error anywhere. Epoch milliseconds also
# make the value independent of the session's DateStyle and TimeZone, which a
# text rendering is not.
_STAMP_SQL = "((EXTRACT(EPOCH FROM updated_at) * 1000)::bigint)::text"

_COLUMNS = (
    "id, title, body, kind, link_url, link_label, starts_at, ends_at, "
    f"is_active, created_by, created_at, updated_at, {_STAMP_SQL} AS stamp"
)


def _require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to manage announcements.",
        )
    return user


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(row.get("kind") or "info").strip().lower()
    return {
        "id": int(row.get("id")),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "kind": kind if kind in KINDS else "info",
        "link_url": row.get("link_url") or "",
        "link_label": row.get("link_label") or "",
        "starts_at": row.get("starts_at"),
        "ends_at": row.get("ends_at"),
        "is_active": bool(row.get("is_active")),
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "stamp": str(row.get("stamp") or ""),
    }


class AnnouncementBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=4000)
    kind: str = Field("info")
    link_url: Optional[str] = Field(None, max_length=2000)
    link_label: Optional[str] = Field(None, max_length=80)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    is_active: Optional[bool] = None


def _clean(body: AnnouncementBody) -> Dict[str, Any]:
    kind = (body.kind or "info").strip().lower()
    if kind not in KINDS:
        kind = "info"
    link_url = (body.link_url or "").strip() or None
    if link_url and not link_url.lower().startswith(("http://", "https://", "/")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The link must start with http://, https:// or / .",
        )
    starts_at = (body.starts_at or "").strip() or None
    ends_at = (body.ends_at or "").strip() or None
    # Caught here rather than left to produce an announcement that can never be
    # visible — a window that ends before it starts shows nothing to nobody, and
    # the author would have no way of telling that from "it worked".
    if starts_at and ends_at and ends_at < starts_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The end of the window is before its start.",
        )
    return {
        "title": body.title.strip(),
        "body": (body.body or "").strip(),
        "kind": kind,
        "link_url": link_url,
        "link_label": (body.link_label or "").strip() or None,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }


# ── What everybody sees ───────────────────────────────────────────────────────


@router.get("/active")
def active_announcements(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Live announcements this user has not already waved away.

    The dismissal join compares the STORED stamp against the row's current
    updated_at, so an edited announcement is un-dismissed for everyone by the
    edit itself rather than by anybody remembering to reset anything.
    """
    user_id = str(current_user.get("id") or "")
    try:
        rows = query_all(
            f"""
            SELECT {_COLUMNS}
              FROM announcements a
             WHERE a.is_active = true
               AND (a.starts_at IS NULL OR a.starts_at <= NOW())
               AND (a.ends_at   IS NULL OR a.ends_at   >= NOW())
               AND NOT EXISTS (
                     SELECT 1
                       FROM announcement_dismissals d
                      WHERE d.user_id = %s
                        AND d.announcement_id = a.id
                        AND d.seen_stamp = ((EXTRACT(EPOCH FROM a.updated_at) * 1000)::bigint)::text
                   )
             ORDER BY
                   CASE a.kind WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                   a.created_at DESC
             LIMIT 20
            """,
            [user_id],
        )
    except Exception as exc:
        # Decoration must never break the dashboard it decorates.
        log.debug("announcements: active query failed: %s", exc)
        return {"success": True, "data": [], "timestamp": _now_iso()}

    return {
        "success": True,
        "data": [_serialize(row) for row in rows],
        "timestamp": _now_iso(),
    }


class DismissBody(BaseModel):
    stamp: str = Field("", max_length=64)


@router.post("/{announcement_id}/dismiss", status_code=204)
def dismiss(
    announcement_id: int,
    body: DismissBody,
    current_user: AuthUser = Depends(get_current_user),
):
    """Mark one announcement as read, for this user, at this version."""
    user_id = str(current_user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")

    row = query_one("SELECT id FROM announcements WHERE id = %s", [announcement_id])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such announcement.")

    # The stamp is read from the row by the INSERT itself. Two reasons: the client
    # cannot dismiss a version it has not been shown, and the value is produced by
    # the same expression the visibility query compares against — a stamp written
    # by Python and compared by Postgres would never match.
    execute(
        f"""
        INSERT INTO announcement_dismissals (user_id, announcement_id, seen_stamp)
        SELECT %s, id, {_STAMP_SQL}
          FROM announcements
         WHERE id = %s
        ON CONFLICT (user_id, announcement_id)
        DO UPDATE SET seen_stamp = EXCLUDED.seen_stamp,
                      dismissed_at = CURRENT_TIMESTAMP
        """,
        [user_id, announcement_id],
    )
    return None


# ── Admin ─────────────────────────────────────────────────────────────────────


@router.get("")
def list_announcements(_admin: AuthUser = Depends(_require_admin)) -> Dict[str, Any]:
    """Everything, including retired and scheduled rows, newest first."""
    rows = query_all(
        f"SELECT {_COLUMNS} FROM announcements ORDER BY created_at DESC, id DESC LIMIT 200"
    )
    data = [_serialize(row) for row in rows]

    # "How many people have seen it" is the question every author asks next, and
    # answering it here saves a second round trip per row.
    counts: Dict[int, int] = {}
    try:
        for row in query_all(
            "SELECT announcement_id, COUNT(*)::bigint AS n "
            "FROM announcement_dismissals GROUP BY announcement_id"
        ):
            counts[int(row.get("announcement_id"))] = int(row.get("n") or 0)
    except Exception:
        counts = {}
    for item in data:
        item["dismissed_by"] = counts.get(item["id"], 0)

    return {"success": True, "data": data, "timestamp": _now_iso()}


@router.post("", status_code=201)
def create_announcement(
    body: AnnouncementBody,
    admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    fields = _clean(body)
    rows = execute_returning(
        f"""
        INSERT INTO announcements
              (title, body, kind, link_url, link_label, starts_at, ends_at, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_COLUMNS}
        """,
        [
            fields["title"],
            fields["body"],
            fields["kind"],
            fields["link_url"],
            fields["link_label"],
            fields["starts_at"],
            fields["ends_at"],
            str(admin.get("email") or admin.get("username") or "")[:255],
        ],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The announcement could not be saved.",
        )
    audit.log_event(
        "announcement.created",
        level=audit.Level.INFO,
        source=audit.Source.APP,
        user_email=str(admin.get("email") or ""),
        metadata={"what": "Posted an announcement", "title": fields["title"], "kind": fields["kind"]},
    )
    return {"success": True, "data": _serialize(rows[0])}


@router.patch("/{announcement_id}")
def update_announcement(
    announcement_id: int,
    body: AnnouncementBody,
    admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    """Edit an announcement.

    ``updated_at`` moves, which is what un-dismisses it for everybody who has
    already read the previous wording. That is the intended behaviour of an edit:
    the posted text changed, so who has read it is no longer known.
    """
    existing = query_one("SELECT id FROM announcements WHERE id = %s", [announcement_id])
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such announcement.")

    fields = _clean(body)
    rows = execute_returning(
        f"""
        UPDATE announcements
           SET title = %s, body = %s, kind = %s, link_url = %s, link_label = %s,
               starts_at = %s, ends_at = %s,
               is_active = COALESCE(%s, is_active),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        RETURNING {_COLUMNS}
        """,
        [
            fields["title"],
            fields["body"],
            fields["kind"],
            fields["link_url"],
            fields["link_label"],
            fields["starts_at"],
            fields["ends_at"],
            body.is_active,
            announcement_id,
        ],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The announcement could not be updated.",
        )
    audit.log_event(
        "announcement.updated",
        level=audit.Level.INFO,
        source=audit.Source.APP,
        user_email=str(admin.get("email") or ""),
        metadata={"what": "Edited an announcement", "title": fields["title"], "id": announcement_id},
    )
    return {"success": True, "data": _serialize(rows[0])}


@router.delete("/{announcement_id}", status_code=204)
def delete_announcement(
    announcement_id: int,
    admin: AuthUser = Depends(_require_admin),
):
    """Remove an announcement outright.

    A hard delete, and its dismissals go with it: a retired announcement that
    lingers as a soft-deleted row would come back the moment somebody flipped
    is_active without realising the row was rewritten in between. Retiring
    without deleting is what the Active switch is for.
    """
    row = query_one("SELECT title FROM announcements WHERE id = %s", [announcement_id])
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such announcement.")
    execute("DELETE FROM announcement_dismissals WHERE announcement_id = %s", [announcement_id])
    execute("DELETE FROM announcements WHERE id = %s", [announcement_id])
    audit.log_event(
        "announcement.deleted",
        level=audit.Level.INFO,
        source=audit.Source.APP,
        user_email=str(admin.get("email") or ""),
        metadata={"what": "Deleted an announcement", "title": row.get("title"), "id": announcement_id},
    )
    return None
