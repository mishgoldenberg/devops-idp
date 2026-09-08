from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
import os
import re
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx

from resilient_http import tls_verify
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from db import execute, query_all, query_one
from security import AuthUser, get_current_user
from sso_config import decrypt_secret, encrypt_secret


log = logging.getLogger(__name__)

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
    # Tolerate base URLs configured without a scheme (hand-entered secrets
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


@router.get("/health")
def integrations_health(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Is each integration configured, connected, and does the token still WORK?

    The portal's most common failure is a token that is missing or has silently expired:
    the user gets a 428 or an empty widget and has to guess which system to fix. This
    endpoint answers it for all of them at once, and it deliberately makes a real (cheap)
    call per system — "a token is stored" is not the same as "the token is still valid",
    and an expired PAT is precisely the case worth catching.

    Every system is probed independently: one unreachable system reports itself as such
    and does not affect the others.
    """
    uid = _user_id(current_user)
    labels = {"sonarqube": "SonarQube", "artifactory": "Artifactory", "confluence": "Confluence"}

    def probe(key: str) -> Dict[str, Any]:
        label = labels[key]
        result = {
            "system": key,
            "label": label,
            "configured": True,
            "connected": False,
            "healthy": False,
            "detail": "",
        }
        try:
            _base_url(key)
        except HTTPException:
            result["configured"] = False
            result["detail"] = "Server URL is not configured. Ask a platform admin."
            return result

        token = _get_token(uid, key)
        if not token:
            result["detail"] = "Not connected — add your token to use this system."
            return result
        result["connected"] = True

        # Reuse the SAME validation the save-token flow uses, so "healthy here" and
        # "accepted when I connected" can never disagree.
        try:
            _test_token(key, token)
            result["healthy"] = True
            result["detail"] = "Connected."
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                result["detail"] = "Token expired or revoked — reconnect."
            else:
                result["detail"] = str(exc.detail or f"{label} is unreachable.")
        except Exception as exc:
            result["detail"] = f"{label} is unreachable: {type(exc).__name__}."
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        systems = list(pool.map(probe, ["sonarqube", "artifactory", "confluence"]))

    return {"success": True, "data": systems, "timestamp": _now_iso()}


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
    try:
        response = _sonar_request(f"{base}/api/projects/search", token)
    except HTTPException:
        raise
    except Exception as exc:
        # Without this the transport error escaped as a bare 500 with no body, so the
        # widget had nothing to show and the reason existed only in the pod log.
        raise HTTPException(status_code=502, detail=_describe_connect_failure("sonarqube", base, exc))
    if response.status_code in {401, 403}:
        raise HTTPException(
            status_code=401,
            detail=f"SonarQube rejected the token (HTTP {response.status_code}). Reconnect with a valid User Token.",
        )
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


def artifactory_ui_root(base: str) -> str:
    """The web UI root for an Artifactory whose REST base is ``base``.

    ARTIFACTORY_BASE_URL points at the REST API, which on a JFrog Platform install
    lives at ``<host>/artifactory``. The web UI is not underneath it — it is a
    SIBLING at ``<host>/ui``. Deriving links straight from the base therefore
    produced ``<host>/artifactory/ui/repos/...``, which 404s for every repository.

    The same correction is made in _artifactory_projects() to reach ``/access``.
    On installs whose base has no ``/artifactory`` suffix this is a no-op, so it is
    safe either way.
    """
    return re.sub(r"/artifactory/?$", "", (base or "").rstrip("/"))


def artifactory_repo_url(base: str, repo: str) -> str:
    """Deep link to a repository in the Artifactory tree browser."""
    return f"{artifactory_ui_root(base)}/ui/repos/tree/General/{quote(repo or '')}"


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
            # NOT item["url"] — Artifactory returns the repository's *download* base
            # there (<host>/artifactory/<key>), which serves a bare file index rather
            # than opening the repository in the UI.
            "url": artifactory_repo_url(base, item.get("key") or ""),
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
        "url": artifactory_repo_url(base, name),
        "artifacts": artifacts,
    }
    return {"success": True, "data": data, "timestamp": _now_iso()}


@router.get("/artifactory/storage")
def artifactory_storage(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    token = _require_token(current_user, "artifactory")
    base = _base_url("artifactory")
    try:
        response = _artifactory_request("GET", f"{base}/api/storageinfo", token)
    except HTTPException as exc:
        # /api/storageinfo is admin-only in Artifactory; a valid non-admin token
        # gets 401/403 here even though /api/repositories works for it. Surface a
        # clear reason instead of the generic "invalid token".
        if exc.status_code in {401, 403}:
            raise HTTPException(
                status_code=403,
                detail="Artifactory storage stats require an admin-scoped token.",
            )
        raise
    payload = response.json()
    binaries = payload.get("binariesSummary") or {}
    # ``fileStoreSummary`` is a TOP-LEVEL key of /api/storageinfo, not a child of
    # ``binariesSummary``. Reading it from ``binaries`` always came back empty, so
    # the widget reported "of Unknown used" and the fill bar was pinned at 0%.
    file_store = payload.get("fileStoreSummary") or binaries.get("fileStoreSummary") or {}
    repos_raw = payload.get("repositoriesSummaryList") or []

    repos = []
    total_row: Dict[str, Any] = {}
    for repo in repos_raw:
        name = str(repo.get("repoKey") or repo.get("repo") or "").strip()
        # Artifactory appends a synthetic "TOTAL" row to the repo list. It is not a
        # repository (it must not appear in the list or sort to the top), but it IS
        # the number the widget's headline wants — see below.
        if name.upper() == "TOTAL":
            total_row = repo
            continue
        if not name:
            continue
        used = repo.get("usedSpace") or "0 B"
        percentage_raw = str(repo.get("percentage") or "0").strip().rstrip("%")
        try:
            percentage = float(percentage_raw)
        except ValueError:
            percentage = 0.0
        repos.append(
            {
                "name": name,
                "used": used,
                "used_bytes": _parse_size_to_bytes(used),
                "percentage": percentage,
            }
        )

    # The API returns repositories in arbitrary order; the widget is meant to show
    # the biggest ones, so rank them by actual size.
    repos.sort(key=lambda r: r["used_bytes"], reverse=True)
    used_total = sum(r["used_bytes"] for r in repos)
    # Kept for the per-project rollup below: storageinfo reports usage per repo and
    # never per project, so project usage has to be summed from these.
    repo_bytes = {r["name"]: r["used_bytes"] for r in repos}
    for repo in repos:
        # Some Artifactory versions omit `percentage`; fall back to each repo's
        # share of the used space so the row bars aren't all empty.
        if not repo["percentage"] and used_total:
            repo["percentage"] = round(repo["used_bytes"] / used_total * 100, 1)
        repo.pop("used_bytes", None)

    # ── What "the whole usage of Artifactory" actually is ────────────────────────
    # /api/storageinfo reports THREE different totals and they legitimately disagree:
    #
    #   fileStoreSummary.usedSpace   physical disk used AFTER deduplication. This was
    #                                the headline, and it is why the widget said 130 GB
    #                                while a single repo underneath it read 1.73 TB —
    #                                Artifactory stores one copy of a binary no matter
    #                                how many repos reference it.
    #   binariesSummary.binariesSize size of the deduplicated binaries.
    #   repositoriesSummaryList TOTAL  the sum of every repository's usedSpace — the
    #                                LOGICAL total, i.e. the number that is consistent
    #                                with the repository list shown right below it.
    #
    # The headline is the TOTAL row, because that is the one that agrees with what the
    # user is looking at. The filestore is still reported, as the disk it all sits on.
    repo_total = _strip_percent_suffix(total_row.get("usedSpace")) if total_row else ""
    logical_total = repo_total or _format_bytes(used_total)

    # The DEDUPLICATED physical footprint — the real amount of unique data Artifactory
    # holds — is the headline now, NOT the per-repo sum. The sum counts a binary once for
    # every repo that references it, so an instance that de-dupes hard reads far larger
    # logically (your 8.70 TB across 493 repos) than it actually occupies. binariesSize is
    # the de-duplicated size; the filestore's usedSpace is the bytes on disk. Prefer the
    # first, fall back to the second.
    binaries_size = _strip_percent_suffix(binaries.get("binariesSize")) or ""
    fs_used = _strip_percent_suffix(file_store.get("usedSpace")) or ""
    fs_total = _strip_percent_suffix(file_store.get("totalSpace")) or ""
    used_space = binaries_size or fs_used or logical_total

    # ── The fill bar's total, and when to trust it ───────────────────────────────
    # The bar measures the deduplicated footprint against disk capacity. The trouble is
    # Artifactory's reported filestore total is whatever mount its data dir sits on, and on
    # this box that mount (688 GB) is SMALLER than the data actually stored (4.91 TB) — it's
    # reporting one partition, not the 5 TB VM. Measured against 688 GB the bar just pins at
    # a meaningless 100%. So capacity is only trusted when it can actually hold what's
    # stored, or when an admin states it explicitly:
    #   1. ARTIFACTORY_DISK_TOTAL (e.g. "5 TB")  → authoritative, always wins.
    #   2. filestore total, but ONLY if it's >= what's stored (a believable mount).
    #   3. otherwise → capacity unknown; the widget asks for ARTIFACTORY_DISK_TOTAL rather
    #      than drawing a bar against the wrong partition.
    disk_used = binaries_size or fs_used or "0 B"
    override = (os.getenv("ARTIFACTORY_DISK_TOTAL") or "").strip()
    used_bytes = _parse_size_to_bytes(disk_used)
    fs_total_bytes = _parse_size_to_bytes(fs_total)
    # The override must parse to a real size with a real unit. An unset Azure DevOps
    # variable is substituted as the literal text "$(ARTIFACTORY_DISK_TOTAL)", and
    # "6 terabytes" would otherwise be read as six BYTES — both would draw a bar
    # against nonsense, which is worse than admitting the capacity is unknown.
    override_bytes = _parse_admin_size(override)
    if override_bytes > 0:
        # Re-rendered from the parsed value so "6TB", "6 tb" and "6144 GB" all display
        # the same way, and so the percentage below measures against what we parsed
        # rather than re-parsing the raw string with the looser reader.
        disk_total = _format_bytes(override_bytes)
        disk_reliable = True
    elif fs_total and used_bytes > 0 and fs_total_bytes >= used_bytes:
        disk_total = fs_total
        disk_reliable = True
    else:
        disk_total = fs_total or "Unknown"
        disk_reliable = False
    disk_percentage = _storage_percentage(disk_used, disk_total) if disk_reliable else 0.0

    data = {
        "used": used_space,                       # headline: deduplicated physical footprint
        "logical": logical_total,                 # sum across repos, before de-duplication
        "files": str(total_row.get("filesCount") or "") if total_row else "",
        "disk_used": disk_used,
        "disk_total": disk_total,
        "disk_reliable": disk_reliable,           # False → don't draw a bar against a wrong mount
        "disk_percentage": disk_percentage,
        # Back-compat with the widget's existing keys.
        "total": disk_total if disk_reliable else "Unknown",
        "percentage": disk_percentage,
        "repositories": repos,
        "projects": _artifactory_projects(base, token, repo_bytes),
    }
    return {"success": True, "data": data, "timestamp": _now_iso()}


def _format_bytes(num: float) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "0 B"


def _artifactory_projects(base: str, token: str, repo_bytes: Dict[str, float]) -> List[Dict[str, Any]]:
    """JFrog Projects with their storage quota usage, largest first.

    Two things make this awkward, and both are handled here rather than in the widget:
    the Projects API lives under ``/access`` (a sibling of ``/artifactory``, not a
    child), and ``/api/storageinfo`` reports usage per repository with no project
    attribution. JFrog prefixes a project's repositories with ``<project-key>-``, so
    usage is summed from the repos we already fetched — no extra round-trip per project.

    Returns [] when the instance has no projects or the token can't read them; the
    widget then just doesn't offer the "All projects" view.
    """
    access_root = re.sub(r"/artifactory/?$", "", base.rstrip("/"))
    try:
        response = _artifactory_request(
            "GET", f"{access_root}/access/api/v1/projects", token, timeout=15.0
        )
        raw = response.json()
    except Exception as exc:
        log.info("Artifactory projects unavailable: %s: %s", type(exc).__name__, exc)
        return []

    if not isinstance(raw, list):
        raw = raw.get("projects") or raw.get("results") or []

    projects: List[Dict[str, Any]] = []
    for proj in raw:
        key = str(proj.get("project_key") or proj.get("projectKey") or "").strip()
        if not key:
            continue
        quota = float(proj.get("storage_quota_bytes") or proj.get("storageQuotaBytes") or 0)
        used_bytes = sum(
            size for repo_key, size in repo_bytes.items() if repo_key.startswith(f"{key}-")
        )
        # A quota of 0 / -1 means "unlimited" in JFrog — show usage without a bar
        # rather than a meaningless 0%.
        percentage = round(min(100.0, used_bytes / quota * 100), 1) if quota > 0 else 0.0
        projects.append(
            {
                "key": key,
                "name": str(proj.get("display_name") or proj.get("displayName") or key),
                "used": _format_bytes(used_bytes),
                "used_bytes": used_bytes,
                "quota": _format_bytes(quota) if quota > 0 else "Unlimited",
                "has_quota": quota > 0,
                "percentage": percentage,
            }
        )
    projects.sort(key=lambda p: p["used_bytes"], reverse=True)
    for proj in projects:
        proj.pop("used_bytes", None)
    return projects


@router.get("/confluence/recent")
def confluence_recent(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Pages THIS USER recently interacted with — viewed, edited or created.

    The widget used to list "the last 10 pages modified anywhere in Confluence", which
    is the whole instance's activity, not the user's. The Confluence token is the user's
    own (``_require_token``), so their own history is reachable:

      1. ``/rest/api/user/history`` — recently VIEWED. Only some Confluence versions
         expose it (Cloud does; older Server builds 404), so it is attempted, not relied on.
      2. CQL ``contributor = currentUser()`` — pages they created or edited. Works
         everywhere, and is the fallback (and the top-up) when history is unavailable.

    Whichever produced a page is recorded on it as ``interaction`` so the widget can say
    why the page is in the list.
    """
    token = _require_token(current_user, "confluence")
    base = _base_url("confluence")

    pages: List[Dict[str, Any]] = []
    seen: set = set()

    def add(items: List[Dict[str, Any]], interaction: str) -> None:
        for page in items:
            pid = str(page.get("id") or page.get("url") or "")
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            page["interaction"] = interaction
            pages.append(page)

    try:
        viewed = _confluence_request(
            f"{base}/rest/api/user/history",
            token,
            params={"limit": 10, "expand": "space,version,history.lastUpdated"},
        )
        add(_format_confluence_pages(viewed.json(), base), "viewed")
    except Exception as exc:
        log.info("Confluence view history unavailable (%s) — falling back to CQL", exc)

    try:
        edited = _confluence_request(
            f"{base}/rest/api/content/search",
            token,
            params={
                "cql": "type=page AND contributor = currentUser() ORDER BY lastmodified DESC",
                "limit": 10,
                "expand": "space,version,history.lastUpdated",
            },
        )
        add(_format_confluence_pages(edited.json(), base), "edited")
    except Exception as exc:
        log.warning("Confluence contributor search failed: %s", exc)

    # Pinned pages float to the top and keep their pin, exactly like every other widget.
    pages = _with_pin_flags(pages[:10], _user_id(current_user), "confluence", "id")
    pages.sort(key=lambda p: (0 if p.get("pinned") else 1))
    return {"success": True, "data": pages, "timestamp": _now_iso()}


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
    """Validate a token by making THE SAME call the widget makes.

    This used to probe endpoints the widgets never touch — /api/system/ping on Artifactory,
    /rest/api/space on Confluence — and those are not present on every deployment. The
    result was a Connections page that reported HTTP 404 (and so "broken") for systems
    whose widgets were working perfectly. A health check that disagrees with the feature it
    is reporting on is worse than no health check.

    So each probe now hits the exact endpoint its widget depends on, with a limit of 1.
    "Healthy" and "the widget works" cannot drift apart, because they are the same call.
    """
    base = _base_url(system)
    try:
        if system == "sonarqube":
            response = _sonar_request(f"{base}/api/projects/search", token, params={"ps": 1}, timeout=8.0)
        elif system == "confluence":
            # What confluence_recent() uses. /rest/api/space is not enabled everywhere.
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(8.0, connect=5.0), headers=headers) as client:
                response = client.get(
                    f"{base}/rest/api/content/search",
                    params={"cql": "type = page", "limit": 1},
                )
        else:
            # What artifactory_repos() uses. /api/system/ping is admin-gated or absent on
            # some deployments, which is why this reported 404 while the widget was fine.
            response = _artifactory_request("GET", f"{base}/api/repositories", token, timeout=8.0)
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
        raise HTTPException(status_code=400, detail=_describe_connect_failure(system, base, exc))


