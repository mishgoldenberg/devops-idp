import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from uuid import uuid4

import db
from db import query_all, query_one, execute_returning, execute
from security import AuthUser, get_current_user
from .pins import load_pin_index, load_seen_index


log = logging.getLogger(__name__)


router = APIRouter()


# Stable set of widget keys that may appear on the home dashboard.
# Kept in-sync with HOME_WIDGET_KEYS in backend/app/ui.py.
_ALLOWED_WIDGET_KEYS = {
    "quick_links",
    "ado_my_work_items",
    "snow_my_tickets",
    "ado_my_pull_requests",
    "ado_prs_for_review",
    "ado_pipeline_status",
    "sonar_projects",
    "artifactory_repos",
    "artifactory_storage",
    "service_health",
}


@router.post("/widgets/sync")
def sync_user_widgets(
    payload: Dict[str, Any] = Body(default={}),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Replace the authenticated user's active-widget list.
    Observability reads from user_widgets so removals decrement counts immediately.
    Silent-fails on DB errors so dashboard rendering is never blocked.
    """
    widgets = (payload or {}).get("widgets") or []
    session_id = (payload or {}).get("session_id")
    session_id = str(session_id).strip()[:128] if session_id else None

    if not isinstance(widgets, list):
        raise HTTPException(status_code=400, detail="'widgets' must be a list")

    keys = [
        str(k).strip()[:255]
        for k in widgets
        if isinstance(k, str) and str(k).strip() in _ALLOWED_WIDGET_KEYS
    ]

    user_id = str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).strip()[:255]
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        db.ensure_observability_tables_once()
        execute("DELETE FROM user_widgets WHERE user_id = %s", [user_id])
        for key in keys:
            execute(
                """
                INSERT INTO user_widgets (user_id, widget_key, session_id, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, widget_key) DO NOTHING
                """,
                [user_id, key, session_id],
            )
    except Exception:
        pass
    return {"success": True, "data": {"widgets": keys}}


class DashboardUpdateRequest(BaseModel):
    name: Optional[str] = None
    layout: Optional[List[Dict[str, Any]]] = None
    widgets: Optional[List[Dict[str, Any]]] = None


class DashboardCreateRequest(BaseModel):
    name: Optional[str] = None
    layout: Optional[List[Dict[str, Any]]] = None
    widgets: Optional[List[Dict[str, Any]]] = None


@router.get("/")
def get_dashboards(current_user: AuthUser = Depends(get_current_user)):
    rows = query_all(
        """
        SELECT id, user_id, name, is_default, layout, widgets, created_at, updated_at
        FROM dashboards
        WHERE user_id = %s
        ORDER BY is_default DESC, name ASC
        """,
        [current_user["id"]],
    )
    return {
        "success": True,
        "data": rows,
        "timestamp": _now_iso(),
    }


@router.get("/default")
def get_default_dashboard(current_user: AuthUser = Depends(get_current_user)):
    dashboard = query_one(
        """
        SELECT id, user_id, name, is_default, layout, widgets, created_at, updated_at
        FROM dashboards
        WHERE user_id = %s AND is_default = true
        LIMIT 1
        """,
        [current_user["id"]],
    )
    if not dashboard:
        dashboard = _create_default_dashboard(current_user["id"], int(current_user["hierarchy_level"]))
    return {
        "success": True,
        "data": dashboard,
        "timestamp": _now_iso(),
    }


@router.put("/{dashboard_id}")
def update_dashboard(
    dashboard_id: str = Path(..., alias="id"),
    body: DashboardUpdateRequest = None,
    current_user: AuthUser = Depends(get_current_user),
):
    if body is None:
        body = DashboardUpdateRequest()

    rows = execute_returning(
        """
        UPDATE dashboards
        SET name = COALESCE(%s, name),
            layout = COALESCE(%s, layout),
            widgets = COALESCE(%s, widgets),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND user_id = %s
        RETURNING *
        """,
        [
            body.name,
            body.layout,
            body.widgets,
            dashboard_id,
            current_user["id"],
        ],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboard not found",
        )

    return {
        "success": True,
        "data": rows[0],
        "timestamp": _now_iso(),
    }


@router.post("/")
def create_dashboard(
    body: DashboardCreateRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    rows = execute_returning(
        """
        INSERT INTO dashboards (user_id, name, layout, widgets)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        [
            current_user["id"],
            body.name or "New Dashboard",
            body.layout or [],
            body.widgets or [],
        ],
    )
    return {
        "success": True,
        "data": rows[0],
        "timestamp": _now_iso(),
    }


