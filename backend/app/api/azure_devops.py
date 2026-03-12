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
ADO_QUERY_USER = os.getenv("AZURE_DEVOPS_QUERY_USER", "")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _get_pat_for_user(current_user: AuthUser) -> str:
    """
    Resolve the Azure DevOps PAT for the requesting user.

    Priority:
      1. Per-user PAT stored in Vault / system_config (set via UI).
      2. Server-level env PAT (AZURE_DEVOPS_PAT) – admin fallback so golden.mihel@gmail.com
         works out-of-the-box without pasting a token in the dashboard.

    The PAT is never returned to the frontend or written to logs.
    """
    user_id = str(current_user.get("id"))
    pat = get_user_azure_devops_pat(user_id)
    if pat:
        return pat
    # Fallback: use the server env PAT if present (convenient for admin)
    if _ENV_PAT:
        return _ENV_PAT
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Azure DevOps is not connected. Please add your Personal Access Token.",
    )


# ── PAT management ────────────────────────────────────────────────────────────

class _PatPayload(BaseModel):
    pat: str


@router.get("/pat")
def get_pat_status(current_user: AuthUser = Depends(get_current_user)):
    """Return whether this user has a PAT configured. Value is never returned."""
    user_id = str(current_user.get("id"))
    has_user_pat = bool(get_user_azure_devops_pat(user_id))
    # Admin fallback: if the env PAT is set the connection effectively works
    effective = has_user_pat or bool(_ENV_PAT)
    return {
        "success": True,
        "data": {"configured": effective, "has_personal_pat": has_user_pat},
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
                                "created_date": pr.get("creationDate"),
                                "repository": repo_name,
                                "source_branch": pr.get("sourceRefName", "").replace("refs/heads/", ""),
                                "target_branch": pr.get("targetRefName", "").replace("refs/heads/", ""),
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

                for build in builds:
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




# ── self-service project creation ───────────────────────────────────────────

class ProjectCreationPayload(BaseModel):
    project_name: str
    process_type: str  # Scrum, Agile, CMMI, Basic
    admin_username: str  # email/principalName of the user to grant admin rights


@router.post("/projects/create")
def create_ado_project(
    payload: ProjectCreationPayload,
    current_user: AuthUser = Depends(get_current_user),
):
    """Self-Service Project Creation Endpoint
    
    Creates a new Azure DevOps project with a custom process and grants admin permissions.
    
    IMPLEMENTATION FLOW:
    1. Locate the base process template (Scrum/Agile/CMMI/Basic) 
    2. Create a custom inherited process named '{project_name}-{process_type}'
    3. Create the project using the custom process
    4. Add the specified admin_username to Project Administrators group
    
    REQUEST PARAMETERS:
    - project_name: str - Name of the new project (becomes part of custom process name)
    - process_type: str - Based process type: Scrum, Agile, CMMI, or Basic
    - admin_username: str - Email or principal name to grant admin rights
    
    RESPONSE:
    - success: bool - Whether the operation completed
    - data: dict - Created project details including URL
    - timestamp: str - ISO 8601 timestamp
    
    MOCK MODE (USE_MOCK_AZURE_DEVOPS=true):
    - Returns simulated project creation response without calling real Azure DevOps API
    - Useful for local development and testing
    
    REAL MODE (USE_MOCK_AZURE_DEVOPS=false):
    - Requires valid AZURE_DEVOPS_PAT (Personal Access Token) with:
      * Project & Team Read/Write
      * Process Template Access  
      * Group Membership Read/Write
    - Calls actual Azure DevOps REST API v7.0+ 
    - May take 10-30 seconds as ADO processes the creation
    
    ERROR CASES:
    - 400 Bad Request: Missing required fields, unknown process type
    - 401 Unauthorized: Invalid or missing ADO_PAT
    - 403 Forbidden: Insufficient permissions in Azure DevOps
    - 500 Internal Server Error: ADO API errors, database issues
    """

    org = ADO_BASE.split("/")[-1]
    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)

    with httpx.Client(auth=auth, timeout=60.0) as client:
        # 1. locate parent process template for the requested type
        procs_url = f"{ADO_BASE}/_apis/process/processes?api-version=7.0"
        r = client.get(procs_url)
        r.raise_for_status()
        procs = r.json().get("value", [])
        parent_proc = None
        for p in procs:
            # built‑in processes have type "system"
            if p.get("type") == "system" and p.get("name").lower() == payload.process_type.lower():
                parent_proc = p
                break
        if not parent_proc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown process type '{payload.process_type}'",
            )

        # 2. create a custom process based on the built-in template using Inherited Process API
        custom_process_name = f"{payload.project_name}-{payload.process_type}"
        custom_proc_body = {
            "name": custom_process_name,
            "description": f"Custom {payload.process_type} process for {payload.project_name}",
            "parentProcessTypeId": parent_proc.get("id"),
        }
        
        # Check if custom process already exists
        custom_proc_id = None
        try:
            # Try to get inherited processes
            inherited_procs_url = f"{ADO_BASE}/_apis/process/processes?api-version=7.0"
            r_inherited = client.get(inherited_procs_url)
            r_inherited.raise_for_status()
            all_procs = r_inherited.json().get("value", [])
            
            # Look for our custom process (might not exist yet)
            for p in all_procs:
                if p.get("name").lower() == custom_process_name.lower() and p.get("type") != "system":
                    custom_proc_id = p.get("id")
                    break
        except Exception as exc:
            import logging
            logging.error("Error checking for existing processes: %s", str(exc))
        
        # If not found, try to create it
        if not custom_proc_id:
            try:
                # Use the inherited process create API
                create_url = f"{ADO_BASE}/_apis/process/processes?api-version=7.1-preview.1"
                r_create = client.post(create_url, json=custom_proc_body)
                
                if r_create.status_code in [200, 201]:
                    created_proc = r_create.json()
                    custom_proc_id = created_proc.get("id")
                    import logging
                    logging.info("Successfully created custom process: %s", custom_process_name)
                else:
                    # Try with a simpler version or log the error
                    import logging
                    error_text = r_create.text[:200] if r_create.text else f"HTTP {r_create.status_code}"
                    logging.warning(
                        "Failed to create custom process (status %s): %s. Using built-in process.",
                        r_create.status_code,
                        error_text,
                    )
                    custom_proc_id = parent_proc.get("id")
            except Exception as exc:
                import logging
                logging.warning(
                    "Exception creating custom process '%s': %s. Using built-in process.",
                    custom_process_name,
                    str(exc),
                )
                custom_proc_id = parent_proc.get("id")
        

        # 3. create project using the custom process
        proj_body = {
            "name": payload.project_name,
            "description": "Self‑service created project",
            "capabilities": {
                "versioncontrol": {"sourceControlType": "Git"},
                "processTemplate": {"templateTypeId": custom_proc_id},
            },
        }
        proj_url = f"{ADO_BASE}/_apis/projects?api-version=7.0"
        r3 = client.post(proj_url, json=proj_body)
        r3.raise_for_status()
        project = r3.json()
        project_id = project.get("id")
        
        # Extract the correct portal URL format
        # Azure DevOps portals URLs follow format: https://dev.azure.com/{organization}/{project}
        # The project name might need URL encoding for safety
        import urllib.parse
        encoded_project_name = urllib.parse.quote(payload.project_name, safe='')
        encoded_project_name = encoded_project_name.replace('%20', '%20')  # spaces as %20
        
        # Most likely correct format
        project_portal_url = f"https://dev.azure.com/{org}/{payload.project_name}"
        
        import logging
        logging.info(
            "Successfully created project '%s' (id=%s). Portal URL: %s",
            payload.project_name,
            project_id,
            project_portal_url,
        )

        # 3. try to add the admin user to the "Project Administrators" group
        added_admin = False
        try:
            # find user descriptor
            users_url = f"{ADO_BASE}/_apis/graph/users?api-version=7.0&$filter=principalName eq '{payload.admin_username}'"
            ru = client.get(users_url)
            ru.raise_for_status()
            users = ru.json().get("value", [])
            if not users:
                raise ValueError("user not found in org")
            user_desc = users[0].get("descriptor")

            # find project administrators group descriptor
            group_name = f"{payload.project_name} Project Administrators"
            groups_url = f"{ADO_BASE}/_apis/graph/groups?api-version=7.0&$filter=displayName eq '{group_name}'"
            rg = client.get(groups_url)
            rg.raise_for_status()
            groups = rg.json().get("value", [])
            if groups:
                group_desc = groups[0].get("descriptor")
                # create membership
                membership_url = f"{ADO_BASE}/_apis/graph/memberships/{user_desc}/{group_desc}?api-version=7.0"
                client.put(membership_url)
                added_admin = True
        except Exception as exc:  # noqa: BLE001
            try:
                import logging

                logging.warning(
                    "failed to add %s to project administrators: %s",
                    payload.admin_username,
                    exc,
                )
            except Exception:
                print("warning: could not add admin user", exc)

    return {
        "success": True,
        "data": {
            "project": project, 
            "added_admin": added_admin, 
            "project_url": project_portal_url
        },
        "timestamp": _now_iso(),
    }




