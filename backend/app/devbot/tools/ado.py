"""
Azure DevOps, read with the person's own PAT.

Every tool here searches ALL collections unless one is named -- the Hub's collections
are discovered the same way the widgets discover them (_discover_ado_bases) -- and a
collection that answers "not mine" (400/401/403/404) is skipped, exactly as the
widgets skip it. Where the Hub already has a reader (pipeline runs, pull requests,
repositories) it is called rather than copied, so DevBot and the widgets cannot
disagree about what exists.

User-supplied values reach WIQL only through _wiql_literal.
"""

from __future__ import annotations

import difflib
import html
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .base import Tool, ToolContext, ToolFailure, register

API = {"api-version": "7.0"}
NOT_MINE = (400, 401, 403, 404)
CLOSED_STATES = ("Done", "Closed", "Removed", "Resolved", "Completed", "Cut")
WI_FIELDS = [
    "System.Id", "System.Title", "System.State", "System.WorkItemType", "System.AssignedTo",
    "System.TeamProject", "System.ChangedDate", "Microsoft.VSTS.Common.Priority",
]


def _ado():
    from api import azure_devops

    return azure_devops


def plain(markup: Any, limit: int = 1200) -> str:
    """Azure DevOps HTML as text a model can read."""
    text = re.sub(r"<\s*br\s*/?>|</\s*(p|div|li|h\d|tr)\s*>", "\n", str(markup or ""), flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def who(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("uniqueName") or "")
    return str(value or "")


def short_branch(ref: Any) -> str:
    return re.sub(r"^refs/heads/", "", str(ref or ""))


def collection_of(base: str) -> str:
    return _ado()._collection_name(base)


def projects(ctx: ToolContext, client: httpx.Client, collection: str = "") -> List[Dict[str, Any]]:
    """Every project the PAT can see, in the named collection or all of them."""
    def produce() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for base in ctx.ado_bases_for(client, collection):
            resp = client.get(f"{base}/_apis/projects", params={**API, "$top": "500"})
            if resp.status_code in NOT_MINE:
                continue
            resp.raise_for_status()
            for p in resp.json().get("value") or []:
                out.append({"name": p.get("name"), "id": p.get("id"), "collection": collection_of(base),
                            "base": base, "description": (p.get("description") or "")[:160]})
        return out

    return ctx.memo(f"ado:projects:{collection.lower()}", produce)


def find_project(ctx: ToolContext, client: httpx.Client, name: str, collection: str = "") -> Dict[str, Any]:
    """The project called ``name`` (case-insensitively), or a refusal naming the close ones."""
    rows = projects(ctx, client, collection)
    wanted = (name or "").strip().lower()
    for row in rows:
        if str(row["name"]).lower() == wanted:
            return row
    near = difflib.get_close_matches(name, [r["name"] for r in rows], n=5, cutoff=0.4)
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise ToolFailure(f"No Azure DevOps project called '{name}' that your token can see.{hint}")


def run_url(base: str, project: str, build_id: Any) -> str:
    return f"{base}/{quote(str(project), safe='')}/_build/results?buildId={build_id}"


def wi_url(base: str, project: str, wi_id: Any) -> str:
    return f"{base}/{quote(str(project), safe='')}/_workitems/edit/{wi_id}"


def work_item_rows(client: httpx.Client, base: str, ids: List[int], fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Several work items in one call, in the order asked for."""
    if not ids:
        return []
    resp = client.get(f"{base}/_apis/wit/workitems",
                      params={**API, "ids": ",".join(str(i) for i in ids[:200]), "fields": ",".join(fields or WI_FIELDS),
                              "errorPolicy": "omit"})
    if resp.status_code != 200:
        return []
    by_id = {int(w.get("id")): w for w in resp.json().get("value") or [] if w and w.get("id")}
    out = []
    for wi_id in ids:
        item = by_id.get(int(wi_id))
        if not item:
            continue
        f = item.get("fields") or {}
        project = f.get("System.TeamProject") or ""
        out.append({
            "id": item["id"],
            "type": f.get("System.WorkItemType"),
            "title": f.get("System.Title"),
            "state": f.get("System.State"),
            "assigned_to": who(f.get("System.AssignedTo")),
            "project": project,
            "collection": collection_of(base),
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
            "changed": str(f.get("System.ChangedDate") or "")[:10],
            "url": wi_url(base, project, item["id"]),
        })
    return out


def wiql(ctx: ToolContext, client: httpx.Client, where: List[str], top: int, collection: str = "",
         order: str = "[System.ChangedDate] DESC") -> List[Dict[str, Any]]:
    """Run one WIQL filter in every collection and merge the answers, newest first."""
    query = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(where or ["[System.Id] > 0"]) + f" ORDER BY {order}"
    rows: List[Dict[str, Any]] = []
    for base in ctx.ado_bases_for(client, collection):
        resp = client.post(f"{base}/_apis/wit/wiql", params={**API, "$top": str(top)}, json={"query": query})
        if resp.status_code in NOT_MINE:
            continue
        resp.raise_for_status()
        ids = [int(w["id"]) for w in (resp.json().get("workItems") or [])[:top]]
        rows += work_item_rows(client, base, ids)
    rows.sort(key=lambda r: r.get("changed") or "", reverse=True)
    return rows[:top]


def literal(value: str) -> str:
    return "'" + _ado()._wiql_literal(value) + "'"


# ── tools ────────────────────────────────────────────────────────────────────

def ado_projects(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client() as client:
        rows = projects(ctx, client, args.get("collection", ""))
    collections = sorted({r["collection"] for r in rows})
    return {
        "data": {"count": len(rows), "collections": collections,
                 "projects": [{"name": r["name"], "collection": r["collection"], "description": r["description"]}
                              for r in rows[:80]]},
        "summary": f"{len(rows)} project{'s' if len(rows) != 1 else ''} in {len(collections)} collection{'s' if len(collections) != 1 else ''}",
    }


def ado_pipeline_runs(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.ado_pat()
    mine = bool(args.get("mine"))
    result = _ado().get_pipelines(project=args.get("project"), collection=args.get("collection"),
                                  scope="mine" if mine else "all", current_user=ctx.user)
    rows = list(result.get("data") or [])
    if args.get("pipeline"):
        needle = args["pipeline"].lower()
        rows = [r for r in rows if needle in str(r.get("name") or "").lower()]
    wanted = (args.get("result") or "").lower()
    if wanted:
        rows = [r for r in rows if wanted in (str(r.get("result") or ""), str(r.get("status") or ""))]
    top = max(1, min(int(args.get("top") or 10), 20))
    out = [{
        "build_id": r.get("id"), "run": r.get("run_id"), "pipeline": r.get("name"), "project": r.get("project"),
        "collection": r.get("collection"), "status": r.get("status"), "result": r.get("result"),
        "branch": short_branch(r.get("source_branch")), "requested_for": r.get("requested_for"),
        "queued": r.get("created_date"), "finished": r.get("finished_date"), "url": r.get("url"),
    } for r in rows[:top]]
    failed = sum(1 for r in out if r["result"] == "failed")
    note = ""
    if mine and result.get("identity_unresolved"):
        note = "Azure DevOps could not say which runs are this person's, so these are everybody's."
    return {
        "data": {"runs": out, "total_matching": len(rows)},
        "note": note,
        "summary": f"{len(out)} run{'s' if len(out) != 1 else ''}" + (f", {failed} failed" if failed else ""),
        "links": [{"title": f"{r['pipeline']} #{r['run']} ({r['result'] or r['status']})", "url": r["url"]} for r in out[:5] if r.get("url")],
    }


def ado_work_items(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    where: List[str] = []
    if args.get("project"):
        where.append(f"[System.TeamProject] = {literal(args['project'])}")
    if args.get("type"):
        where.append(f"[System.WorkItemType] = {literal(args['type'])}")
    state = (args.get("state") or "").strip()
    if state.lower() in ("open", "active", "not done", "not closed"):
        where.append("[System.State] NOT IN (" + ", ".join(literal(s) for s in CLOSED_STATES) + ")")
    elif state:
        where.append(f"[System.State] = {literal(state)}")
    if args.get("assigned_to_me"):
        where.append("[System.AssignedTo] = @Me")
    elif args.get("assigned_to"):
        where.append(f"[System.AssignedTo] = {literal(args['assigned_to'])}")
    query = (args.get("query") or "").strip()
    if query.lstrip("#").isdigit():
        where.append(f"[System.Id] = {int(query.lstrip('#'))}")
    elif query:
        where.append(f"[System.Title] CONTAINS {literal(query)}")
    top = max(1, min(int(args.get("top") or 15), 30))
    with ctx.ado_client() as client:
        rows = wiql(ctx, client, where, top, args.get("collection", ""))
    return {
        "data": {"work_items": rows, "count": len(rows)},
        "summary": f"{len(rows)} work item{'s' if len(rows) != 1 else ''}",
        "links": [{"title": f"#{r['id']} {r['title']}", "url": r["url"]} for r in rows[:6]],
    }


def read_work_item(ctx: ToolContext, client: httpx.Client, wi_id: int, collection: str = "") -> Tuple[str, Dict[str, Any]]:
    """The work item and the collection base it lives in."""
    for base in ctx.ado_bases_for(client, collection):
        resp = client.get(f"{base}/_apis/wit/workitems/{wi_id}", params={**API, "$expand": "relations"})
        if resp.status_code == 200:
            return base, resp.json()
        if resp.status_code not in NOT_MINE:
            resp.raise_for_status()
    raise ToolFailure(f"Work item #{wi_id} was not found in any collection your token can read.")


def describe_work_item(ctx: ToolContext, client: httpx.Client, wi_id: int, collection: str = "") -> Dict[str, Any]:
    base, item = read_work_item(ctx, client, wi_id, collection)
    f = item.get("fields") or {}
    project = f.get("System.TeamProject") or ""
    parent, children, related, prs, commits, builds = [], [], [], [], [], []
    for rel in item.get("relations") or []:
        kind = str(rel.get("rel") or "")
        url = str(rel.get("url") or "")
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if kind == "System.LinkTypes.Hierarchy-Reverse" and tail.isdigit():
            parent.append(int(tail))
        elif kind == "System.LinkTypes.Hierarchy-Forward" and tail.isdigit():
            children.append(int(tail))
        elif kind.startswith("System.LinkTypes") and tail.isdigit():
            related.append(int(tail))
        elif kind == "ArtifactLink":
            lowered = url.lower()
            if "pullrequestid" in lowered:
                parts = re.split(r"%2f|/", url.split("PullRequestId/", 1)[-1], flags=re.I)
                if len(parts) >= 3:
                    prs.append({"project_id": parts[0], "repository_id": parts[1], "id": parts[2]})
            elif "/commit/" in lowered:
                commits.append(url.rsplit("%2F", 1)[-1][:12])
            elif "/build/build/" in lowered:
                builds.append(tail)
    linked = work_item_rows(client, base, (parent + children + related)[:40],
                            ["System.Id", "System.Title", "System.State", "System.WorkItemType", "System.TeamProject",
                             "System.AssignedTo"])
    by_id = {r["id"]: {k: r[k] for k in ("id", "type", "title", "state", "assigned_to", "url")} for r in linked}
    comments: List[Dict[str, Any]] = []
    resp = client.get(f"{base}/{quote(project, safe='')}/_apis/wit/workItems/{wi_id}/comments",
                      params={"api-version": "7.0-preview.3", "$top": "5", "order": "desc"})
    if resp.status_code == 200:
        for c in (resp.json().get("comments") or [])[:5]:
            comments.append({"by": who(c.get("createdBy")), "when": str(c.get("createdDate") or "")[:10],
                             "text": plain(c.get("text"), 300)})
    return {
        "base": base,
        "item": {
            "id": wi_id,
            "type": f.get("System.WorkItemType"),
            "title": f.get("System.Title"),
            "state": f.get("System.State"),
            "reason": f.get("System.Reason"),
            "assigned_to": who(f.get("System.AssignedTo")),
            "created_by": who(f.get("System.CreatedBy")),
            "created": str(f.get("System.CreatedDate") or "")[:10],
            "changed": str(f.get("System.ChangedDate") or "")[:10],
            "project": project,
            "collection": collection_of(base),
            "area": f.get("System.AreaPath"),
            "iteration": f.get("System.IterationPath"),
            "priority": f.get("Microsoft.VSTS.Common.Priority"),
            "severity": f.get("Microsoft.VSTS.Common.Severity"),
            "tags": f.get("System.Tags"),
            "description": plain(f.get("System.Description"), 1200),
            "acceptance_criteria": plain(f.get("Microsoft.VSTS.Common.AcceptanceCriteria"), 700),
            "repro_steps": plain(f.get("Microsoft.VSTS.TCM.ReproSteps"), 1000),
            "url": wi_url(base, project, wi_id),
        },
        "parent": [by_id[i] for i in parent if i in by_id],
        "children": [by_id[i] for i in children if i in by_id],
        "related": [by_id[i] for i in related if i in by_id],
        "pull_request_links": prs[:10],
        "commits": commits[:10],
        "builds": builds[:10],
        "recent_comments": comments,
    }


def ado_work_item(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client() as client:
        info = describe_work_item(ctx, client, int(args["id"]), args.get("collection", ""))
    info.pop("base", None)
    item = info["item"]
    return {
        "data": info,
        "summary": f"#{item['id']} {item['title']} ({item['state']})",
        "links": [{"title": f"#{item['id']} {item['title']}", "url": item["url"]}],
    }


def ado_pull_requests(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.ado_pat()
    status = (args.get("status") or "active").lower()
    top = max(1, min(int(args.get("top") or 15), 30))
    if args.get("repository") and args.get("project"):
        # One repository: every pull request in it, not only the person's own.
        with ctx.ado_client() as client:
            project = find_project(ctx, client, args["project"], args.get("collection", ""))
            base = project["base"]
            resp = client.get(
                f"{base}/{quote(project['name'], safe='')}/_apis/git/repositories/{quote(args['repository'], safe='')}/pullrequests",
                params={**API, "searchCriteria.status": status, "$top": str(top)},
            )
            if resp.status_code == 404:
                raise ToolFailure(f"There is no repository '{args['repository']}' in {project['name']}.")
            resp.raise_for_status()
            rows = []
            for pr in resp.json().get("value") or []:
                repo = pr.get("repository") or {}
                rows.append({
                    "id": pr.get("pullRequestId"), "title": pr.get("title"), "status": pr.get("status"),
                    "created_by": who(pr.get("createdBy")), "created": str(pr.get("creationDate") or "")[:10],
                    "source": short_branch(pr.get("sourceRefName")), "target": short_branch(pr.get("targetRefName")),
                    "repository": repo.get("name"), "repository_id": repo.get("id"), "project": project["name"],
                    "collection": project["collection"], "is_draft": pr.get("isDraft"),
                    "reviewers": [{"name": who(r), "vote": r.get("vote")} for r in (pr.get("reviewers") or [])[:6]],
                    "url": f"{base}/{quote(project['name'], safe='')}/_git/{quote(str(repo.get('name') or ''), safe='')}/pullrequest/{pr.get('pullRequestId')}",
                })
    else:
        role = (args.get("role") or "").lower()
        result = _ado().get_pull_requests(username=None, collection=args.get("collection"), project=args.get("project"),
                                          repository=args.get("repository"),
                                          role=role if role in ("created", "review") else None, current_user=ctx.user)
        rows = [{
            "id": r.get("id"), "title": r.get("title"), "status": r.get("status"), "created_by": r.get("created_by"),
            "created": str(r.get("created_date") or "")[:10], "source": r.get("source_branch"),
            "target": r.get("target_branch"), "repository": r.get("repository"), "repository_id": r.get("repository_id"),
            "project": r.get("project"), "collection": r.get("collection"),
            "mine": r.get("is_creator"), "i_review": r.get("is_reviewer"), "my_vote": r.get("my_vote"), "url": r.get("url"),
        } for r in (result.get("data") or [])][:top]
    return {
        "data": {"pull_requests": rows, "count": len(rows)},
        "summary": f"{len(rows)} pull request{'s' if len(rows) != 1 else ''}",
        "links": [{"title": f"PR {r['id']}: {r['title']}", "url": r["url"]} for r in rows[:6] if r.get("url")],
    }


def ado_repositories(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.ado_pat()
    result = _ado().get_repositories(project=args["project"], collection=args.get("collection"), current_user=ctx.user)
    names = [r.get("name") for r in result.get("data") or []]
    if not names:
        raise ToolFailure(f"No repositories found in project '{args['project']}' (or the project does not exist).")
    return {"data": {"project": args["project"], "repositories": names[:100]},
            "summary": f"{len(names)} repositor{'ies' if len(names) != 1 else 'y'} in {args['project']}"}


COLLECTION = {"type": "string", "description": "Azure DevOps collection name. Omit to search all."}

register(
    Tool("ado_projects", "azure", "read", "Listing Azure DevOps projects",
         "List the Azure DevOps projects the user can see, across collections.",
         {"collection": COLLECTION}, ado_projects),
    Tool("ado_pipeline_runs", "azure", "read", "Finding pipeline runs",
         "Recent pipeline runs, newest first. Filter by project, pipeline name (substring) and result.",
         {"project": {"type": "string"}, "pipeline": {"type": "string", "description": "Part of the pipeline name"},
          "result": {"type": "string", "enum": ["failed", "succeeded", "partiallysucceeded", "canceled", "inprogress"]},
          "mine": {"type": "boolean", "description": "Only runs the user queued"},
          "top": {"type": "integer", "description": "Max runs, up to 20"}, "collection": COLLECTION},
         ado_pipeline_runs),
    Tool("ado_work_items", "azure", "read", "Searching work items",
         "Search work items. query is an ID or words in the title. state 'open' means not done/closed.",
         {"query": {"type": "string"}, "type": {"type": "string", "description": "e.g. Bug, Task, Product Backlog Item, Feature, Epic"},
          "state": {"type": "string"}, "assigned_to_me": {"type": "boolean"}, "assigned_to": {"type": "string"},
          "project": {"type": "string"}, "top": {"type": "integer"}, "collection": COLLECTION},
         ado_work_items),
    Tool("ado_work_item", "azure", "read", "Reading a work item",
         "One work item in full: fields, description, parent/children, linked PRs, commits, builds, recent comments.",
         {"id": {"type": "integer"}, "collection": COLLECTION}, ado_work_item, required=["id"], max_chars=4200),
    Tool("ado_pull_requests", "azure", "read", "Finding pull requests",
         "Pull requests. Without repository: the user's own (role created) or waiting for their review (role review). "
         "With project and repository: every PR in that repository.",
         {"project": {"type": "string"}, "repository": {"type": "string"}, "role": {"type": "string", "enum": ["created", "review"]},
          "status": {"type": "string", "enum": ["active", "completed", "abandoned", "all"]}, "top": {"type": "integer"},
          "collection": COLLECTION},
         ado_pull_requests),
    Tool("ado_repositories", "azure", "read", "Listing repositories",
         "Git repositories in one project.",
         {"project": {"type": "string"}, "collection": COLLECTION}, ado_repositories, required=["project"]),
)
