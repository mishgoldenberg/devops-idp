"""
Azure DevOps Integration API Module

This module provides FastAPI endpoints for Azure DevOps integration, including:
- Real-time work item & PR queries  
- Custom process & project creation (Self-Service)
- Pipeline status monitoring
- Mock implementations for local development

CONFIGURATION:
  - USE_MOCK_AZURE_DEVOPS: Set to 'false' to use real Azure DevOps API (default: 'true' for dev)
  - AZURE_DEVOPS_ORGANIZATION or AZURE_DEVOPS_ORG: Organization name (e.g. MyOrg). Base URL becomes https://dev.azure.com/MyOrg. Required for real API.
  - AZURE_DEVOPS_API_URL: Optional; if set and no org above, used as-is. Otherwise org is required.
  - AZURE_DEVOPS_PAT: Personal Access Token (required for real API calls)
  - AZURE_DEVOPS_QUERY_USER: Optional test user for local development

SELF-SERVICE FEATURE:
  POST /api/azure-devops/projects/create
  - Direct provisioning (no approval workflow)
  - Creates custom process based on requested type (Scrum/Agile/CMMI/Basic)
  - Automatically assigns admin user to Project Administrators group
  - Returns project URL for immediate access

ENDPOINTS:
  GET  /api/azure-devops/work-items          - User's assigned work items
  GET  /api/azure-devops/pull-requests       - User's pull requests
  GET  /api/azure-devops/pipelines           - Project pipeline status
  POST /api/azure-devops/projects/create     - Self-service project creation
"""

from typing import Any, Dict, List, Optional
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
import httpx

from security import AuthUser, get_current_user
from secrets_manager import (
    delete_user_azure_devops_pat,
    get_user_azure_devops_pat,
    store_user_azure_devops_pat,
)


router = APIRouter()

# Configuration from environment variables (read at import so env is respected in K8s/local)
def _get_ado_base() -> str:
    """Build Azure DevOps API base URL. Must include organization: https://dev.azure.com/{org}."""
    org = (os.getenv("AZURE_DEVOPS_ORGANIZATION") or os.getenv("AZURE_DEVOPS_ORG") or "").strip()
    if org:
        return f"https://dev.azure.com/{org}".rstrip("/")
    base = (os.getenv("AZURE_DEVOPS_API_URL") or "https://dev.azure.com").strip().rstrip("/")
    # If URL is still just https://dev.azure.com with no org, we cannot call the API
    if base == "https://dev.azure.com":
        return base  # Caller will get 404/401 until org is set
    return base


USE_MOCK = os.getenv("USE_MOCK_AZURE_DEVOPS", "true").lower() in ("true", "1")
ADO_BASE = _get_ado_base()
# Shared server-level PAT (env). Used as fallback only for admin users so they
# can test immediately without entering a per-user PAT.
_ENV_PAT = os.getenv("AZURE_DEVOPS_PAT", "")
# Admin PAT used for write operations (process creation).
# Requires scope: Process (Read & Manage) — or Full Access.
_ENV_ADMIN_PAT = os.getenv("AZURE_DEVOPS_ADMIN_PAT", "")
ADO_QUERY_USER = os.getenv("AZURE_DEVOPS_QUERY_USER", "")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _get_pat_for_user(current_user: AuthUser) -> str:
    """
    Resolve the Azure DevOps PAT for the requesting user.

    Only the per-user PAT stored in Vault / system_config (set via the dashboard
    UI) is accepted for widget/read endpoints.  The server-level env PATs
    (_ENV_PAT, _ENV_ADMIN_PAT) are intentionally NOT used here — they are
    reserved for self-service write operations (project creation).

    The PAT is never returned to the frontend or written to logs.
    """
    user_id = str(current_user.get("id"))
    try:
        pat = get_user_azure_devops_pat(user_id)
    except Exception:
        pat = None
    if pat:
        return pat
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Azure DevOps is not connected. Please add your Personal Access Token.",
    )


