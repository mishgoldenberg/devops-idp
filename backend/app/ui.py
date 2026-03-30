import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from api.artifactory import get_storage as _art_get_storage
from api.azure_devops import ProjectCreationPayload
from api.azure_devops import create_ado_project as _ado_create_project
from api.azure_devops import get_project_creation_status as _ado_project_status
from api.azure_devops import get_pipelines as _ado_pipelines
from api.azure_devops import get_pull_requests as _ado_pull_requests
from api.azure_devops import get_work_items as _ado_work_items
from api.dashboards import DashboardUpdateRequest
from api.dashboards import get_default_dashboard as _dash_get_default
from api.dashboards import update_dashboard as _dash_update
from api.servicenow import get_tickets as _snow_get_tickets
from api.sonarqube import get_projects as _sonar_get_projects
from db import query_one
from secrets_manager import delete_user_azure_devops_pat, get_user_azure_devops_pat, store_user_azure_devops_pat
from security import AuthUser, decode_access_token

ui_router = APIRouter()

HOME_WIDGET_KEYS = {
    "ado_my_work_items": "ado-tasks-component",
    "snow_my_tickets": "servicenow-tickets-component",
    "ado_my_pull_requests": "pull-requests-component",
    "ado_prs_for_review": "pull-requests-review-component",
    "ado_pipeline_status": "pipelines-component",
    "sonar_quality_gate": "sonarqube-quality-component",
    "artifactory_storage": "artifactory-storage-component",
    "service_health": "service-health-component",
}


def _get_templates(request: Request):
    """Retrieve the Jinja2Templates instance stored on the app state."""
    return request.app.state.templates  # type: ignore


def _format_display_name(username: str) -> str:
    """Convert a login name into a friendlier fallback display name."""
    local_part = username.split("@", 1)[0].replace(".", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in local_part.split() if part) or username


def _get_ui_user(token: str) -> Dict[str, str]:
    """Resolve the authenticated user's display info for the UI layer."""
    payload = decode_access_token(token)
    username = str(payload.get("username", "User"))
    display_name = _format_display_name(username)

    user_id = payload.get("id")
    if user_id:
        try:
            user_row = query_one(
                "SELECT full_name FROM users WHERE id = %s",
                [user_id],
            )
            if user_row and user_row.get("full_name"):
                display_name = str(user_row["full_name"])
        except Exception:
            pass

    return {
        "username": username,
        "display_name": display_name,
    }


def _current_user_from_token(token: str) -> Optional[AuthUser]:
    """Decode cookie token to the AuthUser dict shape used by API functions."""
    if not token:
        return None
    try:
        return AuthUser(decode_access_token(token))
    except Exception:
        return None


def _time_ago(iso_str: str) -> str:
    """Convert an ISO-8601 datetime string to a human-readable relative time."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ""


@ui_router.get("/ui/", response_class=HTMLResponse)
def ui_index(request: Request):
    """Render the HTMX-based UI landing page (home)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "current_page": "home",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/azure-devops", response_class=HTMLResponse)
