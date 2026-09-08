"""
Suggestions — the feedback board.

It used to be a write-only box on the Settings page. You typed an idea, it went into a
table, and that was the last anyone heard of it: you could not see other people's ideas,
you could not say "yes, this, I need it too", and you never found out whether yours was
going to happen. Everything that makes feedback worth collecting was missing.

This is the fider model, which exists because it works:

  * Everyone can SEE the board. An idea nobody can read cannot be agreed with.
  * Everyone can VOTE. Votes are how you find out which of forty ideas actually matter,
    instead of an admin guessing from a list sorted by date.
  * Admins set a STATUS — planned, in progress, completed, declined — and write a
    RESPONSE. A declined idea with a reason is a conversation. A declined idea in
    silence is why people stop bothering.

WHO WROTE IT is anonymous to other users and visible to admins. People self-censor when
their name is on the complaint, and the complaints they swallow are the ones worth
hearing; admins still need to know who to go and ask. The redaction happens HERE, on the
way out of the API — not in the template. A payload that carries every author's address
to every browser and merely declines to paint it is not anonymous, it is one DevTools
tab away from a directory of who said what.


Endpoints:
  GET    /api/suggestions                 -> the board (any authenticated user)
  POST   /api/suggestions                 -> add one
  POST   /api/suggestions/{id}/vote       -> vote
  DELETE /api/suggestions/{id}/vote       -> take it back
  PATCH  /api/suggestions/{id}            -> admin: status + response
  DELETE /api/suggestions/{id}            -> admin: remove
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field

import audit
from db import execute, execute_returning, query_all, query_one
from security import AuthUser, get_current_user, has_effective_admin_access_live

from .notifications import create_notification


log = logging.getLogger(__name__)
router = APIRouter()

# 'new' is what an idea is before anyone has looked at it. The rest are an admin saying
# something, which is the entire value of having statuses at all.
STATUSES = ("new", "planned", "in_progress", "completed", "declined")
STATUS_LABELS = {
    "new": "New", "planned": "Planned", "in_progress": "In progress",
    "completed": "Done", "declined": "Declined",
}


class SuggestionBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=5000)


class CommentBody(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


class UpdateBody(BaseModel):
    status: Optional[str] = Field(None, description="new|planned|in_progress|completed|declined")
    admin_response: Optional[str] = Field(None, max_length=5000)
    # Kept so the old Platform Managing "reviewed" checkbox keeps working against this
    # endpoint rather than 422-ing the moment this ships.
    reviewed: Optional[bool] = None


def _caller_email(user: AuthUser) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="User email missing from auth context",
        )
    return email


def _require_admin(user: AuthUser) -> None:
    if not has_effective_admin_access_live(user):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Admin access required")


def _vote_state(suggestion_id: str, email: str) -> Dict[str, Any]:
    """The count and the caller's own vote, so the button never has to guess."""
    row = query_one(
        """
        SELECT COUNT(*)::int AS votes,
               BOOL_OR(user_email = %s) AS voted
          FROM suggestion_votes
         WHERE suggestion_id = %s
        """,
        [email, suggestion_id],
    ) or {}
    return {"votes": int(row.get("votes") or 0), "voted": bool(row.get("voted"))}


# ── Notifications ────────────────────────────────────────────────────────────
# Who hears about what happens to a suggestion. Everything here is ANONYMOUS to the
# people notified: a message never says who voted or who commented — only that it
# happened — and admin actions are attributed to "the platform team", not a person.
# That keeps the board's promise (colleagues can't see who said what) intact in the
# notifications too. All of it is best-effort; a notification failure never blocks the
# action that triggered it.

def _suggestion_row(suggestion_id: str) -> Optional[Dict[str, Any]]:
    return query_one(
        "SELECT id::text AS id, user_email, title FROM suggestions WHERE id = %s",
        [suggestion_id],
    )


def _followers(suggestion_id: str, include_voters: bool = True, include_commenters: bool = True) -> set:
    """Everyone with a stake in this suggestion: voters and commenters, lower-cased."""
    people: set = set()
    if include_voters:
        for r in query_all("SELECT DISTINCT user_email FROM suggestion_votes WHERE suggestion_id = %s", [suggestion_id]) or []:
            e = (r.get("user_email") or "").strip().lower()
            if e:
                people.add(e)
    if include_commenters:
        for r in query_all("SELECT DISTINCT user_email FROM suggestion_comments WHERE suggestion_id = %s", [suggestion_id]) or []:
            e = (r.get("user_email") or "").strip().lower()
            if e:
                people.add(e)
    return people


