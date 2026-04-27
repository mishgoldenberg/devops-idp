import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

import db
from db import observability_schema_unavailable, query_all, query_all_obs
from security import AuthUser, get_current_user, has_effective_admin_access_live

_log = logging.getLogger(__name__)

# Human-readable labels for the "Self-service usage" widget. When a new
# approval-driven request_type is added to approvals.py, add a label here so
# it surfaces with a clean name on the observability dashboard. Unknown
# request_types fall back to a Title-cased version of the key.
_SELF_SERVICE_LABELS: Dict[str, str] = {
    "ADO_PROJECT_CREATE": "Azure DevOps — Create project",
    "SONAR_PR_SCANNING_ENABLE": "SonarQube — Enable PR scanning",
    "AI_MODEL_ACCESS": "AI model access",
}


def _self_service_label(request_type: str) -> str:
    rt = str(request_type or "").strip()
    if not rt:
        return "Unknown"
    if rt in _SELF_SERVICE_LABELS:
        return _SELF_SERVICE_LABELS[rt]
    return rt.replace("_", " ").title()


def _safe_query_all(sql: str, params: Optional[list] = None) -> list:
    """
    Run a SELECT against a core (non-observability) table but degrade gracefully
    like observability reads do. Observability is an admin read-only view — a
    missing table or permission glitch should surface zero rows, not a 500.
    """
    try:
        return query_all(sql, params or [])
    except Exception as exc:
        _log.warning("observability: core-table read failed: %s", exc)
        return []

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
    "sonar_projects",
    "artifactory_repos",
    "artifactory_storage",
    "recent_activity",
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


# ── Per-user "smart dashboard" helpers ────────────────────────────────────────
# These endpoints power the Customize Dashboard drawer's "Recently used" and
# "Suggested for you" sections. They intentionally:
#   * degrade quietly (missing schema → empty lists, never a 500)
#   * require only a regular authenticated user (NOT admin)
#   * return the same shape as list_widget_usage_events for easy UI reuse

@router.get("/widgets/recent")
def list_recent_widgets_for_me(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Widgets the current user rendered most recently (last 30 days)."""
    from observability_tracking import WIDGET_LABELS

    user_id = str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).strip()
    if not user_id:
        return _obs_ok(data=[])

    try:
        db.ensure_observability_tables_once()
        rows = query_all_obs(
            """
            SELECT widget_key, MAX(created_at) AS last_seen, COUNT(*)::bigint AS views
              FROM widget_usage
             WHERE user_id = %s
               AND created_at >= NOW() - INTERVAL '30 days'
               AND widget_key = ANY(%s)
             GROUP BY widget_key
             ORDER BY last_seen DESC NULLS LAST
             LIMIT 6
            """,
            [user_id[:255], list(_ALLOWED_WIDGET_KEYS)],
        )
    except Exception as exc:
        _log.warning("observability: recent widgets read failed: %s", exc)
        rows = []

    data = [
        {
            "widget_key": r["widget_key"],
            "name": WIDGET_LABELS.get(
                r["widget_key"], str(r["widget_key"]).replace("_", " ").title()
            ),
            "views": int(r.get("views") or 0),
            "last_seen": r.get("last_seen"),
        }
        for r in rows
    ]
    return _obs_ok(data=jsonable_encoder(data))


@router.get("/widgets/suggested")
def list_suggested_widgets_for_me(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Suggest widgets the current user does NOT already have enabled but which
    are popular across the portal. Falls back to a static default ordering if
    the observability schema isn't available.
    """
    from observability_tracking import WIDGET_LABELS

    user_id = str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).strip()
    if not user_id:
        return _obs_ok(data=[])

    # Widgets this user already has enabled (core-table read, best-effort).
    enabled: set = set()
    try:
        for r in _safe_query_all(
            "SELECT widget_key FROM user_widgets WHERE user_id = %s",
            [user_id[:255]],
        ):
            wk = r.get("widget_key")
            if wk:
                enabled.add(str(wk))
    except Exception:
        enabled = set()

    # Popularity ranking from widget_usage (observability) — best-effort.
    popular_rows = []
    try:
        db.ensure_observability_tables_once()
        popular_rows = query_all_obs(
            """
            SELECT widget_key, COUNT(*)::bigint AS views
              FROM widget_usage
             WHERE created_at >= NOW() - INTERVAL '30 days'
               AND widget_key = ANY(%s)
             GROUP BY widget_key
             ORDER BY views DESC
            """,
            [list(_ALLOWED_WIDGET_KEYS)],
        )
    except Exception as exc:
        _log.info("observability: suggested widgets fallback (%s)", exc)

    ranked = [r["widget_key"] for r in popular_rows if r.get("widget_key")]
    # Static tail so new portals with no usage still return sensible suggestions.
    _DEFAULT_ORDER = [
        "ado_my_work_items",
        "snow_my_tickets",
        "ado_my_pull_requests",
        "ado_pipeline_status",
        "quick_links",
        "sonar_projects",
        "artifactory_repos",
        "artifactory_storage",
        "recent_activity",
        "ado_prs_for_review",
    ]
    for key in _DEFAULT_ORDER:
        if key not in ranked:
            ranked.append(key)

    suggestions = []
    for key in ranked:
        if key in enabled or key not in _ALLOWED_WIDGET_KEYS:
            continue
        suggestions.append(
            {
                "widget_key": key,
                "name": WIDGET_LABELS.get(key, key.replace("_", " ").title()),
            }
        )
        if len(suggestions) >= 4:
            break

    return _obs_ok(data=jsonable_encoder(suggestions))