# ── PAT management ────────────────────────────────────────────────────────────

class _PatPayload(BaseModel):
    pat: str


@router.get("/pat")
def get_pat_status(current_user: AuthUser = Depends(get_current_user)):
    """Return whether this user has a personal PAT configured. Value is never returned."""
    user_id = str(current_user.get("id"))
    has_user_pat = bool(get_user_azure_devops_pat(user_id))
    return {
        "success": True,
        "data": {"configured": has_user_pat, "has_personal_pat": has_user_pat},
        "timestamp": _now_iso(),
    }


@router.post("/pat", status_code=204)
def save_pat(body: _PatPayload, current_user: AuthUser = Depends(get_current_user)):
    """Store or update the per-user Azure DevOps PAT."""
    raw = (body.pat or "").strip()
    if len(raw) < 10:
        raise HTTPException(status_code=400, detail="PAT is too short – paste the full token")
    store_user_azure_devops_pat(str(current_user.get("id")), raw)
    return Response(status_code=204)


@router.delete("/pat", status_code=204)
def remove_pat(current_user: AuthUser = Depends(get_current_user)):
    """Delete the per-user Azure DevOps PAT."""
    delete_user_azure_devops_pat(str(current_user.get("id")))
    return Response(status_code=204)


def _fetch_state_categories(client: httpx.Client, project: str, work_item_type: str) -> dict:
    """
    Returns a mapping of {state_name: state_category} for a given project + work item type.
    State categories are ADO-standard strings: 'Proposed', 'InProgress', 'Resolved',
    'Completed', 'Removed' — these are stable regardless of custom state names (e.g. "Doing").
    Returns an empty dict (not None) on failure so callers can fall back to name heuristics.
    """
    import logging
    from urllib.parse import quote

    url = (
        f"{ADO_BASE}/{quote(project, safe='')}/_apis/wit/workitemtypes"
        f"/{quote(work_item_type, safe='')}/"
        f"states?api-version=7.0"
    )
    try:
        r = client.get(url, timeout=10.0)
        r.raise_for_status()
        mapping = {s["name"]: s.get("stateCategory", "") for s in r.json().get("value", [])}
        logging.debug("State categories for %s/%s: %s", project, work_item_type, mapping)
        return mapping
    except Exception as exc:
        logging.warning(
            "Could not fetch state categories for %s/%s (%s) — falling back to name heuristics",
            project, work_item_type, exc,
        )
        return {}


