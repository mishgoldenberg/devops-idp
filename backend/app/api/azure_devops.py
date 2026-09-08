"""
Azure DevOps Integration API Module

This module provides FastAPI endpoints for Azure DevOps integration, including:
- Real-time work item & PR queries  
- Custom process & project creation (Self-Service)
- Pipeline status monitoring

CONFIGURATION:
  - AZURE_DEVOPS_BASE_URL: Azure DevOps organization URL, e.g. https://dev.azure.com/MyOrg.
  - AZURE_DEVOPS_ADMIN_PAT: Admin PAT used for self-service write operations.

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

from typing import Any, Dict, List, Optional, Tuple
import logging
import re
import os
import time
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
import httpx

from resilient_http import explain_integration_failure, tls_verify

# The identity/permission writes live in their own module because they are built on the
# LEGACY /_api/_identity/ endpoints rather than the REST ones — the Graph API is not
# routed on this Azure DevOps Server, which is why every earlier grant silently 404'd.
import ado_identity

from security import AuthUser, get_current_user
from secrets_manager import (
    delete_user_azure_devops_pat,
    get_user_azure_devops_pat,
    store_user_azure_devops_pat,
)


router = APIRouter()
log = logging.getLogger(__name__)


# Configuration from environment variables (read at import so env is respected in K8s/local)
def _get_ado_base() -> str:
    """Return the configured Azure DevOps organization base URL."""
    return (os.getenv("AZURE_DEVOPS_BASE_URL") or "").strip().rstrip("/")


ADO_BASE = _get_ado_base()
# Admin PAT used for write operations (process creation).
# Requires scope: Process (Read & Manage) — or Full Access.
_ENV_ADMIN_PAT = os.getenv("AZURE_DEVOPS_ADMIN_PAT", "")

# Self-service provisions into a FIXED set of collections, not "wherever the PAT can
# reach". On this server that is exactly two — DevCollection-Inheritance and
# TikshuvCollection-Inheritance — discovered from Azure DevOps and matched by name, so
# the list is authoritative even if the server later grows more collections. Override
# with a comma-separated ADO_PROVISION_COLLECTIONS if the names ever change.
_TARGET_COLLECTIONS: List[str] = [
    c.strip()
    for c in os.getenv(
        "ADO_PROVISION_COLLECTIONS",
        "DevCollection-Inheritance,TikshuvCollection-Inheritance",
    ).split(",")
    if c.strip()
]

# The one a request goes to unless the requester picks another.
#
# Provisioning used to create the project in EVERY collection above, which is why the
# logs are full of "A project named X already exists in TikshuvCollection-Inheritance":
# the name only had to be taken in one of them for every request to record a failure,
# and nobody had asked for a second copy of the project in the first place. A request
# now names exactly one collection.
_DEFAULT_COLLECTION: str = (
    os.getenv("ADO_DEFAULT_COLLECTION")
    or (_TARGET_COLLECTIONS[0] if _TARGET_COLLECTIONS else "DevCollection-Inheritance")
).strip()


# ── Identity resolution for provisioning grants ──────────────────────────────
#
# This server is Active-Directory backed, so the reliable identity form for a
# lookup is the down-level account name DOMAIN\user — NOT the e-mail and NOT a
# bare username. A lookup by anything else silently resolves no one, which is
# exactly why the chosen admin was never actually added to a new project.
# ADO_NETBIOS_DOMAIN lets the backend build DOMAIN\user from a bare username; if
# it is unset we fall back to whatever principal was typed.
#
# Read defensively: an unexpanded pipeline macro ("$(...)") or a value with
# whitespace is treated as unset rather than smuggled into a lookup.
_raw_netbios = (os.getenv("ADO_NETBIOS_DOMAIN") or "").strip().strip("\\")
_ADO_NETBIOS_DOMAIN = "" if ("$(" in _raw_netbios or " " in _raw_netbios) else _raw_netbios


def normalize_admin_principal(value: str) -> str:
    """Best identity form for an AD-backed Azure DevOps lookup.

    Already domain-qualified (``DOMAIN\\user``) or an e-mail is used unchanged; a
    bare username becomes ``DOMAIN\\user`` when ADO_NETBIOS_DOMAIN is set, and is
    otherwise left as-is (last resort — it may not resolve).
    """
    v = (value or "").strip()
    if not v or "\\" in v or "@" in v:
        return v
    return f"{_ADO_NETBIOS_DOMAIN}\\{v}" if _ADO_NETBIOS_DOMAIN else v


def default_admin_principal(email: str) -> str:
    """Prefill for the self-service admin field: ``DOMAIN\\<local-part>`` when a
    domain is configured, else the bare local part. Always editable in the form."""
    local = (email or "").split("@")[0].strip()
    if local and _ADO_NETBIOS_DOMAIN:
        return f"{_ADO_NETBIOS_DOMAIN}\\{local}"
    return local


def principal_candidates(principal: str) -> List[str]:
    """Every plausible identity form for what the requester typed, best first.

    One form is not enough. Round 49 sent a single normalised principal and gave up
    if the server did not recognise it — which is indistinguishable, from the outside,
    from the grant working. Different AD-backed collections answer to different forms
    (``DOMAIN\\user`` on one, the UPN on another, the bare sAMAccountName on a third),
    so we try them all and record which one hit.
    """
    p = (principal or "").strip()
    out: List[str] = []

    def add(value: str) -> None:
        v = (value or "").strip()
        if v and v not in out:
            out.append(v)

    if "\\" in p:
        add(p)
        add(p.split("\\", 1)[1])          # bare sAMAccountName
    elif "@" in p:
        local = p.split("@", 1)[0]
        if _ADO_NETBIOS_DOMAIN:
            add(f"{_ADO_NETBIOS_DOMAIN}\\{local}")
        add(p)                            # UPN / mail
        add(local)
    else:
        if _ADO_NETBIOS_DOMAIN:
            add(f"{_ADO_NETBIOS_DOMAIN}\\{p}")
        add(p)
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# Statuses that mean "this collection is not the one that holds what you asked for".
# 400 belongs here because Azure DevOps validates project references inside a query
# body: a collection that does not contain the project answers 400 ("project does not
# exist"), which is a "not mine", exactly like 404. Every per-collection probe skips
# this set — named once so a new probe cannot get the list subtly wrong.
_ADO_NOT_MINE = (400, 401, 403, 404)


def _describe_ado_status(status_code: int) -> str:
    """
    Turn an Azure DevOps status into something true.

    Every one of these used to read "Azure DevOps API error: N. Check
    AZURE_DEVOPS_BASE_URL and PAT." regardless of N — so a 400, which is not an
    authentication failure in any sense, told the user their token was wrong. That
    is the message behind the wave of "please add your PAT" errors seen while the
    PAT was in fact fine, and it sent people to re-enter a working credential.

    Only 401 and 403 are about the token. Say so only for those.
    """
    if status_code in (401, 403):
        return (
            "Azure DevOps rejected the credential (HTTP "
            f"{status_code}). The PAT is missing, expired, or lacks the required scope."
        )
    if status_code == 404:
        return f"Azure DevOps could not find that resource (HTTP {status_code})."
    if status_code == 400:
        return (
            "Azure DevOps rejected the request as malformed (HTTP 400). This usually "
            "means the selected project does not exist in the collection being queried "
            "— it is not a credential problem."
        )
    return f"Azure DevOps returned HTTP {status_code}."


def _is_devops_cloud_root(base: str) -> bool:
    parsed = urlparse(base)
    return parsed.netloc.lower() == "dev.azure.com" and not parsed.path.strip("/")


def _discover_onprem_collections(client: httpx.Client, base: str) -> List[str]:
    """Every project collection on an Azure DevOps Server (on-prem) instance.

    AZURE_DEVOPS_BASE_URL is configured as a single COLLECTION — e.g.
    ``https://tfs.corp/tfs/DefaultCollection``. That is one collection out of however
    many the server hosts, and it is why the portal only ever showed projects from one
    of them however many the PAT could actually reach: nothing ever asked the server
    what else was there.

    The server exposes the list one level up, at ``<server>/_apis/projectCollections``.
    So: strip the collection segment, ask, and query every collection that comes back.

    Best-effort by design. If the endpoint is absent (Azure DevOps Services), the PAT
    cannot enumerate collections, or anything else goes wrong, we fall back to the
    single configured collection — which is exactly the old behaviour, so the worst
    case here is no worse than before.
    """
    root = base.rstrip("/").rsplit("/", 1)[0]
    if not root or root.count("/") < 2:  # e.g. "https://host" with nothing left to strip
        return [base]
    try:
        response = client.get(f"{root}/_apis/projectCollections?api-version=5.0")
        if response.status_code >= 400:
            log.info(
                "Azure DevOps collection discovery unavailable (HTTP %s); "
                "using the configured collection only.",
                response.status_code,
            )
            return [base]
        collections = response.json().get("value", [])
    except Exception as exc:
        log.info(
            "Azure DevOps collection discovery failed (%s: %s); "
            "using the configured collection only.",
            type(exc).__name__, exc,
        )
        return [base]

    bases: List[str] = []
    for collection in collections:
        name = str(collection.get("name") or "").strip()
        if name:
            bases.append(f"{root}/{name}")

    if not bases:
        return [base]

    # The configured collection first — it is the one the operator chose, and the one
    # every other endpoint in this module still defaults to.
    bases.sort(key=lambda b: (b.rstrip("/").lower() != base.rstrip("/").lower(), b.lower()))
    return bases


def _discover_ado_bases(client: httpx.Client) -> List[str]:
    """
    Resolve the Azure DevOps organization/collection URLs available to the PAT.

    `AZURE_DEVOPS_BASE_URL=https://dev.azure.com/` is a service root, not an API
    scope. In that case we ask Azure DevOps for the accounts the PAT can access
    and query each one. On-prem, a configured collection URL is ONE collection of
    several, so we ask the server for the rest (see above).
    """
    if not ADO_BASE:
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_BASE_URL is not configured")

    if not _is_devops_cloud_root(ADO_BASE):
        return _discover_onprem_collections(client, ADO_BASE)

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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Could not discover Azure DevOps organizations for this PAT. "
                "Check that the PAT has access, or set AZURE_DEVOPS_BASE_URL to a specific organization URL."
            ),
        ) from exc

    bases = [f"https://dev.azure.com/{name}" for name in names]
    if not bases:
        raise HTTPException(status_code=404, detail="No Azure DevOps organizations are accessible with this PAT")
    return bases


def _get_pat_for_user(current_user: AuthUser) -> str:
    """
    Resolve the Azure DevOps PAT for the requesting user.

    Only the per-user PAT stored in Vault / system_config (set via the dashboard
    UI) is accepted for widget/read endpoints. The server-level admin PAT is
    intentionally NOT used here; it is reserved for self-service write operations.

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


def _probe_pat_live(pat: str) -> Dict[str, Any]:
    """One call against the CONFIGURED collection: is this token still accepted?"""
    try:
        auth = httpx.BasicAuth("", pat)
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            # /_apis/projects, NOT /_apis/connectionData. connectionData needs the
            # PAT to carry the *profile* scope, and a PAT scoped exactly as the portal
            # asks for it (Work Items / Code / Build, read) does NOT have that scope —
            # so a perfectly good PAT came back 401 here and this page called it
            # "expired or revoked" while every widget kept working. This is the call
            # the widgets themselves make, so the two can no longer disagree.
            resp = client.get(f"{_get_ado_base()}/_apis/projects?$top=1&api-version=7.0")
        if resp.status_code in (401, 403):
            return {"healthy": False, "rejected": True,
                    "detail": "PAT expired or revoked — reconnect."}
        if resp.status_code >= 400:
            return {"healthy": False, "rejected": False,
                    "detail": f"Azure DevOps answered HTTP {resp.status_code}."}
        return {"healthy": True, "rejected": False, "detail": "Connected."}
    except Exception as exc:
        # Unreachable is NOT rejected. A firewall or a restarting server must never
        # be reported to a user as "your token expired" — they would dutifully
        # generate a new one and nothing would change.
        return {"healthy": False, "rejected": False,
                "detail": f"Azure DevOps is unreachable: {type(exc).__name__}."}