@router.get("/self-services")
@router.get("/self_services", include_in_schema=False)
def list_self_service_usage(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    """
    Break down every approval-driven self-service by lifecycle status so the
    admin dashboard answers "what was opened / approved / rejected / completed
    / failed per automation".

    Source of truth is ``approval_requests`` (created by ``POST /api/approvals
    /requests``). The legacy ``self_service_usage`` counter table is no longer
    written to by the approval flow, so reading it would always show zeros.

    "Approved" here counts any request that made it past admin approval —
    i.e. APPROVED + IN_PROGRESS + COMPLETED + EXECUTED + FAILED — which
    matches the user's mental model ("approved = not rejected and not still
    pending").
    """
    rows = _safe_query_all(
        """
        SELECT request_type,
               COUNT(*)::bigint                                                AS total,
               COUNT(*) FILTER (WHERE status = 'PENDING')::bigint              AS pending,
               COUNT(*) FILTER (WHERE status IN (
                   'APPROVED','IN_PROGRESS','COMPLETED','EXECUTED','FAILED'
               ))::bigint                                                      AS approved,
               COUNT(*) FILTER (WHERE status IN ('COMPLETED','EXECUTED'))::bigint  AS completed,
               COUNT(*) FILTER (WHERE status = 'IN_PROGRESS')::bigint          AS in_progress,
               COUNT(*) FILTER (WHERE status = 'FAILED')::bigint               AS failed,
               COUNT(*) FILTER (WHERE status = 'REJECTED')::bigint             AS rejected,
               MAX(executed_at)                                                AS last_executed_at,
               MAX(created_at)                                                 AS last_created_at
          FROM approval_requests
         GROUP BY request_type
         ORDER BY total DESC, request_type ASC
        """
    )

    out = []
    for r in rows:
        rt = r.get("request_type") or ""
        out.append(
            {
                "service_key": rt,
                "name": _self_service_label(rt),
                # `count` preserves the pre-refactor response shape so any older
                # dashboard or external consumer keeps rendering.
                "count": int(r.get("total") or 0),
                "total": int(r.get("total") or 0),
                "pending": int(r.get("pending") or 0),
                "approved": int(r.get("approved") or 0),
                "in_progress": int(r.get("in_progress") or 0),
                "completed": int(r.get("completed") or 0),
                "failed": int(r.get("failed") or 0),
                "rejected": int(r.get("rejected") or 0),
                "last_executed_at": r.get("last_executed_at") or r.get("last_created_at"),
            }
        )
    return _obs_ok(data=jsonable_encoder(out))


@router.get("/azure-projects")
@router.get("/azure_projects", include_in_schema=False)
def list_azure_projects(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    """
    Completed Azure DevOps projects provisioned via the approval workflow.

    Derived from ``approval_requests`` so the list stays in lock-step with
    the "My Requests" / "Approvals" pages — a project shows up here as soon
    as its request reaches COMPLETED/EXECUTED status.
    """
    rows = _safe_query_all(
        """
        SELECT ar.request_payload->>'project_name'    AS project_name,
               ar.request_payload->>'process_type'    AS process_type,
               COALESCE(u.email, u.username)          AS created_by,
               ar.executed_at                         AS creation_date
          FROM approval_requests ar
          LEFT JOIN users u ON ar.requester_id = u.id
         WHERE ar.request_type = 'ADO_PROJECT_CREATE'
           AND ar.status IN ('COMPLETED', 'EXECUTED')
         ORDER BY ar.executed_at DESC NULLS LAST
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


@router.get("/summary")
def get_observability_summary(
    current_user: AuthUser = Depends(get_observability_user),
) -> Dict[str, Any]:
    """
    Consolidated observability numbers for the Admin dashboard header.

    Intentionally best-effort: every sub-query is wrapped so that a missing
    observability schema or a cold DB never turns the admin page into a 500.
    """
    from observability_tracking import WIDGET_LABELS

    def _scalar(sql: str, default: int = 0) -> int:
        try:
            rows = query_all_obs(sql)
        except Exception:
            try:
                rows = query_all(sql)
            except Exception:
                return default
        if not rows:
            return default
        first = rows[0] or {}
        for v in first.values():
            if v is None:
                return default
            try:
                return int(v)
            except Exception:
                return default
        return default

    top_widgets_rows = _widget_usage_rows(
        """
        SELECT widget_key, COUNT(*)::bigint AS count
        FROM user_widgets
        GROUP BY widget_key
        ORDER BY count DESC, widget_key ASC
        LIMIT 5
        """
    )

    top_services = _safe_query_all(
        """
        SELECT request_type, COUNT(*)::bigint AS count
        FROM approval_requests
        GROUP BY request_type
        ORDER BY count DESC, request_type ASC
        LIMIT 5
        """
    )
    top_services_out = [
        {
            "service_key": r.get("request_type") or "",
            "name": _self_service_label(r.get("request_type") or ""),
            "count": int(r.get("count") or 0),
        }
        for r in top_services
    ]

    ss_status = _safe_query_all(
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('COMPLETED','EXECUTED'))::bigint   AS succeeded,
            COUNT(*) FILTER (WHERE status = 'FAILED')::bigint                    AS failed,
            COUNT(*) FILTER (WHERE status = 'PENDING')::bigint                   AS pending,
            COUNT(*) FILTER (WHERE status = 'REJECTED')::bigint                  AS rejected
          FROM approval_requests
        """
    )
    ss_status_row = (ss_status[0] if ss_status else {}) or {}

    # Average approval time: time from request creation to approval/execution.
    # We prefer ``executed_at`` because every approved request has it; fall
    # back to ``updated_at`` for older rows.
    approval_time_row = _safe_query_all(
        """
        SELECT AVG(
                 EXTRACT(EPOCH FROM (COALESCE(executed_at, updated_at) - created_at))
               )::float AS avg_seconds
          FROM approval_requests
         WHERE status IN ('APPROVED','IN_PROGRESS','COMPLETED','EXECUTED')
           AND COALESCE(executed_at, updated_at) IS NOT NULL
        """
    )
    avg_seconds = 0.0
    try:
        avg_seconds = float((approval_time_row[0] or {}).get("avg_seconds") or 0.0)
    except Exception:
        avg_seconds = 0.0

    ado_total = _scalar(
        """
        SELECT COUNT(*)::bigint
          FROM approval_requests
         WHERE request_type = 'ADO_PROJECT_CREATE'
           AND status IN ('COMPLETED', 'EXECUTED')
        """
    )

    snow_total = _scalar("SELECT COUNT(*)::bigint FROM servicenow_tickets")

    return _obs_ok(
        data={
            "top_widgets": top_widgets_rows,
            "top_self_services": top_services_out,
            "self_services": {
                "succeeded": int(ss_status_row.get("succeeded") or 0),
                "failed": int(ss_status_row.get("failed") or 0),
                "pending": int(ss_status_row.get("pending") or 0),
                "rejected": int(ss_status_row.get("rejected") or 0),
                "avg_approval_seconds": round(avg_seconds or 0.0, 1),
            },
            "ado_projects_created": int(ado_total or 0),
            "servicenow_tickets_total": int(snow_total or 0),
        }
    )