def ui_azure_devops_page(request: Request):
    """Render the Azure DevOps tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "azure-devops.html",
        {
            "request": request,
            "user": user,
            "current_page": "azure-devops",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/artifactory", response_class=HTMLResponse)
def ui_artifactory_page(request: Request):
    """Render the Artifactory tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "artifactory.html",
        {
            "request": request,
            "user": user,
            "current_page": "artifactory",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/sonarqube", response_class=HTMLResponse)
def ui_sonarqube_page(request: Request):
    """Render the SonarQube tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "sonarqube.html",
        {
            "request": request,
            "user": user,
            "current_page": "sonarqube",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/servicenow", response_class=HTMLResponse)
def ui_servicenow_page(request: Request):
    """Render the ServiceNow tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "servicenow.html",
        {
            "request": request,
            "user": user,
            "current_page": "servicenow",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/support", response_class=HTMLResponse)
def ui_support_page(request: Request):
    """Render the Support page (ServiceNow tickets and conversation)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "support.html",
        {
            "request": request,
            "user": user,
            "current_page": "support",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/confluence", response_class=HTMLResponse)
def ui_confluence_page(request: Request):
    """Render the Confluence tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "confluence.html",
        {
            "request": request,
            "user": user,
            "current_page": "confluence",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/openshift", response_class=HTMLResponse)
def ui_openshift_page(request: Request):
    """Render the OpenShift tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "openshift.html",
        {
            "request": request,
            "user": user,
            "current_page": "openshift",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/internal-aws", response_class=HTMLResponse)
def ui_internal_aws_page(request: Request):
    """Render the Internal AWS tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "internal-aws.html",
        {
            "request": request,
            "user": user,
            "current_page": "internal-aws",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/grafana", response_class=HTMLResponse)
def ui_grafana_page(request: Request):
    """Render the Grafana tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "grafana.html",
        {
            "request": request,
            "user": user,
            "current_page": "grafana",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/automations", response_class=HTMLResponse)
def ui_automations_page(request: Request):
    """Render the Automations tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "automations.html",
        {
            "request": request,
            "user": user,
            "current_page": "automations",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/approvals", response_class=HTMLResponse)
def ui_approvals_page(request: Request):
    """Render the Approvals tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "approvals.html",
        {
            "request": request,
            "user": user,
            "current_page": "approvals",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/profile", response_class=HTMLResponse)
def ui_profile_page(request: Request):
    """Render the user profile page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "current_page": "profile",
            "email": payload.get("email", ""),
            "saved": request.query_params.get("saved", ""),
        },
    )


@ui_router.post("/ui/profile")
def ui_profile_update(request: Request, display_name: str = Form("")):
    """Update the authenticated user's display name."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    user_id = payload.get("id")
    if user_id:
        try:
            query_one(
                "UPDATE users SET full_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
                [display_name.strip() or None, user_id],
            )
        except Exception:
            pass
    return RedirectResponse(url="/ui/profile?saved=1", status_code=303)


@ui_router.get("/ui/settings", response_class=HTMLResponse)
def ui_settings_page(request: Request):
    """Render placeholder settings page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "current_page": "settings"},
    )


@ui_router.get("/ui/dashboard/preferences")
def ui_dashboard_preferences(request: Request):
    """Get enabled home widgets for the current user."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})

    dash = _dash_get_default(current_user=current_user)
    widgets = (dash.get("data") or {}).get("widgets") or []
    enabled = [w.get("widget_key") for w in widgets if w.get("widget_key") in HOME_WIDGET_KEYS]
    if not enabled:
        enabled = list(HOME_WIDGET_KEYS.keys())

    return {"success": True, "data": {"enabled": enabled}}


@ui_router.post("/ui/dashboard/preferences")
def ui_dashboard_preferences_save(
    request: Request,
    payload: Dict[str, Any] = Body(default={}),
):
    """Persist enabled home widgets to the default dashboard."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})

    requested = payload.get("enabled") or []
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="'enabled' must be a list")

    enabled_keys = [k for k in requested if isinstance(k, str) and k in HOME_WIDGET_KEYS]
    dash = _dash_get_default(current_user=current_user)
    dash_data = dash.get("data") or {}
    dashboard_id = dash_data.get("id")
    existing_widgets = dash_data.get("widgets") or []
    existing_by_key = {w.get("widget_key"): w for w in existing_widgets if w.get("widget_key")}

    new_widgets = []
    for key in enabled_keys:
        existing = existing_by_key.get(key, {})
        new_widgets.append(
            {
                "id": existing.get("id") or f"{key}-home",
                "widget_key": key,
                "config": existing.get("config") or {},
            }
        )

    _dash_update(
        dashboard_id=dashboard_id,
        body=DashboardUpdateRequest(widgets=new_widgets),
        current_user=current_user,
    )
    return {"success": True, "data": {"enabled": enabled_keys}}


@ui_router.get("/ui/azure-devops/pat")
def ui_get_ado_pat_status(request: Request):
    """Cookie-auth endpoint for PAT status used by HTMX templates."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    has_pat = bool(get_user_azure_devops_pat(str(current_user.get("id"))))
    return {"success": True, "data": {"configured": has_pat, "has_personal_pat": has_pat}}


@ui_router.post("/ui/azure-devops/pat")
def ui_save_ado_pat(request: Request, payload: Dict[str, Any] = Body(default={})):
    """Cookie-auth endpoint to save PAT from template widgets."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    pat = str(payload.get("pat", "")).strip()
    if len(pat) < 10:
        raise HTTPException(status_code=400, detail="PAT is too short")
    store_user_azure_devops_pat(str(current_user.get("id")), pat)
    return {"success": True}


@ui_router.delete("/ui/azure-devops/pat")
def ui_delete_ado_pat(request: Request):
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    delete_user_azure_devops_pat(str(current_user.get("id")))
    return {"success": True}


