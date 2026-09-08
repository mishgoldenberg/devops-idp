"""
The portal's own record of every Artifactory cleaner, and what became of it.

WHY THIS EXISTS SEPARATELY FROM THE REQUEST
-------------------------------------------
An approval request finishes the moment its pull request is open. That is a true
statement about the request and a misleading one about the cleaner: nothing has been
deleted, nothing is scheduled, and whether anything ever will be depends on a person
in Azure DevOps who has not looked at it yet. The first cleaner ever reviewed was
ABANDONED, and the portal went on showing "Completed" -- because opening the pull
request was the only thing it had ever recorded.

So the request records what the portal did, and this records what happened next:

    PR_OPEN    the pull request is waiting for review; nothing runs yet
    RUNNING    it was merged, so the CronJob is in the repository
    ABANDONED  it was abandoned; nothing was applied and nothing will be
    FAILED     the request never got as far as a pull request

The state is read from Azure DevOps when somebody looks, cached for a minute, and
written back only when it CHANGED -- and an unreachable server leaves the stored
answer alone rather than overwriting it with a guess.

Both the Requests page and the request detail views read through here, so the two
cannot tell a user different stories about the same cleaner.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from db import execute, query_all

log = logging.getLogger(__name__)

PR_OPEN = "PR_OPEN"
RUNNING = "RUNNING"
ABANDONED = "ABANDONED"
FAILED = "FAILED"
# The removal was merged: the files are out of the repository and the pipeline is
# gone. NOT the end of the removal -- see CLUSTER_PENDING below.
REMOVED = "REMOVED"

# The cluster side of a removal. The CronJob and the ConfigMap live on a different
# cluster, behind a firewall this backend cannot cross, so merging the removal deletes
# the files that DESCRIBE them and leaves the objects themselves running. Somebody has
# to delete them, and until they say they have, the removal is not finished.
CLUSTER_PENDING = "PENDING"
CLUSTER_CLEARED = "CLEARED"

# Once a pull request is merged or abandoned it stays that way, so there is nothing
# to ask Azure DevOps about. Only PR_OPEN is worth a call.
_TERMINAL = {RUNNING, ABANDONED, FAILED, REMOVED}

# Azure DevOps' vocabulary to ours. "notSet" is a draft, which is still waiting.
_FROM_ADO = {
    "active": PR_OPEN,
    "notset": PR_OPEN,
    "completed": RUNNING,
    "abandoned": ABANDONED,
}

_LABEL = {
    PR_OPEN: ("Waiting for review", "pending",
              "The pull request is open. Nothing is deleted until somebody merges it."),
    RUNNING: ("Running", "ok",
              "The pull request was merged, so the schedule is live."),
    ABANDONED: ("Abandoned", "error",
                "The pull request was abandoned, so nothing was applied and this "
                "cleaner never runs. Submit it again if that was not intended."),
    FAILED: ("Failed", "error",
             "The request did not get as far as opening a pull request."),
    REMOVED: ("Removed", "ok",
              "The removal was merged and the pipeline is gone."),
}

_COLUMNS = (
    "id, slug, cleaner_name, repository, rules, summary, owner_id, owner_email, "
    "request_id, pull_request_url, pull_request_id, snow_number, status, pr_state, "
    "pr_closed_at, pr_checked_at, last_error, pending_delete, pipeline_id, "
    "pipeline_name, pipeline_url, pipeline_error, first_run_id, first_run_url, "
    "first_run_state, cluster_applied, cluster_state, cluster_cleared_by, "
    "cluster_cleared_at, created_at, updated_at"
)


# ── Writing ──────────────────────────────────────────────────────────────────

def record(
    request: Dict[str, Any],
    request_row: Dict[str, Any],
    result: Dict[str, Any],
    *,
    summary: str,
    mode: str,
) -> None:
    """Store (or update) the record for this cleaner.

    Keyed on the slug, which is the folder the files live in -- the one identifier
    shared by the record, the repository and the CronJob. An edit rewrites the same
    row, and puts it back to PR_OPEN, because an edit is a new pull request that has
    not been reviewed either.

    Raises. The caller decides what a lost record means; for a create it means the
    cleaner cannot be edited from the portal, which is worth a warning and not worth
    failing an open pull request over.
    """
    rules = {
        "older_than_days": request["older_than_days"],
        "keep_last": request["keep_last"],
        "name_pattern": request["name_pattern"],
        "path_pattern": request["path_pattern"],
        "file_types": request["file_types"],
        "schedule": request["schedule"],
    }
    execute(
        """
        INSERT INTO artifactory_cleaners
               (slug, cleaner_name, repository, rules, summary, owner_id, owner_email,
                request_id, pull_request_url, pull_request_id, status, pr_state,
                pr_checked_at, last_error, cluster_applied)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, 'active',
                CURRENT_TIMESTAMP, NULL, FALSE)
        ON CONFLICT (slug) DO UPDATE SET
               cleaner_name     = EXCLUDED.cleaner_name,
               repository       = EXCLUDED.repository,
               rules            = EXCLUDED.rules,
               summary          = EXCLUDED.summary,
               request_id       = EXCLUDED.request_id,
               pull_request_url = EXCLUDED.pull_request_url,
               pull_request_id  = EXCLUDED.pull_request_id,
               status           = EXCLUDED.status,
               pr_state         = 'active',
               pr_closed_at     = NULL,
               pr_checked_at    = CURRENT_TIMESTAMP,
               last_error       = NULL,
               updated_at       = CURRENT_TIMESTAMP
        """,
        [
            request["slug"], request["cleaner_name"], request["repository"],
            json.dumps(rules), summary,
            str(request_row.get("requester_id") or ""),
            str(request_row.get("requester_email") or ""),
            str(request_row.get("id") or ""),
            result.get("pull_request_url") or "",
            result.get("pull_request_id") or None,
            PR_OPEN,
        ],
    )
    log.warning("cleaner recorded: %s (%s) pr=%s", request["slug"], mode,
                result.get("pull_request_id") or "none")


def mark_failed(request_id: str, error: str) -> None:
    """A request that failed must not leave a record claiming a pull request is open.

    Best-effort and keyed on the request, not the slug: at this point the failure may
    be that the slug was never resolved. Only rows still waiting are touched -- a
    cleaner that is already running is not made to look failed by a later request
    against it going wrong.
    """
    if not str(request_id or "").strip():
        return
    try:
        execute(
            """
            UPDATE artifactory_cleaners
               SET status = %s, last_error = %s, updated_at = CURRENT_TIMESTAMP
             WHERE request_id = %s AND status = %s
            """,
            [FAILED, str(error or "")[:2000], str(request_id), PR_OPEN],
        )
    except Exception as exc:
        log.warning("cleaner failure not recorded for request %s: %s: %s",
                    request_id, type(exc).__name__, exc)


def record_removal(slug: str, request_row: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Mark a cleaner as having a removal waiting for review.

    The row stays. Its files are still on the base branch and its CronJob is still in
    the cluster until somebody merges the pull request that takes them out, so a
    record that vanished at submission would be lying in the other direction.
    """
    execute(
        """
        UPDATE artifactory_cleaners
           SET pending_delete   = TRUE,
               status           = %s,
               pr_state         = 'active',
               pr_closed_at     = NULL,
               pull_request_url = %s,
               pull_request_id  = %s,
               request_id       = %s,
               pr_checked_at    = CURRENT_TIMESTAMP,
               updated_at       = CURRENT_TIMESTAMP
         WHERE slug = %s
        """,
        [
            PR_OPEN,
            result.get("pull_request_url") or "",
            result.get("pull_request_id") or None,
            str(request_row.get("id") or ""),
            slug,
        ],
    )
    log.warning("cleaner removal opened for %s pr=%s", slug,
                result.get("pull_request_id") or "none")


