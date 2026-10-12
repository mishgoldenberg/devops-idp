"""
GitLab actions the portal performs on the signed-in person's behalf.

  POST /api/gitlab/actions/mr-approve       approve a merge request, or take the approval back
  POST /api/gitlab/actions/mr-comment       comment on a merge request
  POST /api/gitlab/actions/pipeline-retry   retry the failed jobs of a finished pipeline
  POST /api/gitlab/actions/pipeline-run     a new pipeline on a branch -- "run again" passes the
                                            branch and the variables of the pipeline it repeats

Exactly as api/ado_actions.py does it, because the dialog is the same dialog:

AS THE PERSON, NEVER AS THE PORTAL. Every call carries the person's own token, so GitLab
records the approval, the comment and the pipeline under their name and applies their
permissions. The Hub has no GitLab token of its own.

CHECKED TWICE. The dialog calls each action with ``dry_run`` first: the merge request is
read (still open? may you approve it? have you already?), the pipeline is read (finished?
anything to retry?), the branch is looked up. The person reads what will happen and
confirms; only then does the real call run, and its result is READ BACK before the Hub
says it happened.

Safe Mode is re-checked inside every action, right before the write. GitLab's 401 and 403
come back as 424 and 403-with-the-scope (never 401, which would sign the person out of
the Hub: gitlab_client.refusal).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import gitlab_client as gl
import safe_mode
import streaks
from integrations_cache import invalidate_owner
from security import AuthUser, get_current_user
from common import dry_run, simulated

log = logging.getLogger(__name__)
router = APIRouter()


# ── request bodies ───────────────────────────────────────────────────────────

class _Target(BaseModel):
    # True: check and describe, change nothing. The dialog's confirmation step.
    dry_run: bool = False


class _MergeRequest(_Target):
    project_id: int = Field(..., ge=1)
    iid: int = Field(..., ge=1)


class ApproveBody(_MergeRequest):
    # False takes an approval back.
    approve: bool = True
    comment: str = Field("", max_length=4000)


class MrCommentBody(_MergeRequest):
    text: str = Field(..., min_length=1, max_length=4000)


class RetryBody(_Target):
    project_id: int = Field(..., ge=1)
    pipeline_id: int = Field(..., ge=1)


class RunBody(_Target):
    project_id: int = Field(..., ge=1)
    ref: str = Field(..., min_length=1, max_length=400)
    variables: Dict[Annotated[str, Field(max_length=255)], Annotated[str, Field(max_length=10000)]] = Field(default_factory=dict)
    # The pipeline this repeats, when it is "run again": its variables are offered.
    from_pipeline_id: Optional[int] = Field(None, ge=1)


# ── plumbing ─────────────────────────────────────────────────────────────────

def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300]
    if isinstance(body, dict):
        for key in ("message", "error", "error_description"):
            value = body.get(key)
            if value:
                return (str(value) if not isinstance(value, (dict, list)) else str(value))[:400]
    return ""


def _fail(resp: httpx.Response, what: str) -> HTTPException:
    """Why GitLab said no, in words that say whose problem it is."""
    code = resp.status_code
    if code == 401:
        return HTTPException(status_code=424, detail="GitLab did not accept your token (HTTP 401). It may have "
                                                     "expired: reconnect it on the Connections page.")
    if code == 403:
        return HTTPException(status_code=403, detail=f"GitLab did not allow you to {what} (HTTP 403). Your token "
                                                     "needs the api scope (read_api only reads), and your account "
                                                     "needs the right role in the project.")
    if code == 404:
        return HTTPException(status_code=404, detail="GitLab could not find it (HTTP 404). It may have been "
                                                     "deleted, or your account cannot see it.")
    detail = _message(resp) or f"HTTP {code}"
    if code in (400, 405, 409, 422):
        return HTTPException(status_code=409, detail=f"GitLab refused: {detail}")
    return HTTPException(status_code=502, detail=f"GitLab answered HTTP {code}: {detail}")


def _read(cl: httpx.Client, path: str, what: str) -> Dict[str, Any]:
    resp = cl.get(path)
    if resp.status_code != 200:
        raise _fail(resp, what)
    data = gl.json_of(resp)
    return data if isinstance(data, dict) else {}


def _done(user: AuthUser, kind: Optional[str]) -> None:
    """After a real write: the cached lists are wrong now, and -- for the kinds a streak
    counts -- the day counts. A pipeline is not one: the day it SUCCEEDS is."""
    try:
        invalidate_owner(gl.SYSTEM, gl.owner(user))
    except Exception as exc:
        log.warning("gitlab actions: cache not cleared for %s: %s", gl.owner(user), exc)
    if kind:
        streaks.record(str(user.get("email") or ""), kind)


# ── merge requests ───────────────────────────────────────────────────────────

def _read_mr(cl: httpx.Client, body: _MergeRequest) -> Dict[str, Any]:
    mr = _read(cl, f"/projects/{body.project_id}/merge_requests/{body.iid}", "read this merge request")
    state = str(mr.get("state") or "").lower()
    if state != "opened":
        raise HTTPException(status_code=409, detail=f"This merge request is {state or 'closed'}, so it no longer "
                                                    "takes approvals or comments.")
    return mr


def _where(mr: Dict[str, Any]) -> str:
    return gl.project_path_of(mr) or "its project"


def _note(cl: httpx.Client, body: _MergeRequest, text: str) -> Dict[str, Any]:
    resp = cl.post(f"/projects/{body.project_id}/merge_requests/{body.iid}/notes", json={"body": text.strip()})
    if resp.status_code >= 300:
        raise _fail(resp, "comment on merge requests")
    note = gl.json_of(resp)
    if not isinstance(note, dict) or not note.get("id"):
        raise HTTPException(status_code=502, detail="GitLab did not return the comment it was sent.")
    return note


def _approval_state(cl: httpx.Client, body: _MergeRequest) -> Dict[str, Any]:
    return _read(cl, f"/projects/{body.project_id}/merge_requests/{body.iid}/approvals", "read its approvals")


def _approved(state: Dict[str, Any], me_id: Any) -> bool:
    if state.get("user_has_approved") is not None:
        return bool(state.get("user_has_approved"))
    return any(((a or {}).get("user") or {}).get("id") == me_id for a in state.get("approved_by") or [])


def _approval_line(state: Dict[str, Any]) -> str:
    names = [((a or {}).get("user") or {}).get("name") or "" for a in state.get("approved_by") or []]
    names = [n for n in names if n]
    have = f"Approved by {', '.join(names[:4])}{' and more' if len(names) > 4 else ''}" if names else "Nobody has approved it yet"
    required = state.get("approvals_required")
    if isinstance(required, int) and required > 0:
        left = state.get("approvals_left")
        return f"{have}: {len(names)} of {required} required" + (f", {left} still needed." if left else ".")
    return have + "."


@router.post("/mr-approve")
def mr_approve(body: ApproveBody, current_user: AuthUser = Depends(get_current_user)):
    token = gl.require_token(current_user)
    with gl.client(token) as cl:
        mr = _read_mr(cl, body)
        who = gl.me(cl)
        state = _approval_state(cl, body)
        already = _approved(state, who["id"])
        title = str(mr.get("title") or f"!{body.iid}")
        verb = "Approve" if body.approve else "Take back your approval of"
        summary = f"{verb} merge request !{body.iid} “{title}” in {_where(mr)}" + (
            ", with your comment" if body.comment.strip() else "")
        checks = ["The merge request is open."]
        if body.approve:
            if already:
                raise HTTPException(status_code=409, detail="You have already approved this merge request.")
            if state.get("user_can_approve") is False:
                raise HTTPException(status_code=409, detail="GitLab says you may not approve this merge request: "
                                                            "you may be its author, or not an eligible approver.")
            if (mr.get("author") or {}).get("id") == who["id"]:
                checks.append("You opened it: GitLab decides whether authors may approve their own.")
            else:
                checks.append("GitLab lists you as able to approve it.")
        else:
            if not already:
                raise HTTPException(status_code=409, detail="You have not approved this merge request, so there is "
                                                            "nothing to take back.")
            checks.append("Your approval is on it now.")
        checks.append(_approval_line(state))
        if mr.get("draft") or mr.get("work_in_progress"):
            checks.append("It is still marked Draft.")
        url = str(mr.get("web_url") or "")
        if body.dry_run:
            return dry_run(summary, checks, url=url)
        if safe_mode.is_enabled():
            return simulated("GitLab", summary, url=url)

        path = f"/projects/{body.project_id}/merge_requests/{body.iid}/{'approve' if body.approve else 'unapprove'}"
        resp = cl.post(path)
        if resp.status_code >= 300:
            raise _fail(resp, "approve merge requests" if body.approve else "take back approvals")
        back = _approval_state(cl, body)
        if _approved(back, who["id"]) != body.approve:
            raise HTTPException(status_code=502, detail="GitLab took the request but does not show the change. Check it there.")
        note = ""
        if body.comment.strip():
            try:
                _note(cl, body, body.comment)
            except HTTPException as exc:
                note = f" Your comment was not added: {exc.detail}"
    _done(current_user, "pr")
    return {
        "success": True,
        "summary": summary,
        "result": ("GitLab now shows your approval. " if body.approve else "Your approval is withdrawn. ")
                  + _approval_line(back) + note,
        "url": url,
    }


@router.post("/mr-comment")
def mr_comment(body: MrCommentBody, current_user: AuthUser = Depends(get_current_user)):
    token = gl.require_token(current_user)
    with gl.client(token) as cl:
        mr = _read_mr(cl, body)
        title = str(mr.get("title") or f"!{body.iid}")
        summary = f"Comment on merge request !{body.iid} “{title}”"
        url = str(mr.get("web_url") or "")
        if body.dry_run:
            return dry_run(summary, ["The merge request is open.", f"It is in {_where(mr)}."], url=url)
        if safe_mode.is_enabled():
            return simulated("GitLab", summary, url=url)
        note = _note(cl, body, body.text)
        back = cl.get(f"/projects/{body.project_id}/merge_requests/{body.iid}/notes/{note['id']}")
        if back.status_code != 200:
            raise HTTPException(status_code=502, detail="GitLab accepted the comment but it cannot be found. Check it there.")
    _done(current_user, "pr")
    return {"success": True, "summary": summary, "result": "Your comment is on the merge request.", "url": url}


# ── pipelines ────────────────────────────────────────────────────────────────

def _pipeline(cl: httpx.Client, project_id: int, pipeline_id: int) -> Dict[str, Any]:
    return _read(cl, f"/projects/{project_id}/pipelines/{pipeline_id}", "read pipelines")


def _project(cl: httpx.Client, project_id: int) -> Dict[str, Any]:
    return _read(cl, f"/projects/{project_id}", "read this project")


@router.post("/pipeline-retry")
def pipeline_retry(body: RetryBody, current_user: AuthUser = Depends(get_current_user)):
    """GitLab's Retry: the failed and canceled jobs of THIS pipeline run again, the ones
    that passed are kept. Nothing to retry in a pipeline that passed: "run again" is
    for that."""
    token = gl.require_token(current_user)
    with gl.client(token) as cl:
        p = _pipeline(cl, body.project_id, body.pipeline_id)
        project = _project(cl, body.project_id)
        raw = str(p.get("status") or "").lower()
        if raw not in ("failed", "canceled"):
            raise HTTPException(status_code=409, detail=(
                f"Pipeline #{p.get('iid') or body.pipeline_id} is {raw or 'in an unknown state'}: only a failed or "
                "canceled pipeline has jobs to retry." + (" Use Run again for a new one." if raw in ("success", "skipped") else "")))
        name = str(project.get("path_with_namespace") or project.get("name") or "the project")
        summary = f"Retry the failed jobs of pipeline #{p.get('iid') or body.pipeline_id} in {name}"
        checks = [f"It finished {raw}, on {p.get('ref') or 'its branch'}.",
                  "The jobs that passed are kept; the failed and canceled ones run again, on the same commit."]
        url = str(p.get("web_url") or "")
        if body.dry_run:
            return dry_run(summary, checks, url=url)
        if safe_mode.is_enabled():
            return simulated("GitLab", summary, url=url)
        resp = cl.post(f"/projects/{body.project_id}/pipelines/{body.pipeline_id}/retry")
        if resp.status_code >= 300:
            raise _fail(resp, "retry pipelines")
        back = _pipeline(cl, body.project_id, body.pipeline_id)
        now = str(back.get("status") or "").lower()
        if now in ("failed", "canceled"):
            raise HTTPException(status_code=502, detail=f"GitLab took the retry but the pipeline still shows {now}. Check it there.")
    _done(current_user, None)
    return {"success": True, "summary": summary, "result": f"Retrying: the pipeline is {now or 'starting'} now.", "url": url}


def _ref_exists(cl: httpx.Client, project_id: int, ref: str) -> Optional[str]:
    """"branch", "tag", or None. A 403 on both is a permission, not a missing ref."""
    for kind, path in (("branch", "branches"), ("tag", "tags")):
        resp = cl.get(f"/projects/{project_id}/repository/{path}/{gl.pid(ref)}")
        if resp.status_code == 200:
            return kind
        if resp.status_code in (401, 403):
            raise _fail(resp, "read this repository")
    return None


@router.post("/pipeline-run")
def pipeline_run(body: RunBody, current_user: AuthUser = Depends(get_current_user)):
    """A NEW pipeline on a branch or tag, with variables. The check reads the project,
    that CI is on, and that the branch exists; GitLab validates the rest when it creates
    the pipeline."""
    if len(body.variables) > 40:
        raise HTTPException(status_code=400, detail="At most 40 variables.")
    token = gl.require_token(current_user)
    ref = body.ref.strip()
    with gl.client(token) as cl:
        project = _project(cl, body.project_id)
        name = str(project.get("path_with_namespace") or project.get("name") or "the project")
        if project.get("jobs_enabled") is False or str(project.get("builds_access_level") or "") == "disabled":
            raise HTTPException(status_code=409, detail=f"CI/CD is switched off in {name}.")
        kind = _ref_exists(cl, body.project_id, ref)
        if not kind:
            raise HTTPException(status_code=409, detail=f"There is no branch or tag {ref} in {name}.")
        variables = {str(k).strip(): str(v) for k, v in body.variables.items() if str(k).strip()}
        summary = f"Run a new pipeline in {name} on {kind} {ref}"
        if variables:
            summary += " with " + ", ".join(f"{k}={v}" for k, v in list(variables.items())[:6])
        checks = [f"The {kind} {ref} exists in {name}.", "It runs from the latest commit of that branch."]
        if body.from_pipeline_id:
            checks.append(f"It repeats pipeline #{body.from_pipeline_id}, which stays as it is.")
        if variables:
            checks.append("GitLab checks the variables when it creates the pipeline.")
        url = str(project.get("web_url") or "") + "/-/pipelines"
        if body.dry_run:
            return dry_run(summary, checks, url=url)
        if safe_mode.is_enabled():
            return simulated("GitLab", summary, url=url)
        resp = cl.post(f"/projects/{body.project_id}/pipeline", json={
            "ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]})
        if resp.status_code >= 300:
            raise _fail(resp, "run pipelines")
        new = gl.json_of(resp) or {}
        if not isinstance(new, dict) or not new.get("id"):
            raise HTTPException(status_code=502, detail="GitLab did not say which pipeline it created.")
        back = cl.get(f"/projects/{body.project_id}/pipelines/{new['id']}")
        if back.status_code != 200:
            raise HTTPException(status_code=502, detail="GitLab accepted the pipeline but it cannot be found. Check it there.")
        url = str(new.get("web_url") or url)
    _done(current_user, None)
    return {"success": True, "summary": summary,
            "result": f"Created pipeline #{new.get('iid') or new['id']}: {gl.json_of(back).get('status') or 'created'}.",
            "url": url}


@router.get("/pipeline-variables")
def pipeline_variables(project_id: int, pipeline_id: int, current_user: AuthUser = Depends(get_current_user)):
    """The variables a pipeline ran with, for "run again" to offer. Masked ones come back
    empty: GitLab never returns their values, and the person types them again or
    leaves them out."""
    token = gl.require_token(current_user)
    with gl.client(token) as cl:
        resp = cl.get(f"/projects/{project_id}/pipelines/{pipeline_id}/variables")
        if resp.status_code != 200:
            raise _fail(resp, "read pipeline variables")
        rows = gl.json_of(resp) or []
    return {"success": True, "data": {str(v.get("key")): str(v.get("value") or "") for v in rows
                                      if isinstance(v, dict) and v.get("key")}}
