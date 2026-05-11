"""
AI Chat Bot ("DevBot") — in-portal Azure DevOps assistant.

This module is the in-portal implementation of the chat-agent described in
``docs/chat-agent-architecture.md``. Unlike the original design, it is **not**
a separate microservice — it lives inside the existing FastAPI portal and
shares the same Postgres, Redis, JWT auth, and Azure DevOps credentials as
the rest of the app.

Scope (intentional):
    * Tools are implemented for Azure DevOps only. The tool registry is
      structured so additional systems (SonarQube, Artifactory, Confluence)
      can be plugged in later without rewriting the orchestrator.
    * The behaviour and tool schemas mimic the public ``mcp-azure-devops``
      server (list projects / repos / work items / pipelines / PRs, get
      work item, etc.) so users get a familiar surface.

Auth + credentials:
    * Every request requires the standard portal ``auth_token`` JWT cookie /
      Bearer header (same as every other ``/api`` route).
    * Azure DevOps base URL comes from ``AZURE_DEVOPS_BASE_URL`` (env), the
      same value used everywhere else in the portal.
    * Azure DevOps PAT is the per-user PAT already stored via
      ``secrets_manager.get_user_azure_devops_pat`` (set on the Azure DevOps
      page). If the user has not connected their PAT the agent short-
      circuits with a friendly prompt — it never calls the LLM in that
      state.

LLM backend:
    * Any OpenAI-compatible ``/v1/chat/completions`` endpoint with
      function-calling support. Configured via ``LLM_BASE_URL``,
      ``LLM_MODEL`` and (optional) ``LLM_API_KEY``.
    * If ``LLM_BASE_URL`` is not configured the agent runs in
      ``stub`` mode: it does not call any LLM and instead replies with a
      polite "AI backend is not configured" message. This keeps the
      feature deployable as a UI scaffold even on environments that have
      not yet provisioned an LLM gateway.

Sessions:
    * Conversation history is stored in Redis under
      ``chat:{user_id}:{session_id}`` with a 24-hour TTL refreshed on every
      turn. If Redis is unavailable the agent degrades to a single-turn
      stateless mode (each message is independent).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from redis_client import get_redis
from secrets_manager import get_user_azure_devops_pat
from security import AuthUser, get_current_user


router = APIRouter()
log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────────

_SESSION_TTL_SECONDS = 24 * 60 * 60
_SESSION_HISTORY_LIMIT = 40
_MAX_TOOL_ITERATIONS = 6
_DEFAULT_LLM_MODEL = "gpt-4o-mini"
_DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
_ADO_API_VERSION = "7.0"

_ADO_KEYWORDS = (
    "azure devops", "ado", "pipeline", "work item", "pull request",
    "sprint", "wiql", "repository", "repositories", "branch", "build",
)


# ─── LLM configuration ────────────────────────────────────────────────────────

def _llm_base_url() -> str:
    return (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")


def _llm_model() -> str:
    return (os.getenv("LLM_MODEL") or _DEFAULT_LLM_MODEL).strip()


def _llm_api_key() -> str:
    return (os.getenv("LLM_API_KEY") or "").strip()


def _llm_is_configured() -> bool:
    # The chart ships with a default base URL + model, so the only real
    # signal for "the LLM gateway is ready to use" is whether the API key
    # has been provisioned. When it is empty the orchestrator falls back
    # to the stub reply rather than making an unauthenticated upstream
    # call that would return 401.
    return bool(_llm_base_url()) and bool(_llm_api_key())


# ─── ADO helpers (mirroring api/azure_devops.py conventions) ──────────────────

def _ado_base() -> str:
    return (os.getenv("AZURE_DEVOPS_BASE_URL") or "").strip().rstrip("/")


def _is_devops_cloud_root(base: str) -> bool:
    parsed = urlparse(base)
    return parsed.netloc.lower() == "dev.azure.com" and not parsed.path.strip("/")


def _discover_ado_bases(client: httpx.Client) -> List[str]:
    """Resolve org base URLs reachable with the current PAT.

    Same logic as ``api.azure_devops._discover_ado_bases`` so the chat agent
    sees exactly the same scope the rest of the portal does.
    """
    base = _ado_base()
    if not base:
        raise RuntimeError("AZURE_DEVOPS_BASE_URL is not configured.")
    if not _is_devops_cloud_root(base):
        return [base]
    try:
        profile = client.get("https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1")
        profile.raise_for_status()
        member_id = profile.json().get("id")
        if not member_id:
            raise ValueError("profile id missing")
        accounts = client.get(
            f"https://app.vssps.visualstudio.com/_apis/accounts?memberId={member_id}&api-version=7.1"
        )
        accounts.raise_for_status()
        names = [
            str(account.get("accountName") or "").strip()
            for account in accounts.json().get("value", [])
            if account.get("accountName")
        ]
    except Exception as exc:
        raise RuntimeError(
            "Could not discover Azure DevOps organisations for this PAT."
        ) from exc
    bases = [f"https://dev.azure.com/{name}" for name in names]
    if not bases:
        raise RuntimeError("No Azure DevOps organisations are accessible with this PAT.")
    return bases


def _ado_client(pat: str) -> httpx.Client:
    return httpx.Client(auth=httpx.BasicAuth("", pat), timeout=20.0)


# ─── Tool implementations (Azure DevOps) ──────────────────────────────────────
# Each tool returns a JSON-serialisable dict. Failures are returned as
# ``{"error": "..."}`` rather than raised — the LLM should see them as
# regular tool output and adapt, not crash the whole turn.


def _tool_error(message: str) -> Dict[str, Any]:
    return {"error": message}


def _safe_get(d: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def tool_list_projects(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """List all Azure DevOps projects the PAT can see."""
    try:
        with _ado_client(pat) as client:
            projects: List[Dict[str, Any]] = []
            for base_url in _discover_ado_bases(client):
                r = client.get(f"{base_url}/_apis/projects?$top=200&api-version={_ADO_API_VERSION}")
                r.raise_for_status()
                for p in r.json().get("value", []):
                    projects.append({
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "description": p.get("description") or "",
                        "url": f"{base_url}/{quote(p.get('name') or '')}",
                    })
        return {"projects": projects, "count": len(projects)}
    except Exception as exc:
        return _tool_error(f"Failed to list projects: {exc}")


def tool_list_repositories(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """List git repositories in a given project."""
    project = (args.get("project") or "").strip()
    if not project:
        return _tool_error("Argument 'project' is required.")
    try:
        with _ado_client(pat) as client:
            for base_url in _discover_ado_bases(client):
                url = f"{base_url}/{quote(project)}/_apis/git/repositories?api-version={_ADO_API_VERSION}"
                r = client.get(url)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                repos = [
                    {
                        "id": repo.get("id"),
                        "name": repo.get("name"),
                        "default_branch": repo.get("defaultBranch") or "",
                        "web_url": repo.get("webUrl") or "",
                    }
                    for repo in r.json().get("value", [])
                ]
                return {"project": project, "repositories": repos, "count": len(repos)}
        return _tool_error(f"Project '{project}' was not found in accessible organisations.")
    except Exception as exc:
        return _tool_error(f"Failed to list repositories: {exc}")


def tool_list_work_items(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """Run a WIQL query and return matching work items.

    Mimics the ``list_work_items`` tool from mcp-azure-devops: callers may
    pass a raw ``wiql`` query, or a structured combination of ``project``,
    ``assigned_to`` (use ``"@Me"`` for the PAT owner), ``state`` and
    ``work_item_type``.
    """
    wiql = (args.get("wiql") or "").strip()
    project = (args.get("project") or "").strip()
    assigned_to = (args.get("assigned_to") or "").strip()
    state = (args.get("state") or "").strip()
    work_item_type = (args.get("work_item_type") or "").strip()
    top = int(args.get("top") or 50)
    top = max(1, min(top, 200))

    if not wiql:
        clauses = []
        if project:
            safe = project.replace("'", "''")
            clauses.append(f"[System.TeamProject] = '{safe}'")
        if assigned_to:
            if assigned_to.lower() in ("@me", "me"):
                clauses.append("[System.AssignedTo] = @Me")
            else:
                safe = assigned_to.replace("'", "''")
                clauses.append(f"[System.AssignedTo] = '{safe}'")
        if state:
            safe = state.replace("'", "''")
            clauses.append(f"[System.State] = '{safe}'")
        if work_item_type:
            safe = work_item_type.replace("'", "''")
            clauses.append(f"[System.WorkItemType] = '{safe}'")
        where = (" AND ".join(clauses)) if clauses else "[System.AssignedTo] = @Me"
        wiql = (
            "SELECT [System.Id], [System.Title], [System.State], "
            "[System.WorkItemType], [System.TeamProject] "
            f"FROM WorkItems WHERE {where} "
            "ORDER BY [System.ChangedDate] DESC"
        )

    try:
        with _ado_client(pat) as client:
            items: List[Dict[str, Any]] = []
            for base_url in _discover_ado_bases(client):
                wiql_url = f"{base_url}/_apis/wit/wiql?api-version={_ADO_API_VERSION}&$top={top}"
                r = client.post(wiql_url, json={"query": wiql})
                if r.status_code in (401, 403, 404):
                    continue
                r.raise_for_status()
                ids = [str(it["id"]) for it in r.json().get("workItems", [])][:top]
                if not ids:
                    continue
                chunk = ",".join(ids[:200])
                r2 = client.get(
                    f"{base_url}/_apis/wit/workitems?ids={chunk}"
                    "&fields=System.Id,System.Title,System.State,System.WorkItemType,"
                    "System.TeamProject,System.AssignedTo,System.CreatedDate,System.ChangedDate"
                    f"&api-version={_ADO_API_VERSION}"
                )
                r2.raise_for_status()
                for it in r2.json().get("value", []):
                    fields = it.get("fields", {})
                    project_name = fields.get("System.TeamProject") or ""
                    items.append({
                        "id": it.get("id"),
                        "title": fields.get("System.Title"),
                        "state": fields.get("System.State"),
                        "type": fields.get("System.WorkItemType"),
                        "project": project_name,
                        "assigned_to": _safe_get(fields, "System.AssignedTo.displayName")
                        or fields.get("System.AssignedTo"),
                        "url": (
                            f"{base_url}/{quote(project_name)}/_workitems/edit/{it.get('id')}"
                            if project_name
                            else f"{base_url}/_workitems/edit/{it.get('id')}"
                        ),
                    })
                if items:
                    break
        return {"work_items": items, "count": len(items), "wiql": wiql}
    except Exception as exc:
        return _tool_error(f"Failed to query work items: {exc}")


def tool_get_work_item(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """Return full detail for a specific work item id."""
    try:
        wid = int(args.get("id") or args.get("work_item_id") or 0)
    except (TypeError, ValueError):
        return _tool_error("Argument 'id' must be an integer.")
    if wid <= 0:
        return _tool_error("Argument 'id' is required.")
    try:
        with _ado_client(pat) as client:
            for base_url in _discover_ado_bases(client):
                url = f"{base_url}/_apis/wit/workitems/{wid}?$expand=all&api-version={_ADO_API_VERSION}"
                r = client.get(url)
                if r.status_code in (401, 403, 404):
                    continue
                r.raise_for_status()
                data = r.json()
                fields = data.get("fields", {})
                project_name = fields.get("System.TeamProject") or ""
                return {
                    "id": data.get("id"),
                    "title": fields.get("System.Title"),
                    "state": fields.get("System.State"),
                    "type": fields.get("System.WorkItemType"),
                    "project": project_name,
                    "assigned_to": _safe_get(fields, "System.AssignedTo.displayName")
                    or fields.get("System.AssignedTo"),
                    "created_by": _safe_get(fields, "System.CreatedBy.displayName")
                    or fields.get("System.CreatedBy"),
                    "created_date": fields.get("System.CreatedDate"),
                    "changed_date": fields.get("System.ChangedDate"),
                    "description": fields.get("System.Description") or "",
                    "tags": fields.get("System.Tags") or "",
                    "url": (
                        f"{base_url}/{quote(project_name)}/_workitems/edit/{wid}"
                        if project_name
                        else f"{base_url}/_workitems/edit/{wid}"
                    ),
                }
        return _tool_error(f"Work item {wid} was not found in any accessible organisation.")
    except Exception as exc:
        return _tool_error(f"Failed to fetch work item: {exc}")


def tool_list_pipelines(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """List pipeline definitions in a project."""
    project = (args.get("project") or "").strip()
    if not project:
        return _tool_error("Argument 'project' is required.")
    try:
        with _ado_client(pat) as client:
            for base_url in _discover_ado_bases(client):
                url = (
                    f"{base_url}/{quote(project)}/_apis/pipelines"
                    f"?api-version={_ADO_API_VERSION}-preview.1&$top=200"
                )
                r = client.get(url)
                if r.status_code in (401, 403, 404):
                    continue
                r.raise_for_status()
                pipelines = [
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "folder": p.get("folder") or "",
                        "url": p.get("_links", {}).get("web", {}).get("href") or "",
                    }
                    for p in r.json().get("value", [])
                ]
                return {"project": project, "pipelines": pipelines, "count": len(pipelines)}
        return _tool_error(f"Project '{project}' was not found in accessible organisations.")
    except Exception as exc:
        return _tool_error(f"Failed to list pipelines: {exc}")


def tool_get_pipeline_runs(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """Return recent runs of a pipeline."""
    project = (args.get("project") or "").strip()
    if not project:
        return _tool_error("Argument 'project' is required.")
    try:
        pipeline_id = int(args.get("pipeline_id") or 0)
    except (TypeError, ValueError):
        return _tool_error("Argument 'pipeline_id' must be an integer.")
    if pipeline_id <= 0:
        return _tool_error("Argument 'pipeline_id' is required.")
    top = max(1, min(int(args.get("top") or 10), 50))
    try:
        with _ado_client(pat) as client:
            for base_url in _discover_ado_bases(client):
                url = (
                    f"{base_url}/{quote(project)}/_apis/pipelines/{pipeline_id}/runs"
                    f"?api-version={_ADO_API_VERSION}-preview.1"
                )
                r = client.get(url)
                if r.status_code in (401, 403, 404):
                    continue
                r.raise_for_status()
                runs = []
                for run in (r.json().get("value", []) or [])[:top]:
                    runs.append({
                        "id": run.get("id"),
                        "name": run.get("name"),
                        "state": run.get("state"),
                        "result": run.get("result"),
                        "created_date": run.get("createdDate"),
                        "finished_date": run.get("finishedDate"),
                        "url": run.get("_links", {}).get("web", {}).get("href") or "",
                    })
                return {
                    "project": project,
                    "pipeline_id": pipeline_id,
                    "runs": runs,
                    "count": len(runs),
                }
        return _tool_error(
            f"Pipeline {pipeline_id} in project '{project}' was not found in accessible organisations."
        )
    except Exception as exc:
        return _tool_error(f"Failed to fetch pipeline runs: {exc}")


def tool_list_pull_requests(args: Dict[str, Any], pat: str) -> Dict[str, Any]:
    """List pull requests in a repo, filtered by status (default: active)."""
    project = (args.get("project") or "").strip()
    repository = (args.get("repository") or "").strip()
    status_filter = (args.get("status") or "active").strip().lower()
    if status_filter not in ("active", "abandoned", "completed", "all"):
        status_filter = "active"
    if not project or not repository:
        return _tool_error("Arguments 'project' and 'repository' are required.")
    try:
        with _ado_client(pat) as client:
            for base_url in _discover_ado_bases(client):
                url = (
                    f"{base_url}/{quote(project)}/_apis/git/repositories/{quote(repository)}"
                    f"/pullrequests?searchCriteria.status={status_filter}&api-version={_ADO_API_VERSION}"
                )
                r = client.get(url)
                if r.status_code in (401, 403, 404):
                    continue
                r.raise_for_status()
                prs = []
                for pr in r.json().get("value", []) or []:
                    prs.append({
                        "id": pr.get("pullRequestId"),
                        "title": pr.get("title"),
                        "status": pr.get("status"),
                        "created_by": _safe_get(pr, "createdBy.displayName"),
                        "creation_date": pr.get("creationDate"),
                        "source_branch": (pr.get("sourceRefName") or "").replace("refs/heads/", ""),
                        "target_branch": (pr.get("targetRefName") or "").replace("refs/heads/", ""),
                        "url": (
                            f"{base_url}/{quote(project)}/_git/{quote(repository)}"
                            f"/pullrequest/{pr.get('pullRequestId')}"
                        ),
                    })
                return {
                    "project": project,
                    "repository": repository,
                    "pull_requests": prs,
                    "count": len(prs),
                }
        return _tool_error(
            f"Repository '{project}/{repository}' was not found in accessible organisations."
        )
    except Exception as exc:
        return _tool_error(f"Failed to list pull requests: {exc}")


# ─── Tool registry ────────────────────────────────────────────────────────────
# (name, OpenAI tool schema, implementation). Adding a new tool means adding a
# tuple here — the orchestrator picks it up automatically.

ToolFn = Callable[[Dict[str, Any], str], Dict[str, Any]]

_AZURE_DEVOPS_TOOLS: List[Tuple[str, Dict[str, Any], ToolFn]] = [
    (
        "ado_list_projects",
        {
            "type": "function",
            "function": {
                "name": "ado_list_projects",
                "description": "List all Azure DevOps projects accessible to the current user.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        tool_list_projects,
    ),
    (
        "ado_list_repositories",
        {
            "type": "function",
            "function": {
                "name": "ado_list_repositories",
                "description": "List git repositories inside an Azure DevOps project.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name."},
                    },
                    "required": ["project"],
                    "additionalProperties": False,
                },
            },
        },
        tool_list_repositories,
    ),
    (
        "ado_list_work_items",
        {
            "type": "function",
            "function": {
                "name": "ado_list_work_items",
                "description": (
                    "Search Azure DevOps work items. Either pass a raw WIQL query via "
                    "'wiql', or pass structured filters (project, assigned_to, state, "
                    "work_item_type). Use assigned_to='@Me' for the PAT owner."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "wiql": {"type": "string", "description": "Raw WIQL query (optional)."},
                        "project": {"type": "string"},
                        "assigned_to": {"type": "string", "description": "User email or '@Me'."},
                        "state": {"type": "string"},
                        "work_item_type": {"type": "string"},
                        "top": {"type": "integer", "description": "Max items (default 50, capped at 200)."},
                    },
                    "additionalProperties": False,
                },
            },
        },
        tool_list_work_items,
    ),
    (
        "ado_get_work_item",
        {
            "type": "function",
            "function": {
                "name": "ado_get_work_item",
                "description": "Fetch a single Azure DevOps work item by id with full fields.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer", "description": "Work item id."}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        tool_get_work_item,
    ),
    (
        "ado_list_pipelines",
        {
            "type": "function",
            "function": {
                "name": "ado_list_pipelines",
                "description": "List pipeline definitions in an Azure DevOps project.",
                "parameters": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                    "required": ["project"],
                    "additionalProperties": False,
                },
            },
        },
        tool_list_pipelines,
    ),
    (
        "ado_get_pipeline_runs",
        {
            "type": "function",
            "function": {
                "name": "ado_get_pipeline_runs",
                "description": "Return recent runs of a specific Azure DevOps pipeline.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "pipeline_id": {"type": "integer"},
                        "top": {"type": "integer", "description": "Max runs (default 10, capped at 50)."},
                    },
                    "required": ["project", "pipeline_id"],
                    "additionalProperties": False,
                },
            },
        },
        tool_get_pipeline_runs,
    ),
    (
        "ado_list_pull_requests",
        {
            "type": "function",
            "function": {
                "name": "ado_list_pull_requests",
                "description": "List pull requests in an Azure DevOps git repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "repository": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["active", "abandoned", "completed", "all"],
                            "description": "PR status filter (default 'active').",
                        },
                    },
                    "required": ["project", "repository"],
                    "additionalProperties": False,
                },
            },
        },
        tool_list_pull_requests,
    ),
]


def _tool_registry() -> Dict[str, Tuple[Dict[str, Any], ToolFn]]:
    return {name: (schema, fn) for name, schema, fn in _AZURE_DEVOPS_TOOLS}


# ─── Session store (Redis) ────────────────────────────────────────────────────

def _session_key(user_id: str, session_id: str) -> str:
    return f"chat:{user_id}:{session_id}"


def _load_history(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    try:
        raw = get_redis().get(_session_key(user_id, session_id))
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(user_id: str, session_id: str, history: List[Dict[str, Any]]) -> None:
    # Trim aggressively so a single session can't balloon Redis memory.
    if len(history) > _SESSION_HISTORY_LIMIT:
        history = history[-_SESSION_HISTORY_LIMIT:]
    try:
        get_redis().setex(
            _session_key(user_id, session_id),
            _SESSION_TTL_SECONDS,
            json.dumps(history, default=str),
        )
    except Exception:
        pass


def _clear_history(user_id: str, session_id: str) -> None:
    try:
        get_redis().delete(_session_key(user_id, session_id))
    except Exception:
        pass


# ─── Missing-token guards ─────────────────────────────────────────────────────

def _ado_is_connected(user_id: str) -> bool:
    try:
        return bool(get_user_azure_devops_pat(user_id))
    except Exception:
        return False


def _mentions_ado(message: str) -> bool:
    needle = message.lower()
    return any(kw in needle for kw in _ADO_KEYWORDS)


# ─── Orchestrator ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """You are DevBot, an AI assistant built into DevOps Hub.
You help users investigate their Azure DevOps projects, work items, pipelines,
and pull requests by calling the provided tools.