@router.delete("/{dashboard_id}")
def delete_dashboard(
    dashboard_id: str = Path(..., alias="id"),
    current_user: AuthUser = Depends(get_current_user),
):
    rows = execute_returning(
        """
        DELETE FROM dashboards
        WHERE id = %s AND user_id = %s AND is_default = false
        RETURNING id
        """,
        [dashboard_id, current_user["id"]],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboard not found or cannot delete default dashboard",
        )

    return {
        "success": True,
        "message": "Dashboard deleted successfully",
        "timestamp": _now_iso(),
    }


@router.get("/widget-types")
def get_widget_types(current_user: AuthUser = Depends(get_current_user)):
    widgets = query_all(
        """
        SELECT id, widget_key, name, description, category, min_role_level, default_config, icon
        FROM widget_types
        WHERE min_role_level >= %s
        ORDER BY category, name
        """,
        [int(current_user["hierarchy_level"])],
    )
    return {
        "success": True,
        "data": widgets,
        "timestamp": _now_iso(),
    }


def _create_default_dashboard(user_id: str, role_level: int) -> Dict[str, Any]:
    default_widgets = _get_default_widgets_for_role(role_level)
    default_layout = _generate_default_layout(len(default_widgets))
    rows = execute_returning(
        """
        INSERT INTO dashboards (user_id, name, is_default, layout, widgets)
        VALUES (%s, %s, true, %s, %s)
        RETURNING *
        """,
        [user_id, "My Dashboard", default_layout, default_widgets],
    )
    return rows[0]


def _get_default_widgets_for_role(role_level: int) -> List[Dict[str, Any]]:
    common_widgets = [
        {"id": str(uuid4()), "widget_key": "ado_my_work_items", "config": {}},
        {"id": str(uuid4()), "widget_key": "ado_my_pull_requests", "config": {}},
        {"id": str(uuid4()), "widget_key": "sonar_projects", "config": {}},
        {"id": str(uuid4()), "widget_key": "artifactory_repos", "config": {}},
        {"id": str(uuid4()), "widget_key": "snow_my_tickets", "config": {}},
    ]

    if role_level <= 3:
        return [
            *common_widgets,
            {"id": str(uuid4()), "widget_key": "executive_branch_dashboard", "config": {}},
            {"id": str(uuid4()), "widget_key": "executive_deployment_frequency", "config": {}},
        ]
    if role_level <= 6:
        return [
            *common_widgets,
            {"id": str(uuid4()), "widget_key": "ado_sprint_progress", "config": {}},
            {"id": str(uuid4()), "widget_key": "snow_team_tickets", "config": {}},
        ]
    return common_widgets


def _generate_default_layout(widget_count: int) -> List[Dict[str, Any]]:
    layout: List[Dict[str, Any]] = []
    x = 0
    y = 0
    for i in range(widget_count):
        layout.append(
            {
                "i": str(i),
                "x": x * 6,
                "y": y * 4,
                "w": 6,
                "h": 4,
                "minW": 3,
                "minH": 3,
            }
        )
        x += 1
        if x >= 2:
            x = 0
            y += 1
    return layout


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Interactive dashboard feeds
#
# Two widgets (Azure DevOps work items, ServiceNow incidents) share the same
# normalized contract so the frontend can render them with a single component:
#
#   {
#     "data": [
#       { id, title, status, updated_at, url, source, pinned, has_update, meta }
#     ],
#     "error": "Service unavailable" | "",
#     "pins_available": true,
#     "timestamp": "..."
#   }
#
# Failures are swallowed into `error` so the widget UX stays stable (HTTP 200,
# empty list, inline warning) instead of throwing 502s at the browser.
# ─────────────────────────────────────────────────────────────────────────────


def _user_id(current_user: AuthUser) -> str:
    return str(
        current_user.get("email")
        or current_user.get("username")
        or current_user.get("id")
        or ""
    ).strip()[:255]


def _parse_ts(raw: Any) -> float:
    """Best-effort parse of an ISO timestamp to epoch seconds (0.0 on failure)."""
    if not raw:
        return 0.0
    if isinstance(raw, datetime):
        return raw.timestamp()
    try:
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        # ServiceNow returns "YYYY-MM-DD HH:MM:SS" in display-value mode.
        try:
            return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0.0


