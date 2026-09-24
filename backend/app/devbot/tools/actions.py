"""
The propose_* tools: how the model asks for a change.

Each one looks up what the change is about (so the card can name it and the dialog can
open straight on it) and returns a proposal. It does not write: the person reviews the
change in the widgets' dialog and confirms it there, as themselves. The model is told
as much, so it never says a change has been made.
"""

from __future__ import annotations

from typing import Any, Dict

from . import ado, confluence, investigate, proposals
from .base import Tool, ToolContext, ToolFailure, register

TOLD = ("The person now sees a card with a Review button, which opens a dialog where they check and confirm it "
        "themselves. Tell them in one sentence what you proposed. Do NOT say it is done.")

VOTES = {"approve": 10, "approve_with_suggestions": 5, "reset": 0, "wait": -5, "reject": -10}


def _proposed(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"data": {"proposed": item["summary"], "status": "waiting for the person to confirm"},
            "actions": [item], "note": TOLD, "summary": "Proposed: " + item["title"]}


def propose_pipeline_rerun(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    # Whether it has finished is checked by the dialog's own first step, against Azure
    # DevOps, at the moment the person reviews it -- not here, minutes earlier.
    row, _, _ = investigate.find_run(ctx, args, want_failed=False)
    return _proposed(proposals.rerun(investigate.run_summary(row)))


def propose_work_item_change(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    change = args["change"]
    with ctx.ado_client() as client:
        base, item = ado.read_work_item(ctx, client, int(args["id"]), args.get("collection", ""))
    fields = item.get("fields") or {}
    title = str(fields.get("System.Title") or f"#{args['id']}")
    target: Dict[str, Any] = {"collection": ado.collection_of(base), "id": int(args["id"]), "title": title,
                              "project": fields.get("System.TeamProject")}
    if change in ("comment", "append"):
        if not args.get("text"):
            raise ToolFailure("Say what the text should be.")
        target["text"] = args["text"]
        kind = "wi-comment" if change == "comment" else "wi-description"
        what = f"Comment on #{args['id']} “{title}”" if change == "comment" else f"Add a paragraph to #{args['id']} “{title}”"
    elif change == "state":
        if not args.get("state"):
            raise ToolFailure("Say which state to move it to.")
        target["state"] = args["state"]
        kind, what = "wi-state", f"Move #{args['id']} “{title}” from {fields.get('System.State')} to {args['state']}"
    else:
        target["assignee"] = args.get("assignee") or "me"
        kind = "wi-assign"
        what = f"Assign #{args['id']} “{title}” to " + ("you" if target["assignee"] == "me" else target["assignee"])
    return _proposed(proposals.action(kind, what, what + ".", target))


def propose_work_item_create(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client() as client:
        project = ado.find_project(ctx, client, args["project"], args.get("collection", ""))
    wi_type = args.get("type") or "Bug"
    target = {"collection": project["collection"], "project": project["name"], "type": wi_type, "title": args["title"],
              "description": args.get("description") or "", "parent_id": args.get("parent_id")}
    return _proposed(proposals.action("wi-create", f"Create a {wi_type}",
                                      f"A {wi_type} in {project['name']} titled “{args['title']}”.", target))


def propose_pr_review(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client() as client:
        base, pr = investigate.find_pull_request(ctx, client, int(args["pr_id"]), args.get("collection", ""))
    repo = pr.get("repository") or {}
    project = (repo.get("project") or {}).get("name") or ""
    target = {"collection": ado.collection_of(base), "project": project, "repository_id": repo.get("id"),
              "id": pr.get("pullRequestId"), "title": pr.get("title")}
    vote = args.get("vote")
    if vote:
        target.update({"vote": VOTES[vote], "comment": args.get("comment") or ""})
        what = f"{vote.replace('_', ' ').capitalize()} pull request {target['id']} “{target['title']}”"
        return _proposed(proposals.action("vote", "Review the pull request", what + ".", target))
    if not args.get("comment"):
        raise ToolFailure("Say how to vote, or what to comment.")
    target["text"] = args["comment"]
    return _proposed(proposals.action("pr-comment", "Comment on the pull request",
                                      f"Comment on pull request {target['id']} “{target['title']}”.", target))


def propose_confluence_page(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.token("confluence")
    if args["mode"] == "append":
        if not args.get("page"):
            raise ToolFailure("Say which page to add to (its id or link).")
        page = confluence.read_page(ctx, args["page"], limit=200)
        target = {"page_id": page["id"], "title": page["title"], "heading": args.get("heading") or "",
                  "content": args["content"]}
        return _proposed(proposals.action("conf-append", "Add a section to the page",
                                          f"Add a section to the end of “{page['title']}”.", target))
    if not args.get("space") or not args.get("title"):
        raise ToolFailure("A new page needs a space key and a title.")
    target = {"space": args["space"], "title": args["title"], "content": args["content"], "parent_id": args.get("parent_id") or ""}
    return _proposed(proposals.action("conf-create", "Create a Confluence page",
                                      f"Create the page “{args['title']}” in space {args['space']}.", target))


COLLECTION = {"type": "string"}

register(
    Tool("propose_pipeline_rerun", "azure", "write", "Preparing a re-run",
         "Propose running a finished pipeline run again (same branch and parameters). The user confirms it.",
         {"run_id": {"type": "string"}, "project": {"type": "string"}, "pipeline": {"type": "string"}, "collection": COLLECTION},
         propose_pipeline_rerun, required=["run_id"]),
    Tool("propose_work_item_change", "azure", "write", "Preparing a work item change",
         "Propose a change to a work item: comment, append (a paragraph to its description), state (move it) or "
         "assign (assignee, or omit for the user). The user confirms it.",
         {"id": {"type": "integer"}, "change": {"type": "string", "enum": ["comment", "append", "state", "assign"]},
          "text": {"type": "string"}, "state": {"type": "string"}, "assignee": {"type": "string"}, "collection": COLLECTION},
         propose_work_item_change, required=["id", "change"]),
    Tool("propose_work_item_create", "azure", "write", "Preparing a new work item",
         "Propose creating a work item (default type Bug) with a title and a plain-text description. The user confirms it.",
         {"project": {"type": "string"}, "type": {"type": "string"}, "title": {"type": "string"},
          "description": {"type": "string"}, "parent_id": {"type": "integer"}, "collection": COLLECTION},
         propose_work_item_create, required=["project", "title"]),
    Tool("propose_pr_review", "azure", "write", "Preparing a pull request review",
         "Propose voting on a pull request (with an optional comment), or only commenting. The user confirms it.",
         {"pr_id": {"type": "integer"}, "vote": {"type": "string", "enum": list(VOTES)}, "comment": {"type": "string"},
          "collection": COLLECTION},
         propose_pr_review, required=["pr_id"]),
    Tool("propose_confluence_page", "confluence", "write", "Preparing a Confluence page",
         "Propose creating a Confluence page (mode create: space, title) or adding a section to one (mode append: "
         "page id or link, optional heading). content is Markdown. The user confirms it.",
         {"mode": {"type": "string", "enum": ["create", "append"]}, "space": {"type": "string"}, "title": {"type": "string"},
          "parent_id": {"type": "string"}, "page": {"type": "string"}, "heading": {"type": "string"},
          "content": {"type": "string"}},
         propose_confluence_page, required=["mode", "content"]),
)
