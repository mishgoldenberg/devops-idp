"""
Self-service approval workflow.

Every self-service action is routed through this module. Azure DevOps project
creation is the only request type wired to a real executor; a request type
without one is rejected at submission and, defensively, is failed loudly rather
than reported as done, so an approval can never claim a side effect that never
happened.

Lifecycle
---------
    PENDING ──approve──► APPROVED ──(auto)──► IN_PROGRESS ──► COMPLETED
                │                                            └► FAILED
                └─reject──► REJECTED

Back-compat: the existing DB enum also contains EXECUTED (legacy
terminal-success). The API treats EXECUTED and COMPLETED as equivalent
for filtering + display purposes.

Notifications are emitted best-effort to the requester on every status
transition; all writes are idempotent so duplicate HTTP retries from the
UI can't double-execute a request.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

import activity
import audit
import safe_mode
from db import execute, execute_returning, query_all, query_one
from security import AuthUser, get_current_user, has_effective_admin_access_live

from .notifications import create_notification


# Where the bell dropdown should navigate when the user clicks a request
# notification. Different request types land on different pages in the UI.
_REQUEST_LINK_DEFAULT = "/ui/my-requests"


def _notification_link(request_row: Dict[str, Any]) -> str:
    rt = str(request_row.get("request_type") or "")
    if rt == "SNOW_TICKET":
        return "/ui/support"
    return _REQUEST_LINK_DEFAULT


log = logging.getLogger(__name__)
router = APIRouter()

# Statuses the UI treats as "terminal success". EXECUTED is the legacy name
# still present in existing rows; new executions write COMPLETED.
TERMINAL_OK_STATUSES = ("COMPLETED", "EXECUTED")

# Admin-side status filters: "active" = everything still moving through the
# pipeline (useful for the Approvals page "In Progress" tab).
ACTIVE_STATUSES = ("APPROVED", "IN_PROGRESS")

# The only request types the portal can actually carry out. A type must appear
# here AND have a branch in `_execute_approved_request` to be executable. This
# is the single gate: `create_request` refuses anything not listed (so it is
# never queued), and the executor fails loudly for anything not listed (so a
# legacy row predating this set can never be reported as completed). Adding a
# new self-service means adding its type here and its executor branch — never
# one without the other.
# Imported, not restated. db.py adds exactly these values to the
# approval_request_type ENUM at startup, and a second copy here is how the
# application came to accept a type Postgres rejected at the INSERT.
from request_types import CLEANER_REQUEST_TYPES, SUPPORTED_REQUEST_TYPES  # noqa: E402


class CreateApprovalRequest(BaseModel):
    request_type: str
    request_title: str
    request_payload: Dict[str, Any]


class ApproveRejectRequest(BaseModel):
    comments: Optional[str] = None


# ─── Create ──────────────────────────────────────────────────────────────

@router.post("/requests")
def create_request(
    body: CreateApprovalRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    if not body.request_type or not body.request_title or not body.request_payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required fields",
        )

    # Only queue a request the portal can actually execute. Refusing here (rather
    # than at approval time) means an admin never approves something that would
    # then fail, and no unexecutable row ever reaches the approvals queue.
    if body.request_type not in SUPPORTED_REQUEST_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{body.request_type}' is not an available self-service action.",
        )

    if not _can_create(current_user, body.request_type):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to create this request type",
        )

    # ── Validate payload (spec: no empty values; project-name rules on ADO) ─
    _validate_request_payload(body.request_type, body.request_title, body.request_payload)

    # ── Duplicate prevention: reject an identical pending request from the
    # same user so a double-click never opens two approval tickets.
    existing = query_one(
        """
        SELECT id, request_title
          FROM approval_requests
         WHERE requester_id = %s
           AND request_type = %s
           AND status = 'PENDING'
           AND request_title = %s
         ORDER BY created_at DESC
         LIMIT 1
        """,
        [current_user["id"], body.request_type, body.request_title],
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You already have a pending request with the same title. "
                "Wait for the existing request to be approved or rejected before "
                "submitting another."
            ),
        )

    rows = execute_returning(
        """
        INSERT INTO approval_requests (requester_id, request_type, request_title, request_payload, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
        RETURNING *
        """,
        [current_user["id"], body.request_type, body.request_title, body.request_payload],
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create approval request")
    row = rows[0]

    _log_audit(current_user["id"], "CREATE_REQUEST", "approval_request", str(row["id"]))

    email = str(current_user.get("email") or "").strip().lower()
    audit.log(
        audit.Action.SELF_SERVICE_REQUEST_CREATED,
        user_email=email,
        metadata={
            "request_id": str(row["id"]),
            "request_type": body.request_type,
            "request_title": body.request_title,
        },
    )
    # User-visible activity entry (dashboard Recent Activity widget).
    # Separate from the admin-facing audit event above — this one is
    # formatted for the requester to see "Used self-service: …" later.
    activity.log_activity(
        user_email=email,
        action_type=activity.ACTION_SELF_SERVICE,
        item_name=body.request_title or body.request_type,
        metadata={
            "request_id": str(row["id"]),
            "request_type": body.request_type,
        },
    )

    # Notify the requester immediately. Admins are notified via the
    # Approvals page polling the "pending" list (no per-admin fan-out).
    if email:
        create_notification(
            user_email=email,
            message=f"Your request '{body.request_title}' was submitted and is pending approval.",
            notif_type="REQUEST_SUBMITTED",
            related_id=str(row["id"]),
            link=_REQUEST_LINK_DEFAULT,
            group_key=f"request:{row['id']}",
        )

    return {"success": True, "data": row, "timestamp": _now_iso()}


# ─── List / detail ──────────────────────────────────────────────────────

@router.get("/requests")
def get_requests(
    status_filter: Optional[str] = Query(None, alias="status"),
    scope: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    List approval requests.

    ``scope=mine``  — restrict to the caller's own requests (used by the
                      user-facing "My Requests" page).
    ``scope=all``   — list everything; admin-only, used by the Approvals page.
    Default         — behaves like ``scope=mine`` for non-admins and
                      ``scope=all`` for admins (keeps backward compatibility
                      with existing callers).
    """
    params: List[Any] = []
    query = (
        "SELECT ar.*, "
        "       u.username as requester_username, "
        "       u.email    as requester_email, "
        "       u.full_name as requester_name, "
        "       approver.username as approver_username, "
        "       approver.email    as approver_email, "
        "       approver.full_name as approver_name "
        "  FROM approval_requests ar "
        "  JOIN users u ON ar.requester_id = u.id "
        "  LEFT JOIN users approver ON ar.approver_id = approver.id "
        " WHERE 1=1"
    )

    if status_filter:
        params.append(status_filter)
        query += " AND ar.status = %s"

    is_admin = _can_approve_any(current_user)
    effective_scope = (scope or "").lower()
    if not effective_scope:
        effective_scope = "all" if is_admin else "mine"

    if effective_scope != "all" or not is_admin:
        params.append(current_user["id"])
        query += " AND ar.requester_id = %s"

    query += " ORDER BY ar.created_at DESC LIMIT 100"

    # Being an admin is not the same as REVIEWING. The reviewer's coordinates -- the
    # pull request, the branch, the commit -- belong to the Approvals screen, which
    # asks for scope=all. The same person looking at My Requests is looking at their
    # own requests as a requester, and a link into Azure DevOps there is an answer to
    # a question nobody on that page asked.
    reviewer_view = is_admin and effective_scope == "all"
    rows = query_all(query, params)
    return {"success": True, "data": _present(rows, reviewer_view), "timestamp": _now_iso()}


# Keys of execution_result that belong to whoever reviews the work, not to whoever
# asked for it. A branch name, a commit id and an Azure DevOps pull request URL are
# the reviewer's coordinates; a requester who clicks that link gets a 404 from a
# server they have no account on, and offering it is worse than not showing one.
_REVIEWER_ONLY_RESULT_KEYS = frozenset({
    "pull_request_url", "pull_request_id", "branch", "base_branch", "commit_id",
    "collection", "repository", "files", "warnings",
})


def _present(rows: Optional[List[Dict[str, Any]]], is_admin: bool) -> List[Dict[str, Any]]:
    """What each request looks like to the person asking for it.

    Two jobs, both of which have to happen HERE rather than in the page that draws it:

      * the reviewer-only parts of a result are dropped for everyone else -- filtering
        in the template leaves the data in the response for anyone who opens the
        network tab;
      * a cleaner request is given the state of its pull request, because "Completed"
        on its own is a half-truth. The request completed; whether the cleaner exists
        depends on a review that may have merged it, may have abandoned it, and had
        done neither at the moment the request finished.
    """
    rows = list(rows or [])
    if not rows:
        return rows

    import cleaner_store

    cleaner_ids = [
        str(row.get("id")) for row in rows
        if str(row.get("request_type") or "").upper().startswith("ARTIFACTORY_CLEANER")
    ]
    records: Dict[str, Dict[str, Any]] = {}
    if cleaner_ids:
        try:
            records = cleaner_store.for_requests(cleaner_ids)
        except Exception as exc:  # a missing record must not empty the page
            log.warning("cleaner records unavailable for the request list: %s: %s",
                        type(exc).__name__, exc)

    for row in rows:
        record = records.get(str(row.get("id")))
        if record:
            row["cleaner"] = cleaner_store.public_view(record, is_admin=is_admin)
        if is_admin:
            continue
        result = row.get("execution_result")
        if isinstance(result, dict):
            row["execution_result"] = {
                k: v for k, v in result.items() if k not in _REVIEWER_ONLY_RESULT_KEYS
            }
    return rows


@router.get("/requests/{request_id}")
def get_request(
    request_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    row = _load_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    # Non-admins can only see their own requests
    is_admin = _can_approve_any(current_user)
    if not is_admin and str(row.get("requester_id")) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"success": True, "data": _present([row], is_admin)[0], "timestamp": _now_iso()}


@router.post("/requests/{request_id}/servicenow-retry")
def retry_servicenow(
    request_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """Order the catalog item for a request whose work already succeeded.

    The item is raised after the work, and is not allowed to fail it -- so a request
    can be genuinely COMPLETED and leave the team no ticket at all. Until now the only
    way to get one was to do the work a second time, which for a quota means adding
    the storage twice and for a cleaner means a second pull request.

    Admin-only, and only for a request that FINISHED and has no number yet: retrying
    one that already has a ticket would raise a duplicate.
    """
    if not _can_approve_any(current_user):
        raise HTTPException(status_code=403, detail="Only an approver can do this.")

    row = _load_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    if str(row.get("status") or "").upper() not in TERMINAL_OK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Only a completed request can have its ServiceNow item raised again.",
        )

    result = dict(row.get("execution_result") or {})
    if result.get("servicenow_number"):
        raise HTTPException(
            status_code=409,
            detail=f"This request already has {result['servicenow_number']}.",
        )

    request_type = str(row.get("request_type") or "").upper()
    payload = dict(row.get("request_payload") or {})
    form_key = (
        "artifactory_quota" if request_type == "ARTIFACTORY_QUOTA_INCREASE"
        else "artifactory_cleaner" if request_type in CLEANER_REQUEST_TYPES
        else ""
    )
    # Whether a form opens a ticket is decided in ONE place -- the form's own spec --
    # so this cannot go on offering to raise one for a request type that stopped
    # raising them. Only the pipeline characterization does now.
    import catalog_forms

    if not form_key or not (catalog_forms.form(form_key) or {}).get("snow_item"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{request_type}' does not open a ServiceNow item — this request is "
                "recorded in the portal and nowhere else."
            ),
        )

    from .catalog import order_catalog_item

    fields = {
        **{k: v for k, v in payload.items() if not isinstance(v, (dict, list))},
        **(payload.get("requester_details") or {}),
        # What actually happened, which is the whole reason this is raised after the
        # work rather than before it.
        **{k: v for k, v in result.items() if isinstance(v, (str, int, float))},
    }
    snow = order_catalog_item(
        form_key, fields, str(row.get("request_title") or "Request"),
        requester_id=str(row.get("requester_id") or ""),
        requester_email=_requester_email(row),
    )
    result["servicenow_number"] = snow.get("number") or ""
    result["servicenow_error"] = snow.get("error") or ""
    execute(
        "UPDATE approval_requests SET execution_result = %s WHERE id = %s",
        [result, request_id],
    )
    if snow.get("number") and request_type in CLEANER_REQUEST_TYPES and payload.get("slug"):
        import cleaner_store

        try:
            cleaner_store.set_snow_number(str(payload["slug"]), snow["number"])
        except Exception as exc:
            log.warning("cleaner snow number not stored on retry: %s: %s",
                        type(exc).__name__, exc)

    log.warning("ServiceNow item re-raised for request %s: %s", request_id,
                snow.get("number") or snow.get("error"))
    return {
        "success": bool(snow.get("number")),
        "data": {"number": snow.get("number") or "", "error": snow.get("error") or ""},
    }


