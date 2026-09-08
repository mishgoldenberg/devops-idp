"""Portal-wide search.

One endpoint that fans out across every integrated system and returns results GROUPED
by source, so the header dropdown can show a few hits per group and the search page can
show many.

Design notes
------------
* **Fan-out, never fail.** A user with no SonarQube token, or an Artifactory that is
  down, must still get their tickets and work items. Every source is isolated: it runs
  in its own thread and its exceptions are swallowed into an empty group. A search that
  half-works is infinitely more useful than one that 500s.

* **Filtering is done here, not in each system.** The integrations expose "list"
  endpoints, not query endpoints, so most sources are fetched and matched locally. The
  two that CAN search server-side (Confluence CQL, and ServiceNow via its own list)
  are the ones with too much data to pull.

* **The endpoint functions are called directly as Python**, not over HTTP — same as
  ui.py does. That means every argument must be passed EXPLICITLY: an omitted argument
  does not become None, it becomes FastAPI's ``Query(...)`` object, which is what
  produced the "'Query' object has no attribute 'strip'" crash.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query

import cache
from security import AuthUser, get_current_user

from . import approvals, azure_devops, integrations, servicenow

# No prefix here: api/__init__.py mounts this under /api/search, the same way every
# other router is mounted.
router = APIRouter(tags=["search"])
log = logging.getLogger(__name__)

# Per-group cap in the header dropdown. The search PAGE asks for more.
PREVIEW_PER_GROUP = 3
PAGE_PER_GROUP = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _matches(needle: str, *haystacks: Any) -> bool:
    return any(needle in str(h or "").lower() for h in haystacks)


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """Unwrap the {"success": ..., "data": [...]} envelope the routers return."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return []


# ── deep links ──────────────────────────────────────────────────────────────
# A search hit should open THE THING, not the portal page that lists things like it.
# Several collectors used to return "/ui/artifactory" or "/ui/sonarqube" for every row,
# so clicking any repository dropped you on the Artifactory tab and left you to find it
# again by hand — the search had told you where it was and then refused to take you.
#
# Work items and Confluence pages already carry a real url from their API. The rest have
# to be built from the system's base URL, and if that is not configured we fall back to
# the portal page rather than emitting a broken link.


def _system_base(system: str) -> str:
    try:
        return integrations._base_url(system)
    except Exception:
        return ""


def _sonar_href(project_key: str) -> str:
    base = _system_base("sonarqube")
    if not base or not project_key:
        return "/ui/sonarqube"
    return f"{base}/dashboard?id={quote(str(project_key), safe='')}"


def _artifactory_href(repo: str) -> str:
    base = _system_base("artifactory")
    if not base or not repo:
        return "/ui/artifactory"
    # One helper, so the search hit and the Artifactory widget cannot drift apart and
    # start pointing at different URLs for the same repository.
    return integrations.artifactory_repo_url(base, str(repo))


def _ado_project_href(name: str) -> str:
    base = azure_devops._get_ado_base()
    if not base or not name:
        return "/ui/azure-devops"
    return f"{base}/{quote(str(name), safe='')}"


# ── one collector per system ────────────────────────────────────────────────
# Each returns (group_key, label, icon, [hits]). A hit is {title, subtitle, meta, href}.