@router.get("/projects")
def get_projects(current_user: AuthUser = Depends(get_current_user)):
    """List all Azure DevOps projects the PAT has access to."""
    if USE_MOCK:
        return {
            "success": True,
            "data": [
                {"id": "mock-1", "name": "DevOps"},
                {"id": "mock-2", "name": "backend-api"},
            ],
            "timestamp": _now_iso(),
        }
    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_ORG is not configured")

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)
    try:
        with httpx.Client(auth=auth, timeout=20.0) as client:
            r = client.get(f"{ADO_BASE}/_apis/projects?$top=200&api-version=7.0")
            r.raise_for_status()
            projects = [
                {"id": p.get("id"), "name": p.get("name")}
                for p in r.json().get("value", [])
            ]
        return {"success": True, "data": projects, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps API error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps request failed: {exc!s}")


@router.get("/work-items")
def get_work_items(
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Return work items assigned to the PAT owner (@Me macro ensures we query the right user).
    Optionally filter by project name.  Each item includes state_category so the
    frontend can group by 'Proposed'/'InProgress'/'Resolved'/'Completed' regardless
    of the team's custom state names (e.g. 'Doing' → 'InProgress').
    """
    if USE_MOCK:
        work_items = [
            {
                "id": 1001,
                "title": "Implement feature",
                "state": "In Progress",
                "state_category": "InProgress",
                "type": "User Story",
                "project": "DevOps",
                "assigned_to": "you",
                "created_date": "2024-01-14T09:00:00Z",
                "changed_date": "2024-01-15T10:00:00Z",
                "url": "https://dev.azure.com/org/project/_workitems/edit/1001",
            }
        ]
        return {"success": True, "data": work_items, "timestamp": _now_iso()}

    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_ORG is not configured on the server",
        )
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)

        # @Me resolves to the identity that owns the PAT – no need to pass the username
        project_clause = f"AND [System.TeamProject] = '{project}'" if project else ""
        # Also exclude Done / Completed states by filtering on category not name
        query = {
            "query": (
                "Select [System.Id], [System.Title], [System.State], "
                "[System.WorkItemType], [System.TeamProject] "
                "From WorkItems "
                f"Where [System.AssignedTo] = @Me "
                "AND [System.State] <> 'Closed' "
                "AND [System.State] <> 'Done' "
                "AND [System.State] <> 'Removed' "
                f"{project_clause}"
                "Order By [System.ChangedDate] Desc"
            )
        }
        # Use test override if configured (helpful when local PAT belongs to a service account)
        if ADO_QUERY_USER:
            query["query"] = query["query"].replace(
                "[System.AssignedTo] = @Me",
                f"[System.AssignedTo] = '{ADO_QUERY_USER}'",
            )

        wiql_url = f"{ADO_BASE}/_apis/wit/wiql?api-version=7.0"
        with httpx.Client(auth=auth, timeout=30.0) as client:
            r = client.post(wiql_url, json=query)
            r.raise_for_status()
            ids = [str(item["id"]) for item in r.json().get("workItems", [])]
            if not ids:
                return {"success": True, "data": [], "timestamp": _now_iso()}

            # Fetch full details in batches of 200 (API limit)
            items: list = []
            for i in range(0, len(ids), 200):
                chunk = ",".join(ids[i : i + 200])
                r2 = client.get(
                    f"{ADO_BASE}/_apis/wit/workitems?ids={chunk}"
                    "&fields=System.Id,System.Title,System.State,System.WorkItemType,"
                    "System.TeamProject,System.AssignedTo,System.CreatedDate,System.ChangedDate"
                    "&api-version=7.0"
                )
                r2.raise_for_status()
                items.extend(r2.json().get("value", []))

            # Build state-category map per (project, type) – one API call each, minimal overhead
            _cat_cache: dict = {}
            org = ADO_BASE.split("/")[-1]

            work_items = []
            for it in items:
                fields = it.get("fields", {})
                wi_id = it.get("id")
                wi_state = fields.get("System.State") or ""
                wi_type = fields.get("System.WorkItemType") or ""
                wi_project = fields.get("System.TeamProject") or ""

                cache_key = (wi_project, wi_type)
                if cache_key not in _cat_cache:
                    _cat_cache[cache_key] = _fetch_state_categories(client, wi_project, wi_type)
                state_category = _cat_cache[cache_key].get(wi_state, "")

                portal_url = (
                    f"https://dev.azure.com/{org}/{wi_project}/_workitems/edit/{wi_id}"
                    if wi_project
                    else f"https://dev.azure.com/{org}/_workitems/edit/{wi_id}"
                )
                work_items.append(
                    {
                        "id": wi_id,
                        "title": fields.get("System.Title"),
                        "state": wi_state,
                        "state_category": state_category,
                        "type": wi_type,
                        "project": wi_project,
                        "assigned_to": fields.get("System.AssignedTo"),
                        "created_date": fields.get("System.CreatedDate"),
                        "changed_date": fields.get("System.ChangedDate"),
                        "url": portal_url,
                    }
                )

        return {"success": True, "data": work_items, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        import logging
        logging.warning("Azure DevOps API error: %s %s", exc.response.status_code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps API error: {exc.response.status_code}. Check AZURE_DEVOPS_ORGANIZATION and PAT.",
        )
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps WIQL request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


@router.get("/pull-requests")
def get_pull_requests(
    username: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    effective_username = username or current_user.get("username")
    if not effective_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username required",
        )

    # If a test ADO account is provided via env, use that
    if ADO_QUERY_USER:
        effective_username = ADO_QUERY_USER

    if USE_MOCK:
        # Mock data
        pull_requests = [
            {
                "id": 2001,
                "title": "Refactor authentication flow",
                "status": "active",
                "created_by": effective_username,
                "created_date": "2024-01-15T08:30:00Z",
                "repository": "backend-api",
                "source_branch": "feature/auth-refactor",
                "target_branch": "main",
                "url": "https://dev.azure.com/org/project/_git/backend-api/pullrequest/2001",
            }
        ]
        return {"success": True, "data": pull_requests, "timestamp": _now_iso()}

    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_ORG is not configured on the server",
        )
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        # Query all projects in the org
        projects_url = f"{ADO_BASE}/_apis/projects?api-version=7.0"
        with httpx.Client(auth=auth, timeout=30.0) as client:
            r_projects = client.get(projects_url)
            r_projects.raise_for_status()
            projects = r_projects.json().get("value", [])

            pull_requests = []
            # Iterate projects and query PRs for each
            for proj in projects:
                project_id = proj.get("id")
                repos_url = f"{ADO_BASE}/{project_id}/_apis/git/repositories?api-version=7.0"
                r_repos = client.get(repos_url)
                if r_repos.status_code != 200:
                    continue
                repos = r_repos.json().get("value", [])

                for repo in repos:
                    repo_id = repo.get("id")
                    prs_url = f"{ADO_BASE}/{project_id}/_apis/git/repositories/{repo_id}/pullrequests?searchCriteria.status=active&api-version=7.0"
                    r_prs = client.get(prs_url)
                    if r_prs.status_code != 200:
                        continue
                    prs = r_prs.json().get("value", [])

                    for pr in prs:
                        created_by = pr.get("createdBy", {}).get("displayName", "Unknown")
                        created_by_email = pr.get("createdBy", {}).get("uniqueName", "").lower()
                        query_user_lower = effective_username.lower()
                        
                        # Filter by creator email or reviewer email
                        is_creator = created_by_email == query_user_lower
                        is_reviewer = any(rev.get("uniqueName", "").lower() == query_user_lower for rev in pr.get("reviewers", []))
                        
                        if is_creator or is_reviewer:
                            # Extract org and project
                            org = ADO_BASE.split("/")[-1]
                            project_name = proj.get("name")
                            repo_name = repo.get("name")
                            pr_id = pr.get("pullRequestId")
                            
                            # Construct proper portal URL for PR
                            portal_url = f"https://dev.azure.com/{org}/{project_name}/_git/{repo_name}/pullrequest/{pr_id}"
                            
                            pull_requests.append({
                                "id": pr_id,
                                "title": pr.get("title"),
                                "status": pr.get("status", "").lower(),
                                "created_by": created_by,
                                "created_by_email": created_by_email,
                                "created_date": pr.get("creationDate"),
                                "repository": repo_name,
                                "source_branch": pr.get("sourceRefName", "").replace("refs/heads/", ""),
                                "target_branch": pr.get("targetRefName", "").replace("refs/heads/", ""),
                                "is_reviewer": is_reviewer,
                                "url": portal_url,
                            })

        return {"success": True, "data": pull_requests, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        import logging
        logging.warning("Azure DevOps API error: %s %s", exc.response.status_code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps API error: {exc.response.status_code}. Check AZURE_DEVOPS_ORGANIZATION and PAT.",
        )
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps PR request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


@router.get("/pipelines")
def get_pipelines(
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    if USE_MOCK:
        pipelines = [
            {
                "id": 3001,
                "name": project or "backend-api CI",
                "run_id": 987,
                "status": "completed",
                "result": "succeeded",
                "created_date": "2024-01-15T07:00:00Z",
                "finished_date": "2024-01-15T07:05:00Z",
                "url": "https://dev.azure.com/org/project/_build/results?buildId=987",
            }
        ]
        return {"success": True, "data": pipelines, "timestamp": _now_iso()}

    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_ORG is not configured on the server",
        )
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        # Query all projects in the org and get their recent builds
        projects_url = f"{ADO_BASE}/_apis/projects?api-version=7.0"
        with httpx.Client(auth=auth, timeout=30.0) as client:
            r_projects = client.get(projects_url)
            r_projects.raise_for_status()
            projects = r_projects.json().get("value", [])

            pipelines = []
            # Iterate projects and query builds for each
            for proj in projects:
                project_id = proj.get("id")
                project_name = proj.get("name")
                
                # Skip if project filter is provided and doesn't match
                if project and project.lower() != project_name.lower():
                    continue

                # Query recent builds (pipeline runs)
                builds_url = f"{ADO_BASE}/{project_id}/_apis/build/builds?$top=20&api-version=7.0"
                r_builds = client.get(builds_url)
                if r_builds.status_code != 200:
                    continue
                builds = r_builds.json().get("value", [])

                query_user_lower = (ADO_QUERY_USER or current_user.get("username", "")).lower()
                for build in builds:
                    requested_for = build.get("requestedFor", {}) or {}
                    requested_for_email = str(requested_for.get("uniqueName", "")).lower()
                    if query_user_lower and requested_for_email != query_user_lower:
                        continue
                    # Extract org and construct portal URL
                    org = ADO_BASE.split("/")[-1]
                    build_id = build.get("id")
                    
                    # Construct proper portal URL for build
                    portal_url = f"https://dev.azure.com/{org}/{project_name}/_build/results?buildId={build_id}"
                    
                    pipelines.append({
                        "id": build_id,
                        "name": build.get("definition", {}).get("name") or project_name,
                        "run_id": build_id,
                        "status": build.get("status", "").lower(),
                        "result": build.get("result", "").lower() if build.get("result") else "in progress",
                        "requested_for": requested_for.get("displayName") or requested_for.get("uniqueName") or "",
                        "created_date": build.get("startTime"),
                        "finished_date": build.get("finishTime"),
                        "url": portal_url,
                    })

            # Sort by creation date descending
            pipelines.sort(key=lambda x: x.get("created_date", ""), reverse=True)
            return {"success": True, "data": pipelines[:20], "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        import logging
        logging.warning("Azure DevOps API error: %s %s", exc.response.status_code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps API error: {exc.response.status_code}. Check AZURE_DEVOPS_ORGANIZATION and PAT.",
        )
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps pipelines request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


# existing mock endpoint for approval flow
@router.post("/projects")
def create_project(payload: Dict[str, Any], current_user: AuthUser = Depends(get_current_user)):
    # Mock project creation, used by approval flow
    name = payload.get("name")
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project name required",
        )
    project = {"id": 4001, "name": name, "description": payload.get("description") or "", "url": f"https://dev.azure.com/org/{name}"}
    return {"success": True, "data": project, "timestamp": _now_iso()}




# ── Self-service project creation via Terraform ──────────────────────────────

import logging as _logging

from terraform_runner import (
    sanitize_ado_name,
    submit_terraform_job,
    get_job_status,
    save_logs_to_gcs,
)

# Track jobs where the post-Terraform admin assignment has already been applied.
# A simple in-memory set is sufficient for single-replica deployments.
_admin_assigned_jobs: set = set()

VALID_PROCESS_TYPES = {"scrum", "agile", "cmmi", "basic"}


class ProjectCreationPayload(BaseModel):
    project_name: str
    process_type: str   # Scrum | Agile | CMMI | Basic
    admin_username: str  # Azure DevOps principal name / e-mail


@router.post("/projects/create")
def create_ado_project(
    payload: ProjectCreationPayload,
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Self-Service: provision an Azure DevOps project via a Terraform container.

    Flow
    ────
    1. Sanitize + validate inputs.
    2. [Real mode only] Verify the project name is unique in Azure DevOps (409 if taken).
    3. [Real mode only] Verify the admin user exists in the organisation (422 if missing).
    4. [Real mode only] Pre-create the custom inherited process
       '<project_name>-<process_type>' via REST API if it does not yet exist.
    5. Generate a Terraform module and submit it as a Kubernetes Job
       (image: hashicorp/terraform:1.6, SA: devops-terraform-sa).
    6. Return a job_id for the client to poll via GET /projects/create/status/{job_id}.
    """
    # ── Basic validation ───────────────────────────────────────────────────
    project_name = sanitize_ado_name(payload.project_name.strip())
    if not project_name:
        raise HTTPException(status_code=400, detail="project_name is required.")

    process_type = payload.process_type.strip()
    if process_type.lower() not in VALID_PROCESS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"process_type must be one of: Scrum, Agile, CMMI, Basic.",
        )

    admin_username = payload.admin_username.strip()
    if not admin_username:
        raise HTTPException(status_code=400, detail="admin_username is required.")

    custom_process_name = f"{project_name}-{process_type}"
    org = ADO_BASE.split("/")[-1]

    # ── Mock mode ──────────────────────────────────────────────────────────
    if USE_MOCK:
        job_id = submit_terraform_job(
            project_name=project_name,
            process_name=custom_process_name,
            ado_org=org,
            admin_username=admin_username,
            use_mock=True,
        )
        return {
            "success": True,
            "data": {"job_id": job_id, "status": "pending"},
            "timestamp": _now_iso(),
        }

    # ── Real mode ──────────────────────────────────────────────────────────
    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_ORG is not configured.")

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)

    try:
        with httpx.Client(auth=auth, timeout=30.0) as client:

            # 1. Uniqueness check — requires Project (Read) PAT scope
            try:
                r = client.get(f"{ADO_BASE}/_apis/projects?$top=500&api-version=7.0")
                r.raise_for_status()
                existing_names = [p["name"].lower() for p in r.json().get("value", [])]
                if project_name.lower() in existing_names:
                    raise HTTPException(
                        status_code=409,
                        detail=f"A project named '{project_name}' already exists in Azure DevOps.",
                    )
            except HTTPException:
                raise
            except httpx.HTTPStatusError as exc:
                _logging.error("ADO projects list failed (%s): %s", exc.response.status_code, exc.response.text[:200])
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Could not reach Azure DevOps (projects API returned {exc.response.status_code}). "
                        "Verify AZURE_DEVOPS_PAT has 'Project and Team (Read & Write)' scope."
                    ),
                )

            # 2. Admin user existence check — requires Graph (Read) PAT scope.
            #    If the PAT lacks that scope (403/401) we skip the check rather than
            #    blocking the request; Terraform will surface the error post-job.
            try:
                users_url = (
                    f"{ADO_BASE}/_apis/graph/users?api-version=7.0"
                    f"&$filter=principalName eq '{admin_username}'"
                )
                ru = client.get(users_url)
                if ru.status_code in (401, 403):
                    _logging.warning(
                        "Graph API returned %s for admin user check — "
                        "PAT may lack 'Graph (Read)' scope. Skipping pre-flight check.",
                        ru.status_code,
                    )
                else:
                    ru.raise_for_status()
                    if not ru.json().get("value"):
                        raise HTTPException(
                            status_code=422,
                            detail=f"Azure DevOps user '{admin_username}' was not found in the organisation.",
                        )
            except HTTPException:
                raise
            except httpx.HTTPStatusError as exc:
                _logging.warning(
                    "Admin user check failed (%s) — skipping pre-flight validation.",
                    exc.response.status_code,
                )

            # 3 + 4. Fetch process list and pre-create the inherited process.
            #
            # Uses the newer _apis/work/processes endpoint (7.1-preview.2) for
            # both read and write.  This API returns customizationType/typeId
            # (vs type/id on the legacy _apis/process/processes endpoint) and
            # supports POST for creating inherited processes.
            #
            # Both calls use the admin PAT so no extra PAT scope is required
            # from the end user.  Failures are surfaced as HTTP 502 — the
            # silent fallback to plain Scrum has been removed.
            proc_auth = httpx.BasicAuth("", _ENV_ADMIN_PAT or pat)

            try:
                r_procs = client.get(
                    f"{ADO_BASE}/_apis/work/processes?api-version=7.1-preview.2",
                    auth=proc_auth,
                )
                r_procs.raise_for_status()
                all_procs = r_procs.json().get("value", [])
            except HTTPException:
                raise
            except httpx.HTTPStatusError as exc:
                _logging.error(
                    "Process list API failed (%s): %s",
                    exc.response.status_code, exc.response.text[:200],
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Could not fetch Azure DevOps process list "
                        f"(HTTP {exc.response.status_code}). "
                        "Ensure AZURE_DEVOPS_ADMIN_PAT has 'Process (Read & Manage)' scope."
                    ),
                )

            parent_proc = next(
                (
                    p for p in all_procs
                    if p.get("customizationType") == "system"
                    and p.get("name", "").lower() == process_type.lower()
                ),
                None,
            )
            if not parent_proc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown process type '{process_type}'. "
                        f"Available system processes: "
                        + ", ".join(
                            p["name"] for p in all_procs
                            if p.get("customizationType") == "system"
                        )
                    ),
                )
            parent_proc_id = parent_proc["typeId"]

            # Skip creation if the inherited process already exists.
            custom_exists = any(
                p.get("name", "").lower() == custom_process_name.lower()
                and p.get("customizationType") != "system"
                for p in all_procs
            )
            if custom_exists:
                _logging.info(
                    "Custom process '%s' already exists — skipping creation.",
                    custom_process_name,
                )
            else:
                r_cp = client.post(
                    f"{ADO_BASE}/_apis/work/processes?api-version=7.1-preview.2",
                    auth=proc_auth,
                    json={
                        "name": custom_process_name,
                        "description": f"Custom {process_type} process for {project_name}",
                        "parentProcessTypeId": parent_proc_id,
                        "isDefault": False,
                        "isEnabled": True,
                    },
                )
                if r_cp.status_code in (200, 201):
                    _logging.info("Created custom process: %s", custom_process_name)
                else:
                    body_preview = r_cp.text[:400]
                    _logging.error(
                        "Custom process creation returned %s: %s",
                        r_cp.status_code, body_preview,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"Azure DevOps rejected the process creation "
                            f"(HTTP {r_cp.status_code}). "
                            "Ensure AZURE_DEVOPS_ADMIN_PAT has 'Process (Read & Manage)' scope. "
                            f"Details: {body_preview}"
                        ),
                    )

    except HTTPException:
        raise
    except httpx.RequestError as exc:
        _logging.error("Network error reaching Azure DevOps: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Azure DevOps. Check network connectivity and AZURE_DEVOPS_ORGANIZATION.",
        )

    # 5. Submit Terraform job
    try:
        job_id = submit_terraform_job(
            project_name=project_name,
            process_name=custom_process_name,
            ado_org=org,
            admin_username=admin_username,
            use_mock=False,
        )
    except ImportError as exc:
        _logging.error("Terraform runner unavailable (kubernetes package missing?): %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "The project provisioning service is not available in this environment. "
                "Ensure the backend image has been rebuilt with the latest requirements "
                "(google-cloud-storage, kubernetes) and that the K8s RBAC prerequisites "
                "from docs/SELF_SERVICE_TERRAFORM.md §7 have been applied."
            ),
        )
    except Exception as exc:
        _logging.error("Failed to submit Terraform job for '%s': %s", project_name, exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not start the provisioning job: {exc}. "
                "Check that kubectl apply -k infrastructure/k8s/base/ has been run, "
                "that the devops-terraform-sa and backend-sa ServiceAccounts exist, "
                "and that the backend pod has permission to create K8s Jobs "
                "(see docs/SELF_SERVICE_TERRAFORM.md §7)."
            ),
        )

    return {
        "success": True,
        "data": {"job_id": job_id, "status": "pending"},
        "timestamp": _now_iso(),
    }


