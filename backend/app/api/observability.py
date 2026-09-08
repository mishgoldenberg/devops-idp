import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    # Cleaners are three request types on one thing, and the catalogue names only
    # the one that creates them. Naming the other two here keeps a change and a
    # removal from reading as raw enum values.
    "ARTIFACTORY_CLEANER_UPDATE": "Artifactory cleaner — change",
    "ARTIFACTORY_CLEANER_DELETE": "Artifactory cleaner — removal",
}


def _catalogue_requests() -> List[Dict[str, str]]:
    """Every request the portal offers, named as the form names it.

    READ FROM THE CATALOGUE, NOT COUNTED FROM THE TABLE. This page used to list
    whatever `approval_requests` happened to be grouped by, which meant a request
    type nobody had submitted yet was simply absent -- indistinguishable from one
    that does not exist. Pipeline Characterization was invisible for a further
    reason: it is ordered from ServiceNow rather than approved here, so it is not
    in `approval_requests` at all and never would have appeared however many
    people submitted one.

    So the rows come from catalog_forms, which is where a request is defined, and
    the counts are joined onto them. A form added tomorrow shows up here on the
    same deploy, at zero.
    """
    out: List[Dict[str, str]] = [
        {"key": "ADO_PROJECT_CREATE", "name": _SELF_SERVICE_LABELS["ADO_PROJECT_CREATE"],
         "source": "approval"},
    ]
    try:
        import catalog_forms
        for spec in catalog_forms.all_forms():
            # The support ticket is a ticket, not a request: it is raised straight
            # away, it has no approval, and it is counted on the ServiceNow panel.
            if spec.get("key") == "support_ticket":
                continue
            if spec.get("request_type"):
                out.append({"key": str(spec["request_type"]), "name": str(spec.get("title") or ""),
                            "source": "approval"})
            elif spec.get("snow_item"):
                out.append({"key": str(spec["key"]), "name": str(spec.get("title") or ""),
                            "source": "servicenow"})
    except Exception as exc:  # pragma: no cover - the page degrades, never 500s
        _log.warning("observability: could not read the form catalogue: %s", exc)
    for key, name in _SELF_SERVICE_LABELS.items():
        if not any(row["key"] == key for row in out):
            out.append({"key": key, "name": name, "source": "approval"})
    return out


def _self_service_label(request_type: str) -> str:
    rt = str(request_type or "").strip()
    if not rt:
        return "Unknown"
    for row in _catalogue_requests():
        if row["key"] == rt:
            return row["name"]
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


# The observability TRACKING tables — the dashboard's own copies of activity. Widget
# usage is deliberately NOT here: "clear" wipes the integration activity, not the record
# of which widgets people use. These are also the ONLY things cleared — operational data
# (the real approval_requests, the actual tickets in ServiceNow) is never touched; these
# tables are just the portal's observability mirror.
_CLEARABLE_OBS_TABLES = ("servicenow_tickets", "azure_projects", "self_service_usage")