def _ado_work_items(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    # iteration="all" — the widget defaults to the current sprint, but a SEARCH should
    # look at everything assigned to you, not just this fortnight.
    payload = azure_devops.get_work_items(
        project=None, work_item_type=None, iteration="all", area_path=None, current_user=user
    )
    return [
        {
            "title": item.get("title") or f"#{item.get('id')}",
            "subtitle": f"#{item.get('id')} · {item.get('type') or 'Work item'}",
            "meta": item.get("state") or "",
            "href": item.get("url") or "/ui/azure-devops",
        }
        for item in _rows(payload)
        if _matches(q, item.get("title"), item.get("id"), item.get("type"))
    ]


def _ado_projects(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    payload = azure_devops.get_projects(current_user=user)
    return [
        {
            "title": p.get("name") or "",
            "subtitle": p.get("description") or "Azure DevOps project",
            "meta": "",
            "href": _ado_project_href(p.get("name")),
        }
        for p in _rows(payload)
        if _matches(q, p.get("name"), p.get("description"))
    ]


def _snow_tickets(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    payload = servicenow.get_tickets(current_user=user)
    return [
        {
            "title": t.get("short_description") or t.get("number") or "",
            "subtitle": t.get("number") or "",
            "meta": t.get("state") or "",
            "href": f"/ui/support?ticket={t.get('sys_id')}" if t.get("sys_id") else "/ui/support",
        }
        for t in _rows(payload)
        if _matches(q, t.get("short_description"), t.get("number"))
    ]


def _confluence(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    # Confluence can search server-side (CQL), so hand it the term rather than
    # dragging every page across the wire to filter here.
    payload = integrations.confluence_search(q=q, current_user=user)
    return [
        {
            "title": p.get("title") or "",
            "subtitle": p.get("space") or "Confluence",
            "meta": p.get("last_updated") or "",
            "href": p.get("url") or "/ui/confluence",
        }
        for p in _rows(payload)
    ]


def _sonarqube(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    payload = integrations.sonarqube_projects(current_user=user)
    return [
        {
            "title": p.get("name") or p.get("key") or "",
            "subtitle": p.get("project_key") or "",
            "meta": p.get("main_branch") or "",
            "href": _sonar_href(p.get("project_key") or p.get("key")),
        }
        for p in _rows(payload)
        if _matches(q, p.get("name"), p.get("project_key"))
    ]


def _artifactory(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    payload = integrations.artifactory_repos(current_user=user)
    return [
        {
            "title": r.get("name") or "",
            "subtitle": r.get("type") or "Repository",
            "meta": "",
            "href": _artifactory_href(r.get("name")),
        }
        for r in _rows(payload)
        if _matches(q, r.get("name"), r.get("type"))
    ]


def _requests(q: str, user: AuthUser) -> List[Dict[str, Any]]:
    payload = approvals.get_requests(status_filter=None, scope="mine", current_user=user)
    return [
        {
            "title": r.get("request_title") or r.get("request_type") or "",
            "subtitle": str(r.get("request_type") or "").replace("_", " "),
            "meta": str(r.get("status") or "").replace("_", " "),
            "href": "/ui/my-requests",
        }
        for r in _rows(payload)
        if _matches(q, r.get("request_title"), r.get("request_type"))
    ]


# (key, label, collector) — order here is the order shown in the UI.
SOURCES: List[tuple] = [
    ("work_items", "Azure DevOps — Work Items", _ado_work_items),
    ("ado_projects", "Azure DevOps — Projects", _ado_projects),
    ("tickets", "ServiceNow — Tickets", _snow_tickets),
    ("confluence", "Confluence — Pages", _confluence),
    ("sonarqube", "SonarQube — Projects", _sonarqube),
    ("artifactory", "Artifactory — Repositories", _artifactory),
    ("requests", "Self-service — My Requests", _requests),
]


def _collect(source: tuple, q: str, user: AuthUser) -> Dict[str, Any]:
    key, label, fn = source
    try:
        hits = fn(q, user) or []
    except Exception as exc:
        # A missing token (428) or an unreachable system must not sink the whole search.
        log.info("search: source %r unavailable: %s: %s", key, type(exc).__name__, exc)
        hits = []
        return {"key": key, "label": label, "items": [], "total": 0, "unavailable": True}
    return {"key": key, "label": label, "items": hits, "total": len(hits), "unavailable": False}


@router.get("")
def search(
    q: str = Query("", description="What to look for"),
    limit: int = Query(PREVIEW_PER_GROUP, description="Max hits per group"),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Search everything, grouped by system.

    ``limit`` is per GROUP, not overall: the header dropdown asks for 3 of each, the
    search page asks for many. ``total`` on each group is the true match count, so the
    UI can say "3 of 17" and offer a way to see the rest.
    """
    needle = str(q or "").strip().lower()
    if len(needle) < 2:
        return {"success": True, "data": {"query": q, "groups": []}, "timestamp": _now_iso()}

    cap = max(1, min(int(limit or PREVIEW_PER_GROUP), PAGE_PER_GROUP))
    uid = str(current_user.get("email") or current_user.get("username") or "anon").lower()

    def fan_out() -> List[Dict[str, Any]]:
        # Every source is an independent network call; serially this would cost the sum
        # of all of them on every search.
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            return list(pool.map(lambda s: _collect(s, needle, current_user), SOURCES))

    # Both search UIs are type-ahead, so a single user typing "artifactory" fires several
    # searches, and each one fans out to SEVEN systems. Without a cache that is a lot of
    # load on ServiceNow and Azure DevOps for one person typing one word. 30s is enough
    # to absorb the keystrokes and the near-certain re-search from the results page,
    # while still being far shorter than anyone would notice as staleness.
    #
    # The key includes the user: results are scoped to their tokens and must never leak
    # across accounts. It does NOT include the cap, so the dropdown (3) and the page (10)
    # share one fan-out — the full result set is cached and sliced per request below.
    try:
        groups = cache.get_cached(f"search:{uid}:{needle}", ttl=30, producer=fan_out)
    except Exception:
        groups = fan_out()

    sliced = [
        {**group, "items": (group.get("items") or [])[:cap]}
        for group in groups
    ]

    return {
        "success": True,
        "data": {
            "query": q,
            "groups": [g for g in sliced if g["total"] or g["unavailable"]],
            "total": sum(g["total"] for g in sliced),
        },
        "timestamp": _now_iso(),
    }
