"""
Azure DevOps actions the portal performs on the signed-in person's behalf.

  POST /api/azure-devops/actions/pr-vote              review a pull request (any vote, optional comment)
  POST /api/azure-devops/actions/pr-comment           comment on a pull request
  POST /api/azure-devops/actions/pipeline-rerun       run a finished pipeline run again
  GET  /api/azure-devops/actions/work-item            what the work-item dialog needs to know
  POST /api/azure-devops/actions/work-item-state      move a work item to another state
  POST /api/azure-devops/actions/work-item-description add a paragraph to its description
  POST /api/azure-devops/actions/work-item-comment    comment on it (its Discussion)
  POST /api/azure-devops/actions/work-item-assign     assign it to yourself or someone else
  POST /api/azure-devops/actions/work-item-create     create one (a bug DevBot drafted, for example)
  GET  /api/azure-devops/actions/people               who a work item can be assigned to

AS THE PERSON, NEVER AS THE PORTAL
----------------------------------
Every call uses the person's OWN token (_get_pat_for_user), so Azure DevOps records
the vote, the run and the edit under their name and applies their permissions. The
admin token is never used here: an approval written with it would be an approval by
nobody.

CHECKED TWICE
-------------
Each action is called twice by the dialog. First with ``dry_run``: the target is read
(is the pull request still active, has the run finished, is the move allowed) and,
for work items, the change goes to Azure DevOps with ``validateOnly`` so the server
itself says whether it would accept it. The person sees what is about to happen and
confirms. Only then does the real call run -- and its result is READ BACK before the
portal says it happened: "we sent it" is not "it is so".

Safe Mode is re-checked inside every action, right before the write.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import safe_mode
import streaks
from integrations_cache import invalidate_owner
from resilient_http import tls_verify
from security import AuthUser, get_current_user

from .azure_devops import (
    _ado_identity,
    _collection_name,
    _discover_ado_bases,
    _fetch_state_categories,
    _get_pat_for_user,
    _my_account_forms,
    _person_forms,
    normalize_admin_principal,
)

log = logging.getLogger(__name__)
router = APIRouter()

API = {"api-version": "7.0"}

# Azure DevOps reviewer votes, and how the portal words each one.
VOTES: Dict[int, str] = {
    10: "Approved",
    5: "Approved with suggestions",
    0: "No vote",
    -5: "Waiting for the author",
    -10: "Rejected",
}
# How the confirmation sentence starts, per vote.
VOTE_VERBS: Dict[int, str] = {
    10: "Approve",
    5: "Approve, with suggestions,",
    0: "Reset your vote on",
    -5: "Send back to the author:",
    -10: "Reject",
}


# ── request bodies ───────────────────────────────────────────────────────────

class _Target(BaseModel):
    collection: str = Field("", max_length=200)
    # True: check and describe, change nothing. The dialog's confirmation step.
    dry_run: bool = False


class _PullRequest(_Target):
    project: str = Field(..., min_length=1, max_length=256)
    repository_id: str = Field(..., min_length=1, max_length=128)
    pull_request_id: int = Field(..., ge=1)


class VoteBody(_PullRequest):
    vote: int
    comment: str = Field("", max_length=4000)


class PrCommentBody(_PullRequest):
    text: str = Field(..., min_length=1, max_length=4000)


class RerunBody(_Target):
    project: str = Field(..., min_length=1, max_length=256)
    build_id: int = Field(..., ge=1)


class _WorkItem(_Target):
    id: int = Field(..., ge=1)


class StateBody(_WorkItem):
    state: str = Field(..., min_length=1, max_length=128)


class TextBody(_WorkItem):
    text: str = Field(..., min_length=1, max_length=4000)


class AssignBody(_WorkItem):
    # Empty means "me".
    assignee: str = Field("", max_length=255)


class CreateBody(_Target):
    project: str = Field(..., min_length=1, max_length=256)
    type: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=8000)
    parent_id: Optional[int] = Field(None, ge=1)


# ── plumbing ─────────────────────────────────────────────────────────────────

def _client(pat: str) -> httpx.Client:
    return httpx.Client(
        verify=tls_verify(),
        auth=httpx.BasicAuth("", pat),
        timeout=httpx.Timeout(20.0, connect=5.0),
    )


def _base(client: httpx.Client, collection: str) -> str:
    """The base URL of the collection a row came from."""
    bases = [b for b in _discover_ado_bases(client) if b]
    wanted = (collection or "").strip().lower()
    if wanted:
        for base in bases:
            if _collection_name(base).lower() == wanted:
                return base
        raise HTTPException(status_code=400, detail=f"Unknown Azure DevOps collection '{collection}'.")
    if len(bases) == 1:
        return bases[0]
    raise HTTPException(status_code=400, detail="Say which Azure DevOps collection this is in.")


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])[:400]
    except ValueError:
        pass
    return (resp.text or "").strip()[:300]


def _json(resp: httpx.Response) -> Dict[str, Any]:
    """The JSON body -- or a refusal when Azure DevOps answered with a web page, which is
    what an unaccepted token gets and which a status code alone does not show."""
    if "html" in (resp.headers.get("content-type") or "").lower():
        raise HTTPException(
            status_code=502,
            detail="Azure DevOps answered with a web page instead of data, which usually means "
                   "it did not accept your token. Reconnect it on the Connections page.",
        )
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Azure DevOps answered with something that is not JSON.")
    return body if isinstance(body, dict) else {}


def _fail(resp: httpx.Response, what: str, scope: str) -> HTTPException:
    """Why Azure DevOps said no, in words that say whose problem it is."""
    code = resp.status_code
    if code == 401:
        return HTTPException(
            status_code=401,
            detail="Azure DevOps did not accept your token (HTTP 401). It may have expired: "
                   "reconnect it on the Connections page.",
        )
    if code == 403:
        return HTTPException(
            status_code=403,
            detail=f"Your token is not allowed to {what} (HTTP 403). It needs the {scope} scope: "
                   "create a new token with the scopes in the guide and paste it on the "
                   "Connections page.",
        )
    if code == 404:
        return HTTPException(
            status_code=404,
            detail="Azure DevOps could not find it (HTTP 404). It may have been deleted, or "
                   "your account cannot see it.",
        )
    detail = _message(resp) or f"HTTP {code}"
    if code in (400, 409, 412):
        return HTTPException(status_code=409 if code != 400 else 400,
                             detail=f"Azure DevOps refused: {detail}")
    return HTTPException(status_code=502, detail=f"Azure DevOps answered HTTP {code}: {detail}")


def _owner(user: AuthUser) -> str:
    return str(user.get("email") or user.get("username") or user.get("id") or "").lower()


def _done(user: AuthUser, kind: Optional[str]) -> None:
    """After a real write: the widgets' cached lists are now wrong, and -- for the kinds
    a streak counts -- the day counts. A re-run is not one: the day it SUCCEEDS is."""
    try:
        invalidate_owner("ado", _owner(user))
    except Exception as exc:
        log.warning("ado actions: cache not cleared for %s: %s", _owner(user), exc)
    if kind:
        streaks.record(str(user.get("email") or ""), kind)


def _dry(summary: str, checks: List[str], **extra: Any) -> Dict[str, Any]:
    return {"success": True, "dry_run": True, "summary": summary, "checks": checks, **extra}


def _simulated(summary: str, **extra: Any) -> Dict[str, Any]:
    return {
        "success": True,
        "simulated": True,
        "summary": summary,
        "result": "Safe Mode is on: nothing was sent to Azure DevOps.",
        **extra,
    }


def _short_branch(ref: str) -> str:
    return re.sub(r"^refs/heads/", "", str(ref or ""))


def _paragraph(text: str) -> str:
    """Plain text as HTML Azure DevOps shows as written: escaped, line breaks kept."""
    return html.escape(text.strip()).replace("\r\n", "\n").replace("\n", "<br>")


def _plain(markup: str) -> str:
    """HTML description as text, for a preview."""
    text = re.sub(r"<\s*br\s*/?>|</\s*(p|div|li|h\d)\s*>", "\n", str(markup or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# ── pull requests ────────────────────────────────────────────────────────────

def _pr_url(base: str, body: _PullRequest) -> str:
    return (f"{base}/{quote(body.project, safe='')}/_apis/git/repositories/"
            f"{quote(body.repository_id, safe='')}/pullrequests/{body.pull_request_id}")


def _read_pr(client: httpx.Client, base: str, body: _PullRequest) -> Dict[str, Any]:
    r = client.get(_pr_url(base, body), params=API)
    if r.status_code != 200:
        raise _fail(r, "read this pull request", "Code (Read)")
    pr = _json(r)
    state = str(pr.get("status") or "").lower()
    if state != "active":
        raise HTTPException(status_code=409, detail=f"This pull request is {state or 'closed'}, so it no longer takes votes or comments.")
    return pr


def _pr_web(base: str, pr: Dict[str, Any], body: _PullRequest) -> str:
    repo = (pr.get("repository") or {}).get("name") or body.repository_id
    return f"{base}/{quote(body.project, safe='')}/_git/{quote(str(repo), safe='')}/pullrequest/{body.pull_request_id}"


def _add_thread(client: httpx.Client, base: str, body: _PullRequest, text: str) -> Dict[str, Any]:
    r = client.post(
        f"{_pr_url(base, body)}/threads",
        params=API,
        json={"comments": [{"parentCommentId": 0, "content": text.strip(), "commentType": 1}], "status": 1},
    )
    if r.status_code >= 300:
        raise _fail(r, "comment on pull requests", "Code (Read & write)")
    thread = _json(r)
    if not thread.get("id"):
        raise HTTPException(status_code=502, detail="Azure DevOps did not return the comment it was sent.")
    return thread


@router.post("/pr-vote")
def pr_vote(body: VoteBody, current_user: AuthUser = Depends(get_current_user)):
    if body.vote not in VOTES:
        raise HTTPException(status_code=400, detail="Not an Azure DevOps vote.")
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        pr = _read_pr(client, base, body)
        me = _ado_identity(client, base)
        forms = _my_account_forms(me, current_user, str(current_user.get("username") or ""))
        reviewer = next((rv for rv in pr.get("reviewers") or [] if _person_forms(rv) & forms), None)
        reviewer_id = str((reviewer or {}).get("id") or me.get("id") or "")
        if not reviewer_id:
            raise HTTPException(
                status_code=409,
                detail="Azure DevOps did not say which reviewer is you: you are not on this pull "
                       "request's reviewer list, and your token lacks the User Profile (Read) scope "
                       "that would let the portal add you. Add that scope, or vote in Azure DevOps.",
            )
        title = str(pr.get("title") or f"#{body.pull_request_id}")
        repo_name = str((pr.get("repository") or {}).get("name") or "")
        summary = f"{VOTE_VERBS[body.vote]} pull request #{body.pull_request_id} “{title}” in {repo_name}" + (
            ", with your comment" if body.comment.strip() else "")
        current = int((reviewer or {}).get("vote") or 0)
        checks = [
            "The pull request is active.",
            "You are a reviewer on it." if reviewer else "You are not a reviewer yet: voting adds you.",
            f"Your vote now: {VOTES.get(current, current)}.",
        ]
        url = _pr_web(base, pr, body)
        if body.dry_run:
            return _dry(summary, checks, url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)

        payload: Dict[str, Any] = {"vote": body.vote}
        if not reviewer:
            payload["id"] = reviewer_id
        put = client.put(f"{_pr_url(base, body)}/reviewers/{quote(reviewer_id, safe='')}", params=API, json=payload)
        if put.status_code >= 300:
            raise _fail(put, "vote on pull requests", "Code (Read & write)")
        back = client.get(f"{_pr_url(base, body)}/reviewers/{quote(reviewer_id, safe='')}", params=API)
        shown = int(_json(back).get("vote") or 0) if back.status_code == 200 else None
        if shown != body.vote:
            raise HTTPException(
                status_code=502,
                detail=f"Azure DevOps took the vote but now shows “{VOTES.get(shown, shown)}”. Check it there.",
            )
        note = ""
        if body.comment.strip():
            try:
                _add_thread(client, base, body, body.comment)
            except HTTPException as exc:
                note = f" Your comment was not added: {exc.detail}"
    _done(current_user, "pr")
    return {
        "success": True,
        "summary": summary,
        "result": f"Azure DevOps now shows your vote as {VOTES[body.vote]}." + note,
        "url": url,
    }


@router.post("/pr-comment")
def pr_comment(body: PrCommentBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        pr = _read_pr(client, base, body)
        title = str(pr.get("title") or f"#{body.pull_request_id}")
        summary = f"Comment on pull request #{body.pull_request_id} “{title}”"
        url = _pr_web(base, pr, body)
        if body.dry_run:
            return _dry(summary, ["The pull request is active."], url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        _add_thread(client, base, body, body.text)
    _done(current_user, "pr")
    return {"success": True, "summary": summary, "result": "Your comment is on the pull request.", "url": url}


# ── pipelines ────────────────────────────────────────────────────────────────

@router.post("/pipeline-rerun")
def pipeline_rerun(body: RerunBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    project = quote(body.project, safe="")
    with _client(pat) as client:
        base = _base(client, body.collection)
        r = client.get(f"{base}/{project}/_apis/build/builds/{body.build_id}", params=API)
        if r.status_code != 200:
            raise _fail(r, "read pipeline runs", "Build (Read)")
        run = _json(r)
        definition = run.get("definition") or {}
        if not definition.get("id"):
            raise HTTPException(status_code=409, detail="Azure DevOps did not say which pipeline this run belongs to.")
        if str(run.get("status") or "").lower() != "completed":
            raise HTTPException(status_code=409, detail="This run has not finished yet. It can be run again once it has.")
        name = str(definition.get("name") or "the pipeline")
        branch = _short_branch(run.get("sourceBranch"))
        summary = f"Run {name} again on {branch or 'its default branch'}"
        checks = [
            f"The last run finished: {str(run.get('result') or 'unknown').lower()}.",
            "The new run starts from the branch's latest commit, with the same parameters.",
        ]
        if body.dry_run:
            return _dry(summary, checks)
        if safe_mode.is_enabled():
            return _simulated(summary)

        queue: Dict[str, Any] = {"definition": {"id": definition["id"]}}
        if run.get("sourceBranch"):
            queue["sourceBranch"] = run["sourceBranch"]
        if isinstance(run.get("templateParameters"), dict) and run["templateParameters"]:
            queue["templateParameters"] = run["templateParameters"]
        if run.get("parameters"):
            queue["parameters"] = run["parameters"]
        q = client.post(f"{base}/{project}/_apis/build/builds", params=API, json=queue)
        if q.status_code >= 300:
            raise _fail(q, "run pipelines", "Build (Read & execute)")
        new = _json(q)
        new_id = new.get("id")
        back = client.get(f"{base}/{project}/_apis/build/builds/{new_id}", params=API) if new_id else None
        if not new_id or back is None or back.status_code != 200:
            raise HTTPException(status_code=502, detail="Azure DevOps accepted the run but it cannot be found. Check the pipeline there.")
        url = f"{base}/{project}/_build/results?buildId={new_id}"
    _done(current_user, None)
    return {
        "success": True,
        "summary": summary,
        "result": f"Queued as run {new.get('buildNumber') or new_id}.",
        "url": url,
    }


# ── work items ───────────────────────────────────────────────────────────────

def _read_work_item(client: httpx.Client, base: str, wi_id: int) -> Dict[str, Any]:
    r = client.get(f"{base}/_apis/wit/workitems/{wi_id}", params=API)
    if r.status_code != 200:
        raise _fail(r, "read work items", "Work Items (Read)")
    return _json(r)


def _patch(client: httpx.Client, base: str, wi_id: int, ops: List[Dict[str, Any]], dry_run: bool) -> Dict[str, Any]:
    params = dict(API)
    if dry_run:
        # Azure DevOps runs every rule it would run for real -- allowed transitions,
        # required fields, known identities -- and saves nothing.
        params["validateOnly"] = "true"
    r = client.patch(
        f"{base}/_apis/wit/workitems/{wi_id}",
        params=params,
        content=json.dumps(ops),
        headers={"Content-Type": "application/json-patch+json"},
    )
    if r.status_code >= 300:
        raise _fail(r, "change work items", "Work Items (Read & write)")
    return _json(r)


def _wi_web(base: str, fields: Dict[str, Any], wi_id: int) -> str:
    project = str(fields.get("System.TeamProject") or "")
    return f"{base}/{quote(project, safe='')}/_workitems/edit/{wi_id}" if project else f"{base}/_workitems/edit/{wi_id}"


def _who(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        return {"display": str(value.get("displayName") or ""), "unique": str(value.get("uniqueName") or "")}
    return {"display": str(value or ""), "unique": ""}


def _text_field(fields: Dict[str, Any]) -> str:
    """Where a work item keeps its main text. Bugs in the Agile and CMMI processes use
    Repro Steps and hide Description, so text added to Description would vanish."""
    if str(fields.get("System.WorkItemType") or "").lower() == "bug" and \
            "Microsoft.VSTS.TCM.ReproSteps" in fields and not fields.get("System.Description"):
        return "Microsoft.VSTS.TCM.ReproSteps"
    return "System.Description"


@router.get("/work-item")
def work_item(
    id: int = Query(..., ge=1),
    collection: str = Query("", max_length=200),
    current_user: AuthUser = Depends(get_current_user),
):
    """Everything the work-item dialog shows before anyone changes anything."""
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, collection)
        item = _read_work_item(client, base, id)
        fields = item.get("fields") or {}
        project = str(fields.get("System.TeamProject") or "")
        wi_type = str(fields.get("System.WorkItemType") or "")
        states = list(_fetch_state_categories(client, base, project, wi_type).keys()) if project and wi_type else []
        me = _ado_identity(client, base)
    text_field = _text_field(fields)
    markup = str(fields.get(text_field) or "")
    return {
        "success": True,
        "data": {
            "id": id,
            "collection": _collection_name(base),
            "title": fields.get("System.Title") or "",
            "type": wi_type,
            "project": project,
            "state": fields.get("System.State") or "",
            "states": states,
            "assigned_to": _who(fields.get("System.AssignedTo")),
            "text_field": "Repro steps" if text_field != "System.Description" else "Description",
            "description_text": _plain(markup)[:4000],
            "rev": item.get("rev"),
            "url": _wi_web(base, fields, id),
            "me": {"display": me.get("display_name") or "", "unique": me.get("unique_name") or ""},
        },
    }


def _changed(client: httpx.Client, base: str, wi_id: int, check) -> bool:
    """Read the item back and ask whether the change is there."""
    try:
        return bool(check((_read_work_item(client, base, wi_id).get("fields") or {})))
    except HTTPException:
        return False


@router.post("/work-item-state")
def work_item_state(body: StateBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        item = _read_work_item(client, base, body.id)
        fields = item.get("fields") or {}
        now = str(fields.get("System.State") or "")
        title = str(fields.get("System.Title") or f"#{body.id}")
        if now == body.state:
            raise HTTPException(status_code=409, detail=f"It is already {now}.")
        summary = f"Move {fields.get('System.WorkItemType') or 'work item'} #{body.id} “{title}” from {now} to {body.state}"
        ops = [
            # Only if nobody changed it since it was read: a move built on a stale
            # read would silently undo whatever they did.
            {"op": "test", "path": "/rev", "value": item.get("rev")},
            {"op": "add", "path": "/fields/System.State", "value": body.state},
        ]
        url = _wi_web(base, fields, body.id)
        if body.dry_run:
            _patch(client, base, body.id, ops, dry_run=True)
            return _dry(summary, [f"Azure DevOps accepts moving it from {now} to {body.state}."], url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        try:
            _patch(client, base, body.id, ops, dry_run=False)
        except HTTPException:
            # Refused -- but a refusal can also mean it already moved (someone else, or
            # a rule). What it IS now is the answer, not what we asked for.
            if not _changed(client, base, body.id, lambda f: f.get("System.State") == body.state):
                raise
        if not _changed(client, base, body.id, lambda f: f.get("System.State") == body.state):
            raise HTTPException(status_code=502, detail=f"Azure DevOps did not keep the move to {body.state}. Check the item there.")
    _done(current_user, "workitem")
    return {"success": True, "summary": summary, "result": f"It is now {body.state}.", "url": url}


@router.post("/work-item-description")
def work_item_description(body: TextBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        item = _read_work_item(client, base, body.id)
        fields = item.get("fields") or {}
        field = _text_field(fields)
        label = "repro steps" if field != "System.Description" else "description"
        current = str(fields.get(field) or "")
        addition = f"<div>{_paragraph(body.text)}</div>"
        title = str(fields.get("System.Title") or f"#{body.id}")
        summary = f"Add a paragraph to the {label} of #{body.id} “{title}”"
        ops = [
            {"op": "test", "path": "/rev", "value": item.get("rev")},
            # Appended, never replaced: whatever formatting is already there -- tables,
            # images, lists -- stays exactly as it was.
            {"op": "add", "path": f"/fields/{field}", "value": current + addition},
        ]
        url = _wi_web(base, fields, body.id)
        if body.dry_run:
            _patch(client, base, body.id, ops, dry_run=True)
            return _dry(summary, [f"Azure DevOps accepts the change. The existing {label} stays as it is."], url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        _patch(client, base, body.id, ops, dry_run=False)
        if not _changed(client, base, body.id, lambda f: _paragraph(body.text) in str(f.get(field) or "")):
            raise HTTPException(status_code=502, detail=f"Azure DevOps did not keep the new text. Check the {label} there.")
    _done(current_user, "workitem")
    return {"success": True, "summary": summary, "result": f"The {label} now ends with your paragraph.", "url": url}


@router.post("/work-item-comment")
def work_item_comment(body: TextBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        item = _read_work_item(client, base, body.id)
        fields = item.get("fields") or {}
        title = str(fields.get("System.Title") or f"#{body.id}")
        summary = f"Comment on #{body.id} “{title}”"
        # System.History is the Discussion: one comment, and nothing else about the
        # item changes. Works on every Azure DevOps Server version, unlike the preview
        # comments API.
        ops = [{"op": "add", "path": "/fields/System.History", "value": _paragraph(body.text)}]
        url = _wi_web(base, fields, body.id)
        if body.dry_run:
            _patch(client, base, body.id, ops, dry_run=True)
            return _dry(summary, ["Azure DevOps accepts the comment."], url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        saved = _patch(client, base, body.id, ops, dry_run=False)
        if int(saved.get("rev") or 0) <= int(item.get("rev") or 0):
            raise HTTPException(status_code=502, detail="Azure DevOps did not record the comment. Check the item there.")
    _done(current_user, "workitem")
    return {"success": True, "summary": summary, "result": "Your comment is in its Discussion.", "url": url}


def _my_identity(client: httpx.Client, base: str, user: AuthUser, fields: Dict[str, Any]) -> str:
    """How Azure DevOps spells this person, for System.AssignedTo."""
    me = _ado_identity(client, base)
    if me.get("unique_name"):
        return str(me["unique_name"])
    # Already assigned to them: ADO's own spelling is right there.
    assigned = fields.get("System.AssignedTo")
    forms = _my_account_forms(me, user, str(user.get("username") or ""))
    if isinstance(assigned, dict) and _person_forms(assigned) & forms and assigned.get("uniqueName"):
        return str(assigned["uniqueName"])
    return normalize_admin_principal(str(user.get("username") or user.get("email") or ""))


@router.post("/work-item-assign")
def work_item_assign(body: AssignBody, current_user: AuthUser = Depends(get_current_user)):
    pat = _get_pat_for_user(current_user)
    with _client(pat) as client:
        base = _base(client, body.collection)
        item = _read_work_item(client, base, body.id)
        fields = item.get("fields") or {}
        target = body.assignee.strip() or _my_identity(client, base, current_user, fields)
        if not target:
            raise HTTPException(status_code=400, detail="Say who to assign it to.")
        title = str(fields.get("System.Title") or f"#{body.id}")
        who = "you" if not body.assignee.strip() else target
        before = _who(fields.get("System.AssignedTo"))
        summary = f"Assign #{body.id} “{title}” to {who}"
        ops = [
            {"op": "test", "path": "/rev", "value": item.get("rev")},
            {"op": "add", "path": "/fields/System.AssignedTo", "value": target},
        ]
        url = _wi_web(base, fields, body.id)
        if body.dry_run:
            # validateOnly is what catches a name Azure DevOps does not know.
            _patch(client, base, body.id, ops, dry_run=True)
            return _dry(summary, [
                f"Now assigned to: {before['display'] or before['unique'] or 'nobody'}.",
                f"Azure DevOps knows “{target}” and accepts the change.",
            ], url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        _patch(client, base, body.id, ops, dry_run=False)
        wanted = {f for f in [target.lower(), target.lower().split("\\")[-1], target.lower().split("@")[0]] if f}
        if not _changed(client, base, body.id,
                        lambda f: bool(_person_forms(f.get("System.AssignedTo") if isinstance(f.get("System.AssignedTo"), dict)
                                                     else {"displayName": str(f.get("System.AssignedTo") or "")}) & wanted)):
            raise HTTPException(status_code=502, detail="Azure DevOps did not keep the new assignee. Check the item there.")
    _done(current_user, "workitem")
    return {"success": True, "summary": summary, "result": f"It is now assigned to {who}.", "url": url}


@router.post("/work-item-create")
def work_item_create(body: CreateBody, current_user: AuthUser = Depends(get_current_user)):
    """A new work item, created as the person. The check is Azure DevOps's own
    validateOnly, which runs every rule it would run for real -- the type exists in the
    project's process, every required field is filled -- and saves nothing."""
    pat = _get_pat_for_user(current_user)
    project = quote(body.project, safe="")
    wi_type = body.type.strip()
    title = " ".join(body.title.split())
    with _client(pat) as client:
        base = _base(client, body.collection)
        # Bugs in the Agile and CMMI processes keep their text in Repro Steps and hide
        # Description, so text written to Description would vanish (see _text_field).
        text_field = "Microsoft.VSTS.TCM.ReproSteps" if wi_type.lower() == "bug" else "System.Description"
        ops: List[Dict[str, Any]] = [{"op": "add", "path": "/fields/System.Title", "value": title}]
        if body.description.strip():
            ops.append({"op": "add", "path": f"/fields/{text_field}", "value": _paragraph(body.description)})
        parent_note = ""
        if body.parent_id:
            parent = _read_work_item(client, base, body.parent_id)
            parent_note = f", under #{body.parent_id} “{(parent.get('fields') or {}).get('System.Title') or ''}”"
            ops.append({"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse", "url": f"{base}/_apis/wit/workItems/{body.parent_id}"}})
        summary = f"Create a {wi_type} “{title}” in {body.project}{parent_note}"
        url_root = f"{base}/{project}/_apis/wit/workitems/${quote(wi_type, safe='')}"

        def post(validate: bool) -> Dict[str, Any]:
            params = dict(API)
            if validate:
                params["validateOnly"] = "true"
            r = client.post(url_root, params=params, content=json.dumps(ops),
                            headers={"Content-Type": "application/json-patch+json"})
            if r.status_code >= 300:
                raise _fail(r, "create work items", "Work Items (Read & write)")
            return _json(r)

        if body.dry_run:
            post(True)
            return _dry(summary, [f"Azure DevOps accepts it: {wi_type} exists in {body.project} and every required field is filled.",
                                  "It is created in your name; nothing else changes."])
        if safe_mode.is_enabled():
            return _simulated(summary)
        created = post(False)
        new_id = int(created.get("id") or 0)
        if new_id <= 0:
            raise HTTPException(status_code=502, detail="Azure DevOps did not return the new work item.")
        back = _read_work_item(client, base, new_id)
        if str((back.get("fields") or {}).get("System.Title") or "") != title:
            raise HTTPException(status_code=502, detail=f"Azure DevOps created #{new_id} but it cannot be read back. Check it there.")
        url = _wi_web(base, back.get("fields") or {}, new_id)
    _done(current_user, "workitem")
    return {"success": True, "summary": summary, "result": f"Created {wi_type} #{new_id}.", "url": url, "id": new_id}