def _platform_admins() -> set:
    """Active Platform Admins, by e-mail. Empty set on any failure.

    A board where ideas arrive and nobody is told is the write-only box this replaced.
    The votes and comments notify the people who asked; this is the other half — the
    people who can answer.
    """
    try:
        rows = query_all(
            """
            SELECT u.email
              FROM users u JOIN roles r ON u.role_id = r.id
             WHERE u.is_active = true AND r.hierarchy_level = 1
            """
        ) or []
    except Exception as exc:
        log.debug("platform admin lookup failed: %s", exc)
        return set()
    return {(r.get("email") or "").strip().lower() for r in rows if r.get("email")}


def _notify(recipients, message: str, group_key: str, exclude: str = "") -> None:
    ex = (exclude or "").strip().lower()
    seen: set = set()
    for raw in recipients:
        e = (raw or "").strip().lower()
        if not e or e == ex or e in seen:
            continue
        seen.add(e)
        try:
            create_notification(
                user_email=e,
                message=message,
                notif_type="SUGGESTION",
                link="/ui/suggestions",
                group_key=group_key,
            )
        except Exception as exc:  # noqa: BLE001 — notifications never block the action
            log.debug("suggestion notify failed for %s: %s", e, exc)


@router.get("")
def list_suggestions(
    status_filter: Optional[str] = Query(None, alias="status"),
    sort: str = Query("votes", description="votes | newest"),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """The whole board — visible to everyone, which is the point.

    Sorted by votes by default: that is the question the board exists to answer. The
    caller's own vote comes back on each row (``voted``) so the UI can render the button
    in the right state without a second round-trip.

    Authors are redacted for non-admins before the rows leave this function.
    """
    email = _caller_email(current_user)
    is_admin = has_effective_admin_access_live(current_user)

    clauses: List[str] = []
    params: List[Any] = [email]
    if status_filter and status_filter in STATUSES:
        clauses.append("s.status = %s")
        params.append(status_filter)
    elif status_filter == "open":
        # "Open" is the useful default view: everything still in play.
        clauses.append("s.status IN ('new', 'planned', 'in_progress')")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "s.created_at DESC" if sort == "newest" else "votes DESC, s.created_at DESC"

    try:
        rows = query_all(
            f"""
            SELECT s.id::text AS id,
                   s.user_email,
                   s.title,
                   s.description,
                   s.status,
                   s.admin_response,
                   s.responded_by,
                   s.responded_at,
                   s.created_at,
                   COALESCE(v.n, 0)::int AS votes,
                   COALESCE(c.n, 0)::int AS comment_count,
                   (mine.user_email IS NOT NULL) AS voted
              FROM suggestions s
              LEFT JOIN (
                    SELECT suggestion_id, COUNT(*) AS n
                      FROM suggestion_votes
                     GROUP BY suggestion_id
              ) v ON v.suggestion_id = s.id
              LEFT JOIN (
                    SELECT suggestion_id, COUNT(*) AS n
                      FROM suggestion_comments
                     GROUP BY suggestion_id
              ) c ON c.suggestion_id = s.id
              LEFT JOIN suggestion_votes mine
                     ON mine.suggestion_id = s.id AND mine.user_email = %s
              {where}
             ORDER BY {order}
             LIMIT 300
            """,
            params,
        )
    except Exception as exc:
        log.warning("list_suggestions failed: %s: %s", type(exc).__name__, exc)
        rows = []

    for row in rows:
        author = (row.get("user_email") or "").strip().lower()
        # "Is this mine?" is decided here, so the browser never needs the address to
        # answer it — that question is the only reason the UI wanted the email at all.
        row["mine"] = bool(author) and author == email
        if not (is_admin or row["mine"]):
            row["user_email"] = None
            row["responded_by"] = None

    return {
        "success": True,
        "data": rows,
        "is_admin": is_admin,
        "me": email,
    }


@router.post("")
def create_suggestion(
    body: SuggestionBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="A title is required")

    try:
        rows = execute_returning(
            """
            INSERT INTO suggestions (user_email, title, description)
            VALUES (%s, %s, %s)
            RETURNING id::text AS id
            """,
            [email, title, (body.description or "").strip()],
        )
    except Exception as exc:
        log.warning("create_suggestion failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to save the suggestion")

    if not rows:
        raise HTTPException(status_code=500, detail="Failed to save the suggestion")

    suggestion_id = rows[0]["id"]

    # You always agree with your own idea. Making people vote for the thing they just
    # posted is a formality that only ever produces suggestions sitting at zero votes.
    try:
        execute(
            "INSERT INTO suggestion_votes (suggestion_id, user_email) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            [suggestion_id, email],
        )
    except Exception as exc:
        log.debug("self-vote failed for %s: %s", suggestion_id, exc)

    audit.log(
        "suggestions.created",
        user_email=email,
        metadata={"what": "Posted a suggestion", "title": title[:120], "id": suggestion_id},
    )

    # Tell the platform team something arrived — without saying who posted it, which is
    # the board's rule and the reason people post at all.
    _notify(
        _platform_admins(),
        f'New suggestion on the board: "{title}".',
        f"sugg:{suggestion_id}:new",
        exclude=email,
    )
    return {"success": True, "data": {"id": suggestion_id}}


@router.post("/{suggestion_id}/vote")
def add_vote(
    suggestion_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        # ON CONFLICT DO NOTHING: voting twice is not an error, it is a double-click.
        execute(
            "INSERT INTO suggestion_votes (suggestion_id, user_email) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            [suggestion_id, email],
        )
    except Exception as exc:
        log.warning("add_vote failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=400, detail="Could not record that vote")

    # Tell the author their idea got a vote — anonymously, and collapsed under one bell
    # entry (the group_key) so a popular suggestion doesn't bury them in notifications.
    row = _suggestion_row(suggestion_id)
    if row:
        _notify(
            [row.get("user_email")],
            f'Someone voted for your suggestion "{row.get("title") or ""}".',
            f"sugg:{suggestion_id}:votes",
            exclude=email,
        )
    return {"success": True, "data": _vote_state(suggestion_id, email)}


@router.delete("/{suggestion_id}/vote")
def remove_vote(
    suggestion_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    try:
        execute(
            "DELETE FROM suggestion_votes WHERE suggestion_id = %s AND user_email = %s",
            [suggestion_id, email],
        )
    except Exception as exc:
        log.warning("remove_vote failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=400, detail="Could not remove that vote")
    return {"success": True, "data": _vote_state(suggestion_id, email)}


@router.patch("/{suggestion_id}")
def update_suggestion(
    suggestion_id: str,
    body: UpdateBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Admin: set the status, and say why."""
    _require_admin(current_user)
    email = _caller_email(current_user)

    new_status = body.status
    if new_status is None and body.reviewed is not None:
        # Back-compat with the old checkbox, which only knew "reviewed" / "not".
        new_status = "completed" if body.reviewed else "new"
    if new_status is not None and new_status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(STATUSES)}",
        )

    existing = query_one(
        "SELECT id, user_email, title FROM suggestions WHERE id = %s", [suggestion_id]
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    sets: List[str] = []
    params: List[Any] = []
    if new_status is not None:
        sets.append("status = %s")
        params.append(new_status)
    if body.admin_response is not None:
        sets.append("admin_response = %s")
        sets.append("responded_by = %s")
        sets.append("responded_at = %s")
        params.extend([body.admin_response.strip() or None, email, datetime.now(timezone.utc)])
    if not sets:
        raise HTTPException(status_code=400, detail="Nothing to update")

    params.append(suggestion_id)
    try:
        rows = execute_returning(
            f"""
            UPDATE suggestions SET {', '.join(sets)}
             WHERE id = %s
             RETURNING id::text AS id, status, admin_response, responded_by, responded_at
            """,
            params,
        )
    except Exception as exc:
        log.warning("update_suggestion failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to update the suggestion")
    if not rows:
        raise HTTPException(status_code=500, detail="Failed to update the suggestion")

    audit.log(
        "suggestions.updated",
        user_email=email,
        metadata={
            "what": "Responded to a suggestion",
            "id": suggestion_id,
            "status": rows[0].get("status"),
        },
    )

    # Tell everyone with a stake — the author, the voters, the commenters — that the
    # platform team acted. This is the update people are actually waiting for: "the thing
    # I asked for is now planned". A written response and a status change are separate
    # events with their own bell entries.
    title = existing.get("title") or "your suggestion"
    audience = _followers(suggestion_id)
    audience.add((existing.get("user_email") or "").strip().lower())
    if body.admin_response is not None and (body.admin_response or "").strip():
        _notify(
            audience,
            f'The platform team responded to "{title}".',
            f"sugg:{suggestion_id}:response",
            exclude=email,
        )
    if new_status is not None:
        _notify(
            audience,
            f'Your suggestion "{title}" is now {STATUS_LABELS.get(new_status, new_status)}.',
            f"sugg:{suggestion_id}:status",
            exclude=email,
        )
    return {"success": True, "data": rows[0]}


@router.delete("/{suggestion_id}")
def delete_suggestion(
    suggestion_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Admin: remove a suggestion. Its votes go with it (ON DELETE CASCADE)."""
    _require_admin(current_user)
    email = _caller_email(current_user)
    try:
        execute("DELETE FROM suggestions WHERE id = %s", [suggestion_id])
    except Exception as exc:
        log.warning("delete_suggestion failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to delete the suggestion")

    audit.log(
        "suggestions.deleted",
        level=audit.Level.WARNING,
        user_email=email,
        metadata={"what": "Deleted a suggestion", "id": suggestion_id},
    )
    return {"success": True}


# ── Comments ─────────────────────────────────────────────────────────────────
# A suggestion is a conversation, not just a poll. Comments follow the board's anonymity
# rule: a regular user's name is hidden from other regular users (shown to admins and to
# the author themselves), while an ADMIN comment is stamped and shown as the platform
# team, because an official reply that hides who it's from is worse than useless.

@router.get("/{suggestion_id}/comments")
def list_comments(
    suggestion_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    is_admin = has_effective_admin_access_live(current_user)
    try:
        rows = query_all(
            """
            SELECT id::text AS id, user_email, is_admin, body, created_at
              FROM suggestion_comments
             WHERE suggestion_id = %s
             ORDER BY created_at ASC
            """,
            [suggestion_id],
        )
    except Exception as exc:
        log.warning("list_comments failed: %s: %s", type(exc).__name__, exc)
        rows = []

    for row in rows:
        author = (row.get("user_email") or "").strip().lower()
        row["is_admin"] = bool(row.get("is_admin"))
        row["mine"] = bool(author) and author == email
        # Redact the author for non-admins — unless it's their own comment, or it's an
        # admin/platform comment (which is meant to be attributed).
        if not (is_admin or row["mine"] or row["is_admin"]):
            row["user_email"] = None

    return {"success": True, "data": rows, "is_admin": is_admin, "me": email}


@router.post("/{suggestion_id}/comments")
def add_comment(
    suggestion_id: str,
    body: CommentBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    email = _caller_email(current_user)
    is_admin = has_effective_admin_access_live(current_user)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="A comment can't be empty")

    row = _suggestion_row(suggestion_id)
    if not row:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    try:
        created = execute_returning(
            """
            INSERT INTO suggestion_comments (suggestion_id, user_email, is_admin, body)
            VALUES (%s, %s, %s, %s)
            RETURNING id::text AS id
            """,
            [suggestion_id, email, is_admin, text],
        )
    except Exception as exc:
        log.warning("add_comment failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to save the comment")
    if not created:
        raise HTTPException(status_code=500, detail="Failed to save the comment")

    title = row.get("title") or "your suggestion"
    author = (row.get("user_email") or "").strip().lower()
    if is_admin:
        # The platform team spoke — everyone with a stake should hear it.
        audience = _followers(suggestion_id)
        audience.add(author)
        _notify(audience, f'The platform team commented on "{title}".',
                f"sugg:{suggestion_id}:comments", exclude=email)
    else:
        # A peer commented — notify the author and the other people in the thread, not
        # every voter (that would be noise). Anonymous, per the board's rule.
        audience = _followers(suggestion_id, include_voters=False)
        audience.add(author)
        _notify(audience, f'New comment on "{title}".',
                f"sugg:{suggestion_id}:comments", exclude=email)

    return {"success": True, "data": {"id": created[0]["id"]}}


@router.delete("/{suggestion_id}/comments/{comment_id}")
def delete_comment(
    suggestion_id: str,
    comment_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Delete a comment. Your own, or any if you're an admin."""
    email = _caller_email(current_user)
    is_admin = has_effective_admin_access_live(current_user)
    row = query_one(
        "SELECT user_email FROM suggestion_comments WHERE id = %s AND suggestion_id = %s",
        [comment_id, suggestion_id],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not is_admin and (row.get("user_email") or "").strip().lower() != email:
        raise HTTPException(status_code=403, detail="You can only delete your own comment")
    try:
        execute("DELETE FROM suggestion_comments WHERE id = %s", [comment_id])
    except Exception as exc:
        log.warning("delete_comment failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to delete the comment")
    return {"success": True}
