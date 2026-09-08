"""
Create and delete the Azure DevOps pipeline that runs a cleaner's YAML.

WHY THIS RUNS AFTER THE MERGE AND NOT AT THE PULL REQUEST
--------------------------------------------------------
A cleaner's pull request adds three files, one of which is pipeline.yaml. That file
is not a pipeline. Azure DevOps has no idea it exists until somebody creates a build
definition pointing at it, and until then merging the pull request applies nothing --
which is why every cleaner so far has needed an admin to go and make one by hand.

The definition cannot be created when the pull request OPENS, though. A YAML
definition names a file on the repository's default branch, and Azure DevOps
validates that the file is there when the definition is saved. At that moment the
file exists only on the cleaner's own branch, so the create is rejected. The one
moment both facts hold -- the file exists on main, and no definition points at it --
is immediately after the merge, which is where cleaner_store calls this.

WHAT IT DOES WHEN IT CANNOT
---------------------------
Nothing here is allowed to fail a merge that has already happened. Every function
answers with ``{"ok": bool, "detail": str}`` and never raises; the caller stores the
detail on the cleaner so an admin reading the record is told, in words, that the
pipeline was not created and what to create by hand -- the name, and the path of the
YAML file. A feature whose failure is invisible is a feature that silently does not
work, and this one's failure is invisible by construction: the pull request merged,
the files are on main, and everything looks finished.

The build definitions API is a different area from the git API this portal already
uses, and this deployment does not route every area (the Graph API answers 404 here).
So a 404 is reported as "not routed on this server", not as "the pipeline is missing"
-- they call for different people.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

log = logging.getLogger(__name__)

_API = "api-version=7.0"


def definition_name(repository: str) -> str:
    """What the pipeline is called: ``artifact-cleaner-automation - <repository>``.

    Named for the Artifactory repository it cleans, not for the cleaner, because the
    list of pipelines is read by somebody asking "what deletes from mapaz-docker-local"
    and the cleaner's own name is a label only its author chose.
    """
    return f"artifact-cleaner-automation - {str(repository or '').strip()}".strip(" -")


def _settings() -> Dict[str, str]:
    import ado_repo

    return ado_repo.repo_settings()


def _yaml_path(folder: str) -> str:
    """Repo-relative, no leading slash -- which is the form yamlFilename takes."""
    return f"{str(folder or '').strip('/')}/pipeline.yaml"


def _queue_id(client: Any, base: str, project: str, pool: str) -> Optional[int]:
    """The agent queue named by ADO_CLEANER_POOL, if it can be read.

    Optional on purpose. pipeline.yaml declares its own ``pool:`` and that is what
    actually runs the job; the queue on the definition is metadata. Refusing to create
    the pipeline because a queue lookup was forbidden would trade the whole feature
    for a field nothing reads.
    """
    try:
        resp = client.get(f"{base}/{project}/_apis/distributedtask/queues?{_API}")
        if resp.status_code >= 400:
            log.warning("ADO queue list: status=%s (pipeline will carry no queue)", resp.status_code)
            return None
        for row in (resp.json() or {}).get("value") or []:
            if str(row.get("name") or "").strip().lower() == pool.strip().lower():
                return int(row.get("id"))
    except Exception as exc:
        log.warning("ADO queue list failed: %s: %s", type(exc).__name__, exc)
    return None


def find(name: str, *, settings: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Look a definition up by exact name.

    Used before creating one so a re-merge does not make a second pipeline against
    the same YAML file -- two definitions on one file is two nightly runs deleting the
    same artifacts, and the second one is invisible from the portal.
    """
    import ado_repo

    cfg = dict(settings or _settings())
    project = cfg["project"]
    wanted = str(name).strip().lower()
    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            # The name goes through quote(), because this one has spaces in it by
            # design -- "artifact-cleaner-automation - devops-hub-backups". Dropped
            # into the query string raw, the filter matched nothing, so a pipeline
            # that plainly existed was reported absent and the create that followed
            # was refused with "already exists for project DevOps".
            resp = client.get(
                f"{base}/{project}/_apis/build/definitions"
                f"?name={quote(str(name), safe='')}&{_API}"
            )
            log.warning("ADO definition lookup: name=%r status=%s", name, resp.status_code)
            if resp.status_code == 404:
                return {"ok": False, "id": None, "url": "", "routed": False,
                        "detail": "the build definitions API is not routed on this server"}
            if resp.status_code >= 400:
                return {"ok": False, "id": None, "url": "", "routed": True,
                        "detail": f"Azure DevOps answered HTTP {resp.status_code}"}
            rows = (resp.json() or {}).get("value") or []
            for row in rows:
                if str(row.get("name") or "").strip().lower() == wanted:
                    return _found(base, project, row)

            # The filter answered, and answered nothing. It is a prefix match with its
            # own rules about spaces and case, so a second pass over the unfiltered
            # list decides it here instead of trusting the server's interpretation of
            # a name this portal chose.
            listed = client.get(f"{base}/{project}/_apis/build/definitions?$top=1000&{_API}")
            if listed.status_code < 400:
                for row in (listed.json() or {}).get("value") or []:
                    if str(row.get("name") or "").strip().lower() == wanted:
                        log.warning("ADO definition %r found by listing, not by filter", name)
                        return _found(base, project, row)
    except Exception as exc:
        log.warning("ADO definition lookup failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "id": None, "url": "", "routed": True,
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "id": None, "url": "", "routed": True, "detail": "no definition by that name"}