def _describe_connect_failure(system: str, base: str, exc: Exception) -> str:
    """Turn a transport error into something a person can act on.

    This used to say only "unreachable … ConnectTimeout", which names the exception
    class and nothing else — it does not say WHICH host was dialled, and it reads like
    a bad token when it is actually a network path problem. The distinction matters
    here: the portal calls out from inside the cluster, so a firewall rule that was
    opened for a laptop does not necessarily cover the pod's egress address.
    """
    label = system.title()
    host = ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        host = parsed.netloc or base
    except Exception:
        host = base
    where = f" at {host}" if host else ""

    if isinstance(exc, httpx.ConnectTimeout):
        return (
            f"Could not reach {label}{where} — the connection timed out before it was "
            "answered. The address resolves but nothing accepted the connection, which "
            "is what a blocked port looks like. Note the portal connects from inside the "
            "cluster, so the firewall rule has to allow the cluster's egress address, not "
            "just your workstation."
        )
    if isinstance(exc, httpx.ReadTimeout):
        return (
            f"{label}{where} accepted the connection but did not answer in time. The path "
            "is open; the server is slow or the request was silently dropped mid-flight."
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            f"Could not open a connection to {label}{where} — the address may be wrong, "
            "unresolvable from inside the cluster, or actively refusing connections."
        )
    if isinstance(exc, (httpx.TooManyRedirects,)):
        return f"{label}{where} redirected too many times — check the base URL."
    # TLS problems arrive as several different classes depending on the stack.
    name = type(exc).__name__
    if "SSL" in name or "Certificate" in name or "Tls" in name:
        return (
            f"TLS handshake with {label}{where} failed ({name}). The service is reachable "
            "but its certificate was rejected."
        )
    return f"Could not reach {label}{where} — {name}."