@ui_router.post("/ui/automations/azure-devops/create")
def ui_create_ado_project(
    request: Request,
    project_name: str = Form(""),
    process_type: str = Form("Scrum"),
    admin_username: str = Form(""),
):
    """Submit an Azure DevOps project creation automation from the UI."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})

    payload = ProjectCreationPayload(
        project_name=project_name,
        process_type=process_type,
        admin_username=admin_username,
    )
    return _ado_create_project(payload=payload, current_user=current_user)


@ui_router.get("/ui/automations/azure-devops/status/{job_id}")
def ui_ado_project_status(request: Request, job_id: str):
    """Poll Azure DevOps automation job status."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    return _ado_project_status(job_id=job_id, current_user=current_user)


@ui_router.get("/ui/auth", response_class=HTMLResponse)
def ui_auth_page(request: Request):
    """Render the login page for username-based authentication."""
    templates = _get_templates(request)

    token = request.cookies.get("auth_token")
    if token:
        try:
            decode_access_token(token)
            return RedirectResponse(url="/ui/", status_code=303)
        except HTTPException:
            pass

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": request.query_params.get("error", ""),
            "username": request.query_params.get("username", ""),
        },
    )


@ui_router.get("/ui/auth/login")
def ui_auth_login(
    username: str = "",
    password: str = "",
):
    """Redirect login requests to the API OAuth login endpoint."""
    del username
    del password
    return RedirectResponse(url="/api/auth/login", status_code=303)


@ui_router.post("/ui/auth/logout")
def ui_auth_logout():
    """Clear auth cookie and return user to login page."""
    response = RedirectResponse(url="/ui/auth", status_code=303)
    response.delete_cookie("auth_token")
    return response


@ui_router.get("/ui/components/banner", response_class=HTMLResponse)
def ui_banner_component(request: Request):
    """Render the top banner for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/banner.html",
        {"request": request},
    )


@ui_router.get("/ui/components/sidebar", response_class=HTMLResponse)
def ui_sidebar_component(request: Request):
    """Render the navigation sidebar for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/sidebar.html",
        {"request": request},
    )


