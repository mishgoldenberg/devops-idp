"""
In-app notifications for the approval workflow (and other async events).

Notifications are keyed by the recipient's SSO email so we don't need to
resolve users -> UUIDs on every tick of the frontend poll. The table is
created on startup by db.ensure_approval_workflow_tables().

Each row carries an optional ``link`` (so the bell dropdown can navigate
to the right page on click) and an optional ``group_key`` (so repeated
events — e.g. several pipeline runs for the same request — collapse
into a single bell entry).

Endpoints (all require an authenticated user and only ever expose/mutate
rows where user_email = the caller's email):

  GET  /api/notifications            -> latest 50 notifications (grouped)
  GET  /api/notifications/unread-count
  GET  /api/notifications/sections   -> per sidebar page: changes since the last visit
  POST /api/notifications/{id}/read  -> mark single notification read
  POST /api/notifications/read-all   -> mark everything read
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status

from db import execute, query_all, query_one
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()

# Keep 60 days of notifications by default. Older rows are removed
# opportunistically whenever anyone lists their inbox.
_RETENTION_DAYS = 60


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


def _cleanup_old_notifications() -> None:
    """Best-effort retention: drop notifications older than _RETENTION_DAYS."""
    try:
        execute(
            "DELETE FROM notifications "
            "WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL '%s days')"
            % int(_RETENTION_DAYS)
        )
    except Exception as exc:
        log.debug("notifications cleanup failed: %s", exc)


def create_notification(
    user_email: str,
    message: str,
    notif_type: Optional[str] = None,
    related_id: Optional[str] = None,
    link: Optional[str] = None,
    group_key: Optional[str] = None,
) -> None:
    """Best-effort: never raises, never blocks the caller's main flow."""
    if not user_email or not message:
        return
    try:
        execute(
            """
            INSERT INTO notifications (user_email, message, notif_type, related_id, link, group_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                user_email.strip().lower(),
                message,
                notif_type,
                related_id,
                (link or None),
                (group_key or None),
            ],
        )
    except Exception as exc:
        # The link/group_key columns may not exist yet on a very old install;
        # fall back to the classic INSERT so the notification still lands.
        log.debug("create_notification extended insert failed: %s", exc)
        try:
            execute(
                """
                INSERT INTO notifications (user_email, message, notif_type, related_id)
                VALUES (%s, %s, %s, %s)
                """,
                [user_email.strip().lower(), message, notif_type, related_id],
            )
        except Exception as inner:
            log.debug("create_notification failed: %s", inner)


def notify_once(
    user_email: str,
    message: str,
    group_key: str,
    notif_type: Optional[str] = None,
    related_id: Optional[str] = None,
    link: Optional[str] = None,
) -> None:
    """Create a notification only if one with this group_key doesn't already exist.

    create_notification() inserts unconditionally, which is right for one-off events but
    wrong for anything derived from STATE. A ticket that has an unseen update is a state,
    re-evaluated on every dashboard load — inserting there would produce a fresh
    notification every 60 seconds until the user opened the ticket.

    The group_key is the identity of the event ("ticket X was updated at time T"), so the
    same update can only ever notify once, no matter how often it is observed.
    """
    if not (user_email and message and group_key):
        return
    try:
        existing = query_one(
            "SELECT 1 AS hit FROM notifications WHERE user_email = %s AND group_key = %s LIMIT 1",
            [user_email.strip().lower(), group_key],
        )
        if existing:
            return
    except Exception as exc:
        # If group_key isn't queryable on this install, stay silent rather than spam.
        log.debug("notify_once dedupe check failed: %s", exc)
        return

    create_notification(
        user_email=user_email,
        message=message,
        notif_type=notif_type,
        related_id=related_id,
        link=link,
        group_key=group_key,
    )


def _fetch_columns() -> str:
    """Return SELECT column list with safe defaults for optional columns."""
    return (
        "id, message, notif_type, related_id, is_read, created_at, "
        "COALESCE(link, NULL) AS link, "
        "COALESCE(group_key, NULL) AS group_key"
    )


def _load_user_rows(email: str, limit: int = 50):
    try:
        return query_all(
            f"""
            SELECT {_fetch_columns()}
            FROM notifications
            WHERE user_email = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            [email, limit],
        )
    except Exception as exc:
        # Retry without the optional columns for legacy installs.
        log.debug("notifications: primary SELECT failed, retrying bare: %s", exc)
        try:
            return query_all(
                """
                SELECT id, message, notif_type, related_id, is_read, created_at
                FROM notifications
                WHERE user_email = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                [email, limit],
            )
        except Exception as inner:
            log.warning("notifications list failed: %s", inner)
            return []


def _group_rows(rows: list) -> list:
    """Collapse notifications sharing the same group_key; newest wins."""
    grouped: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    out: list = []
    for row in rows:
        gk = row.get("group_key")
        if not gk:
            out.append(row)
            continue
        if gk in grouped:
            grouped[gk]["group_count"] = int(grouped[gk].get("group_count") or 1) + 1
            # Preserve unread state if any member is unread.
            if not row.get("is_read"):
                grouped[gk]["is_read"] = False
            continue
        entry = dict(row)
        entry["group_count"] = 1
        grouped[gk] = entry
        out.append(entry)
    # Keep original DESC order; grouped rows already refer to the newest message.
    return out


@router.get("")
def list_notifications(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    _cleanup_old_notifications()
    rows = _load_user_rows(email, limit=50)
    return {"success": True, "data": _group_rows(rows)}


@router.get("/unread-count")
def unread_count(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Unread count that matches the list the bell actually shows.

    Counts DISTINCT groups, not rows. The list collapses everything sharing a
    ``group_key`` into one entry (see ``_group_rows``), so a plain COUNT(*) said "12"
    over a dropdown containing three items — twelve votes on one suggestion are one
    thing to look at, not twelve. The badge and the panel now count the same way.
    """
    email = _caller_email(current_user)
    row = None
    try:
        row = query_one(
            "SELECT COUNT(DISTINCT COALESCE(group_key, id::text)) AS n "
            "FROM notifications WHERE user_email = %s AND is_read = FALSE",
            [email],
        )
    except Exception as exc:
        # Legacy installs without group_key: fall back to the row count.
        log.debug("unread_count grouped query failed: %s", exc)
        try:
            row = query_one(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_email = %s AND is_read = FALSE",
                [email],
            )
        except Exception as inner:
            log.debug("unread_count failed: %s", inner)
    return {"success": True, "data": {"count": int((row or {}).get("n") or 0)}}


# The sidebar pages that carry a "something changed" dot, and where their
# notifications point. A notification belongs to a page by its LINK, not its type:
# a ticket request's approval links to Support, every other request's to My
# Requests, and the link is what the person lands on when they click the bell.
SECTIONS: Dict[str, str] = {
    "my-requests": "/ui/my-requests",
    "support": "/ui/support",
    "suggestions": "/ui/suggestions",
}
# Events the person caused themselves. "Your request was submitted" is a receipt,
# not news -- a dot for something you did a second ago is noise.
_SELF_CAUSED = ("REQUEST_SUBMITTED",)
# Somebody who has never opened a page gets a dot for the last week, not for every
# notification since the account was created.
_FIRST_VISIT_WINDOW_DAYS = 7


def mark_section_seen(email: str, section: str) -> None:
    """Stamp a visit to one of the dotted pages. Best-effort: never raises."""
    e = (email or "").strip().lower()
    if not e or section not in SECTIONS:
        return
    try:
        execute(
            """
            INSERT INTO user_section_seen (user_email, section, seen_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_email, section) DO UPDATE SET seen_at = EXCLUDED.seen_at
            """,
            [e, section],
        )
    except Exception as exc:
        # WARNING: a dot that cannot clear is visible and annoying, and the reason
        # has to be somewhere an admin can read it.
        log.warning("could not stamp %s visit for %s: %s", section, e, exc)


def _observe_ticket_updates(current_user: AuthUser, email: str) -> None:
    """Run the ticket check that raises "has a new update" notifications.

    It used to run only when the dashboard's ticket widget loaded, so somebody who
    had hidden that widget never heard that a ticket was answered. Throttled to once
    every five minutes per person: this is called from every page, and the check
    reads ServiceNow.
    """
    try:
        from integrations_cache import cached_external
        from api.dashboards import get_snow_items

        cached_external(
            "nav", email, "ticket-scan",
            lambda: {"checked": bool(get_snow_items(current_user=current_user))},
            ttl=300,
        )
    except Exception as exc:
        log.info("sections: ticket check skipped: %s: %s", type(exc).__name__, exc)


@router.get("/sections")
def section_news(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """How many things changed on each dotted page since this person last opened it."""
    email = _caller_email(current_user)
    _observe_ticket_updates(current_user, email)
    data: Dict[str, Dict[str, Any]] = {name: {"count": 0, "latest": None} for name in SECTIONS}
    try:
        rows = query_all(
            """
            SELECT s.section,
                   COUNT(DISTINCT COALESCE(n.group_key, n.id::text)) AS n,
                   MAX(n.created_at) AS latest
              FROM UNNEST(%s::text[], %s::text[]) AS s(section, prefix)
              LEFT JOIN user_section_seen v
                     ON v.user_email = %s AND v.section = s.section
              JOIN notifications n
                ON n.user_email = %s
               AND n.link LIKE s.prefix || '%%'
               AND n.created_at > COALESCE(
                       v.seen_at, CURRENT_TIMESTAMP - make_interval(days => %s))
               AND COALESCE(n.notif_type, '') <> ALL(%s::text[])
             GROUP BY s.section
            """,
            [list(SECTIONS.keys()), list(SECTIONS.values()), email, email,
             _FIRST_VISIT_WINDOW_DAYS, list(_SELF_CAUSED)],
        )
    except Exception as exc:
        log.warning("sections: could not count page news for %s: %s", email, exc)
        rows = []
    for row in rows or []:
        name = str(row.get("section") or "")
        if name in data:
            latest = row.get("latest")
            data[name] = {
                # Grouped like the bell: ten votes on one suggestion are one change.
                "count": int(row.get("n") or 0),
                "latest": latest.isoformat() if hasattr(latest, "isoformat") else latest,
            }
    return {"success": True, "data": data}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str = Path(...),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        # Mark the whole GROUP read, not just the row that was clicked. The bell shows
        # one entry per group_key, so reading "3 people voted on your suggestion" and
        # leaving two of its rows unread would put the badge straight back up over an
        # entry the user just dismissed.
        execute(
            """
            UPDATE notifications SET is_read = TRUE
             WHERE user_email = %s
               AND (
                     id = %s
                  OR (group_key IS NOT NULL AND group_key = (
                         SELECT group_key FROM notifications
                          WHERE id = %s AND user_email = %s
                     ))
               )
            """,
            [email, notification_id, notification_id, email],
        )
    except Exception as exc:
        log.debug("mark_read grouped update failed: %s", exc)
        try:
            execute(
                "UPDATE notifications SET is_read = TRUE "
                "WHERE id = %s AND user_email = %s",
                [notification_id, email],
            )
        except Exception as inner:
            log.debug("mark_read failed: %s", inner)
    return {"success": True}


@router.post("/read-all")
def mark_all_read(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        execute(
            "UPDATE notifications SET is_read = TRUE "
            "WHERE user_email = %s AND is_read = FALSE",
            [email],
        )
    except Exception as exc:
        log.debug("mark_all_read failed: %s", exc)
    return {"success": True}