Rules:
- Always call tools to fetch live data — never guess project names, work item
  ids, or pipeline results.
- Be concise. Use bullet points and short tables where they aid readability.
- Use Markdown for formatting; do not output HTML.
- Surface direct links (web_url, url) when the user might want to click through.
- If a tool returns an 'error' field, summarise the error to the user instead
  of retrying blindly.
- This deployment only supports Azure DevOps tools today. If the user asks
  about Artifactory, SonarQube, Confluence, or any other system, explain that
  these are coming soon and that you can only help with Azure DevOps for now.

User: {display_name}
"""


def _build_system_prompt(current_user: AuthUser) -> str:
    name = (
        current_user.get("display_name")
        or current_user.get("username")
        or current_user.get("email")
        or "User"
    )
    return _SYSTEM_PROMPT_TEMPLATE.format(display_name=name)


def _call_llm(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One blocking call to the OpenAI-compatible chat completions endpoint."""
    url = f"{_llm_base_url()}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = _llm_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: Dict[str, Any] = {
        "model": _llm_model(),
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    with httpx.Client(timeout=_DEFAULT_LLM_TIMEOUT_SECONDS) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def _run_orchestrator(
    user_message: str,
    history: List[Dict[str, Any]],
    current_user: AuthUser,
    pat: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run the tool-calling loop and return (final_text, updated_history)."""
    registry = _tool_registry()
    tools_schema = [schema for _, schema, _ in _AZURE_DEVOPS_TOOLS]

    messages: List[Dict[str, Any]] = [{"role": "system", "content": _build_system_prompt(current_user)}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    final_text = ""
    for _ in range(_MAX_TOOL_ITERATIONS):
        try:
            response = _call_llm(messages, tools_schema)
        except httpx.HTTPStatusError as exc:
            log.warning("LLM HTTP error %s: %s", exc.response.status_code, exc.response.text[:200])
            final_text = f"The AI backend returned an error ({exc.response.status_code}). Please try again later."
            break
        except Exception as exc:
            log.exception("LLM call failed: %s", exc)
            final_text = "The AI backend is currently unreachable. Please try again in a moment."
            break

        choice = (response.get("choices") or [{}])[0]
        assistant_msg = choice.get("message") or {}
        tool_calls = assistant_msg.get("tool_calls") or []
        # Mirror the assistant turn back into the message list verbatim so
        # the LLM has its own tool_calls visible on the next iteration.
        echoed: Dict[str, Any] = {
            "role": "assistant",
            "content": assistant_msg.get("content"),
        }
        if tool_calls:
            echoed["tool_calls"] = tool_calls
        messages.append(echoed)

        if not tool_calls:
            final_text = assistant_msg.get("content") or ""
            break

        for call in tool_calls:
            call_id = call.get("id") or str(uuid.uuid4())
            fn_meta = call.get("function") or {}
            name = fn_meta.get("name") or ""
            raw_args = fn_meta.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception:
                args = {}
            entry = registry.get(name)
            if not entry:
                tool_result: Dict[str, Any] = {"error": f"Unknown tool '{name}'."}
            else:
                _, fn = entry
                started = time.time()
                try:
                    tool_result = fn(args, pat)
                except Exception as exc:
                    log.exception("Tool %s crashed: %s", name, exc)
                    tool_result = {"error": f"Tool '{name}' failed: {exc}"}
                log.debug("Tool %s ran in %.2fs", name, time.time() - started)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(tool_result, default=str)[:8000],
            })
    else:
        # Hit iteration cap without a plain assistant response.
        final_text = (
            final_text
            or "I was not able to settle on an answer after several tool calls. "
            "Please rephrase your question."
        )

    # Persist only the user + final assistant turn in the public history;
    # the intermediate tool plumbing is reconstructed from system prompt on
    # the next request. Keeps the history dump readable in the UI.
    public_history = list(history) + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": final_text},
    ]
    return final_text, public_history


# ─── Request / response models ────────────────────────────────────────────────

class ChatMessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = None


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/health")
def chat_health(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Light-weight readiness signal for the chat feature."""
    return {
        "success": True,
        "data": {
            "llm_configured": _llm_is_configured(),
            "ado_connected": _ado_is_connected(str(current_user.get("id"))),
            "ado_base_url_configured": bool(_ado_base()),
        },
    }


@router.get("/connected-systems")
def chat_connected_systems(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Which integrations the user can use through the chat agent."""
    return {
        "success": True,
        "data": {
            "azure_devops": _ado_is_connected(str(current_user.get("id"))),
            # The portal currently only ships ADO chat tools. Leaving the
            # other flags in the response so the frontend status dots can be
            # rendered uniformly and lit up later without a contract change.
            "artifactory": False,
            "sonarqube": False,
            "confluence": False,
        },
    }


@router.post("/message")
def chat_message(
    body: ChatMessageBody = Body(...),
    current_user: AuthUser = Depends(get_current_user),
) -> JSONResponse:
    """Send a chat message and receive the full assistant reply.

    Returns JSON (not SSE) — the streaming variant lives at ``/stream``.
    Both share the same orchestration path; this one waits for the final
    text and returns it in one shot.
    """
    user_id = str(current_user.get("id"))
    session_id = (body.session_id or "").strip() or str(uuid.uuid4())
    user_message = body.message.strip()

    # Tier 1 — no PAT at all and the question is about ADO: short-circuit.
    if not _ado_is_connected(user_id):
        if _mentions_ado(user_message):
            reply = (
                "Your Azure DevOps account is not connected. "
                "Open the Azure DevOps page in the portal and paste your Personal Access Token "
                "to enable this assistant."
            )
            history = _load_history(user_id, session_id) + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ]
            _save_history(user_id, session_id, history)
            return JSONResponse({
                "success": True,
                "data": {
                    "session_id": session_id,
                    "reply": reply,
                    "tools_available": False,
                },
            })

    if not _llm_is_configured():
        reply = (
            "The chat assistant is not yet configured for this environment "
            "(LLM API key is missing). Once the platform team sets the "
            "LLM_API_KEY secret I'll be able to answer questions about "
            "Azure DevOps directly."
        )
        history = _load_history(user_id, session_id) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": reply},
        ]
        _save_history(user_id, session_id, history)
        return JSONResponse({
            "success": True,
            "data": {
                "session_id": session_id,
                "reply": reply,
                "tools_available": _ado_is_connected(user_id),
            },
        })

    pat = ""
    try:
        pat = get_user_azure_devops_pat(user_id) or ""
    except Exception:
        pat = ""

    history = _load_history(user_id, session_id)
    reply, new_history = _run_orchestrator(user_message, history, current_user, pat)
    _save_history(user_id, session_id, new_history)
    return JSONResponse({
        "success": True,
        "data": {
            "session_id": session_id,
            "reply": reply,
            "tools_available": bool(pat),
        },
    })


@router.get("/sessions/{session_id}/history")
def chat_session_history(
    session_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the full message history for a session (no system prompt / tool plumbing)."""
    user_id = str(current_user.get("id"))
    history = _load_history(user_id, session_id)
    return {
        "success": True,
        "data": {"session_id": session_id, "messages": history},
    }


@router.delete("/sessions/{session_id}", status_code=204)
def chat_session_clear(
    session_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> None:
    """Forget a session's chat history."""
    _clear_history(str(current_user.get("id")), session_id)
    return None
