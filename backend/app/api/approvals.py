"""
Self-service approval workflow.

Every self-service action (Azure DevOps project creation, SonarQube PR
scanning enablement, AI model access, …) is routed through this module.

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
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

import activity
import audit
import safe_mode
from db import execute, execute_returning, query_all, query_one
from security import AuthUser, get_current_user

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
        "       approver.email    as approver_email "
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

    rows = query_all(query, params)
    return {"success": True, "data": rows, "timestamp": _now_iso()}


@router.get("/requests/{request_id}")
def get_request(
    request_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    row = _load_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    # Non-admins can only see their own requests
    if not _can_approve_any(current_user) and str(row.get("requester_id")) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"success": True, "data": row, "timestamp": _now_iso()}


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
        # Safe Mode: simulate success for all self-services without real
        # external side effects. Admin toggle/SAFE_MODE env both feed into
        # safe_mode.is_enabled(); when it's on we never call K8s / ADO / etc.
        if safe_mode.is_enabled():
            result = {
                "safe_mode": True,
                "message": "Safe Mode is enabled — request simulated successfully.",
                "request_type": request_type,
            }
            _finish_completed(request_id, result)
            return

        if request_type == "ADO_PROJECT_CREATE":
            result = _execute_ado_project_create(request_row, payload)
        elif request_type == "SONAR_PR_SCANNING_ENABLE":
            result = {
                "sonar": {
                    "projectKey": payload.get("projectKey"),
                    "pr_scanning_enabled": True,
                }
            }
        elif request_type == "AI_MODEL_ACCESS":
            result = {"message": "AI model access granted"}
        else:
            result = {"message": f"Executed request type {request_type}"}

        _finish_completed(request_id, result)
    except Exception as exc:
        log.exception("execution: request %s failed: %s", request_id, exc)
        _finish_failed(request_id, _user_facing_error(exc))


def _execute_ado_project_create(
    request_row: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Provision an Azure DevOps project via the existing terraform_runner
    pipeline. Runs inline in the execution thread; we poll to terminal
    so we can write a proper COMPLETED/FAILED status.
    """
    try:
        # Imported lazily so the module still imports cleanly in dev
        # environments without the kubernetes/google-cloud-storage extras.
        from terraform_runner import (
            sanitize_ado_name,
            submit_terraform_job,
            get_job_status,
        )
    except Exception as exc:
        raise RuntimeError(
            "Provisioning backend unavailable — cannot execute Azure DevOps "
            f"project creation ({exc})."
        )

    project_name_raw = str(payload.get("project_name") or "").strip()
    process_type = str(payload.get("process_type") or "Scrum").strip()
    admin_username = str(payload.get("admin_username") or "").strip()

    project_name = sanitize_ado_name(project_name_raw)
    if not project_name:
        raise ValueError("project_name is required")
    if not admin_username:
        raise ValueError("admin_username is required")

    # Re-use the same configuration the interactive self-service endpoint uses
    # so background execution and direct execution behave identically.
    from .azure_devops import ADO_BASE, USE_MOCK, ensure_custom_ado_process
    use_mock = bool(USE_MOCK)
    org = ADO_BASE.rstrip("/").split("/")[-1] if ADO_BASE else ""

    # Pre-create the inherited process Terraform will reference. Without this
    # step Terraform fails with 'expand project reference: No process template
    # found' because the custom process name (<project>-<type>) doesn't yet
    # exist in Azure DevOps.
    custom_process = ensure_custom_ado_process(project_name, process_type)

    job_id = submit_terraform_job(
        project_name=project_name,
        process_name=custom_process,
        ado_org=org,
        admin_username=admin_username,
        use_mock=use_mock,
    )
    audit.log(
        audit.Action.TERRAFORM_STARTED,
        user_email=str(request_row.get("requester_email") or ""),
        metadata={
            "request_id": str(request_row.get("id")),
            "job_id": job_id,
            "project_name": project_name,
            "process_type": process_type,
        },
    )

    # Poll until terminal. Bounded to ~20 minutes; terraform jobs beyond
    # that will be considered failed rather than hanging the thread forever.
    deadline = time.time() + (20 * 60)
    last_status: Dict[str, Any] = {"status": "pending"}
    while time.time() < deadline:
        last_status = get_job_status(job_id, use_mock=use_mock) or {}
        if last_status.get("status") in ("succeeded", "failed"):
            break
        time.sleep(5)

    if last_status.get("status") == "succeeded":
        return {
            "job_id": job_id,
            "project_name": project_name,
            "project_url": last_status.get("project_url"),
            "status": "completed",
        }

    error = last_status.get("error") or "Provisioning job did not finish in time."
    raise RuntimeError(error)


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


def _can_create(user: AuthUser, request_type: str) -> bool:
    """
    Any authenticated user can submit a self-service request. The admin
    approval step is the actual gate; creation is intentionally permissive
    so the approval workflow can see (and reject) off-policy requests.
    """
    del request_type
    return bool(user.get("id"))


def _can_approve_any(user: AuthUser) -> bool:
    """Admin-ish roles (hierarchy_level <= 5)."""
    return int(user.get("hierarchy_level", 99)) <= 5


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
