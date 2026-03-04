from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel

from ..db import query_all, query_one, execute, execute_returning
from ..security import AuthUser, get_current_user


router = APIRouter()


class CreateApprovalRequest(BaseModel):
    request_type: str
    request_title: str
    request_payload: Dict[str, Any]


class ApproveRejectRequest(BaseModel):
    comments: Optional[str] = None


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

    rows = execute_returning(
        """
        INSERT INTO approval_requests (requester_id, request_type, request_title, request_payload, status)
        VALUES (%s, %s, %s, %s, 'PENDING')
        RETURNING *
        """,
        [current_user["id"], body.request_type, body.request_title, body.request_payload],
    )

    _log_audit(current_user["id"], "CREATE_REQUEST", "approval_request", str(rows[0]["id"]))

    return {
        "success": True,
        "data": rows[0],
        "timestamp": _now_iso(),
    }


@router.get("/requests")
def get_requests(
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: AuthUser = Depends(get_current_user),
):
    params: List[Any] = []
    query = """
      SELECT ar.*,
             u.username as requester_username,
             u.email as requester_email,
             approver.username as approver_username
      FROM approval_requests ar
      JOIN users u ON ar.requester_id = u.id
      LEFT JOIN users approver ON ar.approver_id = approver.id
      WHERE 1=1
    """
    if status_filter:
        params.append(status_filter)
        query += f" AND ar.status = %s"

    if not _can_approve_any(current_user):
        params.append(current_user["id"])
        query += " AND ar.requester_id = %s"

    query += " ORDER BY ar.created_at DESC LIMIT 100"

    rows = query_all(query, params)
    return {
        "success": True,
        "data": rows,
        "timestamp": _now_iso(),
    }


@router.get("/requests/{request_id}")
def get_request(
    request_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    row = query_one(
        """
        SELECT ar.*,
               u.username as requester_username,
               u.email as requester_email,
               approver.username as approver_username
        FROM approval_requests ar
        JOIN users u ON ar.requester_id = u.id
        LEFT JOIN users approver ON ar.approver_id = approver.id
        WHERE ar.id = %s
        """,
        [request_id],
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )
    return {
        "success": True,
        "data": row,
        "timestamp": _now_iso(),
    }


@router.post("/requests/{request_id}/approve")
def approve_request(
    request_id: str,
    body: ApproveRejectRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    request_row = query_one("SELECT * FROM approval_requests WHERE id = %s", [request_id])
    if not request_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )
    if request_row["status"] != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request is not pending",
        )

    rule_row = query_one(
        "SELECT * FROM approval_rules WHERE request_type = %s",
        [request_row["request_type"]],
    )
    if not rule_row or not _can_approve(current_user, int(rule_row["min_approver_role_level"])):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to approve this request",
        )

    execute(
        """
        UPDATE approval_requests
        SET status = 'APPROVED', approver_id = %s, approver_comments = %s, approved_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        [current_user["id"], body.comments, request_id],
    )

    _log_audit(current_user["id"], "APPROVE_REQUEST", "approval_request", request_id)

    # Execute the approved action (mocked locally)
    _execute_approved_request(request_row)

    return {
        "success": True,
        "message": "Request approved successfully",
        "timestamp": _now_iso(),
    }


@router.post("/requests/{request_id}/reject")
def reject_request(
    request_id: str,
    body: ApproveRejectRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    request_row = query_one("SELECT * FROM approval_requests WHERE id = %s", [request_id])
    if not request_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )
    if request_row["status"] != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request is not pending",
        )

    rule_row = query_one(
        "SELECT * FROM approval_rules WHERE request_type = %s",
        [request_row["request_type"]],
    )
    if not rule_row or not _can_approve(current_user, int(rule_row["min_approver_role_level"])):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to reject this request",
        )

    execute(
        """
        UPDATE approval_requests
        SET status = 'REJECTED', approver_id = %s, approver_comments = %s, approved_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        [current_user["id"], body.comments or "Rejected", request_id],
    )

    _log_audit(current_user["id"], "REJECT_REQUEST", "approval_request", request_id)

    return {
        "success": True,
        "message": "Request rejected",
        "timestamp": _now_iso(),
    }


def _execute_approved_request(request_row: Dict[str, Any]) -> None:
    """Mock execution of approved requests, similar to Node approval-service."""
    try:
        payload = request_row.get("request_payload")
        request_type = request_row.get("request_type")

        # In this Python refactor, we don't call separate services over HTTP.
        # Instead, we simulate success and store a simple result.

        if request_type == "ADO_PROJECT_CREATE":
            result_data = {
                "project": {
                    "name": payload.get("name"),
                    "description": payload.get("description"),
                    "status": "created",
                }
            }
        elif request_type == "SONAR_PR_SCANNING_ENABLE":
            result_data = {
                "sonar": {
                    "projectKey": payload.get("projectKey"),
                    "pr_scanning_enabled": True,
                }
            }
        elif request_type == "AI_MODEL_ACCESS":
            result_data = {"message": "AI access granted"}
        else:
            result_data = {"message": f"Executed request type {request_type}"}

        execute(
            """
            UPDATE approval_requests
            SET status = 'EXECUTED', execution_result = %s, executed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            [result_data, request_row["id"]],
        )
    except Exception as exc:
        execute(
            """
            UPDATE approval_requests
            SET status = 'FAILED', execution_result = %s
            WHERE id = %s
            """,
            [{"error": str(exc)}, request_row["id"]],
        )


def _can_create(user: AuthUser, request_type: str) -> bool:
    level = int(user.get("hierarchy_level", 99))
    if level == 1:
        return True
    if request_type in {"ADO_PROJECT_CREATE", "SONAR_PR_SCANNING_ENABLE"}:
        return level <= 5
    if request_type == "AI_MODEL_ACCESS":
        return True
    return False


def _can_approve(user: AuthUser, required_level: int) -> bool:
    return int(user.get("hierarchy_level", 99)) <= required_level


def _can_approve_any(user: AuthUser) -> bool:
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
        # Best-effort only; do not fail main flow on audit error
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