@router.delete("/data")
def clear_observability_data(
    current_user: AuthUser = Depends(get_observability_user),
) -> Dict[str, Any]:
    """Admin: wipe the observability tracking tables, keeping widget-usage stats."""
    cleared: Dict[str, bool] = {}
    for table in _CLEARABLE_OBS_TABLES:
        try:
            db.execute(f"DELETE FROM {table}")  # fixed identifiers, not user input
            cleared[table] = True
        except Exception as exc:
            _log.warning("observability clear: %s failed: %s", table, exc)
            cleared[table] = False

    try:
        import audit

        audit.log(
            "observability.cleared",
            level=audit.Level.WARNING,
            user_email=str(current_user.get("email") or current_user.get("username") or ""),
            metadata={"what": "Cleared observability data", "tables": list(cleared.keys())},
        )
    except Exception as exc:
        _log.debug("audit log for observability clear failed: %s", exc)

    return _obs_ok(cleared=cleared)


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

    # Requests ordered from ServiceNow never touch approval_requests -- there is
    # nothing to approve, the catalogue item is ordered and the answer comes back
    # as a number. They are counted from their own table, and a submission whose
    # order failed (snow_error) is counted as failed rather than quietly dropped.
    snow_rows = _safe_query_all(
        """
        SELECT kind,
               COUNT(*)::bigint                                                   AS total,
               COUNT(*) FILTER (WHERE snow_number IS NOT NULL AND snow_number <> '')::bigint
                                                                                  AS completed,
               COUNT(*) FILTER (WHERE snow_number IS NULL OR snow_number = '')::bigint
                                                                                  AS failed,
               MAX(created_at)                                                    AS last_created_at
          FROM catalog_submissions
         GROUP BY kind
        """
    )

    by_key: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        rt = r.get("request_type") or ""
        by_key[rt] = {
            "total": int(r.get("total") or 0),
            "pending": int(r.get("pending") or 0),
            "approved": int(r.get("approved") or 0),
            "in_progress": int(r.get("in_progress") or 0),
            "completed": int(r.get("completed") or 0),
            "failed": int(r.get("failed") or 0),
            "rejected": int(r.get("rejected") or 0),
            "last_executed_at": r.get("last_executed_at") or r.get("last_created_at"),
        }
    for r in snow_rows:
        by_key[str(r.get("kind") or "")] = {
            "total": int(r.get("total") or 0),
            "pending": 0,
            # An ordered item is approved by being ordered: there is no gate.
            "approved": int(r.get("completed") or 0),
            "in_progress": 0,
            "completed": int(r.get("completed") or 0),
            "failed": int(r.get("failed") or 0),
            "rejected": 0,
            "last_executed_at": r.get("last_created_at"),
        }

    blank = {"total": 0, "pending": 0, "approved": 0, "in_progress": 0,
             "completed": 0, "failed": 0, "rejected": 0, "last_executed_at": None}

    out = []
    for entry in _catalogue_requests():
        counts = by_key.pop(entry["key"], None) or dict(blank)
        out.append({
            "service_key": entry["key"],
            "name": entry["name"],
            "source": entry["source"],
            # `count` preserves the pre-refactor response shape so any older
            # dashboard or external consumer keeps rendering.
            "count": counts["total"],
            **counts,
        })
    # Anything in the tables the catalogue does not name -- a request type that was
    # retired, or rows from an older release. Kept rather than hidden: they are
    # real requests somebody made, and dropping them makes the totals disagree.
    for key, counts in by_key.items():
        out.append({"service_key": key, "name": _self_service_label(key),
                    "source": "retired", "count": counts["total"], **counts})

    out.sort(key=lambda row: (-row["total"], row["name"]))
    return _obs_ok(data=jsonable_encoder(out))


@router.get("/requests")
def list_requests(current_user: AuthUser = Depends(get_observability_user)) -> Dict[str, Any]:
    """Every request anybody has made, one row each.

    The counts table above answers "how much of each"; this answers "which ones",
    the way the Azure DevOps projects table does for the one request that had a
    list of its own. Both kinds of request are in it -- the ones approved here and
    the ones ordered from ServiceNow -- because a requester does not think of
    those as two systems, and an admin asking "what has been asked for this week"
    should not have to look in two places to find out.
    """
    approvals = _safe_query_all(
        """
        SELECT ar.id                                  AS row_id,
               ar.request_type                        AS kind,
               ar.request_title                       AS title,
               ar.status                              AS status,
               COALESCE(u.email, u.username)          AS requester,
               ar.created_at                          AS created_at,
               ar.executed_at                         AS finished_at
          FROM approval_requests ar
          LEFT JOIN users u ON ar.requester_id = u.id
         ORDER BY ar.created_at DESC
         LIMIT 500
        """
    )
    ordered = _safe_query_all(
        """
        SELECT cs.id              AS row_id,
               cs.kind            AS kind,
               cs.title           AS title,
               cs.requester_email AS requester,
               cs.snow_number     AS reference,
               cs.snow_error      AS error,
               cs.created_at      AS created_at
          FROM catalog_submissions cs
         ORDER BY cs.created_at DESC
         LIMIT 500
        """
    )

    out: List[Dict[str, Any]] = []
    # Ids are namespaced. Both tables are SERIAL, so their first rows are both 1 --
    # merged without a prefix, one would overwrite the other in anything keyed by id.
    for r in approvals:
        out.append({
            "id": f"approval:{r.get('row_id')}",
            "kind": r.get("kind") or "",
            "name": _self_service_label(r.get("kind") or ""),
            "title": r.get("title") or "",
            "requester": r.get("requester") or "",
            "status": str(r.get("status") or ""),
            "reference": "",
            "created_at": r.get("created_at"),
            "finished_at": r.get("finished_at"),
            "route": "approval",
        })
    for r in ordered:
        number = str(r.get("reference") or "")
        out.append({
            "id": f"catalog:{r.get('row_id')}",
            "kind": r.get("kind") or "",
            "name": _self_service_label(r.get("kind") or ""),
            "title": r.get("title") or "",
            "requester": r.get("requester") or "",
            # An ordered item has no approval to be pending: it either reached
            # ServiceNow and has a number, or it did not and the reason is stored.
            "status": "ORDERED" if number else "FAILED",
            "reference": number,
            "error": str(r.get("error") or "")[:200],
            "created_at": r.get("created_at"),
            "finished_at": r.get("created_at"),
            "route": "servicenow",
        })

    out.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return _obs_ok(data=jsonable_encoder(out[:500]))


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
