"""
"Needs me today" — the one list of things actually waiting on this person.

WHY IT IS SEPARATE FROM THE WIDGETS

The dashboard shows STATE: here are your work items, your pull requests, your tickets.
That is useful but it is not an answer — you still have to read six cards and work out
which of the forty things on them is blocked on you specifically. Most days the honest
answer is three items, and they are scattered across three systems.

So this asks a different question. Not "what exists" but "what is waiting for me":

  * pull requests where I am a reviewer and have NOT voted yet
  * work items in a review state ("Pending Review", "Needs Review", …) that are mine
    to look at — the half a PR list cannot see
  * approval requests waiting on my decision (admins only)
  * my own self-service requests that failed, or completed with an incomplete grant
  * support tickets where the last word was theirs, not mine

Every source is isolated and best-effort, exactly like search: a SonarQube that is down
must not empty the list. A source that fails contributes an "unavailable" note rather
than silently contributing nothing, so a short list is never mistaken for a quiet day.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from security import AuthUser, get_current_user, has_effective_admin_access_live

log = logging.getLogger(__name__)
router = APIRouter()

# Each source gets its own thread and its own deadline. The whole endpoint must answer
# quickly enough to sit at the top of the dashboard, and the slowest integration must
# not decide that.
_SOURCE_TIMEOUT_S = 12


def _pull_requests_awaiting_me(user: AuthUser) -> List[Dict[str, Any]]:
    from . import azure_devops

    # Every argument is passed EXPLICITLY: these endpoints are called as plain Python,
    # so an omitted argument arrives as FastAPI's Query object, not None.
    result = azure_devops.get_pull_requests(
        username=None, collection=None, project=None, repository=None,
        role="review", current_user=user,
    )
    items: List[Dict[str, Any]] = []
    for pr in (result or {}).get("data", []) or []:
        # Already signed off is not waiting on me. The vote constant is imported from
        # the module that owns it so this and the widget cannot disagree.
        if pr.get("my_vote") == azure_devops._VOTE_APPROVED:
            continue
        items.append({
            "kind": "pull_request",
            "system": "Azure DevOps",
            "title": pr.get("title") or "Pull request",
            "subtitle": f"{pr.get('repository') or ''} · by {pr.get('created_by') or 'someone'}",
            "url": pr.get("url"),
            "age": pr.get("creation_date"),
            "why": "Waiting for your review",
            "key": f"pr:{pr.get('id') or pr.get('pull_request_id') or pr.get('url')}",
        })
    return items


def _approvals_awaiting_me(user: AuthUser) -> List[Dict[str, Any]]:
    if not has_effective_admin_access_live(user):
        return []
    from . import approvals

    result = approvals.get_requests(status_filter=None, scope="all", current_user=user)
    return [
        {
            "kind": "approval",
            "system": "Self-service",
            "title": row.get("request_title") or row.get("request_type") or "Request",
            "subtitle": f"from {row.get('requester_email') or 'a user'}",
            "url": "/ui/approvals",
            "age": row.get("created_at"),
            "why": "Waiting for your decision",
            "key": f"approval:{row.get('id')}",
        }
        for row in (result or {}).get("data", []) or []
        if str(row.get("status") or "").upper() == "PENDING"
    ]


def _my_requests_needing_attention(user: AuthUser) -> List[Dict[str, Any]]:
    from . import approvals

    result = approvals.get_requests(status_filter=None, scope="mine", current_user=user)
    items: List[Dict[str, Any]] = []
    for row in (result or {}).get("data", []) or []:
        status = str(row.get("status") or "").upper()
        execution = row.get("execution_result") or {}
        if status == "FAILED":
            why = "Failed — needs another look"
        elif isinstance(execution, dict) and execution.get("grants_ok") is False:
            # The case that used to be invisible: the project exists but the person it
            # was for never got access.
            why = "Completed, but not every permission was granted"
        else:
            continue
        items.append({
            "kind": "my_request",
            "system": "Self-service",
            "title": row.get("request_title") or row.get("request_type") or "Your request",
            "subtitle": status.replace("_", " ").title(),
            "url": "/ui/my-requests",
            "age": row.get("created_at"),
            "why": why,
            "key": f"request:{row.get('id')}",
        })
    return items


def _tickets_awaiting_me(user: AuthUser) -> List[Dict[str, Any]]:
    from . import servicenow

    result = servicenow.get_tickets(current_user=user)
    items: List[Dict[str, Any]] = []
    for ticket in (result or {}).get("data", []) or []:
        # "Awaiting info" is the state that means the ball is explicitly ours.
        state = str(ticket.get("state") or ticket.get("status") or "").lower()
        if "await" not in state and "info" not in state and "pending" not in state:
            continue
        items.append({
            "kind": "ticket",
            "system": "ServiceNow",
            "title": ticket.get("short_description") or ticket.get("number") or "Ticket",
            "subtitle": ticket.get("number") or "",
            "url": "/ui/support",
            "age": ticket.get("opened_at") or ticket.get("sys_created_on"),
            "why": "Waiting on your reply",
            "key": f"ticket:{ticket.get('sys_id') or ticket.get('number')}",
        })
    return items


def _work_items_awaiting_my_review(user: AuthUser) -> List[Dict[str, Any]]:
    """
    Work items parked in a review state with this user on the hook.

    Without this the strip really is just the PR widget again: a pull request is only
    one kind of review. A story sitting in "Pending Review" assigned to you blocks
    someone exactly the same way, and on the dashboard it is one indistinguishable row
    among all the other items that merely belong to you.
    """
    from . import azure_devops

    items: List[Dict[str, Any]] = []
    for wi in azure_devops.get_work_items_awaiting_my_review(user) or []:
        # Who is actually on the hook. "Pending Review" on its own does not say whose
        # review is blocked, and with a reviewer field configured the assignee is not
        # necessarily the caller — so name them.
        people = []
        if wi.get("assigned_to"):
            people.append(f"Assigned to {wi['assigned_to']}")
        if wi.get("changed_by") and wi.get("changed_by") != wi.get("assigned_to"):
            people.append(f"last updated by {wi['changed_by']}")
        elif wi.get("created_by") and wi.get("created_by") != wi.get("assigned_to"):
            people.append(f"raised by {wi['created_by']}")

        items.append({
            "kind": "work_item",
            "system": "Azure DevOps",
            "title": wi.get("title") or "Work item",
            "subtitle": f"{wi.get('type') or 'Work item'} #{wi.get('id')} · {wi.get('project') or ''}",
            "people": " · ".join(people),
            "url": wi.get("url"),
            "age": wi.get("changed_date"),
            # The state IS the reason, and process templates word it differently —
            # showing the project's own word for it beats a generic label.
            "why": wi.get("state") or "Waiting for your review",
            "key": f"wi:{wi.get('collection') or ''}:{wi.get('project') or ''}:{wi.get('id')}",
        })
    return items


_SOURCES: Dict[str, Callable[[AuthUser], List[Dict[str, Any]]]] = {
    "Pull requests": _pull_requests_awaiting_me,
    "Work items": _work_items_awaiting_my_review,
    "Approvals": _approvals_awaiting_me,
    "Your requests": _my_requests_needing_attention,
    "ServiceNow": _tickets_awaiting_me,
}


@router.get("")
def inbox(
    fresh: bool = False,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Everything waiting on the caller, from every source, in one list.

    Cached per user for 45 seconds. This endpoint fans out to five sources and sits at
    the very top of the dashboard, so it is the slowest thing on the page and the first
    thing looked at — the one place where a stale-but-instant answer beats a correct-but
    -late one. ``?fresh=1`` skips the cache, which is what the Refresh button sends: a
    refresh that re-serves the cached answer is not a refresh.
    """
    owner = _owner_of(current_user)
    if fresh:
        return _collect(current_user)
    try:
        from integrations_cache import cached_external

        return cached_external("inbox", owner, "needs-you", lambda: _collect(current_user), ttl=45)
    except Exception as exc:
        # Imported here and reported, not swallowed: a lazy import that silently falls
        # back is exactly how the external cache was dead in the cluster for months.
        log.warning("inbox: cache unavailable, answering uncached: %s", exc)
        return _collect(current_user)


