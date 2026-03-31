from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

from db import query_all_obs
from security import AuthUser, can_view_observability, get_current_user

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_observability(user: AuthUser) -> None:
    if not can_view_observability(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )


@router.get("/widgets")
def list_widget_usage(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    _require_observability(current_user)
    rows = query_all_obs(
        """
        SELECT widget_name AS name, usage_count AS count, widget_key, last_used_at
        FROM widget_usage
        ORDER BY usage_count DESC, widget_name ASC
        """
    )
    return {
        "success": True,
        "data": jsonable_encoder(rows),
        "timestamp": _now_iso(),
    }


@router.get("/self-services")
def list_self_service_usage(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    _require_observability(current_user)
    rows = query_all_obs(
        """
        SELECT service_name AS name, execution_count AS count, service_key, last_executed_at
        FROM self_service_usage
        ORDER BY execution_count DESC, service_name ASC
        """
    )
    return {
        "success": True,
        "data": jsonable_encoder(rows),
        "timestamp": _now_iso(),
    }


@router.get("/azure-projects")
def list_azure_projects(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    _require_observability(current_user)
    rows = query_all_obs(
        """
        SELECT project_name, created_by, process_type, completed_at AS creation_date
        FROM azure_projects
        WHERE completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 500
        """
    )
    return {
        "success": True,
        "data": jsonable_encoder(rows),
        "timestamp": _now_iso(),
    }


@router.get("/tickets")
def list_portal_tickets(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    _require_observability(current_user)
    total_row = query_all_obs(
        "SELECT COUNT(*)::bigint AS n FROM servicenow_tickets"
    )
    total = int((total_row[0] or {}).get("n") or 0) if total_row else 0

    by_sev = query_all_obs(
        """
        SELECT severity, COUNT(*)::bigint AS count
        FROM servicenow_tickets
        GROUP BY severity
        ORDER BY severity ASC
        """
    )
    severity_breakdown: Dict[str, int] = {"High": 0, "Medium": 0, "Low": 0}
    for row in by_sev:
        sev = str(row.get("severity") or "Medium")
        cnt = int(row.get("count") or 0)
        if sev in severity_breakdown:
            severity_breakdown[sev] = cnt
        else:
            severity_breakdown["Medium"] += cnt

    recent = query_all_obs(
        """
        SELECT ticket_id, short_description, severity, created_by, created_at
        FROM servicenow_tickets
        ORDER BY created_at DESC
        LIMIT 100
        """
    )
    return {
        "success": True,
        "data": jsonable_encoder(
            {
                "total": total,
                "bySeverity": severity_breakdown,
                "recent": recent,
            }
        ),
        "timestamp": _now_iso(),
    }