def _definition_yaml(
    definition_id: Any, *, settings: Optional[Dict[str, str]] = None
) -> Optional[str]:
    """The YAML file a definition runs, or None when it cannot be read.

    None and "" are different answers and the caller treats them differently: a
    definition that could not be read is adopted (assuming it is ours), because
    creating a second pipeline for the same cleaner is the worse of the two mistakes
    -- it would run the same delete spec twice a night.
    """
    import ado_repo

    cfg = dict(settings or _settings())
    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            resp = client.get(
                f"{base}/{cfg['project']}/_apis/build/definitions/{definition_id}?{_API}"
            )
            if resp.status_code >= 400:
                log.warning("ADO definition %s unreadable: status=%s",
                            definition_id, resp.status_code)
                return None
            return str(((resp.json() or {}).get("process") or {}).get("yamlFilename") or "")
    except Exception as exc:
        log.warning("ADO definition %s unreadable: %s: %s",
                    definition_id, type(exc).__name__, exc)
        return None


def _found(base: str, project: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True, "routed": True, "detail": "",
        "id": row.get("id"),
        "url": f"{base}/{project}/_build?definitionId={row.get('id')}",
    }


def create(
    repository: str,
    folder: str,
    *,
    slug: str = "",
    settings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Create the build definition for a merged cleaner. Never raises.

    Returns ``{"ok", "id", "url", "name", "detail", "manual"}``. ``manual`` carries
    what an admin has to do by hand when this could not: the name to give the
    pipeline and the YAML file to point it at. That pair is the entire instruction,
    and having it in the record is the difference between "the pipeline is missing"
    and "somebody has to work out what was supposed to exist".
    """
    import ado_repo

    cfg = dict(settings or _settings())
    project, repo, base_branch = cfg["project"], cfg["repo"], cfg["base_branch"]
    name = definition_name(repository)
    yaml_file = _yaml_path(folder)
    manual = f"Create a YAML pipeline named '{name}' from {repo}/{yaml_file} on {base_branch}."

    existing = find(name, settings=cfg)
    if not existing.get("ok") and existing.get("routed") is False:
        return {"ok": False, "id": None, "url": "", "name": name,
                "detail": existing["detail"], "manual": manual}

    if existing.get("ok") and existing.get("id"):
        # Something already holds this name. Whether that is THIS cleaner's pipeline
        # or another one's is decided by the YAML file it points at, never by the name
        # -- two cleaners on one repository produce the same name, and adopting the
        # wrong one would leave this cleaner unscheduled while the portal recorded a
        # pipeline id that runs somebody else's spec.
        points_at = _definition_yaml(existing["id"], settings=cfg)
        if points_at is None or points_at.strip("/").lower() == yaml_file.strip("/").lower():
            log.warning("ADO pipeline %r already exists (id=%s) and runs %s -- adopted",
                        name, existing["id"], points_at or "an unread file")
            return {"ok": True, "id": existing["id"], "url": existing["url"], "name": name,
                    "detail": "a pipeline with this name already existed", "manual": ""}
        # Taken by a different cleaner on the same repository, so this one needs its
        # own name. The slug is the folder, which is the one thing unique per cleaner.
        name = f"{name} ({slug})" if slug else f"{name} ({folder})"
        manual = f"Create a YAML pipeline named '{name}' from {repo}/{yaml_file} on {base_branch}."
        log.warning(
            "ADO pipeline name for %s was taken by a pipeline running %s -- using %r",
            repository, points_at, name,
        )

    pool = (os.getenv("ADO_CLEANER_POOL") or "DevOpsOrder").strip()
    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            repo_id = ado_repo._repo_id(client, base, project, repo)
            body: Dict[str, Any] = {
                "name": name,
                "path": "\\",
                "type": "build",
                "quality": "definition",
                "repository": {
                    "id": repo_id,
                    "name": repo,
                    "type": "TfsGit",
                    "defaultBranch": f"refs/heads/{base_branch}",
                },
                # type 2 is the YAML process. The file has to be on defaultBranch
                # already, which is why this runs after the merge and not before it.
                "process": {"type": 2, "yamlFilename": yaml_file},
            }
            queue = _queue_id(client, base, project, pool)
            if queue is not None:
                body["queue"] = {"id": queue}

            resp = client.post(
                f"{base}/{project}/_apis/build/definitions?{_API}", json=body
            )
            log.warning(
                "ADO definition create: name=%r yaml=%s status=%s body=%s",
                name, yaml_file, resp.status_code, resp.text[:300],
            )
            if resp.status_code == 404:
                return {"ok": False, "id": None, "url": "", "name": name, "manual": manual,
                        "detail": "the build definitions API is not routed on this server"}
            if resp.status_code >= 400:
                # "already exists" is not a failure, it is the answer. It means the
                # pipeline is there -- created by an admin by hand, or by an earlier
                # merge this portal did not manage to look up -- and the cleaner is
                # scheduled. Reporting it as an error put a red "Merged, not
                # scheduled" on a cleaner that was running perfectly well.
                if "already exists" in (resp.text or "").lower():
                    adopted = find(name, settings=cfg)
                    log.warning("ADO pipeline %r already existed; adopted id=%s",
                                name, adopted.get("id"))
                    return {
                        "ok": True, "name": name, "manual": "",
                        "id": adopted.get("id"), "url": adopted.get("url") or "",
                        "detail": "a pipeline with this name already existed",
                    }
                return {"ok": False, "id": None, "url": "", "name": name, "manual": manual,
                        "detail": f"Azure DevOps refused it (HTTP {resp.status_code}): {resp.text[:200]}"}
            created = resp.json() or {}
    except Exception as exc:
        log.warning("ADO definition create failed for %r: %s: %s", name, type(exc).__name__, exc)
        return {"ok": False, "id": None, "url": "", "name": name, "manual": manual,
                "detail": f"{type(exc).__name__}: {exc}"}

    definition_id = created.get("id")
    log.warning("ADO pipeline created: %r id=%s (cleaner %s)", name, definition_id, slug or folder)
    return {
        "ok": True,
        "id": definition_id,
        "url": f"{base}/{project}/_build?definitionId={definition_id}",
        "name": name,
        "detail": "",
        "manual": "",
    }


def authorize_variable_group(
    definition_id: Any,
    *,
    settings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Let this pipeline use the shared variable group without a human clicking Permit.

    A YAML pipeline that names a variable group it has not been authorised for does
    not fail -- it PAUSES, on its first run, waiting for somebody with access to the
    library to approve it in Azure DevOps. So a cleaner whose pull request merged and
    whose pipeline was created still does nothing on the night it was supposed to
    start, and the only sign is a run sitting in "waiting for approval" that nobody is
    watching.

    The portal already holds an administrator PAT and creates the pipeline, so it can
    do the grant in the same breath. Never raises; the reason is recorded and shown,
    because the failure mode without this is exactly the silent kind.
    """
    import ado_repo

    cfg = dict(settings or _settings())
    project = cfg["project"]
    group_name = (
        os.getenv("ADO_CLEANER_VARIABLE_GROUP")
        or "OpenShift cronjob-updater Service Account Credentials"
    ).strip()
    manual = (
        f"In Azure DevOps, open Pipelines > Library > '{group_name}' > Security and "
        f"give pipeline {definition_id} access, or run the pipeline once and press Permit."
    )
    if not definition_id:
        return {"ok": False, "detail": "no pipeline was created to authorise", "manual": manual}

    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            found = client.get(
                f"{base}/{project}/_apis/distributedtask/variablegroups"
                f"?groupName={quote(group_name, safe='')}&{_API}"
            )
            log.warning("ADO variable group lookup: name=%r status=%s",
                        group_name, found.status_code)
            if found.status_code >= 400:
                return {"ok": False, "manual": manual,
                        "detail": f"the variable group could not be read (HTTP {found.status_code})"}
            groups = (found.json() or {}).get("value") or []
            group = next(
                (g for g in groups
                 if str(g.get("name") or "").strip().lower() == group_name.lower()),
                None,
            )
            if not group:
                return {"ok": False, "manual": manual,
                        "detail": f"Azure DevOps has no variable group named '{group_name}'"}

            # A different API version from the rest of this module: pipeline
            # permissions are only served under the preview route.
            resp = client.patch(
                f"{base}/{project}/_apis/pipelines/pipelinePermissions/variablegroup/{group['id']}"
                "?api-version=7.1-preview.1",
                json={"pipelines": [{"id": int(definition_id), "authorized": True}]},
                headers={"Content-Type": "application/json"},
            )
            log.warning(
                "ADO variable group authorise: group=%s pipeline=%s status=%s",
                group["id"], definition_id, resp.status_code,
            )
            if resp.status_code == 404:
                return {"ok": False, "manual": manual,
                        "detail": "the pipeline permissions API is not routed on this server"}
            if resp.status_code >= 400:
                return {"ok": False, "manual": manual,
                        "detail": f"Azure DevOps refused it (HTTP {resp.status_code}): {resp.text[:200]}"}
            # Read back, because a 200 here has been known to mean "recorded" rather
            # than "granted" -- and the difference only shows up at 03:00.
            granted = [
                p for p in ((resp.json() or {}).get("pipelines") or [])
                if str(p.get("id")) == str(definition_id) and p.get("authorized")
            ]
            if not granted:
                return {"ok": False, "manual": manual,
                        "detail": "Azure DevOps accepted the grant but did not report the pipeline as authorised"}
    except Exception as exc:
        log.warning("ADO variable group authorise failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "manual": manual, "detail": f"{type(exc).__name__}: {exc}"}

    log.warning("ADO pipeline %s authorised for variable group %r", definition_id, group_name)
    return {"ok": True, "detail": "", "manual": "", "group": group_name}


def run(
    definition_id: Any,
    *,
    settings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Queue the pipeline's first build. Never raises.

    WHY A MERGED CLEANER IS RUN IMMEDIATELY
    ---------------------------------------
    Creating the definition does not create anything on the cluster. pipeline.yaml is
    a set of instructions to apply a CronJob and a ConfigMap, and until a build
    actually executes it, neither object exists -- the definition just sits there
    waiting for its next scheduled trigger, which is whenever somebody next touches
    that folder. So a cleaner could be merged, scheduled, green on every screen, and
    have nothing whatsoever on the cluster deleting anything.

    That also broke the other end. A removal hands the operator console links to the
    CronJob and ConfigMap; if no build ever ran, those links lead to a pair of "not
    found" pages and the portal asks somebody to confirm the deletion of two things
    that never existed.

    One build at the merge settles both: the objects exist from the moment the pull
    request lands, and what the portal says about them is true.

    Returns ``{"ok", "id", "url", "detail"}``. A failure here is recorded and shown;
    it never fails the merge, which has already happened.
    """
    import ado_repo

    cfg = dict(settings or _settings())
    project, base_branch = cfg["project"], cfg["base_branch"]
    if not definition_id:
        return {"ok": False, "id": None, "url": "",
                "detail": "no pipeline was recorded for this cleaner"}

    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            resp = client.post(
                f"{base}/{project}/_apis/build/builds?{_API}",
                json={
                    "definition": {"id": int(definition_id)},
                    # Named explicitly. A queue with no branch uses the definition's
                    # default, which is normally the same thing -- but "normally" is
                    # not good enough for a job whose whole purpose is to delete.
                    "sourceBranch": f"refs/heads/{base_branch}",
                },
            )
            log.warning("ADO build queue: definition=%s status=%s body=%s",
                        definition_id, resp.status_code, resp.text[:200])
            if resp.status_code == 404:
                return {"ok": False, "id": None, "url": "",
                        "detail": "the builds API is not routed on this server"}
            if resp.status_code >= 400:
                return {"ok": False, "id": None, "url": "",
                        "detail": f"Azure DevOps refused it (HTTP {resp.status_code}): "
                                  f"{resp.text[:200]}"}
            queued = resp.json() or {}
    except Exception as exc:
        log.warning("ADO build queue failed for definition %s: %s: %s",
                    definition_id, type(exc).__name__, exc)
        return {"ok": False, "id": None, "url": "", "detail": f"{type(exc).__name__}: {exc}"}

    build_id = queued.get("id")
    log.warning("ADO build %s queued for definition %s", build_id, definition_id)
    return {
        "ok": True,
        "id": build_id,
        "url": f"{base}/{project}/_build/results?buildId={build_id}",
        "detail": "",
    }


# What Azure DevOps calls a build, and what this portal calls it. `result` wins when
# the build has finished; `status` is only meaningful before that.
_RUN_FINISHED = {"succeeded": "succeeded", "partiallysucceeded": "succeeded",
                 "failed": "failed", "canceled": "failed"}


def run_states(build_ids, *, settings: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What became of these builds, in ONE call. Never raises.

    Returns ``{"ok", "states": {id: {"state", "url"}}, "detail"}`` where state is
    queued, running, succeeded or failed.

    Batched on purpose: this is read while somebody waits for a page, and a call per
    row would make the cost of the My Requests page depend on how many cleaners were
    merged recently rather than on anything the reader cares about.
    """
    ids = [str(b) for b in (build_ids or []) if b]
    if not ids:
        return {"ok": True, "states": {}, "detail": ""}

    import ado_repo

    cfg = dict(settings or _settings())
    project = cfg["project"]
    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            resp = client.get(
                f"{base}/{project}/_apis/build/builds?buildIds={','.join(ids)}&{_API}"
            )
            if resp.status_code >= 400:
                return {"ok": False, "states": {},
                        "detail": f"HTTP {resp.status_code}: {resp.text[:160]}"}
            rows = (resp.json() or {}).get("value") or []
    except Exception as exc:
        log.warning("ADO build states failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "states": {}, "detail": f"{type(exc).__name__}: {exc}"}

    states: Dict[str, Any] = {}
    for row in rows:
        status = str(row.get("status") or "").lower()
        result = str(row.get("result") or "").lower()
        if status == "completed":
            state = _RUN_FINISHED.get(result, "failed")
        elif status in ("inprogress", "cancelling"):
            state = "running"
        else:
            state = "queued"
        states[str(row.get("id"))] = {
            "state": state,
            "url": f"{base}/{project}/_build/results?buildId={row.get('id')}",
        }
    return {"ok": True, "states": states, "detail": ""}


def delete(
    definition_id: Any,
    *,
    name: str = "",
    settings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Remove the definition when a cleaner's removal is merged. Never raises.

    Deleting the folder stops the pipeline from having anything to run, but it leaves
    a definition in Azure DevOps pointing at a file that no longer exists -- which
    fails on every trigger and reads, to anybody looking at the pipelines list, like a
    cleaner that is broken rather than one that was deliberately removed.

    The id is looked up by name when the record has none, because the pipeline may
    have been created before the portal started recording it.
    """
    import ado_repo

    cfg = dict(settings or _settings())
    project = cfg["project"]
    if not definition_id and name:
        found = find(name, settings=cfg)
        definition_id = found.get("id")
        if not definition_id:
            return {"ok": True, "detail": found.get("detail") or "no such pipeline"}
    if not definition_id:
        return {"ok": True, "detail": "no pipeline was recorded for this cleaner"}

    try:
        base = ado_repo._collection_base(cfg["collection"])
        with ado_repo._client() as client:
            resp = client.delete(
                f"{base}/{project}/_apis/build/definitions/{definition_id}?{_API}"
            )
            log.warning("ADO definition delete: id=%s status=%s", definition_id, resp.status_code)
            # Already gone is the outcome that was asked for.
            if resp.status_code in (200, 202, 204, 404):
                return {"ok": True, "detail": ""}
            return {"ok": False,
                    "detail": f"Azure DevOps refused it (HTTP {resp.status_code}): {resp.text[:200]}"}
    except Exception as exc:
        log.warning("ADO definition delete failed for %s: %s: %s",
                    definition_id, type(exc).__name__, exc)
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