def probe_user_pat(current_user: AuthUser) -> Dict[str, Any]:
    """Is this user's stored PAT configured, and does it still work?

    Cached for ten minutes per user. Called from the dashboard as well as the
    Connections page now, and a token's validity is not a per-page-load question —
    without the cache, 500 people opening the portal at 09:00 would be 500 extra
    calls against the Azure DevOps server every time any of them pressed F5.

    A rejected token also raises ONE notification per user per day. An expired PAT
    used to be invisible until somebody noticed their widgets had been empty for a
    while and thought to look at Connections; every user's token expires eventually,
    and they all connect in the same week, so they will all expire in the same week.
    """
    user_id = str(current_user.get("id"))
    pat = get_user_azure_devops_pat(user_id)
    if not pat:
        return {"configured": False, "has_personal_pat": False, "healthy": False,
                "detail": "Not connected — add your Personal Access Token."}
    owner = str(
        current_user.get("email") or current_user.get("username") or user_id or ""
    ).lower()
    from integrations_cache import cached_external

    result = cached_external("ado", owner, "pat-health", lambda: _probe_pat_live(pat), ttl=600)

    if result.get("rejected"):
        email = str(current_user.get("email") or "").strip().lower()
        if email:
            from api.notifications import notify_once
            from datetime import date

            notify_once(
                user_email=email,
                message=(
                    "Your Azure DevOps token is no longer accepted. Reconnect it on the "
                    "Connections page — your work items, pull requests and pipelines "
                    "stay empty until you do."
                ),
                # Once a day, not once ever: the reminder should keep arriving until
                # it is dealt with, but not once per dashboard load.
                group_key=f"ado-pat-rejected:{date.today().isoformat()}",
                notif_type="warning",
                link="/ui/connections",
            )

    return {
        "configured": True,
        "has_personal_pat": True,
        "healthy": bool(result.get("healthy")),
        "detail": str(result.get("detail") or ""),
    }


@router.get("/pat")
def get_pat_status(current_user: AuthUser = Depends(get_current_user)):
    """Return whether this user has a personal PAT configured. Value is never returned.

    ``healthy`` says whether the stored PAT still WORKS. "A PAT is stored" and "the PAT is
    valid" are different things, and the difference is exactly the case that strands a
    user: an expired PAT leaves every Azure DevOps widget empty with no explanation.
    """
    return {
        "success": True,
        "data": probe_user_pat(current_user),
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


def _fetch_state_categories(client: httpx.Client, base_url: str, project: str, work_item_type: str) -> dict:
    """
    Returns a mapping of {state_name: state_category} for a given project + work item type.
    State categories are ADO-standard strings: 'Proposed', 'InProgress', 'Resolved',
    'Completed', 'Removed' — these are stable regardless of custom state names (e.g. "Doing").
    Returns an empty dict (not None) on failure so callers can fall back to name heuristics.
    """
    import logging
    url = (
        f"{base_url}/{quote(project, safe='')}/_apis/wit/workitemtypes"
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
    if not ADO_BASE:
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_BASE_URL is not configured")

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)
    try:
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            bases = _discover_ado_bases(client)
            projects = []
            reachable = 0
            failures: List[str] = []
            for base_url in bases:
                collection = base_url.rstrip("/").split("/")[-1]
                try:
                    r = client.get(f"{base_url}/_apis/projects?$top=200&api-version=7.0")
                    r.raise_for_status()
                except Exception as exc:
                    # One collection the PAT cannot read must not empty the whole list.
                    # A user with access to six collections and a token scoped to five
                    # should see five, not an error.
                    reason = (
                        f"HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.HTTPStatusError)
                        else f"{type(exc).__name__}"
                    )
                    failures.append(f"{collection}: {reason}")
                    log.info(
                        "Azure DevOps: skipping collection %r (%s: %s)",
                        collection, type(exc).__name__, exc,
                    )
                    continue
                reachable += 1
                projects.extend(
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "collection": collection,
                    }
                    for p in r.json().get("value", [])
                )

        if not reachable:
            # Name the collections and their reasons. "No readable collections"
            # on its own produced a 502 row in the Logs page that an operator
            # could do nothing with: an expired PAT, a collection that has been
            # renamed and a server that is simply down all looked identical.
            why = "; ".join(failures[:6]) or "no collections were returned"
            log.warning(
                "Azure DevOps: every collection failed for this token (%s)", why
            )
            raise HTTPException(
                status_code=502,
                detail=f"No Azure DevOps collection could be read with this token ({why}).",
            )

        # Alphabetical, and stable. The API returns them in an order of its own that
        # changes between calls, so the dropdown reshuffled itself and you had to hunt
        # for the project you picked last time.
        projects.sort(key=lambda p: (str(p.get("name") or "").lower(), str(p.get("collection") or "").lower()))
        return {"success": True, "data": projects, "timestamp": _now_iso()}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps API error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps request failed: {exc!s}")


@router.get("/work-item-types")
def get_work_item_types(
    project: str = Query(..., description="Project name"),
    current_user: AuthUser = Depends(get_current_user),
):
    """Work item types defined by the project's process (Bug, Product Backlog Item,
    Test Case, …). These are process-specific — Agile, Scrum and CMMI each define a
    different set, and a customised process adds its own — so the widget's type
    filter has to be populated from the project rather than hard-coded."""
    if not ADO_BASE:
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_BASE_URL is not configured")

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)
    try:
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            for base_url in _discover_ado_bases(client):
                url = (
                    f"{base_url}/{quote(project, safe='')}"
                    "/_apis/wit/workitemtypes?api-version=7.0"
                )
                r = client.get(url)
                # 400 = the project isn't in this collection; skip like 401/403/404 so the
                # types dropdown falls through to the collection that owns the project.
                if r.status_code in _ADO_NOT_MINE:
                    continue
                r.raise_for_status()
                types = [
                    {"name": t.get("name") or "", "color": t.get("color") or ""}
                    for t in r.json().get("value", [])
                    if t.get("name") and not t.get("isDisabled")
                ]
                types.sort(key=lambda t: t["name"].lower())
                return {"success": True, "data": types, "timestamp": _now_iso()}
        return {"success": True, "data": [], "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps API error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps request failed: {exc!s}")


def _fetch_iterations(client: httpx.Client, base_url: str, project: str) -> List[Dict[str, Any]]:
    """Sprints for the project's default team, newest-relevant first.

    Omitting the team from the route makes Azure DevOps use the project's default
    team, which is the right answer for the overwhelming majority of projects and
    saves the widget an extra "pick your team" selector.
    """
    url = (
        f"{base_url}/{quote(project, safe='')}"
        "/_apis/work/teamsettings/iterations?api-version=7.0"
    )
    r = client.get(url)
    # 400 skipped with 401/403/404: this runs across EVERY collection, and a collection
    # that doesn't contain `project` answers 400 ("project does not exist"). Left to raise,
    # it was logged as "Azure DevOps API error: 400" once per foreign collection — the exact
    # 400 spam seen when a project + current sprint is selected. It means "not mine", like 404.
    if r.status_code in _ADO_NOT_MINE:
        return []
    r.raise_for_status()
    iterations = []
    for it in r.json().get("value", []):
        attrs = it.get("attributes") or {}
        time_frame = str(attrs.get("timeFrame") or "").lower()
        iterations.append(
            {
                "id": it.get("id") or "",
                "name": it.get("name") or "",
                "path": it.get("path") or "",
                "time_frame": time_frame,
                "is_current": time_frame == "current",
                "start_date": attrs.get("startDate") or "",
                "finish_date": attrs.get("finishDate") or "",
            }
        )
    # Current sprint first, then upcoming, then past.
    #
    # Sorting the whole list by (group, start_date ascending) put the PAST sprints in
    # oldest-first order, which is why the dropdown read 2026, then 2023, then 2024.
    # Each group wants its own direction: upcoming ascending (the next one first), past
    # DESCENDING (the one that just ended first). Undated sprints sort last within their
    # group rather than jumping to the top on an empty string.
    def by_date(group: List[Dict[str, Any]], newest_first: bool) -> List[Dict[str, Any]]:
        return sorted(
            group,
            key=lambda i: (not i.get("start_date"), i.get("start_date") or ""),
            reverse=newest_first,
        )

    current = [i for i in iterations if i["time_frame"] == "current"]
    future = [i for i in iterations if i["time_frame"] == "future"]
    past = [i for i in iterations if i["time_frame"] == "past"]
    other = [i for i in iterations if i["time_frame"] not in {"current", "future", "past"}]
    return current + by_date(future, False) + by_date(past, True) + other


def _fetch_area_paths(client: httpx.Client, base_url: str, project: str) -> List[Dict[str, Any]]:
    """Every area path in the project, flattened, shallowest first.

    Area paths are a TREE (classification nodes), and WIQL matches them with UNDER —
    so a parent implicitly includes its children and the widget only needs the flat
    list of node paths to offer as choices.
    """
    url = (
        f"{base_url}/{quote(project, safe='')}"
        "/_apis/wit/classificationnodes/areas?$depth=8&api-version=7.0"
    )
    r = client.get(url)
    # 400 = "this collection doesn't have that project"; skip it like 401/403/404 so a
    # cross-collection area-path probe never raises and logs a spurious 400.
    if r.status_code in _ADO_NOT_MINE:
        return []
    r.raise_for_status()

    out: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], depth: int) -> None:
        # The API's `path` looks like "\Proj\Area\Team"; WIQL wants "Proj\Team" — i.e.
        # no leading slash and with the literal "Area" classification segment removed.
        raw = str(node.get("path") or "").lstrip("\\")
        wiql_path = raw.replace("\\Area\\", "\\", 1)
        if wiql_path:
            out.append({"name": node.get("name") or "", "path": wiql_path, "depth": depth})
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(r.json(), 0)
    return out


@router.get("/area-paths")
def get_area_paths(
    project: str = Query(...),
    current_user: AuthUser = Depends(get_current_user),
):
    """Area paths for a project, for the My Work Items filter."""
    project = _query_str(project) or ""
    if not project:
        return {"success": True, "data": [], "timestamp": _now_iso()}
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            for base_url in _discover_ado_bases(client):
                areas = _fetch_area_paths(client, base_url, project)
                if areas:
                    return {"success": True, "data": areas, "timestamp": _now_iso()}
        return {"success": True, "data": [], "timestamp": _now_iso()}
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("Azure DevOps area paths request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc}",
        )


@router.get("/iterations")
def get_iterations(
    project: str = Query(..., description="Project name"),
    current_user: AuthUser = Depends(get_current_user),
):
    """Sprints (iterations) for the project's default team."""
    if not ADO_BASE:
        raise HTTPException(status_code=500, detail="AZURE_DEVOPS_BASE_URL is not configured")

    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)
    try:
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            for base_url in _discover_ado_bases(client):
                iterations = _fetch_iterations(client, base_url, project)
                if iterations:
                    return {"success": True, "data": iterations, "timestamp": _now_iso()}
        return {"success": True, "data": [], "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps API error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps request failed: {exc!s}")


