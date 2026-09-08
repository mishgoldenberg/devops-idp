"""
Write files into an Azure DevOps repository as a branch and a pull request.

Used by the Artifactory cleaner self-service, which needs three files committed into
artifact-cleaner-automation: a folder per cleaner holding configmap.yaml, cronjob.yaml
and pipeline.yaml, with pipeline.yaml pointing at the shared template.yaml at the repo
root.

Never to main. The files it writes are an AQL delete spec and a CronJob that runs
"jf rt del" against a real repository every night -- a spec with the wrong repo name
or the wrong age window deletes the wrong artifacts on a schedule, and there is no
undo for that outside the Artifactory trash can. So this creates a branch off main,
commits there, and opens a pull request: a human reads the spec before anything
applies it.

A push is one atomic call: the branch is created by the refUpdate in the same request
that carries the commit, so there is no window where a branch exists with nothing on
it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from resilient_http import tls_verify

log = logging.getLogger(__name__)

_API = "api-version=7.0"


def repo_settings() -> Dict[str, str]:
    """Where the cleaner repository lives. Defaults match the repository in use;
    every one is overridable, because a hardcoded coordinate is the thing that breaks
    the day somebody renames a project."""
    return {
        "collection": (os.getenv("ADO_CLEANER_COLLECTION") or os.getenv("ADO_DEFAULT_COLLECTION") or "DevCollection-Inheritance").strip(),
        "project": (os.getenv("ADO_CLEANER_PROJECT") or "DevOps").strip(),
        "repo": (os.getenv("ADO_CLEANER_REPO") or "artifact-cleaner-automation").strip(),
        "base_branch": (os.getenv("ADO_CLEANER_BASE_BRANCH") or "main").strip(),
    }


def is_configured() -> bool:
    return bool((os.getenv("AZURE_DEVOPS_ADMIN_PAT") or "").strip())


def projects_in_collection(collection: str = "") -> List[str]:
    """Every project name in a collection, for the Support ticket's picker.

    Read with the admin PAT rather than the caller's, for the same reason the
    Artifactory pickers are: somebody raising a ticket about a project may not be a
    member of it, and a picker that silently omits their project is a form they
    cannot complete honestly.

    Returns a plain list of names. Raises only when the collection cannot be resolved
    at all -- the caller turns that into an empty picker with a reason, never a 500,
    because a ticket must still be openable when Azure DevOps is unreachable.
    """
    name = str(collection or "").strip() or repo_settings()["collection"]
    base = _collection_base(name)
    with _client() as client:
        resp = client.get(f"{base}/_apis/projects?$top=500&{_API}")
        log.info("ADO project list: collection=%s status=%s", name, resp.status_code)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} listing projects in {name}")
        rows = (resp.json() or {}).get("value") or []
    return sorted({str(r.get("name") or "").strip() for r in rows if r.get("name")})


def _client() -> httpx.Client:
    pat = (os.getenv("AZURE_DEVOPS_ADMIN_PAT") or "").strip()
    if not pat:
        raise RuntimeError(
            "AZURE_DEVOPS_ADMIN_PAT is not configured. Committing the cleaner files "
            "needs an admin token with Code (Read, write) scope."
        )
    return httpx.Client(
        verify=tls_verify(),
        auth=httpx.BasicAuth("", pat),
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(60.0, connect=5.0),
        follow_redirects=True,
    )


def _collection_base(name: str) -> str:
    """The collection's own base URL, resolved from the server rather than assembled.

    Imported here rather than at module scope: api.azure_devops is a router module
    that the api package imports at start-up, and importing it back from a helper the
    package also loads is a cycle. By the time this runs, everything is loaded.
    """
    from api.azure_devops import _admin_ado_client, resolve_collections_named

    with _admin_ado_client() as client:
        found, missing = resolve_collections_named(client, [name])
    if not found:
        raise RuntimeError(
            f"Azure DevOps has no collection named '{name}' "
            f"(missing: {', '.join(missing) or 'unknown'})."
        )
    return found[0][1]


def slugify_folder(value: str) -> str:
    """A folder name that is safe in git, in a YAML path and in a Kubernetes name.

    Lowercase, alphanumerics and single hyphens. The same string becomes the folder,
    the branch suffix and the default resource names, so it is normalised once here
    rather than three times at three call sites.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError("A name is required.")
    return slug[:40].strip("-")


