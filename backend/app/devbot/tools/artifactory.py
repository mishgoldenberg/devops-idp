"""
Artifactory, read with the person's own token.

Requests go through the Hub's _artifactory_request, which tries the token as a Bearer
token and then as an API key -- the Hub does not know which kind a person pasted, and
neither does DevBot. Every value a person gives reaches AQL through _aql_literal.

Links point at the web UI (a sibling of the REST base on a JFrog Platform install),
never at the download URL, which serves a bare file index.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import quote

from fastapi import HTTPException

from .base import Tool, ToolContext, ToolFailure, register


def _integ():
    from api import integrations

    return integrations


def request(ctx: ToolContext, method: str, path: str, **kwargs: Any):
    integ = _integ()
    base = ctx.base("artifactory")
    try:
        return integ._artifactory_request(method, f"{base}{path}", ctx.token("artifactory"), timeout=20.0, **kwargs)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise ToolFailure("Artifactory did not accept your token. Reconnect it on the Connections page.")
        raise


def human_size(num: Any) -> str:
    try:
        size = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def ui_url(ctx: ToolContext, repo: str, path: str = "") -> str:
    root = _integ().artifactory_ui_root(ctx.base("artifactory"))
    native = "/".join(p for p in (repo, path.strip("/")) if p)
    return f"{root}/ui/repos/tree/General/{quote(native)}"


def aql(ctx: ToolContext, query: str) -> List[Dict[str, Any]]:
    resp = request(ctx, "POST", "/api/search/aql", content=query, headers={"Content-Type": "text/plain"})
    return resp.json().get("results") or []


def literal(value: str) -> str:
    return _integ()._aql_literal(value)


# ── tools ────────────────────────────────────────────────────────────────────

def art_repositories(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    params = {"type": args["type"]} if args.get("type") else None
    rows = request(ctx, "GET", "/api/repositories", params=params).json() or []
    wanted = (args.get("package_type") or "").lower()
    out = [{"key": r.get("key"), "type": r.get("type"), "package_type": r.get("packageType"),
            "description": (r.get("description") or "")[:100], "url": ui_url(ctx, r.get("key") or "")}
           for r in rows if r.get("key") and (not wanted or str(r.get("packageType") or "").lower() == wanted)]
    return {"data": {"repositories": out[:100], "count": len(out)},
            "summary": f"{len(out)} repositor{'ies' if len(out) != 1 else 'y'}"}


def art_search(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    name = args["name"].strip().strip("*")
    if len(name) < 2:
        raise ToolFailure("Give at least two characters of the artifact name.")
    criteria: Dict[str, Any] = {"name": {"$match": f"*{name}*"}, "type": "file"}
    if args.get("repo"):
        criteria["repo"] = args["repo"]
    top = max(1, min(int(args.get("top") or 15), 30))
    # AQL criteria are JSON, so json.dumps escapes every value the person typed.
    query = (f"items.find({json.dumps(criteria)})"
             '.include("repo","path","name","size","modified","created_by")'
             '.sort({"$desc":["modified"]})'
             f".limit({top})")
    rows = aql(ctx, query)
    out = []
    for r in rows:
        path = str(r.get("path") or "").strip(".")
        full = "/".join(p for p in (path, r.get("name")) if p)
        out.append({"repo": r.get("repo"), "path": full, "size": human_size(r.get("size")),
                    "modified": str(r.get("modified") or "")[:19], "created_by": r.get("created_by"),
                    "url": ui_url(ctx, r.get("repo") or "", full)})
    return {
        "data": {"artifacts": out, "count": len(out)},
        "summary": f"{len(out)} artifact{'s' if len(out) != 1 else ''} matching '{name}'",
        "links": [{"title": f"{a['repo']}/{a['path']}", "url": a["url"]} for a in out[:5]],
    }


def item_info(ctx: ToolContext, repo: str, path: str) -> Dict[str, Any]:
    path = path.strip("/")
    target = f"/api/storage/{quote(repo)}/{quote(path)}"
    try:
        info = request(ctx, "GET", target).json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise ToolFailure(f"There is nothing at {repo}/{path}.")
        raise
    props: Dict[str, Any] = {}
    try:
        raw = request(ctx, "GET", target, params={"properties": ""}).json().get("properties") or {}
        props = {k: (v[0] if isinstance(v, list) and len(v) == 1 else v) for k, v in list(raw.items())[:30]}
    except Exception:
        props = {}
    out = {
        "repo": repo, "path": path,
        "folder": "children" in info,
        "size": human_size(info.get("size")),
        "created": str(info.get("created") or "")[:19], "created_by": info.get("createdBy"),
        "modified": str(info.get("lastModified") or "")[:19], "modified_by": info.get("modifiedBy"),
        "sha256": (info.get("checksums") or {}).get("sha256"),
        "properties": props,
        "url": ui_url(ctx, repo, path),
    }
    if out["folder"]:
        out["children"] = [c.get("uri", "").strip("/") + ("/" if c.get("folder") else "") for c in (info.get("children") or [])[:40]]
    return out


def art_item(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    info = item_info(ctx, args["repo"], args["path"])
    return {"data": info, "summary": f"{args['repo']}/{info['path']}",
            "links": [{"title": f"{args['repo']}/{info['path']}", "url": info["url"]}]}


def docker_tags(ctx: ToolContext, repo: str, image: str, top: int = 25) -> List[Dict[str, Any]]:
    image = image.strip("/")
    query = (f'items.find({{"repo":"{literal(repo)}","path":{{"$match":"{literal(image)}/*"}},"name":"manifest.json"}})'
             '.include("path","modified","size")'
             '.sort({"$desc":["modified"]})'
             f".limit({top})")
    rows = aql(ctx, query)
    tags = []
    for r in rows:
        path = str(r.get("path") or "")
        if path.count("/") != image.count("/") + 1:
            continue
        tag = path.rsplit("/", 1)[-1]
        tags.append({"tag": tag, "pushed": str(r.get("modified") or "")[:19], "url": ui_url(ctx, repo, path)})
    return tags


def art_docker_tags(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    tags = docker_tags(ctx, args["repo"], args["image"], max(1, min(int(args.get("top") or 20), 40)))
    if not tags:
        raise ToolFailure(f"No tags found for image '{args['image']}' in repository '{args['repo']}'. "
                          "Check the image name: it is the path inside the repository, e.g. team/app.")
    return {"data": {"repo": args["repo"], "image": args["image"], "tags": tags},
            "summary": f"{len(tags)} tag{'s' if len(tags) != 1 else ''} of {args['image']}, newest {tags[0]['tag']}",
            "links": [{"title": f"{args['image']}:{t['tag']}", "url": t["url"]} for t in tags[:3]]}


register(
    Tool("art_repositories", "artifactory", "read", "Listing Artifactory repositories",
         "Artifactory repositories. type: local, remote or virtual. package_type e.g. docker, maven, npm, generic.",
         {"type": {"type": "string", "enum": ["local", "remote", "virtual", "federated"]},
          "package_type": {"type": "string"}}, art_repositories),
    Tool("art_search", "artifactory", "read", "Searching Artifactory",
         "Find artifacts whose file name contains a word, newest first. Optional repo.",
         {"name": {"type": "string"}, "repo": {"type": "string"}, "top": {"type": "integer"}},
         art_search, required=["name"]),
    Tool("art_item", "artifactory", "read", "Reading an artifact",
         "Details of one artifact or folder: size, dates, who deployed it, checksum and properties (build name, number, commit).",
         {"repo": {"type": "string"}, "path": {"type": "string", "description": "Path inside the repository"}},
         art_item, required=["repo", "path"]),
    Tool("art_docker_tags", "artifactory", "read", "Listing Docker tags",
         "Tags of a Docker image in a repository, newest first.",
         {"repo": {"type": "string"}, "image": {"type": "string", "description": "Image path inside the repo, e.g. team/app"},
          "top": {"type": "integer"}},
         art_docker_tags, required=["repo", "image"]),
)
