from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

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


@router.get("/widgets")
def list_widget_usage(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    """
    Count distinct users who have each widget enabled on their home dashboard.
    Reads from home_widget_prefs which is updated every time a user saves
    their Customize Dashboard selection (plain VARCHAR rows, no JSONB).
    """
    from observability_tracking import WIDGET_LABELS
    rows = query_all_obs(
        """
        SELECT
            widget_key,
            COUNT(DISTINCT user_id)::bigint AS count
        FROM home_widget_prefs
        GROUP BY widget_key
        ORDER BY count DESC, widget_key ASC
        """
    )
    labelled = [
        {
            "widget_key": r["widget_key"],
            "name": WIDGET_LABELS.get(r["widget_key"], str(r["widget_key"]).replace("_", " ").title()),
            "count": int(r["count"]),
        }
        for r in rows
    ]
    return _obs_ok(data=jsonable_encoder(labelled))


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