def find_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """The cleaner occupying this folder, if there is one.

    The slug IS the identity: it names the folder in the repository, the ConfigMap and
    the CronJob. Two cleaners with the same slug are not two cleaners -- they are two
    pull requests fighting over one folder, which is what happened when the same name
    was submitted twice before either was reviewed.
    """
    rows = query_all(
        f"SELECT {_COLUMNS} FROM artifactory_cleaners WHERE slug = %s", [str(slug)]
    ) or []
    return rows[0] if rows else None


def on_repository(repository: str, *, exclude_slug: str = "") -> List[Dict[str, Any]]:
    """Other cleaners already deleting from this repository.

    Not an error -- two cleaners with different paths or file types are a normal way
    to express a policy. It IS something an approver has to be told, because two
    delete schedules on one repository can overlap in ways neither author intended.
    """
    rows = query_all(
        "SELECT slug, cleaner_name, summary, status FROM artifactory_cleaners "
        "WHERE LOWER(repository) = LOWER(%s) AND slug <> %s AND status <> %s",
        [str(repository), str(exclude_slug or ""), FAILED],
    ) or []
    return rows


def delete_record(cleaner_id: Any) -> None:
    """Forget a cleaner that was never applied.

    Only ever called for a record whose pull request was abandoned or whose request
    failed: there is nothing in the repository to remove, so there is nothing to
    review, and leaving the row would block the folder name forever.
    """
    execute("DELETE FROM artifactory_cleaners WHERE id = %s", [cleaner_id])
    log.warning("cleaner record %s deleted (nothing had been applied)", cleaner_id)


def delete_record_by_slug(slug: str) -> None:
    execute("DELETE FROM artifactory_cleaners WHERE slug = %s", [str(slug)])
    log.warning("cleaner record %s deleted (its files were already gone)", slug)


def set_snow_number(slug: str, number: str) -> None:
    execute(
        "UPDATE artifactory_cleaners SET snow_number = %s WHERE slug = %s",
        [number, slug],
    )


# ── Reading ──────────────────────────────────────────────────────────────────

def list_for(user_id: str, *, is_admin: bool) -> List[Dict[str, Any]]:
    """Every cleaner this person may see, with its pull request state refreshed.

    Filtered in the query rather than after it: a row the caller may not see must not
    reach the process that would have to remember to drop it.
    """
    rows = query_all(
        f"""
        SELECT {_COLUMNS}
          FROM artifactory_cleaners
         WHERE %s OR owner_id = %s
         ORDER BY created_at DESC
         LIMIT 200
        """,
        [bool(is_admin), str(user_id)],
    ) or []
    return refresh(rows)


