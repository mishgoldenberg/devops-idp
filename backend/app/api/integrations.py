from __future__ import annotations

import os
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx

from resilient_http import tls_verify
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from db import execute, query_all, query_one
from security import AuthUser, get_current_user
from sso_config import decrypt_secret, encrypt_secret


router = APIRouter()

SystemName = Literal["azure", "sonarqube", "artifactory", "confluence"]
_TOKEN_SYSTEMS = {"azure", "sonarqube", "artifactory", "confluence"}
_PIN_SYSTEMS = {"sonarqube", "artifactory"}


class TokenBody(BaseModel):
    token: str = Field(..., min_length=1)


class PinBody(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=255)
    item_name: str = Field("", max_length=512)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _user_id(user: AuthUser) -> str:
    uid = str(user.get("id") or "").strip()
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User id missing")
    return uid


def _normalize_system(system: str, allowed: set[str]) -> str:
    name = (system or "").strip().lower()
    if name not in allowed:
        raise HTTPException(status_code=404, detail="Unsupported integration")
    return name


def _base_url(system: str) -> str:
    if system == "sonarqube":
        url = os.getenv("SONARQUBE_BASE_URL", "").strip().rstrip("/")
    elif system == "artifactory":
        url = os.getenv("ARTIFACTORY_BASE_URL", "").strip().rstrip("/")
    elif system == "confluence":
        url = os.getenv("CONFLUENCE_BASE_URL", "").strip().rstrip("/")
    else:
        url = ""
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{system.title()} URL is not configured",
        )
    # Tolerate base URLs configured without a scheme (closed-network secrets
    # are often set as "host/path"); httpx raises UnsupportedProtocol otherwise.
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _get_token(user_id: str, system: str) -> Optional[str]:
    row = query_one(
        """
        SELECT token_encrypted
        FROM user_integrations
        WHERE user_id = %s AND system = %s
        """,
        [user_id, system],
    )
    if not row:
        return None
    try:
        return decrypt_secret(str(row["token_encrypted"]))
    except ValueError:
        return None


def _require_token(current_user: AuthUser, system: str) -> str:
    token = _get_token(_user_id(current_user), system)
    if token:
        return token
    raise HTTPException(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        detail=f"{system.title()} is not connected. Please add your token.",
    )


def _pin_ids(user_id: str, system: str) -> set[str]:
    rows = query_all(
        "SELECT item_id FROM user_pins WHERE user_id = %s AND system = %s",
        [user_id, system],
    )
    return {str(row["item_id"]) for row in rows}


def _with_pin_flags(items: List[Dict[str, Any]], user_id: str, system: str, id_key: str) -> List[Dict[str, Any]]:
    pinned = _pin_ids(user_id, system)
    for item in items:
        item_id = str(item.get(id_key) or item.get("key") or item.get("name") or "")
        item["pinned"] = item_id in pinned
    return sorted(items, key=lambda item: (not bool(item.get("pinned")), str(item.get("name") or "").lower()))