def _strip_percent_suffix(value: Any) -> str:
    """``fileStoreSummary`` sizes arrive as "5.5 TB (55%)" — keep just the size."""
    text = str(value or "").strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text


def _storage_percentage(used: str, total: str) -> float:
    used_bytes = _parse_size_to_bytes(str(used or "").lower())
    total_bytes = _parse_size_to_bytes(str(total or "").lower())
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


_ADMIN_SIZE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
    "pb": 1024**5,
}

_ADMIN_SIZE_RE = re.compile(r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([a-zA-Z]+)\s*$")


def _parse_admin_size(value: str) -> float:
    """Parse a HAND-TYPED size like "6 TB" / "6TB" / "6144 GB" into bytes.

    Deliberately stricter and more forgiving than ``_parse_size_to_bytes``, which
    exists to read Artifactory's own output and therefore splits on whitespace and
    treats an unrecognised unit as bytes. Both behaviours are wrong for a value a
    person typed into a variable group:

      * "6TB" (no space) parsed as 0 and was silently ignored — the widget went on
        asking for a variable that WAS set.
      * "6 terabytes" parsed as 6 BYTES and would have been accepted, drawing a bar
        against a six-byte disk.

    So: the unit is required and must be one we know, and the space is optional.
    Anything else returns 0.0, which callers treat as "not set".
    """
    match = _ADMIN_SIZE_RE.match(str(value or ""))
    if not match:
        return 0.0
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    unit = match.group(2).lower()
    if unit not in _ADMIN_SIZE_UNITS or number <= 0:
        return 0.0
    return number * _ADMIN_SIZE_UNITS[unit]


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


def _aql_literal(value: str) -> str:
    """Escape a value for embedding in an AQL JSON string literal."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _latest_artifacts(base: str, token: str, repo: str) -> List[Dict[str, Any]]:
    # AQL clause ORDER IS SIGNIFICANT: find → include → sort → limit. With .sort()
    # before .include() Artifactory rejects the query outright, which is why every
    # repo's hover panel said "no data available" — the 400 was being swallowed
    # below and turned into an empty list.
    query = (
        f'items.find({{"repo":"{_aql_literal(repo)}"}})'
        '.include("name","repo","path","type","modified")'
        '.sort({"$desc":["modified"]})'
        ".limit(10)"
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
    except Exception as exc:
        # Don't fail silently: an AQL rejection or a token without search rights is
        # indistinguishable from "this repo is empty" unless we say so.
        log.warning("Artifactory AQL failed for repo %r: %s: %s", repo, type(exc).__name__, exc)
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
                "url": f"{artifactory_ui_root(base)}/ui/native/{quote(native_path)}",
            }
        )
    return artifacts