# ─── Approve ────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/approve")
def approve_request(
    request_id: str,
    body: ApproveRejectRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    request_row = query_one("SELECT * FROM approval_requests WHERE id = %s", [request_id])
    if not request_row:
        raise HTTPException(status_code=404, detail="Request not found")
    if request_row["status"] != "PENDING":
        raise HTTPException(status_code=400, detail="Request is not pending")

    if not _can_approve_any(current_user):
        raise HTTPException(
            status_code=403,
            detail="Insufficient permissions to approve this request",
        )

    # Atomic PENDING -> APPROVED transition guarantees exactly-once execution
    # even if the admin double-clicks or two approvers race.
    approved_rows = execute_returning(
        """
        UPDATE approval_requests
           SET status = 'APPROVED',
               approver_id = %s,
               approver_comments = %s,
               approved_at = CURRENT_TIMESTAMP
         WHERE id = %s AND status = 'PENDING'
        RETURNING *
        """,
        [current_user["id"], body.comments, request_id],
    )
    if not approved_rows:
        # Lost the race — someone else already acted on this request.
        raise HTTPException(status_code=409, detail="Request was already processed")

    _log_audit(current_user["id"], "APPROVE_REQUEST", "approval_request", request_id)
    audit.log(
        audit.Action.REQUEST_APPROVED,
        user_email=str(current_user.get("email") or ""),
        metadata={
            "request_id": str(request_id),
            "request_type": request_row.get("request_type"),
            "approval_time_seconds": _seconds_since(request_row.get("created_at")),
        },
    )

    # Notify requester of approval
    requester_email = _requester_email(request_row)
    if requester_email:
        create_notification(
            user_email=requester_email,
            message=f"Your request '{request_row.get('request_title')}' was approved. Execution is starting now.",
            notif_type="REQUEST_APPROVED",
            related_id=str(request_id),
            link=_notification_link(request_row),
            group_key=f"request:{request_id}",
        )

    # Kick off async execution. Never let executor errors leak to the admin.
    threading.Thread(
        target=_run_execution_safely,
        args=(str(request_id),),
        daemon=True,
        name=f"approval-exec-{request_id[:8]}",
    ).start()

    return {"success": True, "message": "Request approved", "timestamp": _now_iso()}


# ─── Reject ─────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/reject")
def reject_request(
    request_id: str,
    body: ApproveRejectRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    request_row = query_one("SELECT * FROM approval_requests WHERE id = %s", [request_id])
    if not request_row:
        raise HTTPException(status_code=404, detail="Request not found")
    if request_row["status"] != "PENDING":
        raise HTTPException(status_code=400, detail="Request is not pending")

    if not _can_approve_any(current_user):
        raise HTTPException(
            status_code=403,
            detail="Insufficient permissions to reject this request",
        )

    reason = (body.comments or "").strip() or "Rejected"
    rejected_rows = execute_returning(
        """
        UPDATE approval_requests
           SET status = 'REJECTED',
               approver_id = %s,
               approver_comments = %s,
               rejection_reason = %s,
               approved_at = CURRENT_TIMESTAMP
         WHERE id = %s AND status = 'PENDING'
        RETURNING *
        """,
        [current_user["id"], reason, reason, request_id],
    )
    if not rejected_rows:
        raise HTTPException(status_code=409, detail="Request was already processed")

    _log_audit(current_user["id"], "REJECT_REQUEST", "approval_request", request_id)
    audit.log(
        audit.Action.REQUEST_REJECTED,
        user_email=str(current_user.get("email") or ""),
        metadata={
            "request_id": str(request_id),
            "request_type": request_row.get("request_type"),
            "reason": reason,
            "approval_time_seconds": _seconds_since(request_row.get("created_at")),
        },
    )

    requester_email = _requester_email(request_row)
    if requester_email:
        create_notification(
            user_email=requester_email,
            message=f"Your request '{request_row.get('request_title')}' was rejected. Reason: {reason}",
            notif_type="REQUEST_REJECTED",
            related_id=str(request_id),
            link=_notification_link(request_row),
            group_key=f"request:{request_id}",
        )

    return {"success": True, "message": "Request rejected", "timestamp": _now_iso()}


# ─── Admin recovery: force-fail / delete stuck requests ────────────────
#
# The happy path always ends in COMPLETED / FAILED / REJECTED. In practice
# requests can get stuck in APPROVED or IN_PROGRESS when the execution
# thread crashed before finishing (pod restart, DB connection drop, etc.).
# These two endpoints give admins a manual escape hatch so the My Requests
# page doesn't accumulate perpetually "In Progress" cards.

class ForceFailBody(BaseModel):
    reason: Optional[str] = None


# Statuses an admin is allowed to manually flip to FAILED. PENDING is
# included so a very old, forgotten request can be force-closed without
# first going through APPROVE. Terminal statuses are intentionally excluded
# — flipping a COMPLETED back to FAILED would rewrite history.
_FORCE_FAILABLE_STATUSES = ("PENDING", "APPROVED", "IN_PROGRESS")


@router.post("/requests/{request_id}/force-fail")
def force_fail_request(
    request_id: str,
    body: ForceFailBody,
    current_user: AuthUser = Depends(get_current_user),
):
    """Admin-only: mark a stuck request as FAILED with a manual reason."""
    if not _can_approve_any(current_user):
        raise HTTPException(
            status_code=403,
            detail="Admin access required to force-fail requests.",
        )

    request_row = query_one("SELECT * FROM approval_requests WHERE id = %s", [request_id])
    if not request_row:
        raise HTTPException(status_code=404, detail="Request not found")

    current_status = str(request_row.get("status") or "").upper()
    if current_status not in _FORCE_FAILABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot force-fail a request in status '{current_status}'.",
        )

    reason = (body.reason or "").strip() or "Manually failed by administrator."
    result_data = {"error": reason, "force_failed_by": str(current_user.get("email") or "")}

    updated = execute_returning(
        """
        UPDATE approval_requests
           SET status = 'FAILED',
               execution_result = %s,
               executed_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND status = ANY(%s)
        RETURNING *
        """,
        [result_data, request_id, list(_FORCE_FAILABLE_STATUSES)],
    )
    if not updated:
        # Someone else (or the executor thread) beat us to a terminal state.
        raise HTTPException(status_code=409, detail="Request was already processed")

    _log_audit(current_user["id"], "FORCE_FAIL_REQUEST", "approval_request", request_id)
    audit.log(
        audit.Action.TERRAFORM_FAILED,
        user_email=str(current_user.get("email") or ""),
        metadata={
            "request_id": str(request_id),
            "request_type": request_row.get("request_type"),
            "error": reason[:500],
            "manual": True,
        },
    )

    requester_email = _requester_email(request_row)
    if requester_email:
        create_notification(
            user_email=requester_email,
            message=(
                f"Your request '{request_row.get('request_title')}' was manually "
                f"marked as failed by an administrator. Reason: {reason}"
            ),
            notif_type="REQUEST_FAILED",
            related_id=str(request_id),
            link=_notification_link(request_row),
            group_key=f"request:{request_id}",
        )

    return {"success": True, "message": "Request marked as failed", "timestamp": _now_iso()}


@router.delete("/requests/{request_id}")
def delete_request(
    request_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """Admin-only: hard-delete a request row (used to clean stuck/abandoned requests)."""
    if not _can_approve_any(current_user):
        raise HTTPException(
            status_code=403,
            detail="Admin access required to delete requests.",
        )

    request_row = query_one(
        "SELECT id, request_type, request_title, status FROM approval_requests WHERE id = %s",
        [request_id],
    )
    if not request_row:
        raise HTTPException(status_code=404, detail="Request not found")

    execute("DELETE FROM approval_requests WHERE id = %s", [request_id])

    _log_audit(current_user["id"], "DELETE_REQUEST", "approval_request", request_id)
    # Action label is free-form (see audit.Action docstring); we use a
    # namespaced string so this event can be distinguished from creates.
    audit.log(
        "self_service.request_deleted",
        user_email=str(current_user.get("email") or ""),
        metadata={
            "request_id": str(request_id),
            "request_type": request_row.get("request_type"),
            "previous_status": request_row.get("status"),
        },
    )

    return {"success": True, "message": "Request deleted", "timestamp": _now_iso()}


# ─── Execution engine ───────────────────────────────────────────────────

def _run_execution_safely(request_id: str) -> None:
    """Background entry point; never raises."""
    try:
        _transition_status(request_id, from_status="APPROVED", to_status="IN_PROGRESS")
        request_row = _load_request(request_id)
        if not request_row:
            log.warning("execution: request %s vanished mid-flight", request_id)
            return
        _execute_approved_request(request_row)
    except Exception as exc:
        log.exception("execution: unhandled error for %s: %s", request_id, exc)
        _finish_failed(request_id, f"Internal error: {exc!s}")


def _execute_approved_request(request_row: Dict[str, Any]) -> None:
    """Map request_type -> real executor. Always finishes in COMPLETED or FAILED."""
    request_id = str(request_row["id"])
    request_type = request_row.get("request_type")
    payload = request_row.get("request_payload") or {}

    try:
        # No executor for this type: fail loudly. Reaching here means a row whose
        # type is not in SUPPORTED_REQUEST_TYPES was somehow approved (e.g. a
        # legacy row created before that gate existed). Never mark it COMPLETED —
        # that would report a side effect that never happened. Checked before
        # Safe Mode: Safe Mode simulates *available* actions, not absent ones.
        if request_type not in SUPPORTED_REQUEST_TYPES:
            _finish_failed(
                request_id,
                f"'{request_type}' is not an available self-service action and "
                "cannot be executed. No changes were made.",
            )
            return

        # Safe Mode: simulate success without real external side effects. Admin
        # toggle/SAFE_MODE env both feed into safe_mode.is_enabled(); when it's
        # on we never call ADO / external systems.
        if safe_mode.is_enabled():
            _finish_completed(request_id, {
                "safe_mode": True,
                "message": "Safe Mode is enabled — request simulated successfully.",
                "request_type": request_type,
            })
            return

        if request_type == "ADO_PROJECT_CREATE":
            result = _execute_ado_project_create(request_row, payload)
        elif request_type == "ARTIFACTORY_QUOTA_INCREASE":
            result = _execute_artifactory_quota_increase(request_row, payload)
        elif request_type == "ARTIFACTORY_CLEANER_CREATE":
            result = _execute_artifactory_cleaner(request_row, payload, mode="add")
        elif request_type == "ARTIFACTORY_CLEANER_UPDATE":
            result = _execute_artifactory_cleaner(request_row, payload, mode="edit")
        elif request_type == "ARTIFACTORY_CLEANER_DELETE":
            result = _execute_artifactory_cleaner(request_row, payload, mode="delete")
        else:
            # Defensive: listed as supported but no branch here. Unreachable
            # unless SUPPORTED_REQUEST_TYPES and this dispatch drift apart — fail
            # rather than fake success, so the drift surfaces instead of hiding.
            raise RuntimeError(
                f"No executor is wired for supported request type '{request_type}'."
            )

        _finish_completed(request_id, result)
    except Exception as exc:
        log.exception("execution: request %s failed: %s", request_id, exc)
        _finish_failed(request_id, _user_facing_error(exc))


def _execute_ado_project_create(
    request_row: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Provision an Azure DevOps project directly via the REST API.

    No Terraform and no Kubernetes Job: this runs inline in the execution thread and
    creates the inherited process + project in each target collection over REST (see
    ``azure_devops.provision_ado_project``). That removes the OpenShift Job-creation
    requirement entirely — the reason the Terraform path could not run here — along with
    the tfstate backend and the Terraform container image.
    """
    project_name = str(payload.get("project_name") or "").strip()
    process_type = str(payload.get("process_type") or "Scrum").strip()
    admin_username = str(payload.get("admin_username") or "").strip()
    description = str(payload.get("description") or "").strip()
    collection = str(payload.get("collection") or "").strip()

    if not project_name:
        raise ValueError("project_name is required")
    if not admin_username:
        raise ValueError("admin_username is required")

    from .azure_devops import provision_ado_project

    audit.log(
        audit.Action.TERRAFORM_STARTED,
        user_email=str(request_row.get("requester_email") or ""),
        metadata={
            "request_id": str(request_row.get("id")),
            "project_name": project_name,
            "process_type": process_type,
            "collection": collection or "(default)",
            "engine": "rest",
        },
    )

    # Creates in ONE collection — the one the requester chose, defaulting to
    # ADO_DEFAULT_COLLECTION. It used to create in every configured collection, so a
    # name already taken in either of them failed the whole request.
    return provision_ado_project(
        project_name=project_name,
        process_type=process_type,
        admin_username=admin_username,
        description=description,
        collection=collection or None,
    )


def _execute_artifactory_quota_increase(
    request_row: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Raise a JFrog project's storage quota by the requested amount.

    The increase is applied to the quota READ AT THIS MOMENT, not to the one that was
    there when the form was filled in. Between those two points somebody may have
    cleaned the project up or already raised it, and adding to a stale number either
    undoes their change or doubles it.

    A project with no quota at all is refused rather than given one: "unlimited" is a
    deliberate configuration, and turning it into a number is a restriction nobody
    asked for.
    """
    import artifactory_admin

    project_key = str(payload.get("project_key") or "").strip()
    increase = int(payload.get("increase_by_bytes") or 0)
    if not project_key:
        raise ValueError("project_key is required")
    if increase <= 0:
        raise ValueError("increase_by_bytes must be greater than zero")

    project = artifactory_admin.project(project_key)
    if not project:
        raise ValueError(
            f"Artifactory has no project '{project_key}' (or the admin account cannot see it)."
        )
    if project["unlimited"]:
        raise ValueError(
            f"'{project_key}' has no storage quota - it is unlimited, so there is "
            "nothing to enlarge."
        )

    current = int(project["quota_bytes"])
    result = artifactory_admin.set_project_quota(project_key, current + increase)
    result.update({
        "used_bytes": project["used_bytes"],
        "used": artifactory_admin.format_bytes(project["used_bytes"]),
        "increase_by": artifactory_admin.format_bytes(increase),
        "free_after": artifactory_admin.format_bytes(
            max(0, result["quota_after_bytes"] - project["used_bytes"])
        ),
    })
    # The requested item is raised here rather than at submission so it carries what
    # actually happened -- the quota before and after -- instead of what was asked for.
    from .catalog import order_catalog_item

    snow = order_catalog_item(
        "artifactory_quota",
        {
            "project_key": project_key,
            "increase_by": result["increase_by"],
            "quota_before": result["quota_before"],
            "quota_after": result["quota_after"],
            "justification": str(payload.get("justification") or ""),
            **(payload.get("requester_details") or {}),
        },
        f"Artifactory quota: {project_key} +{result['increase_by']}",
        requester_id=str(request_row.get("requester_id") or ""),
        requester_email=str(request_row.get("requester_email") or ""),
    )
    _record_servicenow(result, snow)

    _after_the_work(
        result, "audit entry",
        lambda: audit.log(
            audit.Action.ARTIFACTORY_QUOTA_CHANGED,
            user_email=str(request_row.get("requester_email") or ""),
            metadata={
                "request_id": str(request_row.get("id")),
                "artifactory_project": project_key,
                "quota_before": result["quota_before"],
                "quota_after": result["quota_after"],
            },
        ),
    )
    return result


def _execute_artifactory_cleaner(
    request_row: Dict[str, Any], payload: Dict[str, Any], *, mode: str
) -> Dict[str, Any]:
    """Commit a cleaner's files to a branch, open a pull request, and record it.

    Never to the default branch: these files are a scheduled delete, and the review
    is the only thing standing between a mistyped repository name and a job removing
    the wrong artifacts every night. The request finishes as COMPLETED because what it
    promised - the pull request - exists; every result says plainly that nothing runs
    until somebody merges it.

    mode "add" creates the folder; mode "edit" rewrites the ConfigMap and the CronJob
    of a cleaner that is already there, leaving pipeline.yaml alone because it names
    only the folder and the template and therefore never changes; mode "delete"
    removes all three, which is the only way to stop a cleaner -- deleting the portal's
    record would hide it while the CronJob went on running every night.
    """
    import ado_repo
    import artifactory_cleaner
    import cleaner_store

    from .catalog import order_catalog_item

    request = artifactory_cleaner.validate(payload)
    # A removal takes the whole folder, so it needs every path -- including the
    # pipeline an edit deliberately leaves alone.
    files = artifactory_cleaner.render_files(request, include_pipeline=(mode != "edit"))
    summary = artifactory_cleaner.summary(request)
    request_id = str(request_row.get("id"))
    requester = str(request_row.get("requester_email") or "")
    verb = {"add": "Add", "edit": "Update", "delete": "Remove"}[mode]
    branch = f"cleaner/{'remove-' if mode == 'delete' else ''}{request['slug']}-{request_id[:8]}"

    result = ado_repo.commit_files_on_branch(
        files,
        branch=branch,
        mode=mode,
        commit_message=f"{verb} Artifactory cleaner: {request['cleaner_name']}",
        pr_title=f"{verb} Artifactory cleaner: {request['cleaner_name']}",
        pr_description="\n".join([
            summary,
            "",
            "Requested through DevOps Hub by "
            f"{requester or 'a portal user'} (request {request_id}).",
            "",
            "Merging this removes the folder, so the CronJob stops being applied."
            if mode == "delete" else
            "Merging this runs the pipeline in the folder, which applies the ConfigMap "
            "and the CronJob. Check the spec in configmap.yaml before merging: it is "
            "what decides which artifacts are deleted.",
        ]),
    )
    result.update({
        "cleaner_name": request["cleaner_name"],
        "repository_cleaned": request["repository"],
        "schedule": request["schedule"],
        "summary": summary,
        "next_step": (
            "Review and merge the pull request. The cleaner keeps running until it is merged."
            if mode == "delete"
            else "Review and merge the pull request. Nothing is deleted until it is merged."
        ),
    })
    if result.get("already_absent"):
        # Nothing was in the repository to remove. The outcome asked for is already
        # true, so the record goes rather than waiting for a review that will never
        # happen.
        _after_the_work(result, "portal record",
                        lambda: cleaner_store.delete_record_by_slug(request["slug"]))
        result["next_step"] = (
            "The cleaner's files were already gone from the repository, so nothing "
            "needed removing. The portal's record has been dropped."
        )
        return result

    # ── Past this line the pull request EXISTS ───────────────────────────────
    #
    # Everything below records that fact somewhere: the portal's own table, a
    # ServiceNow item, the audit log. None of it can undo the pull request, so none of
    # it may turn a finished job into a failed one -- which is exactly what happened
    # the first time this ran: the pull request opened, the files were committed, and
    # the request was reported FAILED because a constant in the audit call did not
    # exist. Each step reports its own outcome into result["warnings"] instead.

    _after_the_work(
        result, "portal record",
        (lambda: cleaner_store.record_removal(request["slug"], request_row, result))
        if mode == "delete"
        else (lambda: cleaner_store.record(request, request_row, result, summary=summary, mode=mode)),
    )

    if mode == "delete":
        # The pipeline goes now, at APPROVAL, not when the pull request that removes
        # the files is merged. An approver has decided this cleaner should stop, and
        # until the merge it would otherwise go on deleting artifacts every night on a
        # schedule everybody has agreed to end. If the removal is later abandoned,
        # cleaner_store puts the pipeline back.
        def _drop() -> None:
            outcome = cleaner_store.drop_pipeline_for(request["slug"])
            result["pipeline_removed"] = bool(outcome.get("ok"))
            if not outcome.get("ok"):
                result["pipeline_removal_error"] = outcome.get("detail") or "unknown"

        _after_the_work(result, "pipeline removal", _drop)

        # What this approval does NOT do, carried on the result so the approver reads
        # it at the moment they act rather than discovering it later. Merging the pull
        # request deletes the files that describe the CronJob and the ConfigMap; the
        # objects themselves are on a different cluster, behind a firewall this
        # backend cannot cross, and they keep running until a person deletes them.
        import artifactory_cleaner

        result["cluster_objects"] = artifactory_cleaner.cluster_objects(request["slug"])
        result["cluster_command"] = artifactory_cleaner.cluster_command(request["slug"])

    # The requested item carries the pull request URL, which is the whole reason it is
    # raised HERE and not at submission: at submission there is no pull request to
    # point at, and updating a RITM afterwards is what trips this instance's business
    # rules.
    snow = order_catalog_item(
        "artifactory_cleaner",
        {
            # Scalars only -- a dict or a list has no meaning as a catalog variable.
            **{k: v for k, v in payload.items() if not isinstance(v, (dict, list))},
            # ...EXCEPT the requester's details, which are a nested dict and are
            # exactly what the catalog item asks for as mandatory variables. Filtering
            # every dict dropped all six of them, so the order arrived with no name,
            # no phone and no team, and ServiceNow answered "Mandatory variables are
            # required" -- a 400 with no clue which ones. The quota executor spread
            # them; this one did not, and nothing made the two agree.
            **(payload.get("requester_details") or {}),
            "summary": summary,
            "pull_request_url": result.get("pull_request_url") or "",
        },
        f"Artifactory cleaner: {request['cleaner_name']}",
        requester_id=str(request_row.get("requester_id") or ""),
        requester_email=requester,
    )
    _record_servicenow(result, snow)
    if snow.get("number"):
        _after_the_work(
            result, "ServiceNow number on the record",
            lambda: cleaner_store.set_snow_number(request["slug"], snow["number"]),
        )

    _after_the_work(
        result, "audit entry",
        lambda: audit.log(
            audit.Action.ARTIFACTORY_CLEANER_SUBMITTED,
            user_email=requester,
            metadata={
                "request_id": request_id,
                "artifactory_cleaner": request["cleaner_name"],
                "mode": mode,
                "repository": request["repository"],
                "pull_request": result.get("pull_request_url") or "",
            },
        ),
    )
    return result


def _record_servicenow(result: Dict[str, Any], snow: Dict[str, Any]) -> None:
    """Put the ServiceNow outcome on the result -- unless there was never one to have.

    Three states, and only two of them belong on the screen. A request that raised a
    ticket carries its number; one that tried and failed carries the reason, so an
    approver can raise it again. A request whose form does not order a catalog item at
    all carries NEITHER, because it did not fail: an Artifactory quota is set by this
    backend and a cleaner becomes a pull request, and neither has ever needed a
    ticket. Writing an empty number for those put a ServiceNow line on requests that
    had completed perfectly, which reads as something having gone wrong.
    """
    if snow.get("skipped"):
        return
    result["servicenow_number"] = snow.get("number") or ""
    if snow.get("error"):
        result["servicenow_error"] = snow["error"]


def _after_the_work(result: Dict[str, Any], label: str, step) -> None:
    """Run a step that records work already done, and never let it fail that work.

    The distinction this draws is between DOING the thing and WRITING DOWN that the
    thing was done. A pull request that is open stays open whether or not the audit
    row lands, so a failure here is a warning carried in the result -- visible, named,
    and not a lie in either direction -- rather than an exception that would mark the
    request FAILED and invite somebody to submit it a second time.
    """
    try:
        step()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("%s failed after the work was done: %s", label, detail)
        result.setdefault("warnings", []).append(f"{label}: {detail}")


# ─── DB helpers ─────────────────────────────────────────────────────────

def _load_request(request_id: str) -> Optional[Dict[str, Any]]:
    return query_one(
        """
        SELECT ar.*,
               u.username as requester_username,
               u.email    as requester_email,
               u.full_name as requester_name,
               approver.username as approver_username,
               approver.email    as approver_email
          FROM approval_requests ar
          JOIN users u ON ar.requester_id = u.id
          LEFT JOIN users approver ON ar.approver_id = approver.id
         WHERE ar.id = %s
        """,
        [request_id],
    )


def _transition_status(request_id: str, from_status: str, to_status: str) -> bool:
    rows = execute_returning(
        """
        UPDATE approval_requests
           SET status = %s
         WHERE id = %s AND status = %s
        RETURNING id
        """,
        [to_status, request_id, from_status],
    )
    return bool(rows)


def _finish_completed(request_id: str, result_data: Dict[str, Any]) -> None:
    execute(
        """
        UPDATE approval_requests
           SET status = 'COMPLETED',
               execution_result = %s,
               executed_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND status IN ('IN_PROGRESS', 'APPROVED')
        """,
        [result_data, request_id],
    )
    row = _load_request(request_id)
    email = _requester_email(row or {})
    audit.log(
        audit.Action.TERRAFORM_COMPLETED,
        user_email=email,
        metadata={
            "request_id": str(request_id),
            "request_type": (row or {}).get("request_type"),
        },
    )
    # "Created project: X" activity entry — only for ADO project creation,
    # which is the one self-service flow that produces a real external
    # artefact worth surfacing in the user's activity feed. Other request
    # types already logged a `self_service` entry at submission time.
    request_type = str((row or {}).get("request_type") or "").upper()
    if email and request_type == "ADO_PROJECT_CREATE":
        project_name = ""
        payload = (row or {}).get("request_payload") or {}
        if isinstance(payload, dict):
            project_name = str(payload.get("project_name") or "").strip()
        if not project_name:
            project_name = str((row or {}).get("request_title") or "").strip()
        activity.log_activity(
            user_email=email,
            action_type=activity.ACTION_CREATE_PROJECT,
            item_name=project_name or "Azure DevOps project",
            metadata={
                "request_id": str(request_id),
                "project_url": (result_data or {}).get("project_url"),
            },
        )
    if email and row:
        create_notification(
            user_email=email,
            message=f"Your request '{row.get('request_title')}' completed successfully.",
            notif_type="REQUEST_COMPLETED",
            related_id=str(request_id),
            link=_notification_link(row),
            group_key=f"request:{request_id}",
        )


def _finish_failed(request_id: str, error_message: str) -> None:
    execute(
        """
        UPDATE approval_requests
           SET status = 'FAILED',
               execution_result = %s,
               executed_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND status IN ('IN_PROGRESS', 'APPROVED')
        """,
        [{"error": error_message}, request_id],
    )
    row = _load_request(request_id)
    email = _requester_email(row or {})
    audit.log(
        audit.Action.TERRAFORM_FAILED,
        user_email=email,
        metadata={
            "request_id": str(request_id),
            "request_type": (row or {}).get("request_type"),
            "error": error_message[:500],
        },
    )
    # A failed cleaner request must not leave a record on the Requests page saying a
    # pull request is waiting for review. The row is keyed on the request, so this is
    # the one place that knows both the outcome and which record it belongs to.
    if str((row or {}).get("request_type") or "").upper().startswith("ARTIFACTORY_CLEANER"):
        import cleaner_store

        cleaner_store.mark_failed(str(request_id), error_message)
    if email and row:
        create_notification(
            user_email=email,
            message=f"Your request '{row.get('request_title')}' failed: {error_message}",
            notif_type="REQUEST_FAILED",
            related_id=str(request_id),
            link=_notification_link(row),
            group_key=f"request:{request_id}",
        )


def _requester_email(row: Dict[str, Any]) -> str:
    return str(row.get("requester_email") or "").strip().lower()


def _user_facing_error(exc: Exception) -> str:
    """
    Strip stack traces / internal paths before surfacing to the UI.

    Upstream exceptions (Kubernetes ApiException, HTTPX, Terraform) tend to
    dump enormous JSON bodies + HTTP headers into str(exc), which is both
    user-hostile and a mild information leak. We detect the common shapes
    and rewrite them as single-line, actionable messages; full detail is
    still kept in the backend logs by the caller.
    """
    cls_name = exc.__class__.__name__
    raw = str(exc) or cls_name

    # kubernetes.client.exceptions.ApiException — massive HTTP dump.
    if cls_name == "ApiException":
        status_code = getattr(exc, "status", None)
        if status_code == 403:
            return (
                "Kubernetes denied the request (403 Forbidden). The backend "
                "ServiceAccount is missing permissions to create the "
                "provisioning Job. Ask an admin to verify terraform-rbac.yaml "
                "is applied in the same namespace as the backend pod."
            )
        if status_code == 404:
            return (
                "Kubernetes returned 404 — the target namespace or resource "
                "was not found. Ask an admin to verify the provisioning "
                "namespace exists and K8S_NAMESPACE is set correctly."
            )
        if status_code:
            return f"Kubernetes API error ({status_code}). See backend logs for details."
        return "Kubernetes API error. See backend logs for details."

    # Clip absurdly long messages (terraform plans etc.) and flatten newlines.
    single_line = " ".join(raw.splitlines()).strip()
    return single_line if len(single_line) <= 300 else single_line[:300] + "…"


# ─── Permissions ────────────────────────────────────────────────────────

def _seconds_since(ts: Any) -> Optional[int]:
    """Return integer seconds from ``ts`` to now, or None if ts is falsy/unparseable."""
    if not ts:
        return None
    from datetime import datetime, timezone

    try:
        if isinstance(ts, datetime):
            dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


# Project-name rules used by ADO self-service. Keep in sync with any frontend
# validation so users see the same constraints client-side and server-side.
_ADO_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.]{1,62}$")


def _validate_request_payload(request_type: str, title: str, payload: Dict[str, Any]) -> None:
    """
    Spec: no empty values; project-name length + allowed-character rules for
    Azure DevOps project creation. Raises HTTPException(400) with a single
    human-readable message on first failure.
    """
    if not str(title or "").strip():
        raise HTTPException(status_code=400, detail="Request title is required.")
    if not isinstance(payload, dict) or not any(
        str(v).strip() for v in payload.values() if v is not None
    ):
        raise HTTPException(status_code=400, detail="Request inputs cannot be empty.")

    if request_type == "ARTIFACTORY_QUOTA_INCREASE":
        if not str(payload.get("project_key") or "").strip():
            raise HTTPException(status_code=400, detail="project_key is required.")
        try:
            if int(payload.get("increase_by_bytes") or 0) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="The amount to add must be a size greater than zero.",
            )
        return

    if request_type in CLEANER_REQUEST_TYPES:
        # The same validator the executor uses, so a request that cannot be rendered
        # is refused at submission rather than after somebody has approved it.
        import artifactory_cleaner
        try:
            artifactory_cleaner.validate(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return

    if request_type == "ADO_PROJECT_CREATE":
        name = str(payload.get("project_name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="project_name is required.")
        if not _ADO_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    "project_name must be 2–63 characters, start with a letter or "
                    "digit, and contain only letters, digits, spaces, hyphens, "
                    "underscores, or dots."
                ),
            )
        admin = str(payload.get("admin_username") or "").strip()
        if not admin:
            raise HTTPException(status_code=400, detail="admin_username is required.")

        # Optional, but bounded: this text is written onto the project in Azure DevOps.
        description = str(payload.get("description") or "")
        if len(description) > 4000:
            raise HTTPException(
                status_code=400, detail="Description is too long (4000 characters maximum)."
            )

        # The collection is checked against the configured list HERE as well as in the
        # provisioner. A value that only the executor rejects fails after an admin has
        # already approved it, which wastes the approval and reads as a system fault.
        collection = str(payload.get("collection") or "").strip()
        if collection:
            from .azure_devops import _TARGET_COLLECTIONS

            if _TARGET_COLLECTIONS and collection.lower() not in {
                c.lower() for c in _TARGET_COLLECTIONS
            }:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{collection}' is not a collection available for provisioning "
                        f"({', '.join(_TARGET_COLLECTIONS)})."
                    ),
                )


def _can_create(user: AuthUser, request_type: str) -> bool:
    """
    Any authenticated user can submit a self-service request. The admin
    approval step is the actual gate; creation is intentionally permissive
    so the approval workflow can see (and reject) off-policy requests.
    """
    del request_type
    return bool(user.get("id"))


def _can_approve_any(user: AuthUser) -> bool:
    """Platform Admins, and nobody else.

    This used to be ``hierarchy_level <= 5``, which let four intermediate roles
    approve ANY request type — including ones their own permissions row did not
    list, because that row was never read. Approval is the step that turns a
    request into a real write against Azure DevOps with the admin PAT, so it now
    sits behind the same single boundary as every other privileged action, and is
    re-read from the database rather than trusted from the token.
    """
    return has_effective_admin_access_live(user)


def _log_audit(user_id: str, action: str, resource_type: str, resource_id: str) -> None:
    try:
        execute(
            """
            INSERT INTO audit_logs (user_id, action, resource_type, resource_id, details)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [user_id, action, resource_type, resource_id, {}],
        )
    except Exception:
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
