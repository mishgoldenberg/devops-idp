from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

import db
from db import observability_schema_unavailable, query_all_obs
from security import AuthUser, get_current_user, has_effective_admin_access_live

router = APIRouter()

_OBS_SCHEMA_NOTICE = (
    "The observability tables are not available to the app yet. "
    "As a database admin, run the SQL file deployment/charts/infrastructure/database/06_observability.sql "
    "on the portal database (it creates tables and GRANTs for user devops). "
    "Then click Refresh. This is not a portal crash — metrics will appear after the schema exists."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _obs_ok(**fields: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"success": True, "timestamp": _now_iso(), **fields}
    if observability_schema_unavailable():
        body["notice"] = _OBS_SCHEMA_NOTICE
    return body


def get_observability_user(
    current_user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """Require observability permission (schema is ensured lazily via query_all_obs)."""
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to observability data.",
        )
    return current_user


# Stable set of widget keys the UI can emit — validated on the POST endpoint.
_ALLOWED_WIDGET_KEYS = {
    "quick_links",
    "ado_my_work_items",
    "snow_my_tickets",
    "ado_my_pull_requests",
    "ado_prs_for_review",
    "ado_pipeline_status",
    "sonar_quality_gate",
    "artifactory_storage",
    "service_health",
}


@router.post("/widget")
def record_widget_event(
    payload: Dict[str, Any] = Body(default={}),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Event-based widget tracking. Called by each widget component when it mounts.
    One row per render event; aggregation happens at read time.
    Errors are swallowed so a tracking failure never breaks the dashboard.
    """
    widget_key = str((payload or {}).get("widget_key") or "").strip()
    session_id = (payload or {}).get("session_id")
    session_id = str(session_id).strip()[:128] if session_id else None

    if not widget_key or widget_key not in _ALLOWED_WIDGET_KEYS:
        raise HTTPException(status_code=400, detail="Invalid widget_key")

    user_id = str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        db.ensure_observability_tables_once()
        db.execute(
            """
            INSERT INTO widget_usage (user_id, widget_key, event_type, session_id, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            """,
            [user_id[:255], widget_key[:255], "widget_view", session_id],
        )
    except Exception:
        # Silent-fail per spec: tracking must never break widget rendering.
        pass
    return {"success": True}


def _widget_usage_rows(query: str) -> list:
    """Aggregate widget_usage into [{widget_key, name, count}] rows."""
    from observability_tracking import WIDGET_LABELS
    rows = query_all_obs(query)
    return [
        {
            "widget_key": r["widget_key"],
            "name": WIDGET_LABELS.get(r["widget_key"], str(r["widget_key"]).replace("_", " ").title()),
            "count": int(r["count"] or 0),
        }
        for r in rows
    ]


@router.get("/widgets/usage")
def list_widget_usage_events(
    current_user: AuthUser = Depends(get_observability_user),
) -> Dict[str, Any]:
    """
    Count users who currently have each widget on their dashboard.
    Reads from user_widgets (current state), not from widget_usage (historical events).
    """
    rows = _widget_usage_rows(
        """
        SELECT widget_key, COUNT(*)::bigint AS count
        FROM user_widgets
        GROUP BY widget_key
        ORDER BY count DESC, widget_key ASC
        """
    )
    return _obs_ok(data=jsonable_encoder(rows))


# Kept for backward compatibility with the existing observability page JS.
@router.get("/widgets")
def list_widget_usage(
    current_user: AuthUser = Depends(get_observability_user),
) -> Dict[str, Any]:
    return list_widget_usage_events(current_user)


@router.get("/self-services")
@router.get("/self_services", include_in_schema=False)
def list_self_service_usage(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    rows = query_all_obs(
        """
        SELECT service_name AS name, execution_count AS count, service_key, last_executed_at
        FROM self_service_usage
        ORDER BY execution_count DESC, service_name ASC
        """
    )
    return _obs_ok(data=jsonable_encoder(rows))


@router.get("/azure-projects")
@router.get("/azure_projects", include_in_schema=False)
def list_azure_projects(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    rows = query_all_obs(
        """
        SELECT project_name, created_by, process_type, completed_at AS creation_date
        FROM azure_projects
        WHERE completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 500
        """
    )
    return _obs_ok(data=jsonable_encoder(rows))


@router.get("/tickets")
def list_portal_tickets(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
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
    return _obs_ok(
        data=jsonable_encoder(
            {
                "total": total,
                "bySeverity": severity_breakdown,
                "recent": recent,
            }
        )
    )