def get(cleaner_id: Any) -> Optional[Dict[str, Any]]:
    """One cleaner, with its pull request state refreshed.

    Refreshed rather than read raw because the two callers -- opening the edit form
    and submitting it -- are deciding whether the files are on the base branch, and a
    stale answer to that is the difference between a change Azure DevOps accepts and
    one it rejects.
    """
    rows = query_all(
        f"SELECT {_COLUMNS} FROM artifactory_cleaners WHERE id = %s", [cleaner_id]
    ) or []
    if not rows:
        return None
    return refresh(rows)[0]


def for_requests(request_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """The cleaner record belonging to each of these requests, keyed by request id.

    Used to give a request its real outcome without every request list having to know
    what a cleaner is.

    Refreshed, like every other read. It was not, on the theory that the Requests page
    had already done it -- but My Requests and Approvals are different pages, opened
    by different people, and somebody who never visits Requests was shown "waiting for
    review" next to a pull request that had been abandoned for three hours. A state
    that is only correct if you took a particular route to it is not a state.
    """
    wanted = [str(r) for r in request_ids if str(r or "").strip()]
    if not wanted:
        return {}
    rows = query_all(
        f"SELECT {_COLUMNS} FROM artifactory_cleaners WHERE request_id = ANY(%s)",
        [wanted],
    ) or []
    return {str(row.get("request_id")): row for row in refresh(rows)}


def refresh(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ask Azure DevOps what became of every pull request still open, and store it.

    Bounded, because this runs while somebody waits for a page: rows already in a
    terminal state are not asked about, the whole repository's pull requests come back
    in ONE call however many rows there are, and that call is cached for a minute.
    """
    _refresh_runs(rows)
    pending = [
        row for row in rows
        if str(row.get("status") or "") not in _TERMINAL and row.get("pull_request_id")
    ]
    if not pending:
        return rows

    import ado_repo

    batch = _read_states()
    for row in pending:
        state = batch["states"].get(str(row["pull_request_id"])) if batch.get("ok") else None
        if not state:
            # Either the listing could not be read at all, or it answered without this
            # pull request in it -- older than the last 500, or simply absent. Both
            # left the row frozen on whatever it was last written with, which is how a
            # cleaner went on reading "waiting for review" over a pull request that had
            # been abandoned. One direct read per unanswered row, and only for rows the
            # batch did not cover, so the common case is still a single call.
            direct = ado_repo.pull_request_one(row["pull_request_id"])
            if not direct.get("ok"):
                row["pr_stale_reason"] = (
                    batch.get("detail") or direct.get("detail") or "not readable"
                )
                continue
            log.warning(
                "cleaner %s: pull request %s was not in the listing; read directly (%s)",
                row.get("slug"), row.get("pull_request_id"), direct.get("state"),
            )
            state = direct
        mapped = _FROM_ADO.get(str(state.get("state") or "").lower())
        if not mapped:
            continue
        _store_state(row, mapped, state)
    return rows


def _refresh_runs(rows: List[Dict[str, Any]]) -> None:
    """Catch up on first builds that had not finished last time anybody looked.

    This is what turns "the pipeline was told to run" into "the CronJob exists", and
    it is the only thing allowed to set cluster_applied. Nothing else can know: the
    portal cannot see that cluster, so a build reporting success is the only evidence
    it will ever get that the objects were created.

    Bounded the same way the pull request read is: only rows with an unfinished run,
    and all of them in ONE call. Never raises -- a build state nobody could read
    leaves the row exactly as it was.
    """
    waiting = [
        row for row in rows
        if row.get("first_run_id") and str(row.get("first_run_state") or "") in ("queued", "running")
    ]
    if not waiting:
        return

    import ado_pipeline

    batch = ado_pipeline.run_states([row["first_run_id"] for row in waiting])
    if not batch.get("ok"):
        log.warning("cleaner first-run states unreadable: %s", batch.get("detail"))
        return

    for row in waiting:
        state = (batch["states"] or {}).get(str(row["first_run_id"]))
        if not state or state["state"] == str(row.get("first_run_state") or ""):
            continue
        applied = state["state"] == "succeeded"
        row["first_run_state"] = state["state"]
        row["first_run_url"] = state.get("url") or row.get("first_run_url") or ""
        if applied:
            row["cluster_applied"] = True
        try:
            execute(
                "UPDATE artifactory_cleaners SET first_run_state = %s, first_run_url = %s, "
                "cluster_applied = cluster_applied OR %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                [state["state"], state.get("url") or "", applied, row.get("id")],
            )
        except Exception as exc:
            log.warning("cleaner %s first-run state not stored: %s: %s",
                        row.get("slug"), type(exc).__name__, exc)
        log.warning("cleaner %s first run is %s", row.get("slug"), state["state"])
        if state["state"] == "failed":
            _tell_owner(
                row, "not applied",
                "Its pull request was merged, but the run that creates the scheduled "
                "job failed, so nothing is being deleted yet.",
            )


def _read_states() -> Dict[str, Any]:
    import ado_repo
    from integrations_cache import cached_external

    # Keyed on the repository, not on the viewer: what happened to a pull request is
    # the same fact for everyone, and a per-user key would ask Azure DevOps once per
    # person looking at the same row.
    #
    # A FAILURE is never cached. Caching one turns a momentary blip into a full minute
    # in which every page is told Azure DevOps cannot be reached and no state can
    # change -- and pressing Refresh, which is what a person does about it, is exactly
    # the thing the cache makes pointless.
    batch = cached_external(
        "ado", "system", "cleaner-pull-requests",
        ado_repo.pull_request_states,
        ttl=60,
    )
    if not (batch or {}).get("ok"):
        try:
            from integrations_cache import forget_external

            forget_external("ado", "system", "cleaner-pull-requests")
        except Exception as exc:
            log.warning("cleaner pull request cache not cleared: %s: %s",
                        type(exc).__name__, exc)
    return batch or {"ok": False, "states": {}, "detail": "no answer"}


def _ensure_pipeline(row: Dict[str, Any]) -> None:
    """Create the Azure DevOps pipeline for a cleaner whose files just landed on main.

    This is the step that makes a merged cleaner actually run. Merging puts
    pipeline.yaml in the repository; a YAML file with no build definition pointing at
    it is never executed, so before this existed every merged cleaner still needed an
    admin to make one by hand -- and nothing told them.

    Called at the merge and nowhere else, because that is the only moment when the
    file is on the default branch (Azure DevOps validates that when the definition is
    saved) and no definition exists yet.

    Never raises. The pull request is merged whatever happens here, so a failure is
    recorded on the row and shown -- including to the requester, whose cleaner is not
    running if this did not work.
    """
    import ado_pipeline
    import artifactory_cleaner

    folder = artifactory_cleaner.names(str(row.get("slug") or ""))["folder"]
    outcome = ado_pipeline.create(
        str(row.get("repository") or ""), folder, slug=str(row.get("slug") or ""),
    )
    problem = "" if outcome.get("ok") else "; ".join(
        p for p in (outcome.get("detail"), outcome.get("manual")) if p
    )
    # The pipeline existing is not the same as the pipeline being able to run. It
    # names a variable group, and a pipeline that has not been authorised for one
    # does not fail on its first run -- it stops and waits for a person to press
    # Permit. Nobody is watching at 03:00, so the cleaner simply never starts and
    # everything on this screen says it is running.
    if outcome.get("ok") and outcome.get("id"):
        access = ado_pipeline.authorize_variable_group(outcome["id"])
        if not access.get("ok"):
            problem = "; ".join(
                p for p in (
                    problem,
                    f"The pipeline exists but is not authorised for its variable "
                    f"group, so its first run will wait for approval: {access.get('detail')}",
                    access.get("manual"),
                ) if p
            )
    # And run it, once, now.
    #
    # A definition is not an application. pipeline.yaml describes a CronJob and a
    # ConfigMap; until a build executes it, neither object exists on the cluster --
    # the definition simply waits for its next trigger, which is whenever somebody
    # next edits that folder. So without this a cleaner could be merged, scheduled,
    # green on every screen, and deleting precisely nothing. It also made the removal
    # story false at the other end: the console links handed to an operator pointed at
    # two objects that had never been created.
    run: Dict[str, Any] = {}
    if outcome.get("ok") and outcome.get("id"):
        run = ado_pipeline.run(outcome["id"])
        if not run.get("ok"):
            problem = "; ".join(p for p in (
                problem,
                f"The pipeline exists but its first run could not be started, so "
                f"nothing has been applied to the cluster yet: {run.get('detail')}",
                f"Run '{outcome.get('name')}' once in Azure DevOps.",
            ) if p)

    row["pipeline_id"] = outcome.get("id")
    row["pipeline_name"] = outcome.get("name") or ""
    row["pipeline_url"] = outcome.get("url") or ""
    row["pipeline_error"] = problem
    row["first_run_id"] = run.get("id")
    row["first_run_url"] = run.get("url") or ""
    row["first_run_state"] = "queued" if run.get("ok") else ""
    try:
        execute(
            """
            UPDATE artifactory_cleaners
               SET pipeline_id = %s, pipeline_name = %s, pipeline_url = %s,
                   pipeline_error = %s, first_run_id = %s, first_run_url = %s,
                   first_run_state = %s, updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [outcome.get("id"), outcome.get("name") or "", outcome.get("url") or "",
             problem[:2000], run.get("id"), run.get("url") or "",
             "queued" if run.get("ok") else "", row.get("id")],
        )
    except Exception as exc:
        log.warning("cleaner %s pipeline outcome not stored: %s: %s",
                    row.get("slug"), type(exc).__name__, exc)


def _drop_pipeline(row: Dict[str, Any]) -> Dict[str, Any]:
    """Delete the pipeline for a cleaner being removed. Never raises.

    Removing the folder without removing the definition leaves a pipeline pointing at
    a file that no longer exists. It fails on every trigger, and in the pipelines list
    it is indistinguishable from a cleaner that is broken.

    The record is cleared alongside, so pipeline_id never names a definition that has
    gone -- and so recreating it later, if the removal is abandoned, starts from
    nothing rather than adopting an id Azure DevOps no longer knows.
    """
    import ado_pipeline

    outcome = ado_pipeline.delete(
        row.get("pipeline_id"), name=str(row.get("pipeline_name") or ""),
    )
    if not outcome.get("ok"):
        log.warning("cleaner %s pipeline not deleted: %s", row.get("slug"), outcome.get("detail"))
        return outcome
    row["pipeline_id"] = None
    row["pipeline_url"] = ""
    try:
        execute(
            "UPDATE artifactory_cleaners SET pipeline_id = NULL, pipeline_url = '', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [row.get("id")],
        )
    except Exception as exc:
        log.warning("cleaner %s pipeline id not cleared: %s: %s",
                    row.get("slug"), type(exc).__name__, exc)
    return outcome


def drop_pipeline_for(slug: str) -> Dict[str, Any]:
    """Delete the pipeline of the cleaner occupying this folder. Never raises.

    Called when an approver APPROVES a removal, rather than when the pull request that
    removes the files is merged. Those are different moments and the earlier one is
    what was asked for: the decision has been taken, and leaving a pipeline running
    against a folder somebody has agreed to delete is a nightly delete job nobody
    intends to keep.

    It is put back if the removal is abandoned -- see _store_state -- so the two
    directions stay symmetric and an abandoned removal does not leave a cleaner that
    reads as running with nothing to run it.
    """
    row = find_by_slug(slug)
    if not row:
        return {"ok": True, "detail": "no record for that cleaner"}
    return _drop_pipeline(row)


# ── The cluster side of a removal ────────────────────────────────────────────

def _mark_cluster_pending(row: Dict[str, Any]) -> None:
    """The files are gone; the objects on the cluster are not. Record exactly that."""
    execute(
        "UPDATE artifactory_cleaners SET status = %s, cluster_state = %s, "
        "pending_delete = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        [REMOVED, CLUSTER_PENDING, row.get("id")],
    )
    row["status"] = REMOVED
    row["cluster_state"] = CLUSTER_PENDING
    row["pending_delete"] = False
    left = ", ".join(
        f"{o['kind']} {o['name']}" for o in cluster_objects(str(row.get("slug") or ""))
    )
    log.warning("cleaner %s removed from the repository; %s still on the cluster",
                row.get("slug"), left or "nothing recorded")


def cluster_objects(slug: str) -> List[Dict[str, str]]:
    import artifactory_cleaner

    return artifactory_cleaner.cluster_objects(slug)


def cluster_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """What to show about this cleaner's objects on the cluster.

    Present for a LIVE cleaner too, not only one being removed: the same two links
    answer "what is this thing actually doing" for somebody looking at a running
    cleaner, and a link that only appears at the end is a link nobody has learned to
    trust by then.
    """
    import artifactory_cleaner

    slug = str(row.get("slug") or "")
    if not slug:
        return {"state": "", "objects": [], "command": "", "namespace": ""}
    return {
        "state": str(row.get("cluster_state") or ""),
        # Whether these two objects exist yet. A link to a CronJob no build has ever
        # created is a link to a "not found" page, and asking somebody to confirm they
        # deleted it is asking them to confirm a fiction.
        "applied": bool(row.get("cluster_applied")),
        "run_state": str(row.get("first_run_state") or ""),
        "run_url": str(row.get("first_run_url") or ""),
        "objects": artifactory_cleaner.cluster_objects(slug),
        "command": artifactory_cleaner.cluster_command(slug),
        "namespace": artifactory_cleaner.namespace(),
        "cleared_by": str(row.get("cluster_cleared_by") or ""),
    }


def confirm_cluster_clear(slug: str, who: str) -> Dict[str, Any]:
    """An admin says the CronJob and ConfigMap are gone. This finishes the removal.

    The confirmation is taken at face value -- the portal cannot check, which is the
    reason this button exists at all -- so it records WHO said it. That is the only
    thing that makes an unverifiable claim worth anything later.

    The record is deleted here rather than at the merge, because until now it was the
    only thing holding the two links, and because the folder name it occupies must not
    be reusable while a CronJob still answers to it.
    """
    row = find_by_slug(slug)
    if not row:
        return {"ok": False, "detail": "There is no record of that cleaner."}
    if str(row.get("cluster_state") or "") != CLUSTER_PENDING:
        return {"ok": False,
                "detail": "That cleaner is not waiting for anything on the cluster."}

    execute(
        "UPDATE artifactory_cleaners SET cluster_state = %s, cluster_cleared_by = %s, "
        "cluster_cleared_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s",
        [CLUSTER_CLEARED, str(who or "")[:255], row.get("id")],
    )
    log.warning("cleaner %s confirmed cleared from the cluster by %s", slug, who or "?")

    import audit

    audit.log(
        audit.Action.ARTIFACTORY_CLEANER_CLUSTER_CLEARED,
        user_email=str(who or ""),
        metadata={
            "cleaner": row.get("cleaner_name"),
            "slug": slug,
            "objects": [o["name"] for o in cluster_objects(slug)],
            "namespace": row.get("namespace") or "",
        },
    )
    _tell_owner(row, "fully removed",
                "Its scheduled job and settings have been deleted from the cluster.")
    try:
        delete_record(row.get("id"))
    except Exception as exc:
        log.warning("cleaner %s record not deleted after clearing: %s: %s",
                    slug, type(exc).__name__, exc)
    return {"ok": True, "detail": f"'{row.get('cleaner_name')}' is fully removed."}


def _store_state(row: Dict[str, Any], mapped: str, state: Dict[str, Any]) -> None:
    """Write the new state onto the row and into the table, and tell the owner.

    The notification fires only on a CHANGE, and the change is decided by comparing
    with what is stored -- so refreshing the page ten times does not send ten
    notifications, and a state that was already known sends none.
    """
    # Merging a REMOVAL is the opposite of merging a change, and abandoning one is
    # too. Without this, deleting a cleaner and having the removal approved left a
    # record reading "Running" for a folder that is no longer in the repository, and
    # a rejected removal would have read the same.
    if row.get("pending_delete"):
        if mapped == RUNNING:
            # The pipeline goes BEFORE the record, because the record is where its id
            # is kept. Deleting the row first would leave a definition in Azure DevOps
            # that nothing in the portal knows how to find.
            try:
                _drop_pipeline(row)
            except Exception as exc:
                log.warning("cleaner %s pipeline delete raised: %s: %s",
                            row.get("slug"), type(exc).__name__, exc)
            # The record does NOT get deleted here, and that is the whole point of it.
            #
            # It used to. Merging the removal dropped the row, and with it went the
            # only thing that knew a CronJob named after this cleaner was still on the
            # cluster deleting artifacts every night. The portal forgot; the schedule
            # did not. Nobody was ever going to be told, because the only record that
            # could have told them had just been deleted for looking finished.
            #
            # So the row survives as REMOVED with its cluster side PENDING, holding
            # the two links somebody needs, until a person confirms the objects are
            # gone -- see confirm_cluster_clear, which is what finally deletes it.
            #
            # UNLESS nothing was ever applied. A cleaner whose pipeline never ran has
            # no CronJob and no ConfigMap -- the files described two objects that were
            # never created. Handing an operator links to them would send them to two
            # "not found" pages and then ask them to confirm they had deleted a
            # fiction, which teaches people to click the confirmation without looking.
            # That is a worse outcome than the one this whole mechanism prevents.
            if not row.get("cluster_applied"):
                try:
                    delete_record(row.get("id"))
                except Exception as exc:
                    log.warning("cleaner %s removal not recorded: %s: %s",
                                row.get("slug"), type(exc).__name__, exc)
                    return
                row["status"] = REMOVED
                log.warning("cleaner %s removed; it had never been applied, so there "
                            "is nothing on the cluster", row.get("slug"))
                _tell_owner(row, "removed",
                            "It had never been applied to the cluster, so nothing is "
                            "left behind.")
                return
            try:
                _mark_cluster_pending(row)
            except Exception as exc:
                log.warning("cleaner %s removal not recorded: %s: %s",
                            row.get("slug"), type(exc).__name__, exc)
                return
            _tell_owner(
                row, "removed from the repository",
                "The removal was merged and the pipeline is gone. Its scheduled job "
                "is still on the OpenShift cluster and has to be deleted there.",
            )
            return
        if mapped == ABANDONED:
            execute(
                "UPDATE artifactory_cleaners SET pending_delete = FALSE, status = %s, "
                "pr_state = %s, pr_checked_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                [RUNNING, state.get("state") or "", row.get("id")],
            )
            row["status"] = RUNNING
            row["pending_delete"] = False
            # The pipeline went at approval, so it has to come back now. Without this,
            # abandoning a removal leaves a cleaner reading "Running" with nothing to
            # run it -- the files are on the branch and no pipeline applies them, which
            # is the same silent half-state the pipeline step exists to prevent.
            try:
                _ensure_pipeline(row)
            except Exception as exc:
                log.warning("cleaner %s pipeline not restored: %s: %s",
                            row.get("slug"), type(exc).__name__, exc)
            _tell_owner(row, "still running",
                        "The removal was abandoned, so this cleaner is still scheduled.")
            return

    changed = mapped != str(row.get("status") or "")
    row["status"] = mapped
    row["pr_state"] = state.get("state") or ""
    row["pr_checked_at"] = datetime.now(timezone.utc)
    row.pop("pr_stale_reason", None)
    try:
        execute(
            """
            UPDATE artifactory_cleaners
               SET status = %s, pr_state = %s, pr_closed_at = NULLIF(%s, '')::timestamptz,
                   pr_checked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [mapped, state.get("state") or "", state.get("closed_at") or "", row.get("id")],
        )
    except Exception as exc:
        log.warning("cleaner %s state not stored: %s: %s",
                    row.get("slug"), type(exc).__name__, exc)
        return
    if not changed:
        return

    log.warning("cleaner %s pull request is now %s", row.get("slug"), mapped)
    # Merged means the files are on the default branch, which is the one moment the
    # pipeline can be created. Wrapped, because everything from here down is recording
    # a merge that has already happened and none of it may undo one.
    if mapped == RUNNING:
        try:
            _ensure_pipeline(row)
        except Exception as exc:
            log.warning("cleaner %s pipeline step raised: %s: %s",
                        row.get("slug"), type(exc).__name__, exc)
    label, _tone, detail = _LABEL.get(mapped, ("Updated", "pending", ""))
    owner = str(row.get("owner_email") or "").strip()
    if owner:
        from api.notifications import create_notification

        create_notification(
            user_email=owner,
            message=f"Cleaner '{row.get('cleaner_name')}': {label.lower()}. {detail}",
            notif_type="CLEANER_PR_STATE",
            related_id=str(row.get("request_id") or row.get("id")),
            link="/ui/automations",
            # One row per cleaner: a cleaner that goes open -> abandoned should leave
            # one line saying where it ended up, not a history of every step.
            group_key=f"cleaner:{row.get('id')}",
        )
    if mapped in (ABANDONED, RUNNING):
        import audit

        audit.log(
            audit.Action.ARTIFACTORY_CLEANER_PR_CLOSED,
            user_email=owner,
            metadata={
                "cleaner": row.get("cleaner_name"),
                "slug": row.get("slug"),
                "state": mapped,
                "pull_request": row.get("pull_request_url") or "",
            },
        )


# ── Saying it ────────────────────────────────────────────────────────────────

def _tell_owner(row: Dict[str, Any], what: str, detail: str) -> None:
    owner = str(row.get("owner_email") or "").strip()
    if not owner:
        return
    from api.notifications import create_notification

    create_notification(
        user_email=owner,
        message=f"Cleaner '{row.get('cleaner_name')}': {what}. {detail}",
        notif_type="CLEANER_PR_STATE",
        related_id=str(row.get("request_id") or row.get("id")),
        link="/ui/my-requests",
        group_key=f"cleaner:{row.get('id')}",
    )


def describe(row: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """One sentence about where this cleaner stands, in the same words everywhere.

    tone is "ok" / "pending" / "error" -- what the interface colours it by, chosen
    here rather than in three templates that would each pick their own.
    """
    if not row:
        return {"status": "", "label": "", "tone": "", "detail": ""}
    status = str(row.get("status") or PR_OPEN)
    label, tone, detail = _LABEL.get(status, (status.title(), "pending", ""))
    if row.get("pending_delete") and status == PR_OPEN:
        label, tone = "Removal waiting for review", "pending"
        detail = ("A pull request removing this cleaner is open. It keeps running "
                  "until that is merged.")
    # "Waiting for review" is a claim about a pull request, and a row with no pull
    # request id is not making that claim -- it is a row nothing can ever ask about,
    # frozen at the state it was written with. Saying so matters because every action
    # on a cleaner is decided by this status: called "waiting", it could not be
    # changed, could not be removed and could not be withdrawn, which is how one ended
    # up with no available action at all.
    if status == PR_OPEN and not row.get("pull_request_id"):
        # Worded for whichever of the two this is. Written as one branch it overwrote
        # the removal sentence above it, so a cleaner with an outstanding removal
        # reported that it might never have been applied -- which is the opposite of
        # what a removal means.
        if row.get("pending_delete"):
            label, tone = "Removal not confirmed", "pending"
            detail = (
                "No pull request was recorded for the removal, so the portal cannot "
                "tell whether one was ever opened. This cleaner is still scheduled."
            )
        else:
            label, tone = "Not confirmed", "pending"
            detail = (
                "No pull request was recorded for this cleaner, so the portal cannot "
                "tell whether it was ever applied. Submit it again to open a fresh "
                "one, or remove it."
            )
    if status == FAILED and row.get("last_error"):
        detail = str(row["last_error"])[:300]
    # A merged cleaner with no pipeline is not running. The files are in the
    # repository and nothing executes them, and calling that "Running" is the same
    # class of half-truth as calling an abandoned pull request "Completed".
    if status == RUNNING and row.get("pipeline_error"):
        label, tone = "Merged, not scheduled", "error"
        detail = (
            "The pull request was merged, but the pipeline that runs it could not be "
            f"created, so nothing is deleted yet. {str(row['pipeline_error'])[:300]}"
        )
    # Removed from git is not removed. The CronJob and the ConfigMap are on a cluster
    # this portal cannot reach, so the schedule keeps running until a person deletes
    # them -- and "Removed" on the card is exactly the sentence that stops anybody
    # going to look.
    if str(row.get("cluster_state") or "") == CLUSTER_PENDING:
        label, tone = "Removed, still on the cluster", "error"
        detail = (
            "The files and the pipeline are gone, but its scheduled job is still on "
            "the OpenShift cluster and still deleting on its schedule. Open the two "
            "links below, delete both, then confirm here."
        )
    # Merged, pipeline created, build still going: the CronJob does not exist yet.
    # Calling that "Running" is the same half-truth as calling an open pull request
    # merged, and it is the half-truth that made the removal links point at nothing.
    run_state = str(row.get("first_run_state") or "")
    if status == RUNNING and not row.get("pipeline_error"):
        if run_state in ("queued", "running"):
            label, tone = "Applying to the cluster", "pending"
            detail = ("The pull request was merged and the pipeline is running for the "
                      "first time. Its scheduled job exists once that finishes.")
        elif run_state == "failed":
            label, tone = "Merged, first run failed", "error"
            detail = ("The pull request was merged, but the run that creates the "
                      "scheduled job failed, so nothing is being deleted yet. Open the "
                      "run to see why, fix it, and run it again.")
    if row.get("pr_stale_reason"):
        detail = f"{detail} (Azure DevOps could not be reached just now, so this is the last known state.)"
    return {"status": status, "label": label, "tone": tone, "detail": detail}


def actions_for(row: Dict[str, Any]) -> Dict[str, bool]:
    """Which of the three things can be done to this cleaner right now.

    Decided here, once, and sent to the browser -- rather than the card working it out
    from the status and the endpoints working it out again from the same status. Those
    two rules were written separately and disagreed: the card offered nothing at all
    while a pull request was open, and the endpoints, asked, would have said the same,
    so a cleaner stuck in that state had no way forward from either side.

        change    re-submit the rules. Not while a pull request is open: a second one
                  would be changing the same files.
        remove    take it out. Not while a pull request is open either, because there
                  is nothing in the repository yet to remove.
        withdraw  abandon that pull request. Offered in exactly the case the other two
                  are not, so there is always something to do.
        clear     confirm the CronJob and ConfigMap have been deleted from the
                  cluster. The only action left on a removed cleaner, and the only
                  one the portal cannot perform for itself.
    """
    status = str((row or {}).get("status") or PR_OPEN)
    waiting = status == PR_OPEN and bool((row or {}).get("pull_request_id"))
    # A removed cleaner is not a cleaner any more: there are no files to change and
    # nothing to remove twice. Offering either would open a pull request against a
    # folder that is not in the repository.
    gone = status == REMOVED
    return {
        "change": not waiting and not gone,
        "remove": not waiting and not gone,
        "withdraw": waiting,
        "clear": str((row or {}).get("cluster_state") or "") == CLUSTER_PENDING,
    }


def public_view(row: Dict[str, Any], *, is_admin: bool) -> Dict[str, Any]:
    """The record as a requester or an admin is allowed to see it.

    The pull request link is an Azure DevOps URL that most requesters cannot open,
    and the branch, the repository and the commit are the reviewer's business. A
    requester gets the STATE -- which is the part that answers their question -- and
    the admin gets the link that lets them act on it.
    """
    out = dict(row)
    out["outcome"] = describe(row)
    out["actions"] = actions_for(row)
    # Shown to the requester as well as the admin. These are console links, not
    # credentials, and the requester is the person most likely to notice that the
    # cleaner they asked to have removed is still deleting things.
    out["cluster"] = cluster_view(row)
    if not is_admin:
        # The links stay; the confirmation does not. Whoever asked for the cleaner to
        # go is the last person who should be certifying that it went -- and the
        # endpoint refuses them anyway, so drawing the button would only produce a
        # 403 for pressing the one control the card offered.
        out["actions"] = dict(out["actions"], clear=False)
        for key in ("pull_request_url", "pull_request_id", "owner_email", "owner_id",
                    "last_error", "pr_state", "pipeline_url", "pipeline_id"):
            out.pop(key, None)
    return out


def reconcile(row: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Catch the record up to what Azure DevOps says, and describe the result.

    Called when the portal has just been proved wrong about a pull request -- it
    thought one was open and Azure DevOps refused to edit it because it is closed.
    Everything about applying that state already exists in _store_state, including the
    opposite meanings a merge has for a change and for a removal, so this only decides
    what to SAY.
    """
    mapped = _FROM_ADO.get(str(state.get("state") or "").lower())
    if not mapped:
        return "Azure DevOps did not say what became of that pull request."
    was_removal = bool(row.get("pending_delete"))
    _store_state(row, mapped, state)
    if was_removal:
        return (
            f"That removal was already merged in Azure DevOps, so '{row.get('cleaner_name')}' "
            "no longer exists. The portal has caught up."
            if mapped == RUNNING else
            f"That removal was already abandoned, so '{row.get('cleaner_name')}' is still "
            "scheduled. The portal has caught up."
        )
    return (
        f"That pull request was already merged, so '{row.get('cleaner_name')}' is applied. "
        "The portal has caught up."
        if mapped == RUNNING else
        f"That pull request was already abandoned, so nothing from it was applied. "
        "The portal has caught up — change it and submit it again, or remove it."
    )


def mark_abandoned(row: Dict[str, Any], why: str) -> None:
    """Record that this cleaner's pull request was abandoned from inside the portal.

    Written straight away rather than waiting for the next refresh to notice: the
    person who pressed the button is looking at the list, and a cleaner that still
    reads "waiting for review" after they withdrew it is the same stale-state problem
    in a new place.

    A withdrawn removal is not the same event as a withdrawn change -- one leaves the
    cleaner running, the other leaves it never applied -- so the pending flag decides
    which state it lands in.
    """
    landing = RUNNING if row.get("pending_delete") else ABANDONED
    execute(
        """
        UPDATE artifactory_cleaners
           SET status = %s, pr_state = 'abandoned', pending_delete = FALSE,
               pr_closed_at = CURRENT_TIMESTAMP, pr_checked_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [landing, row.get("id")],
    )
    row["status"] = landing
    row["pending_delete"] = False
    log.warning("cleaner %s withdrawn by %s -> %s", row.get("slug"), why, landing)
    _tell_owner(
        row, "withdrawn",
        "The removal was withdrawn, so this cleaner is still scheduled."
        if landing == RUNNING else
        "The pull request was withdrawn, so nothing was applied. Submit it again when "
        "you are ready.",
    )
