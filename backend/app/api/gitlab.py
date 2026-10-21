"""
GitLab for the widgets, the GitLab page, Needs You and search -- read with the person's
own token (gitlab_client.py says which and why).

  GET /api/gitlab/groups                       top-level groups, the first selector
  GET /api/gitlab/projects?group=              projects, the second selector
  GET /api/gitlab/merge-requests?role=&group=&project=
                                               open merge requests I opened (created) or
                                               that wait for my review (review)
  GET /api/gitlab/pipelines?scope=&group=&project=
                                               recent pipelines, mine or everyone's

The rows are shaped like the Azure DevOps ones, field for field, because the widgets
are the same widgets (pull-requests-widget.html, pipelines.html with a provider). One
fetch per person is cached for a minute and every filter is applied to the cached
answer, so switching a select, or the two merge-request widgets rendering side by side,
costs GitLab nothing.

"Mine" is decided by GitLab, never by matching names: the token's owner is GET /user,
and the lists are asked for by that id (created_by_me, reviewer_id, username).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

import gitlab_client as gl
from integrations_cache import cached_external
from security import AuthUser, get_current_user
from common import now_iso

router = APIRouter()
log = logging.getLogger(__name__)

# Projects a pipelines answer reaches when none is chosen: the most recently active
# ones, two GitLab calls each. Enough for "what did I just run", and bounded.
PIPELINE_PROJECTS = 15
PIPELINES_PER_PROJECT = 20
APPROVAL_READS = 60


def _q(value: Any) -> str:
    """A query value, whether FastAPI or a plain-Python caller passed it."""
    return value.strip() if isinstance(value, str) else ""


def _guarded(fn):
    """Every route's own failures, said in GitLab's words (gitlab_client.describe)."""
    try:
        return fn()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"GitLab at {gl.base_url()} did not answer in time.")
    except httpx.HTTPError as exc:
        from resilient_http import explain_integration_failure

        raise HTTPException(status_code=502, detail=explain_integration_failure("GitLab", exc))


# ── groups and projects ─────────────────────────────────────────────────────

@router.get("/groups")
def groups(current_user: AuthUser = Depends(get_current_user)):
    token = gl.require_token(current_user)

    def produce() -> List[Dict[str, Any]]:
        with gl.client(token) as cl:
            rows = gl.paged(cl, "/groups", {"top_level_only": "true", "min_access_level": 10,
                                            "order_by": "name", "sort": "asc"}, limit=500)
        # top_level_only is ignored by servers older than it; the parent says it too.
        return [{"name": str(g.get("full_path") or g.get("path") or ""), "title": g.get("name") or ""}
                for g in rows if not g.get("parent_id")]

    data = _guarded(lambda: cached_external(gl.SYSTEM, gl.owner(current_user), "groups", produce, ttl=300))
    return {"success": True, "data": data, "timestamp": now_iso()}


def _projects(token: str, group: str, limit: int = 300) -> List[Dict[str, Any]]:
    with gl.client(token) as cl:
        params = {"simple": "true", "archived": "false", "order_by": "last_activity_at", "sort": "desc"}
        if group:
            rows = gl.paged(cl, f"/groups/{gl.pid(group)}/projects", {**params, "include_subgroups": "true"}, limit=limit)
        else:
            rows = gl.paged(cl, "/projects", {**params, "membership": "true"}, limit=limit)
    return [{"id": p.get("id"), "name": str(p.get("path_with_namespace") or p.get("name") or ""),
             "title": p.get("name") or "", "web_url": p.get("web_url") or "",
             "last_activity": p.get("last_activity_at") or ""} for p in rows]


@router.get("/projects")
def projects(group: Optional[str] = Query(None), current_user: AuthUser = Depends(get_current_user)):
    token = gl.require_token(current_user)
    group = _q(group)
    data = _guarded(lambda: cached_external(gl.SYSTEM, gl.owner(current_user), f"projects:{group.lower()}",
                                            lambda: _projects(token, group), ttl=300))
    return {"success": True, "data": data, "timestamp": now_iso()}


# ── merge requests ───────────────────────────────────────────────────────────

def _mr_row(mr: Dict[str, Any], *, creator: bool, reviewer: bool) -> Dict[str, Any]:
    path = gl.project_path_of(mr)
    author = mr.get("author") or {}
    return {
        "id": mr.get("iid"),
        "iid": mr.get("iid"),
        "project_id": mr.get("project_id"),
        "title": mr.get("title") or "",
        "status": "draft" if (mr.get("draft") or mr.get("work_in_progress")) else "active",
        "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
        "created_by": author.get("name") or author.get("username") or "Unknown",
        "created_by_username": author.get("username") or "",
        "created_date": mr.get("created_at"),
        "updated_date": mr.get("updated_at"),
        # The widget's three names: the "collection" GitLab does not have, the group
        # in Azure DevOps's "project" slot, and the GitLab project as the repository.
        "collection": "",
        "project": gl.group_of(path),
        "repository": path.rsplit("/", 1)[-1] if path else "",
        "project_path": path,
        "source_branch": mr.get("source_branch") or "",
        "target_branch": mr.get("target_branch") or "",
        "is_creator": creator,
        "is_reviewer": reviewer,
        "approved_by_me": False,
        "my_vote": 0,
        "approvals": None,
        "merge_status": mr.get("detailed_merge_status") or mr.get("merge_status") or "",
        "has_conflicts": bool(mr.get("has_conflicts")),
        "comments": int(mr.get("user_notes_count") or 0),
        "url": mr.get("web_url") or "",
    }


def _approvals(cl: httpx.Client, row: Dict[str, Any], me_id: Any) -> Dict[str, Any]:
    """Who approved it, whether I did, and -- on Premium -- how many it still needs."""
    resp = cl.get(f"/projects/{row['project_id']}/merge_requests/{row['iid']}/approvals")
    if resp.status_code != 200:
        return {}
    data = gl.json_of(resp) or {}
    approved_by = [((a or {}).get("user") or {}) for a in data.get("approved_by") or []]
    mine = bool(data.get("user_has_approved")) or any(u.get("id") == me_id for u in approved_by)
    return {
        "approved_by_me": mine,
        "can_approve": data.get("user_can_approve"),
        "count": len(approved_by),
        "names": [u.get("name") or u.get("username") or "" for u in approved_by][:6],
        "required": data.get("approvals_required"),
        "left": data.get("approvals_left"),
    }


def fetch_merge_requests(token: str) -> Dict[str, Any]:
    """Uncached: every open merge request this person opened or reviews, flagged."""
    with gl.client(token) as cl:
        who = gl.me(cl)
        created = gl.paged(cl, "/merge_requests", {"scope": "created_by_me", "state": "opened"}, limit=200)
        note = ""
        resp = cl.get("/merge_requests", params={"scope": "all", "state": "opened", "reviewer_id": who["id"], "per_page": 100})
        if resp.status_code == 200:
            review = gl.json_of(resp) or []
        elif resp.status_code == 400:
            # Older than reviewers (13.8): the assignee is who was asked to look.
            review = gl.paged(cl, "/merge_requests", {"scope": "assigned_to_me", "state": "opened"}, limit=200)
            note = "This GitLab has no reviewers yet, so merge requests assigned to you are shown instead."
        else:
            raise gl.refusal(resp)

        rows: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
        for mr in created:
            rows[(mr.get("project_id"), mr.get("iid"))] = _mr_row(mr, creator=True, reviewer=False)
        for mr in review:
            key = (mr.get("project_id"), mr.get("iid"))
            if key in rows:
                rows[key]["is_reviewer"] = True
            else:
                rows[key] = _mr_row(mr, creator=False, reviewer=True)

        wanted = [r for r in rows.values() if r["is_reviewer"]][:APPROVAL_READS]
        wanted += [r for r in rows.values() if not r["is_reviewer"]][: max(0, APPROVAL_READS - len(wanted))]
        if wanted:
            with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
                for row, info in zip(wanted, pool.map(lambda r: _approvals(cl, r, who["id"]), wanted)):
                    if info:
                        row["approved_by_me"] = info["approved_by_me"]
                        row["my_vote"] = 10 if info["approved_by_me"] else 0
                        row["approvals"] = {k: info[k] for k in ("count", "names", "required", "left", "can_approve")}
    data = sorted(rows.values(), key=lambda r: str(r.get("created_date") or ""), reverse=True)
    return {"success": True, "data": data, "me": who, "note": note, "timestamp": now_iso()}


def merge_requests_for(user: AuthUser) -> Dict[str, Any]:
    token = gl.require_token(user)
    return _guarded(lambda: cached_external(gl.SYSTEM, gl.owner(user), "merge-requests",
                                            lambda: fetch_merge_requests(token), ttl=60))


def filter_merge_requests(result: Dict[str, Any], group: str, project: str, role: str) -> Dict[str, Any]:
    rows = list(result.get("data") or [])
    if group:
        rows = [r for r in rows if r["project"].lower() == group.lower()]
    if project:
        rows = [r for r in rows if r["project_path"].lower() == project.lower() or r["repository"].lower() == project.lower()]
    if role == "created":
        rows = [r for r in rows if r["is_creator"]]
    elif role == "review":
        # Approved by me is not waiting on me -- the same rule as Azure DevOps's vote.
        rows = [r for r in rows if r["is_reviewer"] and not r["approved_by_me"]]
    return {**result, "data": rows}


@router.get("/merge-requests")
def merge_requests(
    role: Optional[str] = Query(None, description="'created' or 'review'; omit for both"),
    group: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    result = merge_requests_for(current_user)
    out = filter_merge_requests(result, _q(group), _q(project), _q(role).lower())
    out.pop("me", None)
    return out


# ── pipelines ────────────────────────────────────────────────────────────────

def _pipeline_row(p: Dict[str, Any], project: Dict[str, Any], mine: bool, who: Dict[str, Any]) -> Dict[str, Any]:
    path = str(project.get("name") or "")
    raw = str(p.get("status") or "")
    return {
        "id": p.get("id"),
        "iid": p.get("iid"),
        "project_id": project.get("id"),
        "name": p.get("name") or project.get("title") or path.rsplit("/", 1)[-1],
        "run_id": p.get("iid") or p.get("id"),
        "status": gl.run_status(raw),
        "result": raw,
        "finished": raw in gl.FINISHED,
        "collection": "",
        "project": gl.group_of(path),
        "repository": path.rsplit("/", 1)[-1],
        "project_path": path,
        "source_branch": p.get("ref") or "",
        "source": p.get("source") or "",
        "requested_for": who.get("name") if mine else "",
        "created_date": p.get("created_at"),
        "started_date": p.get("created_at"),
        "finished_date": p.get("updated_at") if raw in gl.FINISHED else None,
        "url": p.get("web_url") or "",
        "mine": mine,
    }


def fetch_pipelines(token: str, group: str, project: str) -> Dict[str, Any]:
    with gl.client(token) as cl:
        who = gl.me(cl)
        if project:
            p = gl.get(cl, f"/projects/{gl.pid(project)}")
            targets = [{"id": p.get("id"), "name": p.get("path_with_namespace") or project, "title": p.get("name") or ""}]
        else:
            targets = _projects(token, group, limit=PIPELINE_PROJECTS)

        def runs(proj: Dict[str, Any]):
            base = f"/projects/{proj['id']}/pipelines"
            params = {"per_page": PIPELINES_PER_PROJECT, "order_by": "id", "sort": "desc"}
            every = cl.get(base, params=params)
            if every.status_code != 200:
                return proj, [], set(), every.status_code
            mine = cl.get(base, params={**params, "username": who["username"]}) if who.get("username") else None
            mine_ids = {x.get("id") for x in (gl.json_of(mine) or [])} if mine is not None and mine.status_code == 200 else set()
            return proj, gl.json_of(every) or [], mine_ids, 200

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
            answers = list(pool.map(runs, targets))

    rows: List[Dict[str, Any]] = []
    unreadable: List[Dict[str, Any]] = []
    for proj, pipelines, mine_ids, code in answers:
        if code != 200:
            if len(unreadable) < 12:
                unreadable.append({"name": proj.get("name"), "status": code})
            continue
        rows.extend(_pipeline_row(p, proj, p.get("id") in mine_ids, who) for p in pipelines)
    rows.sort(key=lambda r: str(r.get("created_date") or ""), reverse=True)
    return {"rows": rows, "me": who, "projects": len(targets), "unreadable": unreadable}


@router.get("/pipelines")
def pipelines(
    scope: Optional[str] = Query("mine", description="'mine' = pipelines I started, 'all' = everyone's"),
    group: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    token = gl.require_token(current_user)
    group, project = _q(group), _q(project)
    scope = (_q(scope) or "mine").lower()
    fetched = _guarded(lambda: cached_external(
        gl.SYSTEM, gl.owner(current_user), f"pipelines:{group.lower()}:{project.lower()}",
        lambda: fetch_pipelines(token, group, project), ttl=60))
    rows = fetched["rows"]
    mine = [r for r in rows if r["mine"]]
    showing_all = scope == "all"
    return {
        "success": True,
        "data": (rows if showing_all else mine)[:25],
        "showing_all": showing_all,
        "counts": {"mine": len(mine), "all": len(rows)},
        # GitLab says whose a pipeline is; there is no guessing to own up to.
        "identity_unresolved": False,
        "diagnostics": {
            "projects": fetched["projects"],
            "projects_unreadable": fetched["unreadable"],
            "builds_seen": len(rows),
            "builds_mine": len(mine),
            "identity": [fetched["me"].get("username") or ""],
            "limited_to": PIPELINE_PROJECTS if not project else None,
        },
        "timestamp": now_iso(),
    }