@ui_router.get("/ui/components/quick-links", response_class=HTMLResponse)
def ui_quick_links_component(request: Request):
    """Render the Quick Links dashboard component for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/quick-links.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


def _mock_ado_task_counts() -> Dict[str, int]:
    return {"todo": 12, "in_progress": 4, "blocked": 2, "done": 7}


def _describe_ado_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail.strip():
            return detail
    return "Unable to load Azure DevOps data right now."


def _get_ado_task_counts(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    empty_counts: Dict[str, int] = {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0}
    if not current_user:
        return {"counts": empty_counts, "error": "Sign in to load your Azure DevOps tasks."}
    try:
        # Work-items endpoint accepts optional project filter (not username).
        result = _ado_work_items(project=None, current_user=current_user)
        items = result.get("data", []) if isinstance(result, dict) else []
        counts: Dict[str, int] = {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0}
        state_map = {
            "to do": "todo",
            "new": "todo",
            "proposed": "todo",
            "active": "in_progress",
            "in progress": "in_progress",
            "committed": "in_progress",
            "doing": "in_progress",
            "resolved": "done",
            "done": "done",
            "closed": "done",
            "completed": "done",
            "blocked": "blocked",
            "impediment": "blocked",
        }
        for item in items:
            bucket = state_map.get((item.get("state") or "").lower(), "todo")
            counts[bucket] += 1
        return {"counts": counts, "error": ""}
    except Exception as exc:
        return {"counts": empty_counts, "error": _describe_ado_error(exc)}


@ui_router.get("/ui/components/azure-devops-tasks", response_class=HTMLResponse)
def ui_azure_devops_tasks_component(request: Request):
    """Render the Azure DevOps Tasks dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    widget_state = _get_ado_task_counts(current_user)
    return templates.TemplateResponse(
        "partials/components/azure-devops-tasks.html",
        {
            "request": request,
            "counts": widget_state["counts"],
            "error": widget_state["error"],
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


def _mock_pr_data() -> list:
    return [
        {"id": 1, "title": "feat: Add user authentication flow", "repository": "my-app", "source_branch": "feat/auth", "target_branch": "main", "age": "1d ago", "url": ""},
        {"id": 2, "title": "fix: Null pointer in dashboard service", "repository": "devops-idp", "source_branch": "fix/null-ptr", "target_branch": "develop", "age": "3d ago", "url": ""},
        {"id": 3, "title": "chore: Bump dependency versions", "repository": "pipeline-lib", "source_branch": "chore/deps", "target_branch": "main", "age": "5d ago", "url": ""},
        {"id": 4, "title": "refactor: Extract metrics collector", "repository": "my-app", "source_branch": "refactor/metrics", "target_branch": "develop", "age": "7d ago", "url": ""},
    ]


def _get_pr_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    if not current_user:
        return {"prs": [], "error": "Sign in to load your Azure DevOps pull requests."}
    try:
        result = _ado_pull_requests(username=None, current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        return {
            "prs": [
                {
                    "id": pr.get("id"),
                    "title": pr.get("title"),
                    "repository": pr.get("repository"),
                    "source_branch": pr.get("source_branch"),
                    "target_branch": pr.get("target_branch"),
                    "age": _time_ago(pr.get("created_date", "")),
                    "url": pr.get("url", ""),
                    "created_by_email": pr.get("created_by_email", ""),
                    "is_reviewer": bool(pr.get("is_reviewer")),
                }
                for pr in rows
            ],
            "error": "",
        }
    except Exception as exc:
        return {"prs": [], "error": _describe_ado_error(exc)}


def _get_pr_created_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    state = _get_pr_data(current_user)
    if state.get("error"):
        return state
    username = (current_user or {}).get("username", "").lower() if current_user else ""
    prs = state.get("prs", [])
    created = [
        pr for pr in prs
        if (pr.get("created_by_email", "").lower() == username)
    ]
    return {"prs": created, "error": ""}


def _get_pr_review_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    state = _get_pr_data(current_user)
    if state.get("error"):
        return state
    review = [pr for pr in state.get("prs", []) if pr.get("is_reviewer")]
    return {"prs": review, "error": ""}


def _mock_pipeline_data() -> list:
    return [
        {"name": "Build & Test", "project": "my-app", "result": "succeeded", "age": "1h ago", "url": ""},
        {"name": "Deploy to Dev", "project": "my-app", "result": "succeeded", "age": "2h ago", "url": ""},
        {"name": "Security Scan", "project": "pipeline-lib", "result": "running", "age": "4h ago", "url": ""},
        {"name": "Build & Test", "project": "devops-idp", "result": "failed", "age": "5h ago", "url": ""},
        {"name": "Deploy to Staging", "project": "my-app", "result": "succeeded", "age": "1d ago", "url": ""},
    ]


def _get_pipeline_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    if not current_user:
        return {"runs": [], "error": "Sign in to load your Azure DevOps pipelines."}
    try:
        result = _ado_pipelines(project=None, current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        mapped = [
                {
                    "name": run.get("name", ""),
                    "project": run.get("project", ""),
                    "result": run.get("result", "in progress"),
                    "age": _time_ago(run.get("created_date", "")),
                    "url": run.get("url", ""),
                }
                for run in rows
            ]
        return {
            "runs": mapped[:1],
            "error": "",
        }
    except Exception as exc:
        return {"runs": [], "error": _describe_ado_error(exc)}


def _mock_sonar_data() -> list:
    return [
        {"name": "my-app", "quality_gate_status": "OK", "bugs": 2, "vulnerabilities": 0, "coverage": 87},
        {"name": "devops-idp", "quality_gate_status": "OK", "bugs": 5, "vulnerabilities": 1, "coverage": 72},
        {"name": "pipeline-lib", "quality_gate_status": "ERROR", "bugs": 12, "vulnerabilities": 3, "coverage": 43},
    ]


def _get_sonar_data(current_user: Optional[AuthUser]) -> list:
    if not current_user:
        return _mock_sonar_data()
    try:
        result = _sonar_get_projects(current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        return [
            {
                "name": p.get("name"),
                "quality_gate_status": p.get("quality_gate", {}).get("status", "UNKNOWN"),
                "bugs": p.get("metrics", {}).get("bugs", 0),
                "vulnerabilities": p.get("metrics", {}).get("vulnerabilities", 0),
                "coverage": p.get("metrics", {}).get("coverage", 0),
            }
            for p in rows
        ]
    except Exception:
        return _mock_sonar_data()


def _fmt_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(value)
    for unit in units:
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} PB"


def _normalize_artifactory_storage(data: Dict[str, Any]) -> Dict[str, Any]:
    used = data.get("used", 0)
    total = data.get("total", 0)
    percentage = data.get("percentage", 0)

    if isinstance(used, (int, float)):
        used_out = _fmt_bytes(float(used))
    else:
        used_out = str(used)

    if isinstance(total, (int, float)):
        total_out = _fmt_bytes(float(total))
    else:
        total_out = str(total)

    repos_out = []
    for repo in data.get("repositories", []):
        repo_used = repo.get("used", 0)
        if isinstance(repo_used, (int, float)):
            repo_used_out = _fmt_bytes(float(repo_used))
        else:
            repo_used_out = str(repo_used)
        repos_out.append(
            {
                "name": repo.get("name", ""),
                "used": repo_used_out,
                "percentage": repo.get("percentage", 0),
            }
        )

    return {
        "used": used_out,
        "total": total_out,
        "percentage": percentage,
        "repositories": repos_out,
    }


def _mock_artifactory_data() -> Dict[str, Any]:
    return {
        "used": "48.3 GB",
        "total": "100 GB",
        "percentage": 48,
        "repositories": [
            {"name": "docker-local", "used": "22.1 GB", "percentage": 46},
            {"name": "npm-local", "used": "14.7 GB", "percentage": 30},
            {"name": "pypi-local", "used": "8.3 GB", "percentage": 17},
            {"name": "generic-local", "used": "3.2 GB", "percentage": 7},
        ],
    }


def _get_artifactory_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    if not current_user:
        return _mock_artifactory_data()
    try:
        result = _art_get_storage(current_user=current_user)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        if not data:
            return _mock_artifactory_data()
        return _normalize_artifactory_storage(data)
    except Exception:
        return _mock_artifactory_data()


def _get_service_health_data() -> list:
    """Return service health status data for the Service Health widget."""
    return [
        {"name": "Azure DevOps", "status": "healthy"},
        {"name": "SonarQube", "status": "healthy"},
        {"name": "Artifactory", "status": "healthy"},
        {"name": "ServiceNow", "status": "unhealthy"},
        {"name": "Database", "status": "healthy"},
        {"name": "Redis", "status": "healthy"},
    ]


@ui_router.get("/ui/components/pull-requests", response_class=HTMLResponse)
def ui_pull_requests_component(request: Request):
    """Render the Pull Requests dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    widget_state = _get_pr_created_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pull-requests.html",
        {"request": request, "prs": widget_state["prs"], "error": widget_state["error"]},
    )


@ui_router.get("/ui/components/pull-requests-review", response_class=HTMLResponse)
def ui_pull_requests_review_component(request: Request):
    """Render the PRs-to-review dashboard widget."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    widget_state = _get_pr_review_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pull-requests-review.html",
        {"request": request, "prs": widget_state["prs"], "error": widget_state["error"]},
    )


@ui_router.get("/ui/components/pipelines", response_class=HTMLResponse)
def ui_pipelines_component(request: Request):
    """Render the Pipelines dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    widget_state = _get_pipeline_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pipelines.html",
        {"request": request, "runs": widget_state["runs"], "error": widget_state["error"]},
    )


@ui_router.get("/ui/components/sonarqube-quality", response_class=HTMLResponse)
def ui_sonarqube_quality_component(request: Request):
    """Render the SonarQube Code Quality dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    return templates.TemplateResponse(
        "partials/components/sonarqube-quality.html",
        {"request": request, "projects": _get_sonar_data(current_user)},
    )


@ui_router.get("/ui/components/artifactory-storage", response_class=HTMLResponse)
def ui_artifactory_storage_component(request: Request):
    """Render the Artifactory Storage dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    return templates.TemplateResponse(
        "partials/components/artifactory-storage.html",
        {"request": request, "storage": _get_artifactory_data(current_user)},
    )


@ui_router.get("/ui/components/service-health", response_class=HTMLResponse)
def ui_service_health_component(request: Request):
    """Render the Service Health dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/service-health.html",
        {"request": request, "services": _get_service_health_data()},
    )


@ui_router.get("/ui/components/servicenow-tickets", response_class=HTMLResponse)
def ui_servicenow_tickets_component(request: Request):
    """Render the ServiceNow Tickets dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    stats = {"open": 0, "in_progress": 0, "resolved": 0, "total": 0}
    error = ""
    if current_user:
        try:
            res = _snow_get_tickets(current_user=current_user)
            tickets = (res.get("data") or []) if isinstance(res, dict) else []
            for t in tickets:
                state = str(t.get("state", "")).lower()
                stats["total"] += 1
                if state == "new":
                    stats["open"] += 1
                elif state == "in progress":
                    stats["in_progress"] += 1
                elif state in {"resolved", "closed"}:
                    stats["resolved"] += 1
        except Exception as exc:
            if isinstance(exc, HTTPException):
                detail = exc.detail
                error = detail if isinstance(detail, str) else "ServiceNow unavailable"
            else:
                error = "ServiceNow unavailable"
    return templates.TemplateResponse(
        "partials/components/servicenow-tickets.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
            "stats": stats,
            "error": error,
        },
    )