@router.get("/work-items")
def get_work_items(
    project: Optional[str] = Query(None),
    work_item_type: Optional[str] = Query(None, description="e.g. Bug, Product Backlog Item"),
    iteration: Optional[str] = Query(
        None,
        description="Iteration path, or 'current' for the project's active sprint, or 'all'",
    ),
    area_path: Optional[str] = Query(
        None,
        description="Area path to scope to (matched with UNDER, so children are included)",
    ),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Return work items assigned to the PAT owner (@Me macro ensures we query the right user).
    Filterable by project, work item type and iteration. Each item includes
    state_category so the frontend can group by 'Proposed'/'InProgress'/'Resolved'/
    'Completed' regardless of the team's custom state names (e.g. 'Doing' → 'InProgress').

    ``iteration`` accepts an explicit iteration path, ``"current"`` (resolve the
    project's active sprint) or ``"all"``. It defaults to ``"current"`` when a project
    is selected: an unscoped "everything ever assigned to me" list is the reason this
    widget read as a pile of unrelated work items.

    Results are cached per (user, project, type, iteration) for 60 seconds so multiple
    dashboard widgets that share this endpoint don't each trigger a full WIQL round-trip.
    """
    # This function is also called directly from ui.py (not only over HTTP), and a
    # direct call that omits an argument gets FastAPI's ``Query(...)`` object as the
    # value rather than None — which then blew up as
    # "'Query' object has no attribute 'strip'". Normalize before using any of them.
    project = _query_str(project)
    work_item_type = _query_str(work_item_type)
    iteration = _query_str(iteration)
    area_path = _query_str(area_path)

    if not ADO_BASE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_BASE_URL is not configured on the server",
        )

    # ── 60s per-user cache ────────────────────────────────────────────────
    # The dashboard renders this endpoint through the "work items" widget AND
    # normalizes it for the ADO items feed, so two concurrent browser requests
    # would otherwise run WIQL twice. Caching serialized output short-circuits
    # both. See backend/app/integrations_cache.py.
    try:
        from integrations_cache import cached_external
        owner = str(
            current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
        ).lower()

        def _produce():
            return _fetch_work_items_live(project, work_item_type, iteration, area_path, current_user)

        cached = cached_external(
            "ado",
            owner,
            f"work-items:{project or ''}:{work_item_type or ''}:{iteration or ''}:{area_path or ''}",
            _produce,
            ttl=60,
        )
        return cached
    except HTTPException:
        raise
    except Exception:
        # If the cache layer itself fails, fall back to a direct call.
        return _fetch_work_items_live(project, work_item_type, iteration, area_path, current_user)


@router.get("/workitem-tasks")
def get_workitem_tasks(
    id: int = Query(..., description="Parent Azure DevOps work item id"),
    current_user: AuthUser = Depends(get_current_user),
):
    """Return child tasks for a work item.

    We first fetch the parent item's
    hierarchy-forward relations and then batch-load those children. The frontend
    calls this lazily on hover, so we avoid adding extra weight to the normal
    dashboard work-items list.
    """
    if not ADO_BASE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_BASE_URL is not configured on the server",
        )

    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            bases = _discover_ado_bases(client)
            parent = None
            parent_base = ""
            for base_url in bases:
                res = client.get(
                    f"{base_url}/_apis/wit/workitems/{id}?$expand=relations&api-version=7.0"
                )
                if res.status_code == 200:
                    parent = res
                    parent_base = base_url
                    break
            if parent is None:
                raise HTTPException(status_code=404, detail="Azure DevOps work item was not found in accessible organizations")
            child_ids = []
            for rel in parent.json().get("relations", []) or []:
                if rel.get("rel") != "System.LinkTypes.Hierarchy-Forward":
                    continue
                url = rel.get("url", "")
                child_id = url.rstrip("/").split("/")[-1]
                if child_id.isdigit():
                    child_ids.append(child_id)

            if not child_ids:
                return {"success": True, "data": [], "timestamp": _now_iso()}

            tasks = []
            for i in range(0, len(child_ids), 200):
                chunk = ",".join(child_ids[i : i + 200])
                res = client.get(
                    f"{parent_base}/_apis/wit/workitems?ids={chunk}"
                    "&fields=System.Id,System.Title,System.State,System.WorkItemType"
                    "&api-version=7.0"
                )
                res.raise_for_status()
                for item in res.json().get("value", []):
                    fields = item.get("fields", {})
                    tasks.append(
                        {
                            "id": item.get("id"),
                            "title": fields.get("System.Title") or f"Task #{item.get('id')}",
                            "state": fields.get("System.State") or "",
                            "type": fields.get("System.WorkItemType") or "",
                            "url": f"{parent_base}/_workitems/edit/{item.get('id')}",
                        }
                    )
        return {"success": True, "data": tasks, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps API error: {exc.response.status_code}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Azure DevOps request failed: {exc!s}")


def _query_str(value: Any) -> Optional[str]:
    """Coerce a possibly-unresolved FastAPI ``Query`` default into a plain str/None.

    Endpoints in this module double as plain Python functions (ui.py imports and calls
    them). When such a call omits an argument, the parameter's default — the ``Query``
    object itself — arrives instead of None, and every string operation on it fails.
    """
    return value if isinstance(value, str) else None


def _wiql_literal(value: str) -> str:
    """Escape a value for a single-quoted WIQL literal."""
    return str(value or "").replace("'", "''")


# States that mean "somebody has to look at this before it can move on". Process
# templates name it differently — Agile, Scrum, CMMI and every customised inherited
# process disagree — so the list is configurable and the default is generous. A state
# named here that does not exist in a project simply never matches.
_REVIEW_STATES: List[str] = [
    s.strip()
    for s in os.getenv(
        "ADO_REVIEW_STATES",
        "Pending Review,Needs Review,Ready for Review,In Review,Code Review,"
        "Review,Awaiting Review,Pending Approval,Ready for Test",
    ).split(",")
    if s.strip()
]

# Some inherited processes carry an explicit reviewer field rather than reassigning
# the item. Set ADO_REVIEWER_FIELD to its reference name (e.g. "Custom.Reviewer") and
# it is matched in addition to AssignedTo. Left unset, only AssignedTo is used —
# guessing a field name that does not exist makes the whole WIQL 400.
_REVIEWER_FIELD = (os.getenv("ADO_REVIEWER_FIELD") or "").strip()


def _identity_display(value: Any) -> str:
    """
    A readable name out of an Azure DevOps identity field.

    Work-item identity fields come back either as an object with displayName/uniqueName,
    or — on older Server responses — as the bare string "Display Name <DOMAIN\\user>".
    Both shapes appear on the same server, so both are handled here rather than at each
    call site.
    """
    if not value:
        return ""
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("uniqueName") or "").strip()
    text = str(value).strip()
    if "<" in text:
        text = text.split("<", 1)[0].strip()
    return text


def get_work_items_awaiting_my_review(current_user: AuthUser) -> List[Dict[str, Any]]:
    """
    Work items sitting in a review state with this user on the hook for them.

    This is the half of "needs you" that the pull-request source cannot see. A PR is
    only one kind of review; a story parked in "Pending Review" and assigned to you is
    just as blocking and appears in no widget as anything other than another row in
    "My Work Items", indistinguishable from the twenty items that are merely yours.

    Identity comes from ``@Me``, which Azure DevOps resolves against the PAT OWNER —
    and the PAT here is the caller's own, never the admin one, so @Me is the signed-in
    user. That is also why this cannot be done with the admin token: it would silently
    answer for the wrong person.
    """
    pat = _get_pat_for_user(current_user)
    auth = httpx.BasicAuth("", pat)
    states = ", ".join(f"'{_wiql_literal(s)}'" for s in _REVIEW_STATES)
    if not states:
        return []

    reviewer_clause = "[System.AssignedTo] = @Me"
    if _REVIEWER_FIELD:
        reviewer_clause = f"([System.AssignedTo] = @Me OR [{_REVIEWER_FIELD}] = @Me)"

    query = {
        "query": (
            "Select [System.Id], [System.Title], [System.State], "
            "[System.WorkItemType], [System.TeamProject] "
            "From WorkItems "
            f"Where {reviewer_clause} "
            f"AND [System.State] IN ({states}) "
            "Order By [System.ChangedDate] Asc"
        )
    }

    out: List[Dict[str, Any]] = []
    with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        for base_url in _discover_ado_bases(client):
            try:
                r = client.post(f"{base_url}/_apis/wit/wiql?api-version=7.0", json=query)
                # A collection that does not know a field or state answers 400, exactly
                # like one that does not hold the project. Skip it; the others answer.
                if r.status_code in _ADO_NOT_MINE:
                    continue
                r.raise_for_status()
                ids = [str(item["id"]) for item in r.json().get("workItems", [])][:50]
                if not ids:
                    continue
                r2 = client.get(
                    f"{base_url}/_apis/wit/workitems?ids={','.join(ids)}"
                    "&fields=System.Id,System.Title,System.State,System.WorkItemType,"
                    "System.TeamProject,System.ChangedDate,System.AssignedTo,"
                    "System.CreatedBy,System.ChangedBy"
                    "&api-version=7.0"
                )
                r2.raise_for_status()
                for it in r2.json().get("value", []):
                    fields = it.get("fields", {})
                    project = fields.get("System.TeamProject") or ""
                    out.append(
                        {
                            "id": it.get("id"),
                            "title": fields.get("System.Title") or f"#{it.get('id')}",
                            "state": fields.get("System.State") or "",
                            "type": fields.get("System.WorkItemType") or "Work item",
                            "project": project,
                            "collection": _collection_name(base_url),
                            "changed_date": fields.get("System.ChangedDate"),
                            # Who is on the hook and who put it there. An item in
                            # "Pending Review" tells you nothing about WHOSE review is
                            # blocked without these.
                            "assigned_to": _identity_display(fields.get("System.AssignedTo")),
                            "created_by": _identity_display(fields.get("System.CreatedBy")),
                            "changed_by": _identity_display(fields.get("System.ChangedBy")),
                            "url": f"{base_url}/{quote(project)}/_workitems/edit/{it.get('id')}",
                        }
                    )
            except HTTPException:
                raise
            except Exception as exc:
                log.warning("review work items: %s failed: %s", base_url, exc)
                continue
    return out


def _fetch_work_items_live(
    project: Optional[str],
    work_item_type: Optional[str],
    iteration: Optional[str],
    area_path: Optional[str],
    current_user: AuthUser,
) -> Dict[str, Any]:
    """Uncached real-mode WIQL fetch. Raises HTTPException on failure."""
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)

        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            work_items = []
            for base_url in _discover_ado_bases(client):
                # @Me resolves to the identity that owns the PAT — no username needed.
                project_clause = (
                    f"AND [System.TeamProject] = '{_wiql_literal(project)}' " if project else ""
                )
                type_clause = (
                    f"AND [System.WorkItemType] = '{_wiql_literal(work_item_type)}' "
                    if work_item_type
                    else ""
                )

                # Sprint scope. "current" resolves the project's active iteration; an
                # explicit path is used as given. UNDER (not =) so items filed against a
                # sub-iteration of the sprint still count.
                iteration_path = ""
                wanted = (iteration or "").strip()
                if project and wanted.lower() != "all":
                    if not wanted or wanted.lower() == "current":
                        current = next(
                            (i for i in _fetch_iterations(client, base_url, project) if i["is_current"]),
                            None,
                        )
                        iteration_path = (current or {}).get("path") or ""
                    else:
                        iteration_path = wanted
                iteration_clause = (
                    f"AND [System.IterationPath] UNDER '{_wiql_literal(iteration_path)}' "
                    if iteration_path
                    else ""
                )

                # Area scope. UNDER, not =, because area paths are a tree: picking a
                # parent area is meant to include everything filed beneath it. "all"
                # (or nothing) means every area.
                wanted_area = (area_path or "").strip()
                area_clause = (
                    f"AND [System.AreaPath] UNDER '{_wiql_literal(wanted_area)}' "
                    if wanted_area and wanted_area.lower() != "all"
                    else ""
                )

                # Epics and Features are PLANNING artefacts, not personal work. They are
                # usually assigned to a lead — or to nobody — so filtering them by
                # "assigned to me" showed an empty list to everyone except that one lead,
                # which is not what a person picking "Epics" is asking to see. They want
                # the epics, all of them. Everything else (Task, Bug, User Story) stays
                # scoped to the signed-in user, which is what "My Work Items" means.
                is_portfolio = (work_item_type or "").strip().lower() in {"epic", "feature"}
                assigned_clause = "" if is_portfolio else "AND [System.AssignedTo] = @Me "

                query = {
                    "query": (
                        "Select [System.Id], [System.Title], [System.State], "
                        "[System.WorkItemType], [System.TeamProject] "
                        "From WorkItems "
                        "Where [System.State] <> 'Closed' "
                        "AND [System.State] <> 'Done' "
                        "AND [System.State] <> 'Removed' "
                        f"{assigned_clause}"
                        f"{project_clause}"
                        f"{type_clause}"
                        f"{iteration_clause}"
                        f"{area_clause}"
                        "Order By [System.ChangedDate] Desc"
                    )
                }

                wiql_url = f"{base_url}/_apis/wit/wiql?api-version=7.0"
                r = client.post(wiql_url, json=query)
                # 400 is skipped alongside 401/403/404 on purpose. Since discovery began
                # querying EVERY collection, a project-scoped WIQL runs against collections
                # that don't contain the selected project — and Azure DevOps validates
                # [System.TeamProject] in the query text, so it answers 400 ("project does
                # not exist") for those. That is this collection saying "not mine", exactly
                # like a 404; the collection that owns the project still answers. Without
                # this, picking any project 400'd the whole widget.
                if r.status_code in _ADO_NOT_MINE:
                    continue
                r.raise_for_status()
                ids = [str(item["id"]) for item in r.json().get("workItems", [])]
                if not ids:
                    continue

                # Fetch full details in batches of 200 (API limit)
                items: list = []
                for i in range(0, len(ids), 200):
                    chunk = ",".join(ids[i : i + 200])
                    r2 = client.get(
                        f"{base_url}/_apis/wit/workitems?ids={chunk}"
                        "&fields=System.Id,System.Title,System.State,System.WorkItemType,"
                        "System.TeamProject,System.AssignedTo,System.CreatedDate,System.ChangedDate,"
                        "System.IterationPath,System.AreaPath"
                        "&api-version=7.0"
                    )
                    r2.raise_for_status()
                    items.extend(r2.json().get("value", []))

                # Build state-category map per (project, type) – one API call each, minimal overhead
                _cat_cache: dict = {}

                for it in items:
                    fields = it.get("fields", {})
                    wi_id = it.get("id")
                    wi_state = fields.get("System.State") or ""
                    wi_type = fields.get("System.WorkItemType") or ""
                    wi_project = fields.get("System.TeamProject") or ""

                    cache_key = (base_url, wi_project, wi_type)
                    if cache_key not in _cat_cache:
                        _cat_cache[cache_key] = _fetch_state_categories(client, base_url, wi_project, wi_type)
                    state_category = _cat_cache[cache_key].get(wi_state, "")

                    portal_url = (
                        f"{base_url}/{wi_project}/_workitems/edit/{wi_id}"
                        if wi_project
                        else f"{base_url}/_workitems/edit/{wi_id}"
                    )
                    work_items.append(
                        {
                            "id": wi_id,
                            "title": fields.get("System.Title"),
                            "state": wi_state,
                            "state_category": state_category,
                            "type": wi_type,
                            "project": wi_project,
                            "iteration": fields.get("System.IterationPath") or "",
                            "area_path": fields.get("System.AreaPath") or "",
                            "assigned_to": fields.get("System.AssignedTo"),
                            "created_date": fields.get("System.CreatedDate"),
                            "changed_date": fields.get("System.ChangedDate"),
                            "url": portal_url,
                        }
                    )

        return {"success": True, "data": work_items, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        import logging
        code = exc.response.status_code
        # A cross-collection 400 is expected traffic, not a fault: it is one collection
        # saying "not mine". Logging it at WARNING is what filled the Logs page with a
        # hundred identical Azure DevOps errors while everything was working. Kept at
        # debug so it is still there when actually investigating.
        if code in _ADO_NOT_MINE:
            logging.debug("Azure DevOps %s (treated as not-mine): %s", code, exc.response.text[:200])
        else:
            logging.warning("Azure DevOps API error: %s %s", code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_describe_ado_status(code),
        )
    except HTTPException:
        # A "PAT not connected" 400 (or any deliberate HTTP error) is control flow,
        # not a crash. Let it propagate untouched — logging it via logging.exception
        # below would mirror it into the Logs page as a portal error and re-wrap the
        # 400 as a 502, which is exactly the spurious "not connected" spam users saw
        # while Azure DevOps was in fact connected.
        raise
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps WIQL request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


# Azure DevOps reviewer votes. 10 = approved, 5 = approved with suggestions,
# 0 = no vote yet, -5 = waiting for the author, -10 = rejected.
# Anything >= 5 means "I have signed this off"; everything else still wants me.
_VOTE_APPROVED = 5


@router.get("/pull-requests")
def get_pull_requests(
    username: Optional[str] = Query(None),
    collection: Optional[str] = Query(None, description="Collection name; omit for all"),
    project: Optional[str] = Query(None, description="Project name; omit for all"),
    repository: Optional[str] = Query(None, description="Repository name; omit for all"),
    role: Optional[str] = Query(
        None,
        description="'created' = PRs I opened, 'review' = PRs still awaiting my review; omit for both",
    ),
    current_user: AuthUser = Depends(get_current_user),
):
    effective_username = username or current_user.get("username")
    if not effective_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username required",
        )
    collection = _query_str(collection)
    project = _query_str(project)
    repository = _query_str(repository)
    role = (_query_str(role) or "").lower()

    if not ADO_BASE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_BASE_URL is not configured on the server",
        )

    # 60s per-(user, effective_username) cache. PR listing iterates every repo
    # in every project so is by far the most expensive dashboard call; two
    # widgets (authored / reviewer) render it concurrently.
    #
    # Only the COLLECTION is part of the cache key, because it is the only filter
    # that changes what we fetch upstream. Project/repository/role are applied to
    # the cached result, so flipping between projects (or between the two widgets)
    # re-uses one fetch instead of paying for a fresh crawl each time.
    try:
        from integrations_cache import cached_external
        owner = str(
            current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
        ).lower()

        def _produce():
            return _fetch_pull_requests_live(effective_username, current_user, collection)

        result = cached_external(
            "ado",
            owner,
            f"pull-requests:{effective_username}:{(collection or '').lower()}",
            _produce,
            ttl=60,
        )
    except HTTPException:
        raise
    except Exception:
        result = _fetch_pull_requests_live(effective_username, current_user, collection)

    return _filter_pull_requests(result, project, repository, role)


def _filter_pull_requests(
    result: Dict[str, Any],
    project: Optional[str],
    repository: Optional[str],
    role: str,
) -> Dict[str, Any]:
    """Narrow a fetched PR list to one project/repo/role.

    Split out of the fetch so the expensive upstream crawl is cached once and the
    cheap narrowing runs per request — and so both widgets agree on what "mine"
    and "still needs me" mean instead of each re-deriving it.
    """
    rows = list(result.get("data") or [])

    if project:
        rows = [p for p in rows if (p.get("project") or "").lower() == project.lower()]
    if repository:
        rows = [p for p in rows if (p.get("repository") or "").lower() == repository.lower()]

    if role == "created":
        rows = [p for p in rows if p.get("is_creator")]
    elif role == "review":
        # A PR I have already approved is not waiting on me, so it drops off the
        # review list. Rejected / waiting-for-author / not-yet-voted all still are,
        # so they stay — the point of the widget is "what still needs me".
        rows = [
            p for p in rows
            if p.get("is_reviewer") and int(p.get("my_vote") or 0) < _VOTE_APPROVED
        ]

    return {**result, "data": rows}


def _fetch_pull_requests_live(
    effective_username: str,
    current_user: AuthUser,
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Uncached real-mode PR fetch. Raises HTTPException on failure.

    The per-project/per-repo PR enumeration used to run sequentially, which
    made the dashboard widget feel sluggish on any org with more than a
    handful of repos. We now parallelize the repo+PR GETs through a
    ``ThreadPoolExecutor`` while keeping the outer httpx client around for
    connection reuse.
    """
    from concurrent.futures import ThreadPoolExecutor
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            projects: List[Dict[str, Any]] = []
            me: Dict[str, str] = {}
            bases = _discover_ado_bases(client)
            if collection:
                # Fall back to every collection if the name matches none, rather than
                # returning an empty widget for what is probably a stale saved filter.
                bases = [b for b in bases if _collection_name(b).lower() == collection.lower()] or bases
            for base_url in bases:
                # Who the PAT belongs to, as ADO spells it — the same identity in every
                # collection, so the first one that answers is enough.
                if not me:
                    me = _ado_identity(client, base_url)
                r_projects = client.get(f"{base_url}/_apis/projects?api-version=7.0")
                # 400 skipped alongside 401/403/404: a collection this token can't answer
                # for must not raise and kill the whole widget (which is why both PR widgets
                # showed NOTHING — one bad collection 502'd the lot) nor log a spurious
                # "API error: 400".
                if r_projects.status_code in _ADO_NOT_MINE:
                    continue
                r_projects.raise_for_status()
                projects.extend({**p, "_ado_base_url": base_url} for p in r_projects.json().get("value", []))

            # "Mine" is matched on NORMALISED account forms, not raw string equality. On
            # on-prem ADO a PR's createdBy/reviewer uniqueName is DOMAIN\\user, which never
            # equals the portal login (an email) — so the old exact match filtered out every
            # PR, including the user's own, and both widgets showed nothing. This is the same
            # identity handling the pipelines widget already uses.
            mine_forms = _my_account_forms(me, current_user, effective_username)

            # ONE query per project, not one per repo. The project-level pull-requests
            # endpoint returns active PRs across every repo in the project, and each PR
            # already carries its repository — so an org with hundreds of repos is a handful
            # of calls, not hundreds. This is why the PR widgets were by far the slowest.
            def _prs_for_project(proj: Dict[str, Any]):
                base_url = proj.get("_ado_base_url") or ADO_BASE
                project_id = proj.get("id")
                url = (
                    f"{base_url}/{project_id}/_apis/git/pullrequests"
                    "?searchCriteria.status=active&$top=200&api-version=7.0"
                )
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        return proj, []
                    return proj, r.json().get("value", []) or []
                except Exception:
                    return proj, []

            pull_requests: List[Dict[str, Any]] = []
            max_workers = min(16, max(4, len(projects) or 4))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for proj, prs in pool.map(_prs_for_project, projects):
                    base_url = proj.get("_ado_base_url") or ADO_BASE
                    project_name = proj.get("name")
                    for pr in prs:
                        created_by = pr.get("createdBy", {}).get("displayName", "Unknown")
                        created_by_email = pr.get("createdBy", {}).get("uniqueName", "").lower()

                        # Match on normalised account forms (DOMAIN\\user ≡ user@corp ≡ user),
                        # not raw equality — see mine_forms above.
                        is_creator = bool(_person_forms(pr.get("createdBy")) & mine_forms)
                        my_vote = 0
                        is_reviewer = False
                        for rev in pr.get("reviewers", []) or []:
                            if _person_forms(rev) & mine_forms:
                                is_reviewer = True
                                try:
                                    my_vote = int(rev.get("vote") or 0)
                                except (TypeError, ValueError):
                                    my_vote = 0
                                break

                        if is_creator or is_reviewer:
                            repo = pr.get("repository") or {}
                            repo_name = repo.get("name")
                            pr_id = pr.get("pullRequestId")
                            portal_url = f"{base_url}/{project_name}/_git/{repo_name}/pullrequest/{pr_id}"

                            pull_requests.append({
                                "id": pr_id,
                                "title": pr.get("title"),
                                "status": pr.get("status", "").lower(),
                                "created_by": created_by,
                                "created_by_email": created_by_email,
                                "created_date": pr.get("creationDate"),
                                "repository": repo_name,
                                "collection": _collection_name(base_url),
                                "project": project_name,
                                "source_branch": pr.get("sourceRefName", "").replace("refs/heads/", ""),
                                "target_branch": pr.get("targetRefName", "").replace("refs/heads/", ""),
                                # Both flags are computed HERE, where the identity forms
                                # live. The UI layer used to re-derive "is this mine?" by
                                # comparing created_by_email to the portal username as raw
                                # strings, which never matches an on-prem DOMAIN\\user — so
                                # "My opened PRs" was permanently empty even though this
                                # function had already worked out the answer correctly.
                                "is_creator": is_creator,
                                "is_reviewer": is_reviewer,
                                "my_vote": my_vote,
                                "url": portal_url,
                            })

        return {"success": True, "data": pull_requests, "timestamp": _now_iso()}
    except httpx.HTTPStatusError as exc:
        import logging
        code = exc.response.status_code
        # A cross-collection 400 is expected traffic, not a fault: it is one collection
        # saying "not mine". Logging it at WARNING is what filled the Logs page with a
        # hundred identical Azure DevOps errors while everything was working. Kept at
        # debug so it is still there when actually investigating.
        if code in _ADO_NOT_MINE:
            logging.debug("Azure DevOps %s (treated as not-mine): %s", code, exc.response.text[:200])
        else:
            logging.warning("Azure DevOps API error: %s %s", code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_describe_ado_status(code),
        )
    except HTTPException:
        # Deliberate HTTP error (e.g. PAT not connected). Propagate, don't log-and-wrap.
        raise
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps PR request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


def _collection_name(base_url: str) -> str:
    """The collection segment of an Azure DevOps base URL (last non-empty path part)."""
    parts = [p for p in str(base_url or "").rstrip("/").split("/") if p]
    return parts[-1] if parts else str(base_url or "")


def _ado_identity(client: httpx.Client, base_url: str) -> Dict[str, str]:
    """Who the PAT belongs to, as Azure DevOps itself sees them.

    The pipelines widget was always empty because it compared a build's
    ``requestedFor.uniqueName`` (a domain account, e.g. DOMAIN\\user or an email)
    against the HUB's username — two different identity namespaces, which never match,
    so every build was filtered out. Ask ADO who the token is instead.
    """
    try:
        r = client.get(f"{base_url}/_apis/connectionData?api-version=7.0")
        if r.status_code != 200:
            return {}
        user = (r.json() or {}).get("authenticatedUser") or {}
        props = user.get("properties") or {}
        account = props.get("Account") or {}
        return {
            "id": str(user.get("id") or ""),
            "unique_name": str(account.get("$value") or user.get("subjectDescriptor") or ""),
            "display_name": str(user.get("providerDisplayName") or ""),
        }
    except Exception:
        return {}


def _account_forms(value: str) -> set:
    """Every way one account can be spelled, reduced to a comparable set.

    Azure DevOps writes the same person three different ways depending on where you read
    them from: ``DOMAIN\\jsmith``, ``jsmith@corp.example``, or just ``jsmith``. Comparing
    those literally never matches, which is what left the pipelines widget empty. Reduce
    each to its bare account name and compare THAT as well as the full string.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return set()
    forms = {raw}
    if "\\" in raw:  # DOMAIN\user
        forms.add(raw.rsplit("\\", 1)[-1])
    if "@" in raw:  # user@domain
        forms.add(raw.split("@", 1)[0])
    forms.discard("")
    return forms


def _person_forms(person: Dict[str, Any]) -> set:
    """Every spelling of an ADO person record (createdBy / reviewer / requestedFor).

    Azure DevOps returns people with ``uniqueName`` (``DOMAIN\\user`` on-prem, an email
    in the cloud), ``displayName`` and ``id``. Reduce all three to their comparable
    account forms so a person can be matched no matter which field ADO populated.
    """
    forms = set()
    for field in ("uniqueName", "displayName", "id"):
        forms |= _account_forms((person or {}).get(field))
    return forms


def _my_account_forms(me: Dict[str, str], current_user: AuthUser, effective_username: str) -> set:
    """The signed-in user's identity as every spelling ADO might use.

    ``me`` comes from /_apis/connectionData, which needs a PAT scope the portal does not
    request, so it is routinely empty — the portal login (email / username) is the
    identity of last resort, exactly as the pipelines widget does it.
    """
    forms = set()
    for value in (
        me.get("id"), me.get("unique_name"), me.get("display_name"),
        current_user.get("email"), current_user.get("username"), effective_username,
    ):
        forms |= _account_forms(value)
    return forms


def _is_mine(build: Dict[str, Any], me: Dict[str, str], user_email: str) -> bool:
    """Was this build queued by the signed-in user?

    ``me`` comes from /_apis/connectionData, which requires the PAT to carry the *profile*
    scope — and a PAT scoped the way the portal asks for it does not have it. So ``me`` is
    routinely EMPTY, and when it was, nothing matched and the widget showed nothing. The
    signed-in user's own email/username is always available, so it is used as the identity
    of last resort and the comparison is done on normalised account names.
    """
    who = build.get("requestedFor") or {}
    candidates = set()
    for field in ("uniqueName", "displayName", "id"):
        candidates |= _account_forms(who.get(field))

    mine = set()
    for value in (me.get("id"), me.get("unique_name"), me.get("display_name"), user_email):
        mine |= _account_forms(value)

    return bool(candidates & mine)


def _run_status(build: Dict[str, Any]) -> str:
    """Normalise ADO's two-field status/result into one word the widget can colour.

    ADO splits this across `status` (notStarted/inProgress/completed/cancelling) and
    `result` (succeeded/failed/canceled/partiallySucceeded), and `result` is only set
    once the run finishes — so neither field alone tells you what happened.
    """
    status_raw = str(build.get("status") or "").lower()
    result_raw = str(build.get("result") or "").lower()
    if status_raw in {"notstarted", "postponed"}:
        return "pending"
    if status_raw in {"inprogress", "cancelling"}:
        return "running"
    if result_raw == "succeeded":
        return "succeeded"
    if result_raw == "partiallysucceeded":
        return "partial"
    if result_raw == "failed":
        return "failed"
    if result_raw == "canceled":
        return "canceled"
    return status_raw or "unknown"


@router.get("/provisioning/collections")
def provisioning_collections(current_user: AuthUser = Depends(get_current_user)):
    """The collections a request may target, and which one is preselected."""
    return {
        "success": True,
        "data": {"collections": list(_TARGET_COLLECTIONS), "default": _DEFAULT_COLLECTION},
        "timestamp": _now_iso(),
    }


@router.get("/provisioning/name-check")
def provisioning_name_check(
    name: str = Query(..., description="Proposed project name"),
    collection: Optional[str] = Query(None, description="Target collection; omit for the default"),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Is this project name free in the target collection?

    Checked BEFORE the request is submitted, because the alternative is what has been
    happening: the requester waits for an admin to approve, the approval runs, and only
    then does Azure DevOps say the name is taken — a round trip through a human to learn
    something that was knowable at typing time.

    Read-only, and it uses the admin PAT because a requester's own token often cannot see
    the target collection at all. It returns nothing but a boolean and the name that
    collided, so it cannot be used to enumerate anything a project list would not.
    """
    wanted = (collection or "").strip() or _DEFAULT_COLLECTION
    if _TARGET_COLLECTIONS and wanted.lower() not in {c.lower() for c in _TARGET_COLLECTIONS}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{wanted}' is not a collection available for provisioning.",
        )

    candidate = sanitize_ado_name((name or "").strip())
    if not candidate:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A project name is required.")

    try:
        with _admin_ado_client() as client:
            targets, _missing = resolve_collections_named(client, [wanted])
            _name, base = targets[0]
            rp = client.get(f"{base}/_apis/projects?$top=500&api-version=7.0")
            rp.raise_for_status()
            taken = any(
                (p.get("name") or "").strip().lower() == candidate.lower()
                for p in rp.json().get("value", [])
            )
        return {
            "success": True,
            "data": {"name": candidate, "collection": wanted, "available": not taken},
            "timestamp": _now_iso(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        # A check that cannot run must not block the request — it is an early warning,
        # not the authority. The create still verifies before it writes.
        logging.warning("name-check failed for %s in %s: %s", candidate, wanted, exc)
        return {
            "success": True,
            "data": {
                "name": candidate,
                "collection": wanted,
                "available": None,
                "detail": explain_integration_failure("azure_devops", exc),
            },
            "timestamp": _now_iso(),
        }


@router.get("/collections")
def get_collections(current_user: AuthUser = Depends(get_current_user)):
    """The Azure DevOps collections this PAT can reach — the pipelines widget's first selector."""
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            bases = _discover_ado_bases(client)
        return {
            "success": True,
            "data": [{"name": _collection_name(b), "url": b} for b in bases],
            "timestamp": _now_iso(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("Azure DevOps collections request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc}",
        )


@router.get("/repositories")
def get_repositories(
    project: Optional[str] = Query(None, description="Project name; required for a useful list"),
    collection: Optional[str] = Query(None, description="Collection name; omit for all"),
    current_user: AuthUser = Depends(get_current_user),
):
    """Git repositories in a project — the PR widgets' third selector.

    Scoped to a project on purpose: asking every collection for every repository is
    a slow call that returns a list too long to pick from. With no project selected
    the widgets simply offer "All repositories".
    """
    if not project:
        return {"success": True, "data": [], "timestamp": _now_iso()}
    try:
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        repos: List[Dict[str, Any]] = []
        seen: set = set()
        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            bases = _discover_ado_bases(client)
            if collection:
                bases = [b for b in bases if _collection_name(b).lower() == collection.lower()] or bases
            for base_url in bases:
                r = client.get(f"{base_url}/{project}/_apis/git/repositories?api-version=7.0")
                # 400 alongside 401/403/404: a collection that does not hold this
                # project answers 400, which is a "not mine", not a failure.
                if r.status_code in _ADO_NOT_MINE:
                    continue
                for repo in (r.json() or {}).get("value", []) or []:
                    name = str(repo.get("name") or "")
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        repos.append({"name": name, "project": project})
        repos.sort(key=lambda x: x["name"].lower())
        return {"success": True, "data": repos, "timestamp": _now_iso()}
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning("Azure DevOps repositories request failed: %s", exc)
        # A missing repo list must not break the widget — it just means the third
        # selector stays on "All repositories".
        return {"success": True, "data": [], "timestamp": _now_iso()}


@router.get("/pipelines")
def get_pipelines(
    project: Optional[str] = Query(None),
    collection: Optional[str] = Query(None, description="Collection name; omit for all"),
    scope: Optional[str] = Query("mine", description="'mine' = runs I queued, 'all' = every run"),
    current_user: AuthUser = Depends(get_current_user),
):
    project = _query_str(project)
    collection = _query_str(collection)
    scope = (_query_str(scope) or "mine").lower()
    if not ADO_BASE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AZURE_DEVOPS_BASE_URL is not configured on the server",
        )
    try:
        from concurrent.futures import ThreadPoolExecutor
        pat = _get_pat_for_user(current_user)
        auth = httpx.BasicAuth("", pat)
        user_email = str(current_user.get("email") or "").strip()

        with httpx.Client(verify=tls_verify(), auth=auth, timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            bases = _discover_ado_bases(client)
            if collection:
                bases = [b for b in bases if _collection_name(b).lower() == collection.lower()] or bases

            # Diagnostics. Every step below can drop things silently — a collection the
            # PAT cannot enumerate, a project whose builds it cannot read — and when they
            # do, the widget looks empty for reasons nothing on screen explains. Counting
            # what was skipped, and why, is the difference between "the widget is broken"
            # and "the token cannot read Builds in 11 of 14 projects".
            diag: Dict[str, Any] = {
                "collections": len(bases),
                "collections_skipped": [],
                "projects": 0,
                "projects_unreadable": [],
                "builds_seen": 0,
                "builds_mine": 0,
                "requesters": [],
                "identity": [],
            }

            projects: List[Dict[str, Any]] = []
            identities: Dict[str, Dict[str, str]] = {}
            for base_url in bases:
                identities[base_url] = _ado_identity(client, base_url)
                r_projects = client.get(f"{base_url}/_apis/projects?api-version=7.0")
                # 400 skipped too — a collection this token can't enumerate must not raise
                # and log a spurious "API error: 400" while pipelines otherwise work fine.
                if r_projects.status_code in _ADO_NOT_MINE:
                    diag["collections_skipped"].append(
                        {"name": _collection_name(base_url), "status": r_projects.status_code}
                    )
                    continue
                r_projects.raise_for_status()
                projects.extend({**p, "_ado_base_url": base_url} for p in r_projects.json().get("value", []))

            target_projects = [
                p for p in projects
                if not project or project.lower() == (p.get("name") or "").lower()
            ]

            def _builds_for(proj):
                base_url = proj.get("_ado_base_url") or ADO_BASE
                pid = proj.get("id")
                # queueTimeDescending so a queued-but-not-started run still sorts to the
                # top — ordering on startTime hides exactly the runs you're waiting on.
                url = (
                    f"{base_url}/{pid}/_apis/build/builds"
                    "?$top=25&queryOrder=queueTimeDescending&api-version=7.0"
                )
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        # Reported, not swallowed. A PAT without Build (read) on a project
                        # answers 401/403 here, and every one of those is a project whose
                        # runs are absent from the widget for a nameable reason.
                        return proj, [], r.status_code
                    return proj, r.json().get("value", []) or [], 200
                except Exception:
                    return proj, [], 0

            pipelines = []
            max_workers = min(16, max(4, len(target_projects) or 4))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                builds_by_project = list(pool.map(_builds_for, target_projects))

            diag["projects"] = len(target_projects)
            seen_requesters: List[str] = []

            for proj, builds, http_status in builds_by_project:
                project_name = proj.get("name")
                base_url = proj.get("_ado_base_url") or ADO_BASE
                me = identities.get(base_url) or {}
                if http_status != 200 and len(diag["projects_unreadable"]) < 12:
                    diag["projects_unreadable"].append(
                        {"name": project_name, "status": http_status}
                    )
                for build in builds:
                    who = build.get("requestedFor") or {}
                    # Who Azure DevOps says queued these runs. Seeing this next to who the
                    # portal thinks you are turns "why is this empty" into one glance.
                    label = str(who.get("uniqueName") or who.get("displayName") or "").strip()
                    if label and label not in seen_requesters and len(seen_requesters) < 8:
                        seen_requesters.append(label)
                    build_id = build.get("id")
                    pipelines.append({
                        "id": build_id,
                        "name": (build.get("definition") or {}).get("name") or project_name,
                        "run_id": build.get("buildNumber") or build_id,
                        "status": _run_status(build),
                        "result": str(build.get("result") or "").lower(),
                        "project": project_name,
                        "collection": _collection_name(base_url),
                        "requested_for": who.get("displayName") or who.get("uniqueName") or "",
                        # queueTime is the only timestamp every run has; startTime is null
                        # while a run is still queued.
                        "created_date": build.get("queueTime") or build.get("startTime"),
                        "started_date": build.get("startTime"),
                        "finished_date": build.get("finishTime"),
                        "url": f"{base_url}/{project_name}/_build/results?buildId={build_id}",
                        "mine": _is_mine(build, me, user_email),
                    })

            pipelines.sort(key=lambda x: str(x.get("created_date") or ""), reverse=True)

            # Identity matching is best-effort — /_apis/connectionData needs a PAT scope the
            # portal does not ask for, and on-prem writes accounts as DOMAIN\user, so "which
            # of these runs are mine" can genuinely come back unanswerable. When that
            # happens the widget must NOT show an empty box next to a project full of runs;
            # it shows every run and says so. An empty widget is indistinguishable from a
            # broken one, and this widget was reported as broken for exactly that reason.
            mine = [p for p in pipelines if p.get("mine")]
            # Two different reasons to show everything, and the widget needs to tell
            # them apart: the user ASKED for all runs (scope=all), or identity could not
            # be resolved at all and "mine" would be a misleading empty box.
            unresolved = not mine and bool(pipelines)
            showing_all = scope == "all" or unresolved
            data = (pipelines if showing_all else mine)[:25]

            any_me = next((identities[b] for b in identities if identities[b]), {})
            diag["builds_seen"] = len(pipelines)
            diag["builds_mine"] = len(mine)
            diag["requesters"] = seen_requesters
            diag["identity"] = sorted(
                _my_account_forms(any_me, current_user, user_email or str(current_user.get("username") or ""))
            )[:8]

            return {
                "success": True,
                "data": data,
                "showing_all": showing_all,
                # Counts are for the widget's Mine/All toggle: seeing "Mine 1 · All 47"
                # is what turns a suspiciously short list into an understandable one.
                "counts": {"mine": len(mine), "all": len(pipelines)},
                "identity_unresolved": unresolved,
                "diagnostics": diag,
                "timestamp": _now_iso(),
            }
    except httpx.HTTPStatusError as exc:
        import logging
        code = exc.response.status_code
        # A cross-collection 400 is expected traffic, not a fault: it is one collection
        # saying "not mine". Logging it at WARNING is what filled the Logs page with a
        # hundred identical Azure DevOps errors while everything was working. Kept at
        # debug so it is still there when actually investigating.
        if code in _ADO_NOT_MINE:
            logging.debug("Azure DevOps %s (treated as not-mine): %s", code, exc.response.text[:200])
        else:
            logging.warning("Azure DevOps API error: %s %s", code, exc.response.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_describe_ado_status(code),
        )
    except HTTPException:
        # Deliberate HTTP error (e.g. PAT not connected). Propagate, don't log-and-wrap.
        raise
    except Exception as exc:
        import logging
        logging.exception("Azure DevOps pipelines request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Azure DevOps request failed: {exc!s}",
        )


def sanitize_ado_name(name: str) -> str:
    """Return a name safe for Azure DevOps (letters, digits, hyphens, spaces, max 64 chars)."""
    s = re.sub(r"[^a-zA-Z0-9 _.\-]", "-", name)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]


# ── REST-only project provisioning (no Terraform, no Kubernetes) ─────────────
#
# The old path generated a Terraform module and ran it in a Kubernetes Job. On a
# cluster where the service account cannot create Jobs — which is the case here —
# that path cannot run at all, and it dragged along a tfstate backend and a
# Terraform container image for what is, underneath, a handful of REST calls.
#
# Terraform's only real job was ONE call: create the project. The inherited process,
# the uniqueness check and the admin assignment were already REST. So this does all of
# it in REST, from the backend, using the admin PAT — no Job, no pod, no state.
#
# The admin PAT (AZURE_DEVOPS_ADMIN_PAT) must carry, per collection:
#   * Project and Team (Read, Write & Manage)   — create the project
#   * Process (Read & Manage)                    — create the inherited process
#   * Graph (Read & Manage)                      — resolve the user + add them to admins
#   * Identity (Read) + Security (Manage)        — resolve the identity descriptor and
#                                                  write the process Administer ACL
#
# On Azure DevOps Server a process lives INSIDE a collection, so the inherited process
# is created independently in each target collection before the project that uses it.
# The chosen admin gets TWO grants, which are different permission systems: membership
# of the project's Project Administrators group (graph), and Administer on the inherited
# process (an ACL on the Process security namespace). Both are best-effort — the project
# is created regardless — and their outcome is reported in the result's per-collection
# ``grants`` plus a top-level ``grants_ok``.


def _admin_ado_client() -> httpx.Client:
    """An httpx client authenticated as the server-level admin PAT."""
    if not _ENV_ADMIN_PAT:
        raise RuntimeError(
            "AZURE_DEVOPS_ADMIN_PAT is not configured. Project provisioning needs an "
            "admin token with Project, Process and Graph (Read & Manage) scopes."
        )
    return httpx.Client(
        verify=tls_verify(),
        auth=httpx.BasicAuth("", _ENV_ADMIN_PAT),
        timeout=httpx.Timeout(60.0, connect=5.0),
    )


def resolve_provision_collections(client: httpx.Client) -> Tuple[List[Tuple[str, str]], List[str]]:
    """(name, base_url) for each configured target collection the server actually exposes.

    Returns ``(found, missing)``. Names are matched case-insensitively against the live
    collection list so a configured name that no longer exists is reported rather than
    silently provisioned into the wrong place. Raises if NONE of the targets are found —
    provisioning into zero collections is never what the user asked for.
    """
    return resolve_collections_named(client, _TARGET_COLLECTIONS)


def resolve_collections_named(
    client: httpx.Client, wanted: List[str]
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """The same resolution, for an explicit subset — one chosen collection, normally."""
    discovered = _discover_onprem_collections(client, ADO_BASE)  # base URLs, configured first
    by_name = {_collection_name(b).lower(): b for b in discovered}

    found: List[Tuple[str, str]] = []
    missing: List[str] = []
    for name in wanted:
        base = by_name.get(name.lower())
        if base:
            found.append((_collection_name(base), base))
        else:
            missing.append(name)

    if not found:
        available = ", ".join(sorted(_collection_name(b) for b in discovered)) or "none"
        raise RuntimeError(
            "None of the configured provisioning collections were found on the server "
            f"({', '.join(_TARGET_COLLECTIONS)}). Collections available to this token: {available}."
        )
    return found, missing


def _ensure_inherited_process(
    client: httpx.Client, base_url: str, project_name: str, process_type: str
) -> Dict[str, str]:
    """Ensure ``<project_name>-<process_type>`` exists in this collection; return name + typeId.

    Idempotent: an already-existing inherited process of that name is reused (the
    project-create call needs its ``typeId``, so we return it either way).
    """
    custom_name = f"{project_name}-{process_type.lower()}"
    procs_url = f"{base_url}/_apis/work/processes?api-version=7.1-preview.2"

    r = client.get(procs_url)
    r.raise_for_status()
    all_procs = r.json().get("value", [])

    existing = next(
        (
            p for p in all_procs
            if p.get("name", "").lower() == custom_name.lower()
            and p.get("customizationType") != "system"
        ),
        None,
    )
    if existing:
        return {
            "name": existing.get("name") or custom_name,
            "type_id": existing.get("typeId") or "",
            "parent_type_id": existing.get("parentProcessTypeId") or "",
        }

    parent = next(
        (
            p for p in all_procs
            if p.get("customizationType") == "system"
            and p.get("name", "").lower() == process_type.lower()
        ),
        None,
    )
    if not parent:
        available = ", ".join(
            p["name"] for p in all_procs if p.get("customizationType") == "system"
        )
        raise RuntimeError(
            f"Unknown process type '{process_type}'. Available system processes in "
            f"{_collection_name(base_url)}: {available or 'none'}."
        )

    # No description. The generated one ("Inherited Scrum process for X") said nothing
    # the process NAME does not already say, and it is not the requester's text — a
    # description field that nobody chose is noise on every process in the collection.
    rc = client.post(
        procs_url,
        json={
            "name": custom_name,
            "parentProcessTypeId": parent["typeId"],
            "isDefault": False,
            "isEnabled": True,
        },
    )
    if rc.status_code not in (200, 201):
        raise RuntimeError(
            f"Azure DevOps rejected the process creation in {_collection_name(base_url)} "
            f"(HTTP {rc.status_code}): {rc.text[:300]}"
        )
    created = rc.json()
    return {
        "name": created.get("name") or custom_name,
        "type_id": created.get("typeId") or "",
        "parent_type_id": created.get("parentProcessTypeId") or parent["typeId"],
    }


def _poll_operation(client: httpx.Client, base_url: str, operation_id: str, timeout: float = 180.0) -> None:
    """Block until an Azure DevOps async operation reaches a terminal state.

    Project creation returns an operation reference, not a finished project — so we poll
    it. Raises RuntimeError on failure or timeout with the server's own message.
    """
    deadline = time.time() + timeout
    op_url = f"{base_url}/_apis/operations/{operation_id}?api-version=7.0"
    while time.time() < deadline:
        ro = client.get(op_url)
        ro.raise_for_status()
        body = ro.json() or {}
        status_raw = str(body.get("status") or "").lower()
        if status_raw == "succeeded":
            return
        if status_raw in ("failed", "cancelled"):
            raise RuntimeError(body.get("resultMessage") or f"Operation {status_raw}.")
        time.sleep(3)
    raise RuntimeError("Timed out waiting for Azure DevOps to finish creating the project.")


def _create_project_in_collection(
    client: httpx.Client,
    base_url: str,
    project_name: str,
    process_type_id: str,
    description: str = "",
) -> str:
    """Create one project in one collection and wait for it. Returns the project URL.

    Raises RuntimeError (with a collection-named message) if the name is taken or the
    create is rejected, so the orchestrator can record a per-collection outcome.

    ``description`` is whatever the requester typed, or empty. It is no longer a
    generated string: "Provisioned via the DevOps Hub self-service (Scrum)" was written
    onto every project ever created here, where it displaced the one line that would
    have said what the project is FOR.
    """
    collection = _collection_name(base_url)

    rp = client.get(f"{base_url}/_apis/projects?$top=500&api-version=7.0")
    rp.raise_for_status()
    if any((p.get("name") or "").lower() == project_name.lower() for p in rp.json().get("value", [])):
        raise RuntimeError(f"A project named '{project_name}' already exists in {collection}.")

    body: Dict[str, Any] = {
        "name": project_name,
        "capabilities": {
            "versioncontrol": {"sourceControlType": "Git"},
            "processTemplate": {"templateTypeId": process_type_id},
        },
    }
    if (description or "").strip():
        body["description"] = description.strip()

    rc = client.post(f"{base_url}/_apis/projects?api-version=7.0", json=body)
    if rc.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Azure DevOps rejected the project creation in {collection} "
            f"(HTTP {rc.status_code}): {rc.text[:300]}"
        )

    operation_id = (rc.json() or {}).get("id")
    if operation_id:
        _poll_operation(client, base_url, str(operation_id))

    return f"{base_url}/{quote(project_name, safe='')}"


def _graph_subject_descriptor(client: httpx.Client, base_url: str, principal: str) -> str:
    """Graph subject descriptor for a principal, via subjectQuery.

    Fallback for when the Identities API row carries no ``subjectDescriptor`` (older
    Server builds). Best-effort: returns "" on any failure.
    """
    try:
        r = client.post(
            f"{base_url}/_apis/graph/subjectquery?api-version=7.0-preview.1",
            json={"query": principal, "subjectKind": ["User"]},
        )
        if not r.is_success:
            return ""
        vals = (r.json() or {}).get("value", []) or []
        return vals[0].get("descriptor", "") if vals else ""
    except Exception:
        return ""


# ── Identity, group and grant plumbing ──────────────────────────────────────
#
# THE PROBLEM THIS KEEPS RUNNING INTO: the chosen user is not in the collection yet.
#
# Azure DevOps adds whoever CREATES a project to its Project Administrators group, so
# the admin PAT's owner is always a project admin on a freshly provisioned project —
# that part needs no code and is why the token's owner kept being the only account with
# access. Our grant of the REQUESTED user is the part that was doing nothing.
#
# Both APIs that can grant have the same blind spot on Azure DevOps Server: an Active
# Directory user who has never signed in to that collection has no identity row and no
# graph subject in it. Look them up and you get an empty list, not an error — which is
# why three rounds of "find the user, then add them" found nobody and reported success.
# A domain account existing in AD is not the same as existing in the collection.
#
# So the order is now MATERIALISE FIRST, and only then grant:
#
#   1. Graph "create user" (``POST _apis/graph/users``) with the principal name. On an
#      AD-backed server this binds the domain account into the collection, creating the
#      identity that was missing — and with ``groupDescriptors`` it adds them to the
#      Project Administrators group in the SAME call. One request that both fixes the
#      cause and performs the grant.
#   2. The classic Identities membership route, for servers where Graph writes are off.
#   3. The Graph memberships PUT.
#
# The process ACL then re-resolves the identity, which now exists because of step 1.
#
# Every request records its HTTP status, so a failure says what the server answered
# instead of looking like an empty result. That is what the diagnostics card in Platform
# Managing reads.


def _identity_search(
    client: httpx.Client, base_url: str, mode: str, value: str
) -> Tuple[List[Dict[str, Any]], str]:
    """One Identities API query. Returns ``(rows, status)``; never raises.

    ``status`` is the HTTP code (or an error name) and is carried all the way out to the
    diagnostics UI: "0 rows" and "401 Unauthorized" are completely different problems and
    for three rounds they were indistinguishable from the outside.

    The api-version is tried newest-first because Server builds differ on which they
    accept, and an unsupported one answers 400 rather than falling back.
    """
    last = ""
    for api in ("6.0", "7.0", "5.0"):
        try:
            r = client.get(
                f"{base_url}/_apis/identities?api-version={api}"
                f"&searchFilter={mode}&filterValue={quote(value, safe='')}"
                "&queryMembership=None"
            )
        except Exception as exc:
            last = f"{type(exc).__name__}"
            continue
        if r.is_success:
            rows = (r.json() or {}).get("value", []) or []
            return rows, f"{r.status_code}/{len(rows)} rows"
        last = f"HTTP {r.status_code}"
        # A version the server does not know is worth retrying at another version;
        # a refusal is not — it will refuse identically every time.
        if r.status_code not in (400, 404, 406):
            break
    logging.warning(
        "identity search %s='%s' in %s -> %s",
        mode, value, _collection_name(base_url), last,
    )
    return [], last


def _materialize_user(
    client: httpx.Client, base_url: str, principal: str, group_descriptor: str = ""
) -> Dict[str, str]:
    """Bind an AD account into this collection — and, if given a group, into that group.

    This is the call that was missing. ``POST _apis/graph/users`` with a principalName
    creates the collection-local identity for a domain user who has never signed in;
    passing ``groupDescriptors`` makes the same request add them to the group. So for the
    common case — a brand-new project handed to someone who has never opened this
    collection — one request both creates the identity and grants Project Administrator.

    Returns ``{"status", "detail", "subject_descriptor"}``. Best-effort throughout.
    """
    tried: List[str] = []
    for api in ("6.0-preview.1", "7.0-preview.1", "5.0-preview.1"):
        url = f"{base_url}/_apis/graph/users?api-version={api}"
        if group_descriptor:
            url += f"&groupDescriptors={quote(group_descriptor, safe='')}"
        try:
            r = client.post(url, json={"principalName": principal})
        except Exception as exc:
            tried.append(f"{api}:{type(exc).__name__}")
            continue
        if r.status_code in (200, 201):
            try:
                body = r.json() or {}
            except Exception:
                body = {}
            return {
                "status": "ok",
                "detail": f"bound via graph/users@{api}"
                          + (" +group" if group_descriptor else ""),
                "subject_descriptor": body.get("descriptor") or "",
            }
        # 409 means the subject is already there, which is a success for our purpose.
        if r.status_code == 409:
            return {
                "status": "exists",
                "detail": f"already present (409@{api})",
                "subject_descriptor": "",
            }
        tried.append(f"{api}:HTTP {r.status_code} {r.text[:80]}")
        if r.status_code in (401, 403):
            break  # a permissions refusal will not change with the api-version
    return {"status": "failed", "detail": " | ".join(tried), "subject_descriptor": ""}


def _identity_prop(ident: Dict[str, Any], name: str) -> str:
    """Read an Identities API ``properties`` value, which may be a bare value or
    the ``{"$type": …, "$value": …}` wrapper depending on server version."""
    raw = (ident.get("properties") or {}).get(name)
    if isinstance(raw, dict):
        raw = raw.get("$value")
    return str(raw or "")


def _bare_account(value: str) -> str:
    """``CONTOSO\\jdoe`` / ``jdoe@corp.example`` / ``jdoe`` -> ``jdoe`` (lower-cased)."""
    return (value or "").split("\\")[-1].split("@")[0].strip().lower()


def _pick_identity(rows: List[Dict[str, Any]], candidate: str) -> Optional[Dict[str, Any]]:
    """Best USER match for ``candidate`` among Identities API rows.

    A search can return several rows (a user and a group of the same name, disabled
    duplicates from an old domain). Taking ``rows[0]`` blindly is how a grant lands on
    the wrong subject, so groups are excluded and an exact account match wins.
    """
    want = _bare_account(candidate)
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for row in rows:
        if _identity_prop(row, "SchemaClassName").lower() == "group":
            continue
        score = 0
        if want and _bare_account(_identity_prop(row, "Account")) == want:
            score += 4
        if want and _bare_account(row.get("providerDisplayName") or "") == want:
            score += 2
        if row.get("descriptor"):
            score += 1
        if row.get("subjectDescriptor"):
            score += 1
        if score > best_score:
            best, best_score = row, score
    return best


def _resolve_provision_identity(
    client: httpx.Client, base_url: str, principal: str
) -> Dict[str, Any]:
    """Resolve the requester's chosen admin in ONE collection.

    Tries every plausible form of the principal (see ``principal_candidates``) against
    both Identities search filters, and returns the first real user match. Always
    returns a dict — ``resolved`` says whether it found anyone, ``attempts`` says what
    was tried, so "nobody was granted" is never a silent outcome.
    """
    attempts: List[str] = []
    for candidate in principal_candidates(principal):
        # AccountName is the exact-match filter and goes first for a domain-qualified
        # form; General is the broad one. MailAddress and DisplayName catch servers that
        # index the account under neither of the first two.
        if "\\" in candidate:
            modes = ("AccountName", "General", "DisplayName")
        elif "@" in candidate:
            modes = ("MailAddress", "General", "AccountName")
        else:
            modes = ("AccountName", "General", "DisplayName")
        for mode in modes:
            rows, status_text = _identity_search(client, base_url, mode, candidate)
            attempts.append(f"{mode}:{candidate} -> {status_text}")
            ident = _pick_identity(rows, candidate) if rows else None
            if not ident:
                continue
            subject = ident.get("subjectDescriptor") or _graph_subject_descriptor(
                client, base_url, candidate
            )
            return {
                "resolved": True,
                "matched_as": f"{mode}:{candidate}",
                "attempts": attempts,
                "identity_id": str(ident.get("id") or ""),
                "identity_descriptor": ident.get("descriptor") or "",
                "subject_descriptor": subject or "",
                "account": _identity_prop(ident, "Account"),
                "display": (
                    ident.get("providerDisplayName")
                    or ident.get("customDisplayName")
                    or candidate
                ),
            }
    logging.warning(
        "Could not resolve '%s' in %s. Tried: %s",
        principal, _collection_name(base_url), "; ".join(attempts),
    )
    return {
        "resolved": False, "matched_as": "", "attempts": attempts,
        "identity_id": "", "identity_descriptor": "", "subject_descriptor": "",
        "account": "", "display": principal,
    }


def _project_admins_group_descriptor(
    client: httpx.Client, base_url: str, project_name: str
) -> str:
    """Graph descriptor of ``<project>``'s Project Administrators group, or "".

    Scoped correctly: the group's displayName is just "Project Administrators" — the
    project lives in the group's SCOPE, not its name — so we resolve the project's
    scope descriptor and list groups within it, rather than filtering displayName by
    "``<project>`` Project Administrators", a string that never matches.

    Kept as the FALLBACK behind the Identities lookup: on Server the Graph projection
    may not carry the group at all, which is why it cannot be the only route.
    """
    try:
        rp = client.get(
            f"{base_url}/_apis/projects/{quote(project_name, safe='')}?api-version=7.0"
        )
        project_id = (rp.json() or {}).get("id") if rp.is_success else ""
        if not project_id:
            return ""
        rd = client.get(f"{base_url}/_apis/graph/descriptors/{project_id}?api-version=7.0")
        scope = (rd.json() or {}).get("value", "") if rd.is_success else ""
        if not scope:
            return ""
        rg = client.get(
            f"{base_url}/_apis/graph/groups?scopeDescriptor={quote(scope, safe='')}&api-version=7.0"
        )
        groups = (rg.json() or {}).get("value", []) if rg.is_success else []
        for g in groups:
            if (g.get("displayName") or "").strip().lower() == "project administrators":
                return g.get("descriptor") or ""
        return ""
    except Exception as exc:
        logging.warning("admins-group graph lookup failed for %s: %s", project_name, exc)
        return ""


def _resolve_admins_group(
    client: httpx.Client, base_url: str, project_name: str
) -> Dict[str, Any]:
    """Resolve ``<project>``'s Project Administrators group, Identities API first.

    On-prem the group's canonical name is ``[<project>]\\Project Administrators`` — the
    square brackets are part of it. Returns ``identity_id``/``identity_descriptor`` when
    found that way (what the classic membership route needs), or just a Graph
    ``subject_descriptor`` from the fallback.
    """
    attempts: List[str] = []
    for value in (
        f"[{project_name}]\\Project Administrators",
        f"{project_name}\\Project Administrators",
    ):
        rows, status_text = _identity_search(client, base_url, "General", value)
        attempts.append(f"General:{value} -> {status_text}")
        for row in rows:
            name = str(
                row.get("providerDisplayName") or row.get("customDisplayName") or ""
            ).lower()
            if "project administrators" not in name:
                continue
            # Guard against the same group name in another project: the display name
            # carries the project in brackets, so require ours when it is present.
            if "[" in name and project_name.lower() not in name:
                continue
            return {
                "resolved": True, "via": "identities", "attempts": attempts,
                "identity_id": str(row.get("id") or ""),
                "identity_descriptor": row.get("descriptor") or "",
                "subject_descriptor": row.get("subjectDescriptor") or "",
            }

    graph_desc = _project_admins_group_descriptor(client, base_url, project_name)
    attempts.append(f"graph-scope->{'hit' if graph_desc else 'miss'}")
    if graph_desc:
        return {
            "resolved": True, "via": "graph", "attempts": attempts,
            "identity_id": "", "identity_descriptor": "", "subject_descriptor": graph_desc,
        }
    logging.warning(
        "Project Administrators group not found for '%s' in %s. Tried: %s",
        project_name, _collection_name(base_url), "; ".join(attempts),
    )
    return {
        "resolved": False, "via": "", "attempts": attempts,
        "identity_id": "", "identity_descriptor": "", "subject_descriptor": "",
    }


def _classic_membership_ok(response: httpx.Response) -> bool:
    """The classic Identities membership route answers 200 with a JSON boolean.

    A 200 carrying ``false`` means "not added" — treating any 200 as success is how a
    refused membership gets reported as granted.
    """
    if response.status_code not in (200, 201, 204):
        return False
    body = (response.text or "").strip().strip('"').lower()
    return body != "false"


def _assign_project_admin(
    client: httpx.Client, base_url: str, project_name: str, principal: str,
    identity: Dict[str, Any],
) -> Dict[str, Any]:
    r"""Put ``principal`` in ``<project>``'s Project Administrators group.

    Uses the legacy ``/_api/_identity/`` endpoints, because the Graph API is NOT ROUTED
    on this Azure DevOps Server — ``_apis/graph/*`` answers 404 and
    ``_apis/resourceAreas`` is empty. Every previous version of this function was built
    on Graph, so it was calling endpoints that do not exist here, and a 404 on a
    best-effort grant is indistinguishable from "user not found".

    ``AddIdentities`` does the whole job in one call: it resolves ``DOMAIN\user``
    against Active Directory, binds the account into the collection, and joins it to the
    group. There is no separate "materialise the user" step left to get wrong.

    Returns ``{"status", "detail", "identity"}``. ``identity`` carries the member row and
    its ACL descriptor when they could be read back, so the process grant that runs next
    has something to key on. Best-effort — never fails the provision.
    """
    groups, group_status = ado_identity.read_scoped_groups(client, base_url, project_name)
    if not groups:
        return {
            "status": "admins-group-not-found",
            "detail": f"could not read the project's groups ({group_status})",
            "identity": identity,
        }

    admins = ado_identity.find_group(groups, "Project Administrators")
    if not admins:
        available = ", ".join(sorted(ado_identity.bare_group_name(g) for g in groups))[:200]
        return {
            "status": "admins-group-not-found",
            "detail": f"no 'Project Administrators' among: {available}",
            "identity": identity,
        }

    group_tfid = str(admins.get("TeamFoundationId") or "")
    if not group_tfid:
        return {
            "status": "admins-group-not-found",
            "detail": "the group row carries no TeamFoundationId",
            "identity": identity,
        }

    # Every plausible spelling of the account, best first. The server resolves the name
    # against AD itself, so the only open question is which form it recognises.
    attempts: List[str] = []
    added: Optional[Dict[str, str]] = None
    for candidate in principal_candidates(principal):
        result = ado_identity.add_identities(
            client, base_url, project_name, group_tfid, new_users=[candidate]
        )
        attempts.append(f"{candidate} -> {result['status']}: {result['detail'][:120]}")
        if result["status"] == "granted":
            added = result
            break

    if not added:
        logging.warning(
            "Project Admin grant for '%s' on '%s' in %s failed. %s",
            principal, project_name, _collection_name(base_url), " | ".join(attempts),
        )
        return {"status": "failed", "detail": " | ".join(attempts), "identity": identity}

    # Read the group back and look for them in it. This both confirms the grant landed
    # and yields the TeamFoundationId the process ACL needs.
    members = ado_identity.read_group_members(client, base_url, project_name, group_tfid)
    member = ado_identity.find_member(members, principal)
    enriched = dict(identity)
    if member:
        tfid = str(member.get("TeamFoundationId") or "")
        domain = (member.get("Domain") or "").strip()
        account = (member.get("AccountName") or "").strip()
        enriched.update({
            "resolved": True,
            "team_foundation_id": tfid,
            "display": (
                member.get("DisplayName")
                or member.get("FriendlyDisplayName")
                or principal
            ),
            "account": f"{domain}\\{account}" if domain and account else account,
            "identity_descriptor": (
                identity.get("identity_descriptor")
                or ado_identity.descriptor_for_tfid(client, base_url, tfid)
            ),
        })

    return {
        "status": "granted",
        "detail": f"{added['detail']}; verified={str(member is not None).lower()}",
        "identity": enriched,
    }


def _verify_group_membership(
    client: httpx.Client, base_url: str, group: Dict[str, Any], identity: Dict[str, Any]
) -> Optional[bool]:
    """Read the group's members back and look for ours. ``None`` if unreadable.

    A write that returns 200 and a group that actually contains the user are two
    different claims. This checks the second one so "granted" means granted.
    """
    container = group.get("identity_id") or group.get("identity_descriptor") or ""
    if not container:
        return None
    try:
        r = client.get(
            f"{base_url}/_apis/identities/{quote(container, safe='')}/members?api-version=6.0"
        )
        if not r.is_success:
            return None
        members = (r.json() or {}).get("value", r.json())
        blob = str(members).lower()
    except Exception:
        return None
    for needle in (identity.get("identity_descriptor"), identity.get("identity_id")):
        if needle and str(needle).lower() in blob:
            return True
    return False


def _describe_process_namespace(client: httpx.Client, base_url: str) -> Dict[str, Any]:
    """Read-only: can we see the Process security namespace, and does it have Administer?

    Diagnostics only. If this says the namespace is missing or the PAT cannot read the
    namespace list, the process grant cannot work no matter what else is right — and
    that is worth knowing before provisioning rather than after.
    """
    try:
        r = client.get(f"{base_url}/_apis/securitynamespaces?api-version=6.0")
    except Exception as exc:
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}
    if not r.is_success:
        return {"reachable": False, "detail": f"HTTP {r.status_code} {r.text[:120]}"}
    ns = next(
        (n for n in ((r.json() or {}).get("value") or []) if (n.get("name") or "") == "Process"),
        None,
    )
    if not ns:
        return {"reachable": True, "found": False, "detail": "no namespace named 'Process'"}
    actions = [str(a.get("name")) for a in (ns.get("actions") or [])]
    return {
        "reachable": True,
        "found": True,
        "actions": actions,
        "has_administer": any(a.lower() == "administer" for a in actions),
    }


def _grant_process_admin(
    client: httpx.Client, base_url: str, process: Dict[str, str], identity: Dict[str, Any]
) -> Dict[str, str]:
    """Give the chosen user Administer on the inherited process.

    This is the "Users" list in that process's Security dialog. Project Administrator
    does not include it — they are separate permission systems.

    Two things were wrong before and both are fixed here by asking the server rather
    than assuming:

    * THE ENDPOINT. It was posting to ``_apis/accesscontrolentries`` with an
      ``accessControlEntries`` list. The shape this server takes is
      ``_apis/accesscontrollists`` with an ``acesDictionary`` — the same call the
      working migration script makes.
    * THE TOKEN. ``$PROCESS:<typeId>:`` was a guess, and a wrong token is refused with
      a message that looks like a permissions problem. Now the namespace's EXISTING
      tokens are read and their shape is copied with our process id substituted, so the
      format comes from the server instead of from me.
    """
    process_type_id = (process or {}).get("type_id") or ""
    descriptor = identity.get("identity_descriptor") or ""
    tfid = identity.get("team_foundation_id") or ""

    if not process_type_id:
        return {"status": "skipped", "detail": "process typeId unknown"}
    if not descriptor and tfid:
        descriptor = ado_identity.descriptor_for_tfid(client, base_url, tfid)
    if not descriptor:
        return {
            "status": "user-not-resolved",
            "detail": "no ACL descriptor for the user; the project-admin step must run first",
        }

    try:
        rns = client.get(f"{base_url}/_apis/securitynamespaces?api-version=6.0")
        namespaces = (rns.json() or {}).get("value", []) if rns.is_success else []
    except Exception as exc:
        return {"status": "failed", "detail": f"namespace read: {type(exc).__name__}: {exc}"}
    if not rns.is_success:
        return {"status": "process-namespace-not-found", "detail": f"HTTP {rns.status_code}"}

    ns = next((n for n in namespaces if (n.get("name") or "") == "Process"), None)
    if not ns:
        names = ", ".join(sorted(str(n.get("name")) for n in namespaces))[:200]
        return {"status": "process-namespace-not-found", "detail": f"namespaces seen: {names}"}

    ns_id = ns.get("namespaceId") or ""
    actions = ns.get("actions") or []

    def _bit(*names: str):
        wanted = {n.lower() for n in names}
        return next(
            (a.get("bit") for a in actions if (a.get("name") or "").lower() in wanted),
            None,
        )

    bit, bit_name = _bit("administer", "administerprocesspermissions"), "Administer"
    if bit is None:
        bit, bit_name = _bit("edit"), "Edit"
    if bit is None:
        return {
            "status": "administer-bit-not-found",
            "detail": ", ".join(str(a.get("name")) for a in actions)[:200],
        }

    token, how = ado_identity.discover_acl_token(client, base_url, ns_id, process_type_id)
    result = ado_identity.set_acl(client, base_url, ns_id, token, descriptor, int(bit))
    if result["status"] == "granted":
        return {
            "status": "granted",
            "detail": f"{bit_name} via {result['detail']} (token {how})",
        }

    logging.warning(
        "Process %s grant for '%s' in %s failed (token %s, %s): %s",
        bit_name, identity.get("display"), _collection_name(base_url), token, how,
        result["detail"],
    )
    return {"status": "failed", "detail": f"token={token} ({how}); {result['detail']}"}


# The four processes Azure DevOps ships with, lowercased for comparison. These are
# exactly the options the request form offers (`automations-container.html`), and the
# error message below spells them the way a person would type them.
#
# THIS WAS DEFINED ONCE AND LOST. The Terraform-era module declared it; the REST
# rewrite kept the line that READS it and dropped the line that sets it, so every
# approved project request raised NameError at the moment the approver approved it
# -- after the request had been accepted, and nowhere near the form that collected
# it. `check_imports.py` now refuses a build with a name nothing defines.
VALID_PROCESS_TYPES = {"scrum", "agile", "cmmi", "basic"}


def provision_ado_project(
    project_name: str,
    process_type: str,
    admin_username: str,
    description: str = "",
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the project in ONE collection, via REST. No Terraform, no K8s.

    Ensure the inherited process ``<project>-<type>`` exists, create the project with
    it, then grant the chosen admin — Project Administrator on the project AND Administer
    on the inherited process (two distinct permission systems). The grants are
    best-effort and reported in ``results[].grants`` rather than fatal: the project
    exists and is usable even if a grant is refused.

    ``collection`` defaults to ADO_DEFAULT_COLLECTION. Until now this created the project
    in EVERY configured collection, which is where the recurring "already exists in
    TikshuvCollection-Inheritance" failures came from — a name taken in either collection
    failed the request, for a second copy nobody had asked for.
    """
    process_type = (process_type or "Scrum").strip()
    if process_type.lower() not in VALID_PROCESS_TYPES:
        raise ValueError("process_type must be one of: Scrum, Agile, CMMI, Basic.")
    project_name = sanitize_ado_name((project_name or "").strip())
    if not project_name:
        raise ValueError("project_name is required.")

    wanted = (collection or "").strip() or _DEFAULT_COLLECTION
    # Only a configured collection may be targeted: the value arrives from a request
    # payload, and "provision wherever this string says" is not a decision a requester
    # gets to make.
    if _TARGET_COLLECTIONS and wanted.lower() not in {c.lower() for c in _TARGET_COLLECTIONS}:
        raise ValueError(
            f"'{wanted}' is not one of the collections available for provisioning "
            f"({', '.join(_TARGET_COLLECTIONS)})."
        )

    # Resolve the identity form ONCE: a bare username becomes DOMAIN\user (the only
    # form the AD-backed lookup reliably resolves) when a domain is configured.
    principal = normalize_admin_principal(admin_username)

    # Safe Mode is checked by the callers (approval executor, create endpoint), but it
    # is re-checked HERE so no future caller of this orchestrator can fire real,
    # admin-PAT REST writes against Azure DevOps while the platform is in Safe Mode.
    # The guard belongs next to the side effects, not only at the UI layer.
    try:
        import safe_mode as _safe_mode
        if _safe_mode.is_enabled():
            return {
                "project_name": project_name,
                "process_type": process_type,
                "status": "completed",
                "safe_mode": True,
                "collection": wanted,
                "results": [
                    {"collection": wanted, "status": "simulated", "project_url": f"safe-mode://{wanted}/{project_name}",
                     "grants": {"principal": principal, "resolved": True,
                                "project_admin": "simulated", "process_admin": "simulated"}}
                ],
                "project_url": f"safe-mode://{project_name}",
                "grants_ok": True,
            }
    except Exception:
        pass

    if not ADO_BASE:
        raise RuntimeError("AZURE_DEVOPS_BASE_URL is not configured.")

    results: List[Dict[str, Any]] = []
    with _admin_ado_client() as client:
        targets, missing = resolve_collections_named(client, [wanted])
        for name, base in targets:
            try:
                proc = _ensure_inherited_process(client, base, project_name, process_type)
                url = _create_project_in_collection(
                    client, base, project_name, proc["type_id"], description or "",
                )
                # Grants are best-effort and recorded, not fatal: the project exists
                # and is usable even if a grant is refused. Identities are collection-
                # scoped, so resolve inside each collection.
                identity = _resolve_provision_identity(client, base, principal)
                # The project grant may CREATE the identity (binding an AD account that
                # has never opened this collection), so it hands back the re-resolved
                # user — and the process ACL, which needs a descriptor, uses that rather
                # than the empty lookup from a moment earlier.
                project_admin = _assign_project_admin(
                    client, base, project_name, principal, identity
                )
                identity = project_admin.get("identity") or identity
                process_admin = _grant_process_admin(client, base, proc, identity)
                grants = {
                    "principal": principal,
                    "resolved": bool(identity.get("resolved")),
                    "matched_as": identity.get("matched_as"),
                    "display": identity.get("display"),
                    "account": identity.get("account"),
                    "project_admin": project_admin["status"],
                    "project_admin_detail": project_admin["detail"],
                    "process_admin": process_admin["status"],
                    "process_admin_detail": process_admin["detail"],
                }
                if not identity.get("resolved"):
                    grants["resolve_attempts"] = identity.get("attempts")
                if project_admin["status"] != "granted" or process_admin["status"] != "granted":
                    logging.warning("Grants incomplete in %s: %s", name, grants)
                results.append({
                    "collection": name,
                    "status": "created",
                    "process_name": proc["name"],
                    "project_url": url,
                    "grants": grants,
                })
            except Exception as exc:  # noqa: BLE001 — one collection must not sink the others
                logging.error("Provisioning failed in collection %s: %s", name, exc)
                results.append({"collection": name, "status": "failed", "error": str(exc)})

    created = [r for r in results if r["status"] == "created"]
    if not created:
        errors = "; ".join(f"{r['collection']}: {r.get('error')}" for r in results)
        raise RuntimeError(f"Project creation failed. {errors}")

    _ok = {"granted", "simulated"}
    grants_ok = all(
        (r.get("grants") or {}).get("project_admin") in _ok
        and (r.get("grants") or {}).get("process_admin") in _ok
        for r in created
    )

    return {
        "project_name": project_name,
        "process_type": process_type,
        "collection": wanted,
        "description": (description or "").strip(),
        "status": "completed" if len(created) == len(results) else "partial",
        "results": results,
        "skipped_collections": missing,
        # False when the project was created but a Project-Admin or process grant did
        # not land — the project is usable, but someone should finish the grant by hand.
        "grants_ok": grants_ok,
        # A representative URL for UIs that show a single link.
        "project_url": created[0]["project_url"],
    }