@router.get("/{system}/token")
def get_token_status(system: str, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    name = _normalize_system(system, _TOKEN_SYSTEMS)
    configured = bool(_get_token(_user_id(current_user), name))
    return {"success": True, "data": {"configured": configured}, "timestamp": _now_iso()}


@router.post("/{system}/token", status_code=204)
def save_token(
    system: str,
    body: TokenBody,
    current_user: AuthUser = Depends(get_current_user),
) -> Response:
    name = _normalize_system(system, _TOKEN_SYSTEMS)
    raw = (body.token or "").strip()
    if len(raw) < 4:
        raise HTTPException(status_code=400, detail="Token is too short")
    if name in {"sonarqube", "artifactory", "confluence"}:
        _test_token(name, raw)
    execute(
        """
        INSERT INTO user_integrations (user_id, system, token_encrypted)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, system) DO UPDATE SET
          token_encrypted = EXCLUDED.token_encrypted,
          updated_at = CURRENT_TIMESTAMP
        """,
        [_user_id(current_user), name, encrypt_secret(raw)],
    )
    return Response(status_code=204)


@router.delete("/{system}/token", status_code=204)
def delete_token(system: str, current_user: AuthUser = Depends(get_current_user)) -> Response:
    name = _normalize_system(system, _TOKEN_SYSTEMS)
    execute("DELETE FROM user_integrations WHERE user_id = %s AND system = %s", [_user_id(current_user), name])
    return Response(status_code=204)


@router.get("/{system}/pins")
def list_pins(system: str, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    name = _normalize_system(system, _PIN_SYSTEMS)
    rows = query_all(
        """
        SELECT item_id, item_name, created_at
        FROM user_pins
        WHERE user_id = %s AND system = %s
        ORDER BY created_at DESC
        """,
        [_user_id(current_user), name],
    )
    return {"success": True, "data": rows, "timestamp": _now_iso()}


@router.post("/{system}/pins")
def add_pin(system: str, body: PinBody, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    name = _normalize_system(system, _PIN_SYSTEMS)
    item_id = body.item_id.strip()
    item_name = (body.item_name or "").strip() or item_id
    execute(
        """
        INSERT INTO user_pins (user_id, system, item_id, item_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, system, item_id) DO UPDATE SET item_name = EXCLUDED.item_name
        """,
        [_user_id(current_user), name, item_id, item_name],
    )
    return {"success": True}


@router.delete("/{system}/pins")
def remove_pin(system: str, body: PinBody, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    name = _normalize_system(system, _PIN_SYSTEMS)
    execute(
        "DELETE FROM user_pins WHERE user_id = %s AND system = %s AND item_id = %s",
        [_user_id(current_user), name, body.item_id.strip()],
    )
    return {"success": True}


@router.get("/sonarqube/projects")
def sonarqube_projects(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "sonarqube")
    base = _base_url("sonarqube")
    response = _sonar_request(f"{base}/api/projects/search", token)
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="Invalid token or connection failed")
    response.raise_for_status()
    payload = response.json()
    projects = [
        {
            "project_key": item.get("key") or "",
            "key": item.get("key") or "",
            "name": item.get("name") or item.get("key") or "",
            "language": (item.get("qualifier") or "Project"),
            "main_branch": item.get("mainBranch") or "main",
            "branches": [item.get("mainBranch") or "main"],
        }
        for item in payload.get("components", [])
    ]
    projects = _with_pin_flags(projects, _user_id(current_user), "sonarqube", "project_key")
    return {"success": True, "data": projects, "timestamp": _now_iso()}


@router.get("/sonarqube/project-details")
def sonarqube_project_details(project_key: str, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "sonarqube")
    base = _base_url("sonarqube")
    metrics = "bugs,vulnerabilities,coverage,duplicated_lines_density,ncloc,code_smells"
    response = _sonar_request(
        f"{base}/api/measures/component",
        token,
        params={"component": project_key, "metricKeys": metrics},
    )
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="Invalid token or connection failed")
    response.raise_for_status()
    payload = response.json()
    component = payload.get("component") or {}
    measures = {m.get("metric"): m.get("value") for m in component.get("measures", [])}
    data = {
        "project_key": component.get("key") or project_key,
        "name": component.get("name") or project_key,
        "language": "Project",
        "branches": ["main"],
        "main_branch": "main",
        "bugs": measures.get("bugs", "0"),
        "vulnerabilities": measures.get("vulnerabilities", "0"),
        "code_smells": measures.get("code_smells", "0"),
        "coverage": measures.get("coverage", "0"),
        "duplications": measures.get("duplicated_lines_density", "0"),
        "lines_of_code": measures.get("ncloc", "0"),
    }
    return {"success": True, "data": data, "timestamp": _now_iso()}


@router.get("/artifactory/repos")
def artifactory_repos(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "artifactory")
    base = _base_url("artifactory")
    response = _artifactory_request("GET", f"{base}/api/repositories", token)
    payload = response.json()
    repos = [
        {
            "name": item.get("key") or "",
            "type": item.get("packageType") or item.get("type") or "",
            "description": item.get("description") or "Artifactory repository",
            "url": item.get("url") or f"{base}/ui/repos/tree/General/{quote(item.get('key') or '')}",
        }
        for item in payload
        if item.get("key")
    ]
    repos = _with_pin_flags(repos, _user_id(current_user), "artifactory", "name")
    return {"success": True, "data": repos, "timestamp": _now_iso()}


@router.get("/artifactory/repo-details")
def artifactory_repo_details(name: str, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "artifactory")
    base = _base_url("artifactory")
    artifacts = _latest_artifacts(base, token, name)
    data = {
        "name": name,
        "type": "",
        "url": f"{base}/ui/repos/tree/General/{quote(name)}",
        "artifacts": artifacts,
    }
    return {"success": True, "data": data, "timestamp": _now_iso()}


@router.get("/artifactory/storage")
def artifactory_storage(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "artifactory")
    base = _base_url("artifactory")
    response = _artifactory_request("GET", f"{base}/api/storageinfo", token)
    payload = response.json()
    binaries = payload.get("binariesSummary") or {}
    repos_raw = payload.get("repositoriesSummaryList") or []
    repos = []
    for repo in repos_raw:
        percentage_raw = str(repo.get("percentage") or "0").strip().rstrip("%")
        try:
            percentage = float(percentage_raw)
        except ValueError:
            percentage = 0.0
        repos.append(
            {
                "name": repo.get("repoKey") or repo.get("repo") or "",
                "used": repo.get("usedSpace") or "0 B",
                "percentage": percentage,
            }
        )
    data = {
        "used": binaries.get("binariesSize") or "0 B",
        "total": binaries.get("fileStoreSummary", {}).get("totalSpace") or binaries.get("totalSpace") or "Unknown",
        "percentage": _storage_percentage(binaries),
        "repositories": repos,
    }
    return {"success": True, "data": data, "timestamp": _now_iso()}


@router.get("/confluence/recent")
def confluence_recent(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Recently edited Confluence pages across all spaces the user can see.

    Mirrors the existing Sonar/Artifactory token-gated pattern: returns a 428 via
    ``_require_token`` if the user hasn't set their personal token yet; otherwise
    proxies a single REST call so we never expose the token to the browser.
    """
    token = _require_token(current_user, "confluence")
    base = _base_url("confluence")
    response = _confluence_request(
        f"{base}/rest/api/content",
        token,
        params={
            "type": "page",
            "orderby": "modified",
            "limit": 10,
            "expand": "space,version,history.lastUpdated",
        },
    )
    payload = response.json()
    return {
        "success": True,
        "data": _format_confluence_pages(payload, base),
        "timestamp": _now_iso(),
    }


@router.get("/confluence/search")
def confluence_search(
    q: str,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Full-text search Confluence pages via CQL, scoped to type=page.

    The widget swaps the recent-pages list with these results inline, so the
    response shape is identical to ``/confluence/recent``.
    """
    token = _require_token(current_user, "confluence")
    query = (q or "").strip()
    if not query:
        return {"success": True, "data": [], "timestamp": _now_iso()}
    base = _base_url("confluence")
    # Escape any embedded double-quotes before placing the query inside the CQL
    # ``text~"..."`` clause so attackers can't break out of the literal.
    safe = query.replace('"', '\\"')
    cql = f'type=page AND text~"{safe}"'
    response = _confluence_request(
        f"{base}/rest/api/content/search",
        token,
        params={
            "cql": cql,
            "limit": 10,
            "expand": "space,version,history.lastUpdated",
        },
    )
    payload = response.json()
    return {
        "success": True,
        "data": _format_confluence_pages(payload, base),
        "timestamp": _now_iso(),
    }


def _confluence_request(
    url: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """Authenticated GET to Confluence; surfaces 401/403 as 401 to the browser."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(timeout, connect=5.0), headers=headers) as client:
            response = client.get(url, params=params)
    except Exception:
        raise HTTPException(status_code=502, detail="Confluence is unreachable")
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="Invalid token or connection failed")
    response.raise_for_status()
    return response


def _format_confluence_pages(payload: Dict[str, Any], base: str) -> List[Dict[str, Any]]:
    """Reshape Confluence page JSON into the small surface the widget consumes."""
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    pages: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        page_id = str(item.get("id") or "")
        title = str(item.get("title") or "Untitled")
        # Space name lives on either ``space.name`` (preferred) or falls back to
        # the space key when expansion is partial in some Confluence editions.
        space = item.get("space") or {}
        space_name = (space.get("name") if isinstance(space, dict) else None) or (
            space.get("key") if isinstance(space, dict) else None
        ) or ""
        # Last-modified can come from history.lastUpdated.when or version.when —
        # try both so we work with both Cloud and Data Center responses.
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        version = item.get("version") if isinstance(item.get("version"), dict) else {}
        last_updated = ""
        if isinstance(history, dict):
            last = history.get("lastUpdated")
            if isinstance(last, dict):
                last_updated = str(last.get("when") or "")
        if not last_updated and isinstance(version, dict):
            last_updated = str(version.get("when") or "")
        # Resolve link: tinyui (cloud) or webui (DC) -> absolute URL.
        links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
        webui = str(links.get("tinyui") or links.get("webui") or "").strip()
        link = ""
        if webui:
            if webui.startswith("http://") or webui.startswith("https://"):
                link = webui
            else:
                link = f"{base.rstrip('/')}{webui if webui.startswith('/') else '/' + webui}"
        pages.append(
            {
                "id": page_id,
                "title": title,
                "space_name": space_name,
                "last_updated": last_updated,
                "url": link or base,
            }
        )
    return pages


def _test_token(system: str, token: str) -> None:
    base = _base_url(system)
    try:
        if system == "sonarqube":
            response = _sonar_request(f"{base}/api/projects/search", token, params={"ps": 1}, timeout=8.0)
        elif system == "confluence":
            # Lightweight reachability + auth check; same Bearer header the
            # widget endpoints use so a valid result here means the token is
            # accepted by Confluence's REST API.
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(8.0, connect=5.0), headers=headers) as client:
                response = client.get(f"{base}/rest/api/space", params={"limit": 1})
        else:
            response = _artifactory_request("GET", f"{base}/api/system/ping", token, timeout=8.0)
        if response.status_code in {401, 403}:
            raise HTTPException(
                status_code=401,
                detail=f"{system.title()} rejected the token (HTTP {response.status_code}).",
            )
        response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{system.title()} returned HTTP {exc.response.status_code} during token validation.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{system.title()} unreachable during token validation: {type(exc).__name__}.",
        )


def _storage_percentage(binaries: Dict[str, Any]) -> float:
    used = str(binaries.get("binariesSize") or "").lower()
    total = str(binaries.get("fileStoreSummary", {}).get("totalSpace") or binaries.get("totalSpace") or "").lower()
    used_bytes = _parse_size_to_bytes(used)
    total_bytes = _parse_size_to_bytes(total)
    if not used_bytes or not total_bytes:
        return 0.0
    return round(min(100.0, (used_bytes / total_bytes) * 100), 1)


def _parse_size_to_bytes(value: str) -> float:
    parts = str(value or "").strip().split()
    if not parts:
        return 0.0
    try:
        number = float(parts[0].replace(",", ""))
    except ValueError:
        return 0.0
    unit = (parts[1].lower() if len(parts) > 1 else "b").rstrip("s")
    multipliers = {
        "b": 1,
        "byte": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
        "pb": 1024**5,
    }
    return number * multipliers.get(unit, 1)


def _artifactory_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _artifactory_header_sets(token: str) -> List[Dict[str, str]]:
    # Accept */* not application/json: /api/system/ping returns text/plain, and
    # demanding JSON makes Artifactory answer 406 Not Acceptable. JSON endpoints
    # (repositories, storageinfo, AQL) still return JSON under */*.
    return [
        {"Authorization": f"Bearer {token}", "Accept": "*/*"},
        {"X-JFrog-Art-Api": token, "Accept": "*/*"},
    ]


def _artifactory_request(
    method: str,
    url: str,
    token: str,
    *,
    timeout: float = 10.0,
    **kwargs: Any,
) -> httpx.Response:
    extra_headers = dict(kwargs.pop("headers", {}) or {})
    for headers in _artifactory_header_sets(token):
        merged_headers = {**headers, **extra_headers}
        with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(timeout, connect=5.0), headers=merged_headers) as client:
            response = client.request(method, url, **kwargs)
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return response
    raise HTTPException(status_code=401, detail="Invalid token or connection failed")


def _sonar_request(
    url: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """GET against SonarQube, tolerant of token-auth differences between versions.

    SonarQube >= 10.0 accepts ``Authorization: Bearer <token>``; older versions
    only accept the token as the HTTP Basic *username* (empty password). Try
    Bearer first, then fall back to Basic so both server generations work.
    """
    attempts = (
        {"headers": {"Authorization": f"Bearer {token}", "Accept": "application/json"}},
        {"auth": (token, ""), "headers": {"Accept": "application/json"}},
    )
    response: Optional[httpx.Response] = None
    for kwargs in attempts:
        with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(timeout, connect=5.0)) as client:
            response = client.get(url, params=params, **kwargs)
        if response.status_code not in {401, 403}:
            return response
    # Both schemes rejected — hand back the last response so the caller decides.
    assert response is not None
    return response


def _latest_artifacts(base: str, token: str, repo: str) -> List[Dict[str, Any]]:
    query = (
        f'items.find({{"repo":"{repo}"}})'
        '.sort({"$desc":["modified"]})'
        ".limit(10)"
        '.include("name","repo","path","type","modified")'
    )
    try:
        response = _artifactory_request(
            "POST",
            f"{base}/api/search/aql",
            token,
            content=query,
            headers={"Content-Type": "text/plain"},
        )
        results = response.json().get("results", [])
    except Exception:
        return []
    artifacts: List[Dict[str, Any]] = []
    for item in results[:10]:
        path = str(item.get("path") or "").strip(".")
        name = str(item.get("name") or "")
        native_path = "/".join(part for part in (repo, path, name) if part)
        artifacts.append(
            {
                "name": name,
                "type": item.get("type") or "file",
                "last_updated": item.get("modified"),
                "url": f"{base}/ui/native/{quote(native_path)}",
            }
        )
    return artifacts