def pull_request_states(*, settings: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Every pull request in the cleaner repository, keyed by id.

    Opening the pull request is where this feature's work ends, but it is not where
    the story ends: a reviewer merges it, or abandons it, and those are opposite
    outcomes -- one puts a nightly delete job into the cluster, the other means
    nothing was ever applied. The portal recorded neither until it started asking.

    ONE call for all of them, not one per cleaner. This runs while somebody waits for
    a page, and resolving the collection and the repository costs two round trips
    before the first answer -- doing that per row turns a list of five cleaners into
    fifteen sequential calls against a server on the other side of a slow link.

    Returns ``{"ok", "detail", "states": {id: {...}}}``. ``ok`` is False when nothing
    could be read; the caller must then keep what it has stored rather than overwrite
    it with a guess. An id that is simply absent from the answer is also left alone:
    the listing is paged, and "older than the last 500 pull requests" is not "gone".
    """
    cfg = dict(settings or repo_settings())
    project, repo = cfg["project"], cfg["repo"]
    try:
        base = _collection_base(cfg["collection"])
        with _client() as client:
            repo_id = _repo_id(client, base, project, repo)
            resp = client.get(
                f"{base}/{project}/_apis/git/repositories/{repo_id}/pullrequests"
                f"?searchCriteria.status=all&$top=500&{_API}"
            )
            log.info("ADO pull request list: repo=%s status=%s", repo, resp.status_code)
            if resp.status_code >= 400:
                return {
                    "ok": False, "states": {},
                    "detail": f"Azure DevOps answered HTTP {resp.status_code} listing pull requests",
                }
            rows = (resp.json() or {}).get("value") or []
    except Exception as exc:
        log.warning("ADO pull requests unreadable: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "states": {}, "detail": f"{type(exc).__name__}: {exc}"}

    states: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        pr_id = str(row.get("pullRequestId") or "").strip()
        if not pr_id:
            continue
        states[pr_id] = {
            "state": str(row.get("status") or "").strip().lower(),
            "merge_status": str(row.get("mergeStatus") or "").strip().lower(),
            "closed_at": str(row.get("closedDate") or ""),
            "url": f"{base}/{project}/_git/{repo}/pullrequest/{pr_id}",
        }
    return {"ok": True, "states": states, "detail": ""}


def pull_request_state(
    pull_request_id: Any, *, settings: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """One pull request's state, read out of the same listing.

    Kept as a separate entry point because "what happened to this one" is the
    question the edit path asks, but it goes through the same call so there is one
    place that knows how Azure DevOps names these things.
    """
    pr_id = str(pull_request_id or "").strip()
    if not pr_id:
        return {"ok": False, "state": "", "detail": "no pull request id recorded"}
    batch = pull_request_states(settings=settings)
    if not batch["ok"]:
        return {"ok": False, "state": "", "detail": batch["detail"]}
    found = batch["states"].get(pr_id)
    if not found:
        return {
            "ok": False, "state": "",
            "detail": f"pull request {pr_id} is not in the repository's recent pull requests",
        }
    return {"ok": True, "detail": "", **found}


def pull_request_one(
    pull_request_id: Any, *, settings: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Read ONE pull request directly, by id.

    The batched listing is what every page uses, and it is the right default. This is
    for the two cases the listing cannot answer: a pull request older than the last
    500, and a listing that came back without it for any reason at all. Both look
    identical from the caller's side -- the row's state simply never changes -- and a
    cleaner frozen on "waiting for review" over a pull request somebody abandoned
    hours ago is the exact failure the state machine exists to prevent.
    """
    pr_id = str(pull_request_id or "").strip()
    if not pr_id:
        return {"ok": False, "state": "", "detail": "no pull request id recorded"}
    cfg = dict(settings or repo_settings())
    project, repo = cfg["project"], cfg["repo"]
    try:
        base = _collection_base(cfg["collection"])
        with _client() as client:
            repo_id = _repo_id(client, base, project, repo)
            resp = client.get(
                f"{base}/{project}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}?{_API}"
            )
            log.warning("ADO pull request read: id=%s status=%s", pr_id, resp.status_code)
            if resp.status_code >= 400:
                return {"ok": False, "state": "",
                        "detail": f"Azure DevOps answered HTTP {resp.status_code}"}
            row = resp.json() or {}
    except Exception as exc:
        log.warning("ADO pull request %s unreadable: %s: %s", pr_id, type(exc).__name__, exc)
        return {"ok": False, "state": "", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "detail": "",
        "state": str(row.get("status") or "").strip().lower(),
        "merge_status": str(row.get("mergeStatus") or "").strip().lower(),
        "closed_at": str(row.get("closedDate") or ""),
        "url": f"{base}/{project}/_git/{repo}/pullrequest/{pr_id}",
    }


def abandon_pull_request(
    pull_request_id: Any, *, settings: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Abandon a pull request, so an unreviewed cleaner is never a dead end.

    Every action on a cleaner whose pull request is still open was refused: it cannot
    be changed (a second pull request would race the first), and it cannot be removed
    (there is nothing in the repository yet to remove). Both refusals are correct and
    together they left a cleaner with no available action at all -- the requester's
    only route out was to find the pull request in Azure DevOps, which is the one
    place the portal deliberately does not send them.

    So the portal offers the third thing: withdraw it. The change was theirs and it
    has not been reviewed, so taking it back needs nobody's permission.

    Returns ``{"ok", "detail"}`` and does not raise; the caller reports the reason.
    """
    pr_id = str(pull_request_id or "").strip()
    if not pr_id:
        return {"ok": False, "detail": "no pull request id was recorded for this cleaner"}
    cfg = dict(settings or repo_settings())
    project, repo = cfg["project"], cfg["repo"]
    try:
        base = _collection_base(cfg["collection"])
        with _client() as client:
            repo_id = _repo_id(client, base, project, repo)
            resp = client.patch(
                f"{base}/{project}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}?{_API}",
                json={"status": "abandoned"},
            )
            log.warning("ADO pull request abandon: id=%s status=%s", pr_id, resp.status_code)
            if resp.status_code >= 400:
                # TF401181 / PullRequestNotEditableException means the pull request is
                # ALREADY closed -- merged or abandoned by somebody in Azure DevOps.
                # That is not a failure to withdraw it, it is the withdrawal having
                # happened without the portal noticing, so the caller is told the real
                # state instead of an exception dump. The portal being behind is the
                # thing to fix, and it cannot fix it by refusing.
                body = resp.text or ""
                if "TF401181" in body or "NotEditable" in body:
                    return {
                        "ok": False,
                        "already_closed": True,
                        "detail": "the pull request is already closed in Azure DevOps",
                    }
                return {
                    "ok": False,
                    "detail": f"Azure DevOps refused it (HTTP {resp.status_code}): {resp.text[:200]}",
                }
    except Exception as exc:
        log.warning("ADO pull request abandon failed for %s: %s: %s",
                    pr_id, type(exc).__name__, exc)
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "detail": ""}


def _repo_id(client: httpx.Client, base: str, project: str, repo: str) -> str:
    url = f"{base}/{project}/_apis/git/repositories/{repo}?{_API}"
    resp = client.get(url)
    log.info("ADO repo read: %s/%s status=%s", project, repo, resp.status_code)
    if resp.status_code == 404:
        raise RuntimeError(f"Azure DevOps has no repository '{repo}' in project '{project}'.")
    resp.raise_for_status()
    return str((resp.json() or {}).get("id") or "")


def _branch_tip(client: httpx.Client, base: str, project: str, repo_id: str, branch: str) -> str:
    url = f"{base}/{project}/_apis/git/repositories/{repo_id}/refs?filter=heads/{branch}&{_API}"
    resp = client.get(url)
    log.info("ADO ref read: heads/%s status=%s", branch, resp.status_code)
    resp.raise_for_status()
    refs = (resp.json() or {}).get("value") or []
    for ref in refs:
        if str(ref.get("name") or "") == f"refs/heads/{branch}":
            return str(ref.get("objectId") or "")
    raise RuntimeError(f"Branch '{branch}' does not exist in the repository.")


def _existing_paths(
    client: httpx.Client, base: str, project: str, repo_id: str, branch: str, paths: List[str]
) -> List[str]:
    """Which of these files already exist on the base branch.

    Checked before the push, not after: a push whose changeType is "add" fails
    outright when the path exists, and the useful message is "there is already a
    cleaner called X" rather than the server's complaint about an object id.
    """
    present: List[str] = []
    for path in paths:
        url = (
            f"{base}/{project}/_apis/git/repositories/{repo_id}/items"
            f"?path={path}&versionDescriptor.version={branch}&{_API}"
        )
        resp = client.get(url)
        if resp.status_code == 200:
            present.append(path)
        elif resp.status_code not in (404, 400):
            log.warning("ADO item probe: path=%s status=%s", path, resp.status_code)
    return present


def commit_files_on_branch(
    files: Dict[str, str],
    *,
    branch: str,
    commit_message: str,
    pr_title: str,
    pr_description: str,
    mode: str = "add",
    settings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Create a branch off the base branch with these files on it, and open a PR.

    files maps a repo-absolute path (starting with /) to its content.

    mode is "add" for a new cleaner and "edit" for a change to one that exists. The
    two are not interchangeable to Azure DevOps: an "add" whose path already exists is
    rejected, and an "edit" whose path does not exist is rejected too. Checking which
    files are actually there first means the message says "there is already a cleaner
    called X" or "that cleaner is not in the repository" rather than the server's
    complaint about an object id.
    """
    cfg = dict(settings or repo_settings())
    project, repo, base_branch = cfg["project"], cfg["repo"], cfg["base_branch"]
    base = _collection_base(cfg["collection"])

    with _client() as client:
        repo_id = _repo_id(client, base, project, repo)
        tip = _branch_tip(client, base, project, repo_id, base_branch)

        present = _existing_paths(client, base, project, repo_id, base_branch, list(files))
        if mode == "edit":
            absent = [p for p in files if p not in present]
            if absent:
                raise RuntimeError(
                    "These files are not on "
                    f"{base_branch}: {', '.join(absent)}. The cleaner may have been "
                    "removed, or its pull request was never merged."
                )
        elif mode == "delete":
            # Nothing to remove is the outcome asked for, not a failure. Somebody may
            # have deleted the folder by hand, or an earlier removal may already have
            # been merged; either way the cleaner is gone, which is what was wanted.
            if not present:
                return {
                    "collection": cfg["collection"], "project": project,
                    "repository": repo, "branch": "", "base_branch": base_branch,
                    "commit_id": "", "files": sorted(files),
                    "pull_request_id": None, "pull_request_url": "",
                    "already_absent": True,
                }
            # Only what is actually there: a delete of a path that does not exist is
            # rejected for the whole push, taking the files that DO exist with it.
            files = {path: "" for path in present}
        elif present:
            raise RuntimeError(
                "These files already exist on "
                f"{base_branch}: {', '.join(present)}. Pick a different name."
            )

        push_body = {
            "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": tip}],
            "commits": [{
                "comment": commit_message,
                "changes": [
                    {
                        "changeType": mode if mode in ("edit", "delete") else "add",
                        "item": {"path": path},
                        **({} if mode == "delete" else {
                            "newContent": {"content": content, "contentType": "rawtext"},
                        }),
                    }
                    for path, content in files.items()
                ],
            }],
        }
        push = client.post(
            f"{base}/{project}/_apis/git/repositories/{repo_id}/pushes?{_API}",
            json=push_body,
        )
        log.warning(
            "ADO push: repo=%s branch=%s files=%s status=%s",
            repo, branch, sorted(files), push.status_code,
        )
        if push.status_code >= 400:
            raise RuntimeError(
                f"Azure DevOps refused the commit (HTTP {push.status_code}): {push.text[:300]}"
            )
        commit_id = ""
        pushed = (push.json() or {}).get("commits") or []
        if pushed:
            commit_id = str(pushed[0].get("commitId") or "")

        pr = client.post(
            f"{base}/{project}/_apis/git/repositories/{repo_id}/pullrequests?{_API}",
            json={
                "sourceRefName": f"refs/heads/{branch}",
                "targetRefName": f"refs/heads/{base_branch}",
                "title": pr_title,
                "description": pr_description,
            },
        )
        log.warning("ADO pull request: branch=%s status=%s", branch, pr.status_code)
        if pr.status_code >= 400:
            # The branch is pushed and the work is not lost, which is what the message
            # has to say -- otherwise this reads as "nothing happened" and somebody
            # submits it a second time.
            raise RuntimeError(
                f"The files were committed to branch '{branch}', but opening the pull "
                f"request failed (HTTP {pr.status_code}): {pr.text[:200]}. Open it by hand."
            )
        pr_json = pr.json() or {}

    pr_id = pr_json.get("pullRequestId")
    return {
        "collection": cfg["collection"],
        "project": project,
        "repository": repo,
        "branch": branch,
        "base_branch": base_branch,
        "commit_id": commit_id,
        "files": sorted(files),
        "pull_request_id": pr_id,
        "pull_request_url": f"{base}/{project}/_git/{repo}/pullrequest/{pr_id}" if pr_id else "",
    }