def _apply_user_flags(
    items: List[Dict[str, Any]],
    *,
    user_id: str,
    source: str,
) -> List[Dict[str, Any]]:
    """Annotate items with pinned/has_update and sort pinned → recent first."""
    pins = load_pin_index(user_id, source) if user_id else set()
    seen = load_seen_index(user_id, source) if user_id else {}
    for item in items:
        iid = str(item.get("id", ""))
        item["pinned"] = iid in pins
        updated_ts = _parse_ts(item.get("updated_at"))
        seen_ts = _parse_ts(seen.get(iid)) if iid in seen else 0.0
        # If the user never opened it, treat as new (has_update=true); else
        # compare timestamps. Small slack avoids false positives on identical stamps.
        if seen_ts == 0.0:
            item["has_update"] = True
        else:
            item["has_update"] = updated_ts > seen_ts + 1.0
    items.sort(
        key=lambda it: (
            0 if it.get("pinned") else 1,
            -_parse_ts(it.get("updated_at")),
        )
    )
    return items


@router.get("/ado-items")
def get_ado_items(
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Latest Azure DevOps work items assigned to the current user, normalized."""
    from api.azure_devops import get_work_items as _ado_work_items

    uid = _user_id(current_user)
    error = ""
    items: List[Dict[str, Any]] = []
    try:
        result = _ado_work_items(project=project, current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        for wi in rows:
            assigned = wi.get("assigned_to")
            if isinstance(assigned, dict):
                assigned_name = assigned.get("displayName") or assigned.get("uniqueName") or ""
            else:
                assigned_name = str(assigned or "")
            items.append(
                {
                    "id": str(wi.get("id", "")),
                    "title": wi.get("title") or f"Work item #{wi.get('id', '')}",
                    "status": wi.get("state") or "",
                    "updated_at": wi.get("changed_date") or wi.get("created_date") or "",
                    "url": wi.get("url") or "",
                    "source": "azure_devops",
                    "meta": {
                        "id_display": f"#{wi.get('id', '')}" if wi.get("id") else "",
                        "type": wi.get("type") or "",
                        "project": wi.get("project") or "",
                        "state_category": wi.get("state_category") or "",
                        "assigned_to": assigned_name,
                    },
                }
            )
    except HTTPException as exc:
        log.info("get_ado_items: ADO unavailable: %s", exc.detail)
        detail = exc.detail if isinstance(exc.detail, str) else "Azure DevOps unavailable"
        error = detail or "Azure DevOps unavailable"
    except Exception as exc:
        log.warning("get_ado_items: unexpected error: %s", exc)
        error = "Service unavailable"

    items = _apply_user_flags(items[:20], user_id=uid, source="azure_devops")
    return {
        "success": True,
        "data": items,
        "error": error,
        "source": "azure_devops",
        "timestamp": _now_iso(),
    }


@router.get("/snow-items")
def get_snow_items(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Latest ServiceNow incidents for the current user, normalized."""
    from api.servicenow import get_tickets as _snow_get_tickets

    uid = _user_id(current_user)
    error = ""
    items: List[Dict[str, Any]] = []
    try:
        result = _snow_get_tickets(current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        for t in rows:
            sys_id = t.get("sys_id") or ""
            items.append(
                {
                    "id": sys_id,
                    "title": t.get("short_description") or t.get("number") or "Incident",
                    "status": t.get("state") or "",
                    "updated_at": t.get("updated_at") or t.get("opened_at") or "",
                    # Clicking navigates inside the portal; the support page
                    # picks up ?ticket=<sys_id> and auto-opens the conversation.
                    "url": f"/ui/support?ticket={sys_id}" if sys_id else "/ui/support",
                    "source": "servicenow",
                    "meta": {
                        "id_display": t.get("number") or "",
                        "priority": t.get("priority") or "",
                        "assigned_to": t.get("assigned_to") or "",
                    },
                }
            )
    except HTTPException as exc:
        # Upstream raised a structured error — don't leak JSON decode traces.
        log.info("get_snow_items: SN unavailable: %s", exc.detail)
        error = "Service unavailable"
    except Exception as exc:
        log.warning("get_snow_items: unexpected error: %s", exc)
        error = "Service unavailable"

    items = _apply_user_flags(items[:20], user_id=uid, source="servicenow")
    return {
        "success": True,
        "data": items,
        "error": error,
        "source": "servicenow",
        "timestamp": _now_iso(),
    }