def _owner_of(current_user: AuthUser) -> str:
    """The cache-scoping key for one user. Defined once so a read and an invalidation
    can never compute different keys — which would leave a stale strip on screen."""
    return str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).lower()


def _dismissals(user_id: str) -> Dict[str, str]:
    """``item_key -> item_stamp`` for everything this user has waved off."""
    try:
        from db import query_all

        rows = query_all(
            "SELECT item_key, item_stamp FROM inbox_dismissals WHERE user_id = %s",
            [str(user_id)],
        )
    except Exception as exc:
        # A dismissal that cannot be read means the item comes back. Annoying; the
        # opposite (hiding things because a query failed) loses work silently.
        log.warning("inbox: could not read dismissals: %s", exc)
        return {}
    return {str(r["item_key"]): str(r.get("item_stamp") or "") for r in rows or []}


def _is_dismissed(item: Dict[str, Any], dismissed: Dict[str, str]) -> bool:
    """
    Dismissed AND untouched since.

    Comparing the stamp is what keeps this from being a permanent mute: if the item has
    changed since it was waved off, that is new information and it comes back. ISO-8601
    timestamps sort correctly as strings, which is why they are compared as text.
    """
    key = item.get("key")
    if not key or key not in dismissed:
        return False
    stamp_at_dismissal = dismissed[key]
    current = str(item.get("age") or "")
    if not stamp_at_dismissal or not current:
        return True
    return current <= stamp_at_dismissal