@router.get("/people")
def people(
    project: str = Query(..., min_length=1, max_length=256),
    collection: str = Query("", max_length=200),
    current_user: AuthUser = Depends(get_current_user),
):
    """People a work item in this project can be handed to: the project's default
    team. Needs Project and Team (Read); without it the list is empty and the dialog
    still takes a typed name, which Azure DevOps checks before anything is saved."""
    pat = _get_pat_for_user(current_user)
    out: List[Dict[str, str]] = []
    reason = ""
    with _client(pat) as client:
        base = _base(client, collection)
        proj = client.get(f"{base}/_apis/projects/{quote(project, safe='')}", params=API)
        team = ((_json(proj) if proj.status_code == 200 else {}).get("defaultTeam") or {}).get("id")
        if proj.status_code != 200 or not team:
            reason = f"The project's team could not be read (HTTP {proj.status_code})."
        else:
            members = client.get(
                f"{base}/_apis/projects/{quote(project, safe='')}/teams/{team}/members",
                params={**API, "$top": "300"},
            )
            if members.status_code == 200:
                for m in _json(members).get("value") or []:
                    ident = m.get("identity") or {}
                    if ident.get("isContainer"):
                        continue
                    out.append({"display": str(ident.get("displayName") or ""), "unique": str(ident.get("uniqueName") or "")})
            else:
                reason = f"The team's members could not be read (HTTP {members.status_code})."
    out.sort(key=lambda p: p["display"].lower())
    return {"success": True, "data": out, "reason": reason}
