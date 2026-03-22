import os
from datetime import datetime
from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

ui_router = APIRouter()


def _get_templates(request: Request):
    """Retrieve the Jinja2Templates instance stored on the app state."""
    return request.app.state.templates  # type: ignore


@ui_router.get("/ui/", response_class=HTMLResponse)
def ui_index(request: Request):
    """Render the HTMX-based UI landing page."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
        },
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


def _get_ado_task_counts() -> Dict[str, int]:
    """Return Task-only counts for the Azure DevOps widget.

    Defaults to dummy data until auth is wired for UI components.
    Set UI_USE_DUMMY_ADO_TASKS=false to enable integration call path below.
    """
    if os.getenv("UI_USE_DUMMY_ADO_TASKS", "true").lower() in ("true", "1"):
        return {"todo": 12, "in_progress": 4, "blocked": 2, "done": 7}

    # Integration path is intentionally disabled until UI auth/session wiring is ready.
    # Future path should call existing backend integration endpoint:
    #   GET /api/azure-devops/work-items?work_item_type=Task
    # and then map states into this widget.
    return {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0}


@ui_router.get("/ui/components/azure-devops-tasks", response_class=HTMLResponse)
def ui_azure_devops_tasks_component(request: Request):
    """Render the Azure DevOps Tasks dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    counts = _get_ado_task_counts()
    return templates.TemplateResponse(
        "partials/components/azure-devops-tasks.html",
        {
            "request": request,
            "counts": counts,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


def _get_pr_data() -> list:
    """Return pull request data for the PR Review Queue widget.

    Defaults to dummy data until auth is wired for UI components.
    Future path: GET /api/azure-devops/pull-requests
    """
    if os.getenv("UI_USE_DUMMY_DATA", "true").lower() in ("true", "1"):
        return [
            {"id": 1, "title": "feat: Add user authentication flow", "repository": "my-app", "source_branch": "feat/auth", "target_branch": "main", "age": "1d ago"},
            {"id": 2, "title": "fix: Null pointer in dashboard service", "repository": "devops-idp", "source_branch": "fix/null-ptr", "target_branch": "develop", "age": "3d ago"},
            {"id": 3, "title": "chore: Bump dependency versions", "repository": "pipeline-lib", "source_branch": "chore/deps", "target_branch": "main", "age": "5d ago"},
            {"id": 4, "title": "refactor: Extract metrics collector", "repository": "my-app", "source_branch": "refactor/metrics", "target_branch": "develop", "age": "7d ago"},
        ]
    return []


def _get_pipeline_data() -> list:
    """Return recent pipeline run data for the Pipelines widget.

    Future path: GET /api/azure-devops/pipelines
    """
    if os.getenv("UI_USE_DUMMY_DATA", "true").lower() in ("true", "1"):
        return [
            {"name": "Build & Test", "project": "my-app", "result": "succeeded", "age": "1h ago"},
            {"name": "Deploy to Dev", "project": "my-app", "result": "succeeded", "age": "2h ago"},
            {"name": "Security Scan", "project": "pipeline-lib", "result": "running", "age": "4h ago"},
            {"name": "Build & Test", "project": "devops-idp", "result": "failed", "age": "5h ago"},
            {"name": "Deploy to Staging", "project": "my-app", "result": "succeeded", "age": "1d ago"},
        ]
    return []


def _get_sonar_data() -> list:
    """Return SonarQube project quality data for the Code Quality widget.

    Future path: GET /api/sonarqube/projects
    """
    if os.getenv("UI_USE_DUMMY_DATA", "true").lower() in ("true", "1"):
        return [
            {"name": "my-app", "quality_gate_status": "OK", "bugs": 2, "vulnerabilities": 0, "coverage": 87},
            {"name": "devops-idp", "quality_gate_status": "OK", "bugs": 5, "vulnerabilities": 1, "coverage": 72},
            {"name": "pipeline-lib", "quality_gate_status": "ERROR", "bugs": 12, "vulnerabilities": 3, "coverage": 43},
        ]
    return []


def _get_artifactory_data() -> dict:
    """Return Artifactory storage data for the Storage widget.

    Future path: GET /api/artifactory/storage
    """
    if os.getenv("UI_USE_DUMMY_DATA", "true").lower() in ("true", "1"):
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
    return {"used": "0 GB", "total": "0 GB", "percentage": 0, "repositories": []}


def _get_service_health_data() -> list:
    """Return service health status data for the Service Health widget.

    Future path: GET /api/metrics/services
    """
    if os.getenv("UI_USE_DUMMY_DATA", "true").lower() in ("true", "1"):
        return [
            {"name": "Azure DevOps", "status": "healthy"},
            {"name": "SonarQube", "status": "healthy"},
            {"name": "Artifactory", "status": "healthy"},
            {"name": "ServiceNow", "status": "unhealthy"},
            {"name": "Database", "status": "healthy"},
            {"name": "Redis", "status": "healthy"},
        ]
    return []


@ui_router.get("/ui/components/pull-requests", response_class=HTMLResponse)
def ui_pull_requests_component(request: Request):
    """Render the Pull Requests dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/pull-requests.html",
        {"request": request, "prs": _get_pr_data()},
    )


@ui_router.get("/ui/components/pipelines", response_class=HTMLResponse)
def ui_pipelines_component(request: Request):
    """Render the Pipelines dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/pipelines.html",
        {"request": request, "runs": _get_pipeline_data()},
    )


@ui_router.get("/ui/components/sonarqube-quality", response_class=HTMLResponse)
def ui_sonarqube_quality_component(request: Request):
    """Render the SonarQube Code Quality dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/sonarqube-quality.html",
        {"request": request, "projects": _get_sonar_data()},
    )


@ui_router.get("/ui/components/artifactory-storage", response_class=HTMLResponse)
def ui_artifactory_storage_component(request: Request):
    """Render the Artifactory Storage dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/artifactory-storage.html",
        {"request": request, "storage": _get_artifactory_data()},
    )


@ui_router.get("/ui/components/service-health", response_class=HTMLResponse)
def ui_service_health_component(request: Request):
    """Render the Service Health dashboard widget for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/service-health.html",
        {"request": request, "services": _get_service_health_data()},
    )