def _collect(current_user: AuthUser) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    unavailable: List[str] = []

    with ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
        futures = {
            name: pool.submit(fn, current_user) for name, fn in _SOURCES.items()
        }
        for name, future in futures.items():
            try:
                items.extend(future.result(timeout=_SOURCE_TIMEOUT_S) or [])
            except Exception as exc:  # noqa: BLE001 — one dead source must not empty the list
                log.info("inbox: %s unavailable: %s: %s", name, type(exc).__name__, exc)
                unavailable.append(name)

    dismissed = _dismissals(str(current_user.get("id") or ""))
    hidden = 0
    if dismissed:
        kept = [i for i in items if not _is_dismissed(i, dismissed)]
        hidden = len(items) - len(kept)
        items = kept

    # Oldest first: the thing that has been waiting longest is the thing most likely to
    # be forgotten, which is the entire reason for showing this list at all.
    items.sort(key=lambda i: str(i.get("age") or ""))

    return {
        "success": True,
        "data": items,
        "count": len(items),
        # Named so the UI can say "ServiceNow is unavailable" rather than letting an
        # empty list read as "nothing to do".
        "unavailable": unavailable,
        "hidden": hidden,
    }


class DismissBody(BaseModel):
    key: str = Field(..., max_length=512, description="The item's stable key")
    stamp: str = Field("", max_length=64, description="The item's age/changed-date when dismissed")
    reason: str = Field("done", max_length=32, description="'done' or 'hidden'")


@router.post("/dismiss")
def dismiss(
    body: DismissBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Mark one item as dealt with, for this user only.

    Nothing is written to Azure DevOps or ServiceNow — this is a personal note, not a
    state change. That distinction is the whole design: the strip shows items from
    systems where the caller often has no authority to close anything, and a button
    that silently edited a work item would be far worse than one that did nothing.
    """
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key is required")
    reason = (body.reason or "done").strip().lower()
    if reason not in {"done", "hidden"}:
        reason = "done"
    try:
        from db import execute

        execute(
            """
            INSERT INTO inbox_dismissals (user_id, item_key, item_stamp, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, item_key)
            DO UPDATE SET item_stamp = EXCLUDED.item_stamp,
                          reason = EXCLUDED.reason,
                          dismissed_at = CURRENT_TIMESTAMP
            """,
            [str(current_user.get("id")), key, (body.stamp or "").strip(), reason],
        )
    except Exception as exc:
        log.warning("inbox: dismiss failed for %s: %s", key, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save that. The item will still be here.",
        )
    _invalidate(current_user)
    return {"success": True, "data": {"key": key, "reason": reason}}


@router.post("/restore")
def restore(
    body: DismissBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Undo a dismissal — the same operation with the flag flipped, so they cannot drift."""
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key is required")
    try:
        from db import execute

        execute(
            "DELETE FROM inbox_dismissals WHERE user_id = %s AND item_key = %s",
            [str(current_user.get("id")), key],
        )
    except Exception as exc:
        log.warning("inbox: restore failed for %s: %s", key, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not undo that.",
        )
    _invalidate(current_user)
    return {"success": True, "data": {"key": key}}


def _invalidate(current_user: AuthUser) -> None:
    """Drop this user's cached strip so the next read reflects the change immediately."""
    try:
        from integrations_cache import invalidate_owner

        invalidate_owner("inbox", _owner_of(current_user))
    except Exception as exc:
        log.warning("inbox: cache invalidation failed: %s", exc)
