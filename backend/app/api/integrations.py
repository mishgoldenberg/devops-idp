from __future__ import annotations

import os
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from db import execute, query_all, query_one
from security import AuthUser, get_current_user
from sso_config import decrypt_secret, encrypt_secret


router = APIRouter()

SystemName = Literal["azure", "sonarqube", "artifactory"]
_TOKEN_SYSTEMS = {"azure", "sonarqube", "artifactory"}
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
        url = os.getenv("SONARQUBE_URL", "").strip().rstrip("/")
    elif system == "artifactory":
        url = os.getenv("ARTIFACTORY_URL", "").strip().rstrip("/")
    else:
        url = ""
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{system.title()} URL is not configured",
        )
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
    if name in {"sonarqube", "artifactory"}:
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
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=10.0, headers=headers) as client:
        response = client.get(f"{base}/api/projects/search")
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
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=10.0, headers=headers) as client:
        response = client.get(
            f"{base}/api/measures/component",
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


def _test_token(system: str, token: str) -> None:
    base = _base_url(system)
    try:
        if system == "sonarqube":
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            with httpx.Client(timeout=8.0, headers=headers) as client:
                response = client.get(f"{base}/api/projects/search", params={"ps": 1})
        else:
            response = _artifactory_request("GET", f"{base}/api/system/ping", token, timeout=8.0)
        if response.status_code in {401, 403}:
            raise HTTPException(status_code=401, detail="Invalid token")
        response.raise_for_status()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token or connection failed")


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
    return [
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        {"X-JFrog-Art-Api": token, "Accept": "application/json"},
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
        with httpx.Client(timeout=timeout, headers=merged_headers) as client:
            response = client.request(method, url, **kwargs)
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return response
    raise HTTPException(status_code=401, detail="Invalid token or connection failed")


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