@router.get("/projects/create/status/{job_id}")
def get_project_creation_status(
    job_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Poll the status of a Terraform project-creation job.

    Returns
    ───────
    data.status  : pending | running | succeeded | failed
    data.project_url : (on succeeded) Azure DevOps project URL
    data.error   : (on failed) user-facing error message

    On first 'succeeded' poll:
      • Assigns admin_username to the project's 'Project Administrators' group.
      • Saves Terraform logs to gs://devops-control-center-tfstate/terraform/logs/
    """
    result = get_job_status(job_id, use_mock=USE_MOCK)

    if result["status"] == "succeeded" and job_id not in _admin_assigned_jobs:
        _admin_assigned_jobs.add(job_id)
        # Best-effort admin assignment; never fails the response
        try:
            _assign_admin_post_terraform(job_id, current_user)
        except Exception as exc:
            _logging.warning("Post-Terraform admin assignment failed: %s", exc)
        try:
            save_logs_to_gcs(job_id, job_id)
        except Exception as exc:
            _logging.warning("GCS log save failed: %s", exc)

    if result["status"] == "failed":
        if job_id not in _admin_assigned_jobs:
            _admin_assigned_jobs.add(job_id)
            try:
                save_logs_to_gcs(job_id, job_id)
            except Exception:
                pass

    return {"success": True, "data": result, "timestamp": _now_iso()}


def _assign_admin_post_terraform(job_id: str, current_user: AuthUser) -> None:
    """
    After Terraform creates the project, add the admin user to the
    'Project Administrators' group via the ADO Graph API.
    Failures are tolerated (logged as warnings) so the UI still shows success.
    """
    if USE_MOCK:
        return

    if not ADO_BASE or ADO_BASE.rstrip("/") == "https://dev.azure.com":
        return

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)

    # Retrieve project metadata from the job annotations (K8s only)
    admin_username: Optional[str] = None
    project_name: Optional[str] = None
    try:
        from kubernetes import client as k8s, config as k8s_cfg  # type: ignore

        try:
            k8s_cfg.load_incluster_config()
        except Exception:
            k8s_cfg.load_kube_config()
        job_obj = k8s.BatchV1Api().read_namespaced_job(
            name=job_id, namespace="devops-control-center"
        )
        annotations = job_obj.metadata.annotations or {}
        project_name = annotations.get("devops-portal/project-name")
        admin_username = annotations.get("devops-portal/admin-username")
    except Exception as exc:
        _logging.debug("Could not read job annotations for admin assignment: %s", exc)
        return

    if not project_name:
        return

    with httpx.Client(auth=auth, timeout=20.0) as client:
        # Resolve user descriptor
        ru = client.get(
            f"{ADO_BASE}/_apis/graph/users?api-version=7.0"
            f"&$filter=principalName eq '{admin_username}'"
        )
        users = ru.json().get("value", []) if ru.is_success else []
        if not users:
            _logging.warning("Admin user '%s' not found for post-TF assignment.", admin_username)
            return
        user_desc = users[0].get("descriptor")

        # Resolve Project Administrators group descriptor
        groups_url = (
            f"{ADO_BASE}/_apis/graph/groups?api-version=7.0"
            f"&$filter=displayName eq '{project_name} Project Administrators'"
        )
        rg = client.get(groups_url)
        groups = rg.json().get("value", []) if rg.is_success else []
        if not groups:
            return
        group_desc = groups[0].get("descriptor")

        client.put(
            f"{ADO_BASE}/_apis/graph/memberships/{user_desc}/{group_desc}?api-version=7.0"
        )
        _logging.info("Assigned '%s' to Project Administrators for '%s'.", admin_username, project_name)


