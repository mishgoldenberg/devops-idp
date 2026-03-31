import functools
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, TypeVar

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

from db import observability_schema_unavailable, query_all_obs
from security import AuthUser, can_view_observability, get_current_user

router = APIRouter()
log = logging.getLogger(__name__)

T = TypeVar("T")

_OBS_SCHEMA_NOTICE = (
    "Observability tables are missing and could not be created with the app database user. "
    "Apply deployment/charts/infrastructure/database/06_observability.sql as a database admin, "
    "or grant CREATE on the portal database to the application user. "
    "A non-blocking migrate Job also runs on each deploy when enabled in Helm values."
)


def _observability_safe(fn: Callable[..., T]) -> Callable[..., T]:
    """Map unexpected DB/runtime errors to 503 with an actionable message (logged server-side)."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception:
            log.exception("Observability API failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Observability could not read the database. Check backend logs; "
                    "ensure 06_observability.sql has been applied if tables are missing."
                ),
            )

    return inner


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
    if not can_view_observability(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return current_user


@router.get("/widgets")
@_observability_safe
def list_widget_usage(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    rows = query_all_obs(
        """
        SELECT widget_name AS name, usage_count AS count, widget_key, last_used_at
        FROM widget_usage
        ORDER BY usage_count DESC, widget_name ASC
        """
    )
    return _obs_ok(data=jsonable_encoder(rows))


@router.get("/self-services")
@router.get("/self_services", include_in_schema=False)
@_observability_safe
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
@_observability_safe
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
@_observability_safe
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
