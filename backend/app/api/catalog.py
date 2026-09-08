"""
The self-service catalog: one submit endpoint for every form the portal offers.

Forms are described in catalog_forms.py and rendered by one component in the browser.
This module is what happens when one is sent, and there are only two destinations:

  * a form with a request_type becomes an approval request, executed by
    api/approvals.py once a platform admin approves it (Artifactory quota, cleaner);

  * a form with a snow_item is submitted straight to ServiceNow as a REQUESTED ITEM
    and kept here as well (pipeline characterization).

Both are validated here, against the same spec the browser rendered, because the
browser decides what to SHOW and never what is required.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

import artifactory_admin
import artifactory_cleaner
import catalog_forms
import cleaner_store
import snow_catalog
from db import execute_returning, query_all
from request_types import CLEANER_REQUEST_TYPES
from security import AuthUser, get_current_user, has_effective_admin_access_live

log = logging.getLogger(__name__)
router = APIRouter()

# One attachment, and it has to fit in a request body that also carries the answers.
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class Attachment(BaseModel):
    field: str
    filename: str = ""
    content_type: str = ""
    data_base64: str = ""


class SubmitRequest(BaseModel):
    answers: Dict[str, Any] = {}
    attachments: List[Attachment] = []


# ── The forms themselves ─────────────────────────────────────────────────────

@router.get("/forms")
def list_forms(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    del current_user
    return {"success": True, "data": catalog_forms.all_forms()}


@router.get("/forms/{key}")
def get_form(key: str, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    del current_user
    spec = catalog_forms.form(key)
    if not spec:
        raise HTTPException(status_code=404, detail=f"There is no form called '{key}'.")
    return {"success": True, "data": spec}


# ── Option lists a form loads when it opens ──────────────────────────────────

def _identities(user: AuthUser) -> List[str]:
    """Every form the same person can appear under in Artifactory.

    A member is named by whichever identity their account was created with -- the
    username, the e-mail, or the part before the @. Trying one and stopping either
    finds nobody or matches the wrong subject, which is the same mistake the Azure
    DevOps grants made three rounds running.
    """
    email = str(user.get("email") or "").strip()
    return [x for x in (str(user.get("username") or "").strip(), email, email.split("@")[0]) if x]


@router.get("/options/{source}")
def options(
    source: str,
    project: str = "",
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Live option lists for select fields that cannot be a fixed list.

    Read with the Artifactory ADMIN account, not the caller's token: the caller is
    picking a project whose usage they are not allowed to read, and a picker that
    silently omits it is a form nobody can complete.

    An unconfigured or unreachable Artifactory answers with an empty list and a
    reason, rather than a 500 -- the form then says why the picker is empty instead of
    failing to open.
    """
    if source not in ("artifactory_my_projects", "artifactory_projects",
                      "artifactory_repositories", "azure_devops_projects"):
        raise HTTPException(status_code=404, detail=f"No option source '{source}'.")

    # Azure DevOps projects, for the Support ticket's collection-scoped picker. It
    # answers BEFORE the Artifactory guard below, because it has nothing to do with
    # Artifactory: an instance with no Artifactory admin credential would otherwise be
    # told its Azure DevOps list is empty because a JFrog token is missing.
    if source == "azure_devops_projects":
        try:
            import ado_repo

            names = ado_repo.projects_in_collection(project)
        except Exception as exc:  # noqa: BLE001 - a picker degrades, it does not 500
            log.warning("ADO project list unavailable: %s: %s", type(exc).__name__, exc)
            return {"success": True, "data": [],
                    "unavailable": "The project list could not be read. Type the name instead."}
        return {
            "success": True,
            "data": [{"value": n, "label": n} for n in names],
            "note": "" if names else
                    ("No projects came back for that collection. Type the name instead."),
        }

    if not artifactory_admin.is_configured():
        return {
            "success": True,
            "data": [],
            "unavailable": (
                "Artifactory admin access is not configured on the backend. Set "
                "ARTIFACTORY_ADMIN_USERNAME with ARTIFACTORY_ADMIN_TOKEN (an access "
                "token — the Projects API is served by JFrog Access, which does not "
                "accept a UI password), or ARTIFACTORY_ADMIN_PASSWORD."
            ),
        }

    note = ""
    empty_reason = ""
    try:
        if source in ("artifactory_my_projects", "artifactory_projects"):
            if source == "artifactory_my_projects":
                scoped = artifactory_admin.projects_for_user(_identities(current_user))
                projects, note = scoped["projects"], scoped["note"]
            else:
                projects = artifactory_admin.projects_with_usage()
            if not projects:
                # A 200 with nothing in it. The credential works and the picker is
                # still empty, which looks identical to a broken integration from the
                # form and is not one -- so it says which of the two it is.
                empty_reason = (
                    "Artifactory answered, but it has no projects this account can "
                    f"see (signed in as {artifactory_admin.account() or 'an unnamed account'}). "
                    "Either this instance defines no JFrog Projects, or that account "
                    "is not an administrator. You can still type the project key below."
                )
            data = [
                {
                    "value": proj["key"],
                    "label": f"{proj['name']} ({proj['key']})",
                    "meta": {
                        "used": artifactory_admin.format_bytes(proj["used_bytes"]),
                        "used_bytes": proj["used_bytes"],
                        "quota": "Unlimited" if proj["unlimited"] else artifactory_admin.format_bytes(proj["quota_bytes"]),
                        "quota_bytes": proj["quota_bytes"],
                        "free": None if proj["free_bytes"] is None else artifactory_admin.format_bytes(proj["free_bytes"]),
                        "free_bytes": proj["free_bytes"],
                        "unlimited": proj["unlimited"],
                        "percentage": proj["percentage"],
                    },
                }
                for proj in projects
            ]
        else:
            repos = artifactory_admin.repositories()
            total = len(repos)
            # Scoped to the chosen project when there is one. JFrog names a project's
            # repositories <project-key>-something, which is the only link between the
            # two -- and picking one repository out of two hundred is the part of this
            # form people get wrong, because the wrong key here is a nightly delete
            # against somebody else's artifacts.
            wanted = str(project or "").strip().lower()
            if wanted:
                scoped = [r for r in repos if r["key"].lower().startswith(f"{wanted}-")]
                if scoped:
                    repos = scoped
                    note = f"{len(scoped)} of {total} repositories belong to {project}."
                else:
                    note = (
                        f"No repository is named {project}-*, so all {total} are listed. "
                        "Check the key before submitting."
                    )
            data = [
                {"value": repo["key"], "label": repo["key"],
                 "meta": {"type": repo["type"], "package_type": repo["package_type"]}}
                for repo in repos
            ]
            if not data:
                empty_reason = (
                    "Artifactory answered, but listed no repositories for "
                    f"{artifactory_admin.account() or 'this account'}. "
                    "You can still type the repository key below."
                )
    except artifactory_admin.ArtifactoryProbeError as exc:
        # The status IS the message. "Artifactory did not answer" sent an operator to
        # check the network for what was a 403 on an admin-only endpoint.
        log.warning("catalog options %s failed: %s", source, exc)
        return {"success": True, "data": [], "unavailable": str(exc)}
    except Exception as exc:
        log.exception("catalog options %s failed", source)
        return {
            "success": True,
            "data": [],
            "unavailable": f"Artifactory could not be read: {type(exc).__name__}: {exc}",
        }

    return {"success": True, "data": data, "note": note, "unavailable": empty_reason}


@router.post("/cleaners/{cleaner_id}/remove")
def remove_cleaner(
    cleaner_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Get rid of a cleaner, by whichever route its current state allows.

    Deleting the portal's record is only the right answer when the repository has
    nothing in it to remove. A cleaner that was merged is a CronJob running in the
    cluster every night, and forgetting the row would hide it while it went on
    deleting artifacts -- so that one is a pull request, reviewed like every other
    change to those files.

        waiting for review  refused, and pointed at Withdraw -- otherwise two pull
                            requests touch the same folder
        running             a request that opens a pull request removing the folder
        abandoned / failed  nothing was ever applied, so the record simply goes
        not confirmed       no pull request was ever recorded, so there is nothing in
                            the repository this record corresponds to; it goes too
    """
    cleaner = cleaner_store.get(cleaner_id)
    if not cleaner:
        raise HTTPException(status_code=404, detail="That cleaner does not exist.")

    is_admin = has_effective_admin_access_live(current_user)
    if not is_admin and str(cleaner["owner_id"]) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="That cleaner belongs to someone else.")

    state = str(cleaner.get("status") or cleaner_store.PR_OPEN)
    if not cleaner_store.actions_for(cleaner)["remove"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{cleaner['cleaner_name']}' has a pull request waiting for review, "
                "so there is nothing in the repository to remove yet. Withdraw it "
                "first — that abandons the pull request and leaves this cleaner "
                "unapplied, and then it can be removed."
            ),
        )

    if state in (cleaner_store.ABANDONED, cleaner_store.FAILED) or not cleaner.get("pull_request_id"):
        cleaner_store.delete_record(cleaner_id)
        return {
            "success": True,
            "kind": "deleted",
            "data": {
                "message": (
                    f"'{cleaner['cleaner_name']}' was never applied, so it has just "
                    "been removed from the portal. Nothing changed in Artifactory or "
                    "in the repository."
                ),
            },
        }

    payload = {
        "cleaner_name": cleaner["cleaner_name"],
        "slug": cleaner["slug"],
        "repository": cleaner["repository"],
        **{k: v for k, v in (cleaner.get("rules") or {}).items()},
    }
    payload["cleaner_id"] = cleaner["id"]
    payload["requester_details"] = {}

    from .approvals import CreateApprovalRequest, create_request

    result = create_request(
        CreateApprovalRequest(
            request_type="ARTIFACTORY_CLEANER_DELETE",
            request_title=f"Remove Artifactory cleaner: {cleaner['cleaner_name']}",
            request_payload=payload,
        ),
        current_user,
    )
    return {"success": True, "kind": "approval", "data": result}


@router.post("/cleaners/{cleaner_id}/withdraw")
def withdraw_cleaner(
    cleaner_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Abandon the pull request waiting on this cleaner, from inside the portal.

    The action that was missing. A cleaner whose pull request is open cannot be
    changed (a second pull request would race the first) and cannot be removed (there
    is nothing applied to remove) -- both refusals are right, and together they left
    every waiting cleaner with no available action at all. The one thing that IS
    always legitimate is taking back an unreviewed change of your own, so that is what
    this does.

    Abandoning first and recording second: if Azure DevOps refuses, nothing is written
    here, because a record saying "abandoned" over a pull request that is still open
    is the stale state this whole feature exists to avoid.
    """
    import ado_repo

    cleaner = cleaner_store.get(cleaner_id)
    if not cleaner:
        raise HTTPException(status_code=404, detail="That cleaner does not exist.")
    if not has_effective_admin_access_live(current_user) and \
            str(cleaner["owner_id"]) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="That cleaner belongs to someone else.")
    if not cleaner_store.actions_for(cleaner)["withdraw"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{cleaner['cleaner_name']}' has no pull request waiting for review, "
                "so there is nothing to withdraw."
            ),
        )

    outcome = ado_repo.abandon_pull_request(cleaner.get("pull_request_id"))
    if outcome.get("already_closed"):
        # Somebody closed it in Azure DevOps and the portal had not caught up. The
        # request was "stop waiting on this pull request", and it is already not
        # waiting -- so read what actually happened to it and record THAT, rather than
        # showing the requester a TF401181 for a state they cannot do anything about.
        real = ado_repo.pull_request_one(cleaner.get("pull_request_id"))
        if real.get("ok"):
            settled = cleaner_store.reconcile(cleaner, real)
            return {
                "success": True,
                "data": {"message": settled},
            }
        raise HTTPException(
            status_code=502,
            detail=(
                "That pull request is already closed in Azure DevOps, but its state "
                f"could not be read back: {real.get('detail')}"
            ),
        )
    if not outcome.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"The pull request could not be abandoned: {outcome.get('detail')}",
        )
    cleaner_store.mark_abandoned(cleaner, str(current_user.get("username") or current_user["id"]))
    removal = bool(cleaner.get("pending_delete"))
    return {
        "success": True,
        "data": {
            "message": (
                f"The removal of '{cleaner['cleaner_name']}' was withdrawn. It is still "
                "scheduled and still deleting artifacts."
                if removal else
                f"The pull request for '{cleaner['cleaner_name']}' was abandoned. "
                "Nothing was applied — change it and submit it again, or remove it."
            ),
        },
    }


@router.post("/cleaners/{cleaner_id}/cluster-cleared")
def confirm_cluster_cleared(
    cleaner_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Record that the CronJob and ConfigMap have been deleted from the cluster.

    ADMIN ONLY, and not because the information is sensitive -- because this is an
    assertion the portal cannot check. There is a firewall between this cluster and
    the one the cleaner runs on, so nothing here can confirm the objects are gone; the
    button records that a named person says they are, and that name is the entire
    value of the record. Letting the requester press it would mean the person who
    wanted the cleaner gone is also the person certifying it is, with nobody able to
    tell the difference between a check and a click.

    This is the step that finishes a removal. Until it happens the cleaner keeps
    saying it is still on the cluster, which is true.
    """
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only a platform admin can confirm that the cleaner's job has been "
                "deleted from the OpenShift cluster."
            ),
        )
    cleaner = cleaner_store.get(cleaner_id)
    if not cleaner:
        raise HTTPException(status_code=404, detail="That cleaner does not exist.")

    outcome = cleaner_store.confirm_cluster_clear(
        str(cleaner.get("slug") or ""),
        str(current_user.get("username") or current_user.get("email") or current_user["id"]),
    )
    if not outcome.get("ok"):
        raise HTTPException(status_code=409, detail=outcome.get("detail") or "Nothing to confirm.")
    return {"success": True, "data": {"message": outcome.get("detail")}}


@router.get("/artifactory-diagnostics")
def artifactory_diagnostics(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Every Artifactory call this feature makes, with the status each one returned.

    Admin only, and read-only. The quota form failing looks identical whether the
    account is wrong, unprivileged, or pointed at a host that does not route the
    Projects API -- and each of those is somebody else's job to fix.
    """
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(status_code=403, detail="This view is for platform admins.")
    return {"success": True, "data": artifactory_admin.diagnose()}


@router.get("/servicenow-diagnostics")
def servicenow_diagnostics(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Can each catalog item this portal orders actually be found and ordered?

    Exists because the failure is silent by construction. The requested item is raised
    AFTER the work is done, and a failure there must not fail the work -- so a cleaner
    whose pull request opened perfectly can leave no ServiceNow record at all, and the
    only trace is a line in the result nobody reads. Every reason for it looks the
    same from outside: the item was renamed, the sys_id variable is unset, the service
    account cannot see the catalog, or the account cannot order.

    Read-only: it resolves each item and reads back the variables the item declares.
    Nothing is ordered.
    """
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(status_code=403, detail="This view is for platform admins.")

    import os

    items: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for spec in catalog_forms.all_forms():
        name = str(spec.get("snow_item") or "").strip()
        if not name:
            continue
        env_var = str(spec.get("snow_item_env") or "")
        key = f"{name}|{env_var}"
        if key in seen:
            # Two forms can order the same item; say so once, and say which forms.
            seen[key]["forms"].append(spec["key"])
            continue

        entry = {
            "forms": [spec["key"]],
            "item": name,
            "env_var": env_var,
            "env_set": bool((os.getenv(env_var) or "").strip()) if env_var else False,
            "sys_id": "",
            "ok": False,
            "detail": "",
            "variables": [],
        }
        try:
            entry["sys_id"] = snow_catalog.resolve_item(name, env_var)
            entry["ok"] = bool(entry["sys_id"])
        except Exception as exc:
            entry["detail"] = f"{type(exc).__name__}: {exc}"
        if entry["ok"]:
            try:
                specs = snow_catalog.item_variables(entry["sys_id"])
                entry["variables"] = [str(v.get("name") or "") for v in specs]
                # The question a 400 never answers: which MANDATORY variables would
                # arrive empty. Worked out by mapping the form's own field names the
                # way a real submission does, so what this reports is what would
                # actually be sent.
                offered = {f["key"]: "x" for f in catalog_forms.fields_of(spec)}
                offered.update({k: "x" for k in snow_catalog.variable_overrides(spec["key"])})
                unmapped: List[str] = []
                filled = snow_catalog.map_variables(specs, offered, dropped_into=unmapped)
                filled.update(snow_catalog.variable_overrides(spec["key"]))
                # The other half of the question, and the half that fails silently. A
                # mandatory variable with no answer is a 400 somebody sees; a form
                # field that maps to nothing is an answer that vanishes, and the only
                # sign of it is the item keeping its own default -- which reads as a
                # value somebody chose.
                entry["unmapped_fields"] = sorted(unmapped)
                # Exactly the routes a real order takes, in the same order, so this
                # cannot warn about a variable that would in fact go through. It did:
                # it reported the item's own mandatory choice variable as unfillable
                # while the ordering path was about to fill it from its first choice,
                # so the strip went on naming a problem that had been fixed.
                unfillable = [
                    f"{v['name']} ({v['label']})" for v in specs
                    if v.get("mandatory") and not filled.get(v["name"])
                    and not v.get("default") and not snow_catalog.fallback_for(v)
                ]
                entry["mandatory"] = [v["name"] for v in specs if v.get("mandatory")]
                entry["unfillable"] = unfillable
                if unfillable:
                    entry["ok"] = False
                    entry["detail"] = (
                        "requires " + ", ".join(unfillable) + " — each names another "
                        "record, so only a sys_id will do. Set "
                        f"SNOW_VARS_{spec['key'].upper()} to supply them, e.g. "
                        '{"' + unfillable[0].split(" ")[0] + '": "<sys_id>"}.'
                    )
            except Exception as exc:
                # Not readable is not OK. This left ok True, so the summary read "All
                # 1 catalog item(s) resolve and can be read" over an item whose
                # variables had just failed to read -- the single sentence an admin
                # relies on, saying the opposite of what happened.
                entry["ok"] = False
                entry["detail"] = f"variables unreadable: {type(exc).__name__}: {exc}"
        seen[key] = entry
        items.append(entry)

    # What actually happened the last few times, which beats any probe. Every attempt
    # already stores its own reason here; nothing has ever shown it, so a request that
    # quietly reached no queue looked exactly like one that did.
    #
    # Bounded two ways, because unbounded it becomes a permanent accusation. It was
    # reporting five failures from a form that no longer orders anything at all, in
    # wording from a version of the code that had since been replaced -- so the strip
    # went on describing a fixed problem with no way for anybody to make it stop.
    ordering_kinds = sorted(
        spec["key"] for spec in catalog_forms.all_forms() if spec.get("snow_item")
    )
    # And only failures NEWER than the last submission of that kind that worked. A
    # failure you have since fixed is not a current problem, and a strip that keeps
    # reciting one has no way of ever going quiet: the fortnight bound alone means
    # getting it right today still leaves the morning's failure on screen until the
    # calendar clears it.
    failures = query_all(
        """
        SELECT s.title, s.kind, s.snow_error, s.created_at,
               EXTRACT(DAY FROM NOW() - s.created_at)::int AS days_ago
          FROM catalog_submissions s
         WHERE s.snow_error IS NOT NULL AND s.snow_error <> ''
           AND s.created_at > NOW() - INTERVAL '14 days'
           AND s.kind = ANY(%s)
           AND s.created_at > COALESCE((
                 SELECT MAX(ok.created_at) FROM catalog_submissions ok
                  WHERE ok.kind = s.kind
                    AND ok.snow_number IS NOT NULL AND ok.snow_number <> ''
               ), '-infinity'::timestamptz)
         ORDER BY s.created_at DESC
         LIMIT 5
        """,
        [ordering_kinds],
    ) or []

    broken = [i for i in items if not i["ok"]]
    if not items:
        summary = "No form orders a ServiceNow catalog item."
    elif not broken:
        summary = f"All {len(items)} catalog item(s) resolve and can be read."
    else:
        summary = "; ".join(
            f"'{i['item']}' cannot be ordered: {i['detail']}"
            + (f" Set {i['env_var']} to its sys_id." if i["env_var"] and not i["env_set"] else "")
            for i in broken
        )
    lost = [
        f"{i['forms'][0]}: {', '.join(i['unmapped_fields'])}"
        for i in items if i.get("unmapped_fields")
    ]
    if lost:
        summary += (
            " Answers that reach no variable on the item and will be silently "
            f"discarded — {'; '.join(lost)}."
        )
    if failures:
        days = failures[0].get("days_ago")
        when = "today" if not days else f"{days} day(s) ago"
        summary += (
            f" {len(failures)} submission(s) in the last fortnight raised no item; the "
            f"most recent, {when}, said: {str(failures[0].get('snow_error') or '')[:200]}"
        )
    return {
        "success": True,
        "data": {
            "items": items,
            "recent_failures": failures,
            "summary": summary,
            "ok": not broken and not failures,
        },
    }


# ── Submitting ───────────────────────────────────────────────────────────────

def _decode(attachment: Attachment) -> bytes:
    raw = attachment.data_base64 or ""
    # A browser FileReader hands back a data: URL; keep only the payload.
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail=f"'{attachment.filename}' could not be read.")
    if len(blob) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"'{attachment.filename}' is larger than 5 MB.",
        )
    return blob


@router.post("/submit/{key}")
def submit(
    key: str,
    body: SubmitRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    spec = catalog_forms.form(key)
    if not spec:
        raise HTTPException(status_code=404, detail=f"There is no form called '{key}'.")

    # Defaults first, so what is stored, validated and sent is the form as the person
    # saw it -- including the toggles they never touched.
    answers = catalog_forms.apply_defaults(spec, dict(body.answers or {}))
    # A file field's value in the answers is only its name; the bytes travel
    # separately. Recording the name means the stored submission still says which
    # diagram it was even when the upload to ServiceNow failed.
    for attachment in body.attachments or []:
        if attachment.field:
            answers.setdefault(attachment.field, attachment.filename or "attachment")

    errors = catalog_forms.validate_answers(spec, answers)
    if errors:
        first = next(iter(errors.values()))
        raise HTTPException(status_code=400, detail=first)

    # Any failure below is reported WITH its reason. Letting it reach the app's
    # global handler turns every one of them into "Something went wrong. Please try
    # again.", which is a message nobody can act on and which sent a real submission
    # failure back as a mystery.
    try:
        if spec.get("request_type"):
            return _submit_as_approval(spec, answers, current_user)
        return _submit_to_servicenow(spec, answers, body.attachments or [], current_user)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("catalog submit %s failed", key)
        raise HTTPException(
            status_code=500,
            detail=f"The request could not be submitted: {type(exc).__name__}: {exc}",
        ) from exc


def _submit_as_approval(
    spec: Dict[str, Any], answers: Dict[str, Any], current_user: AuthUser
) -> Dict[str, Any]:
    """Queue it as an approval request, through the one path that already exists.

    Calling approvals.create_request rather than inserting a row: it owns the
    duplicate check, the audit entry, the notification and the executable-type gate,
    and a second insert here would quietly have none of them.
    """
    from .approvals import CreateApprovalRequest, create_request

    request_type = spec["request_type"]
    if request_type == "ARTIFACTORY_QUOTA_INCREASE":
        payload, title = _quota_payload(answers)
    elif request_type == "ARTIFACTORY_CLEANER_CREATE":
        payload, title = _cleaner_payload(answers)
    else:
        raise HTTPException(status_code=400, detail=f"'{request_type}' has no submission handler.")

    payload["requester_details"] = {
        field["key"]: answers.get(field["key"], "")
        for section in spec["sections"] if section.get("key") == "requester"
        for field in section.get("fields") or []
    }

    result = create_request(
        CreateApprovalRequest(request_type=request_type, request_title=title, request_payload=payload),
        current_user,
    )

    return {"success": True, "kind": "approval", "data": result}


def order_catalog_item(
    form_key: str,
    fields: Dict[str, Any],
    title: str,
    *,
    requester_id: str,
    requester_email: str = "",
) -> Dict[str, Any]:
    """Order the form's ServiceNow catalog item and keep a row for it.

    Called AFTER the work is done, not when the request is submitted, so the item can
    carry the outcome the team actually needs: the new quota, or the URL of the pull
    request they have to review. A requested item raised at submission would have to
    be updated afterwards, and updating a record on this instance is what trips the
    business rules that made the Support page's replies fail.

    Best-effort, and never silent: the work has already happened by the time this
    runs, so a ServiceNow outage must not fail the request -- but the error is stored
    and returned, because "the team has a ticket" and "the team has no ticket" are
    the two states an operator has to be able to tell apart.
    """
    spec = catalog_forms.form(form_key) or {}
    if not spec.get("snow_item"):
        # Not a failure, and it must not be reported as one. Only the pipeline
        # characterization reaches ServiceNow; a quota is set by this backend and a
        # cleaner becomes a pull request, so for those two there is no ticket to raise
        # and never was anything to go wrong. Returning an "error" here put a red
        # "No item was raised" line on requests that had completed perfectly.
        return {"number": "", "error": "", "skipped": True}

    payload = dict(fields)

    number = sys_id = error = ""
    try:
        ordered = snow_catalog.submit(
            spec["snow_item"], spec.get("snow_item_env") or "", payload,
            overrides=snow_catalog.variable_overrides(form_key),
            requester_email=str(requester_email or ""),
        )
        number = ordered.get("number") or ""
        sys_id = ordered.get("ritm_sys_id") or ordered.get("request_sys_id") or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("catalog item %r not ordered: %s", spec.get("snow_item"), error)

    try:
        execute_returning(
            """
            INSERT INTO catalog_submissions
                   (kind, requester_id, requester_email, title, answers, snow_number, snow_sys_id, snow_error)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id
            """,
            [
                form_key, str(requester_id), str(requester_email or ""),
                title, json.dumps(payload), number or None, sys_id or None, error or None,
            ],
        )
    except Exception as exc:
        log.warning("catalog submission row not stored for %s: %s", form_key, exc)

    return {"number": number, "error": error}


def _quota_payload(answers: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    project_key = str(answers.get("project_key") or "").strip()
    if not project_key:
        raise HTTPException(status_code=400, detail="Pick a project.")
    try:
        increase_bytes = artifactory_admin.parse_size(str(answers.get("increase_by") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A snapshot, clearly labelled as one. The executor reads the quota again at the
    # moment it writes, because between filling this in and an admin approving it the
    # project may have grown, been cleaned, or had its quota changed by somebody else.
    snapshot: Dict[str, Any] = {}
    try:
        project = artifactory_admin.project(project_key)
        if project:
            snapshot = {
                "used_bytes": project["used_bytes"],
                "quota_bytes": project["quota_bytes"],
                "free_bytes": project["free_bytes"],
                "unlimited": project["unlimited"],
            }
    except Exception as exc:
        log.warning("quota snapshot unavailable for %s: %s", project_key, exc)

    payload = {
        "project_key": project_key,
        "increase_by_bytes": increase_bytes,
        "increase_by": artifactory_admin.format_bytes(increase_bytes),
        "justification": str(answers.get("justification") or "").strip(),
        "snapshot_at_request": snapshot,
    }
    title = f"Artifactory quota: {project_key} +{payload['increase_by']}"
    return payload, title


def _cleaner_payload(answers: Dict[str, Any], *, allow_slug: str = "") -> tuple[Dict[str, Any], str]:
    try:
        request = artifactory_cleaner.validate(answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The name is not a label, it is the FOLDER. Everything about a cleaner is keyed
    # on the slug it produces -- the folder in the repository, the ConfigMap, the
    # CronJob and this portal's own record -- so two cleaners with the same name are
    # two pull requests fighting over one folder, and the record can only remember one
    # of them. Nothing further down catches it: the second pull request is a valid
    # "add" until the first is merged, so both open cleanly and the loser is silently
    # forgotten.
    _refuse_duplicate_cleaner(request["slug"], request["cleaner_name"], allow_slug=allow_slug)

    payload = dict(request)
    payload["summary"] = artifactory_cleaner.summary(request)
    payload["files"] = sorted(artifactory_cleaner.render_files(request))
    # Shown to the approver: another schedule already deleting from this repository is
    # legitimate and is also the thing most likely to delete something twice.
    payload["others_on_repository"] = [
        row["cleaner_name"] for row in cleaner_store.on_repository(request["repository"])
    ]
    return payload, f"Artifactory cleaner: {request['cleaner_name']}"


def _refuse_duplicate_cleaner(slug: str, name: str, *, allow_slug: str = "") -> None:
    """Refuse a name that is already taken, by a cleaner or by a request in flight.

    Two checks, because a name can be taken in two ways and only one of them is
    visible in the cleaners list:

      * a RECORD exists -- the cleaner is running, waiting for review, or was
        abandoned and can be re-submitted from its own card;
      * a REQUEST is in flight -- submitted twice in a row before either was approved,
        which leaves no record at all until the first one executes.
    """
    existing = None if slug == allow_slug else cleaner_store.find_by_slug(slug)
    if existing:
        state = cleaner_store.describe(existing)["label"].lower()
        # A cleaner that was removed but whose CronJob is still on the cluster holds
        # its folder name on purpose: the objects up there answer to names built from
        # this slug, so a new cleaner using it would apply itself over the one nobody
        # has deleted yet. Telling the requester to "change that one" would be advice
        # they cannot follow -- there is nothing left to change.
        if str(existing.get("cluster_state") or "") == cleaner_store.CLUSTER_PENDING:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{existing['cleaner_name']}' was removed, but its scheduled job "
                    f"is still on the OpenShift cluster, so the name '{slug}' is still "
                    "taken. Pick a different name, or ask a platform admin to delete "
                    "that job and confirm it in My Requests."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"There is already a cleaner called '{existing['cleaner_name']}' using "
                f"the folder '{slug}' ({state}). Pick a different name, or change that "
                "one from My Requests."
            ),
        )
    rows = query_all(
        """
        SELECT id, request_title, status FROM approval_requests
         WHERE request_type::text = ANY(%s)
           AND status IN ('PENDING', 'APPROVED', 'IN_PROGRESS')
           AND request_payload->>'slug' = %s
         LIMIT 1
        """,
        [sorted(CLEANER_REQUEST_TYPES), slug],
    ) or []
    if rows:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A request for a cleaner called '{name}' is already waiting "
                f"({str(rows[0].get('status') or '').lower()}). Wait for it to be "
                "decided rather than submitting a second one for the same folder."
            ),
        )


# ── What the approver sees ───────────────────────────────────────────────────

@router.get("/quota-preview")
def quota_preview(
    project: str = "",
    add: str = "",
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """The whole estate, for whoever is about to approve a quota increase.

    A requester is shown their own project and nothing else. An approver needs the
    question nobody can currently answer: every quota that has been promised, added
    up, against the disk it all has to fit on -- and where this request would put
    that total. Each project's quota looks reasonable on its own; the sum is the
    thing that overruns.

    Admin only, because it is the storage position of the entire instance.
    """
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(status_code=403, detail="This view is for platform admins.")
    if not artifactory_admin.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Artifactory admin access is not configured on the backend.",
        )

    try:
        rollup = artifactory_admin.quota_rollup()
        chosen = artifactory_admin.project(project) if project else None
    except Exception as exc:
        log.warning("quota preview failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"Artifactory did not answer ({type(exc).__name__}).")

    added = 0
    if add:
        try:
            added = artifactory_admin.parse_size(add)
        except ValueError:
            added = 0

    disk = rollup["disk_total_bytes"]
    committed = rollup["committed_bytes"]
    after = committed + added

    # "Over the disk" is two different situations and they need two different
    # sentences. An estate whose quotas already add up to more than the disk is
    # over-committed no matter what anyone approves today, and blaming a 10 GB
    # request for a 5 TB overage tells the approver to reject something that is not
    # the problem. The other case -- this request is what crosses the line -- is a
    # real objection to THIS request.
    already_over = disk > 0 and committed > disk
    tips_over = disk > 0 and not already_over and after > disk
    return {
        "success": True,
        "data": {
            **rollup,
            "requested_bytes": added,
            "requested": artifactory_admin.format_bytes(added) if added else "",
            "committed_after_bytes": after,
            "committed_after": artifactory_admin.format_bytes(after),
            "percentage_after": round(min(100.0, after / disk * 100), 1) if disk > 0 else 0.0,
            "over_disk": disk > 0 and after > disk,
            "already_over": already_over,
            "tips_over": tips_over,
            # How far past the disk the promises already go, and how much room is
            # left if they do not. Only one of these is ever meaningful.
            "overage_bytes": max(0, committed - disk) if disk > 0 else 0,
            "overage": artifactory_admin.format_bytes(max(0, committed - disk)) if already_over else "",
            "headroom_bytes": max(0, disk - after) if disk > 0 else 0,
            "headroom": artifactory_admin.format_bytes(max(0, disk - after)) if disk > 0 else "",
            "ratio": round(committed / disk, 2) if disk > 0 else 0.0,
            # Promised is a commitment; used is what is actually on the disk. An
            # over-committed estate with plenty of free space is a normal, survivable
            # position, and an approver cannot tell that from the promise alone.
            "used_percentage": round(min(100.0, rollup["used_bytes"] / disk * 100), 1) if disk > 0 else 0.0,
            "project": chosen,
        },
    }


# ── Cleaners the portal has created ──────────────────────────────────────────

@router.get("/cleaners")
def list_cleaners(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Your cleaners, or everybody's for a platform admin, each with what became of it.

    Filtered in the query. The pull request URL goes to admins only: it is an Azure
    DevOps link most requesters cannot open, and offering a link that 404s for the
    person clicking it is worse than not showing one. The STATE of that pull request
    goes to everybody, because "is my cleaner running or did somebody throw it away"
    is the requester's question, not the reviewer's.
    """
    is_admin = has_effective_admin_access_live(current_user)
    rows = cleaner_store.list_for(str(current_user["id"]), is_admin=is_admin)
    return {
        "success": True,
        "data": [cleaner_store.public_view(row, is_admin=is_admin) for row in rows],
        "is_admin": is_admin,
    }


class CleanerEdit(BaseModel):
    answers: Dict[str, Any] = {}


@router.post("/cleaners/{cleaner_id}/edit")
def edit_cleaner(
    cleaner_id: int,
    body: CleanerEdit,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Change a cleaner's rules or schedule: a new approval request, then a new PR.

    The slug is taken from the STORED record and never from the form, because it is
    the folder the files live in: letting a rename move it would open a pull request
    that adds a second cleaner rather than changing this one, and leave the original
    running.

    Which KIND of change it is depends on what became of the last pull request, and
    getting that wrong is not a cosmetic error -- Azure DevOps rejects an edit to a
    file that is not on the branch, and rejects an add of one that is:

      * merged        the files are on main, so this is an edit;
      * abandoned or
        never opened  nothing was ever applied, so this is a fresh create;
      * still open     there is already an unreviewed change to these same files, and
                      a second one would race it. Refused, with the state as the
                      reason rather than Azure DevOps' complaint about an object id.
    """
    cleaner = cleaner_store.get(cleaner_id)
    if not cleaner:
        raise HTTPException(status_code=404, detail="That cleaner does not exist.")

    is_admin = has_effective_admin_access_live(current_user)
    if not is_admin and str(cleaner["owner_id"]) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="That cleaner belongs to someone else.")

    state = str(cleaner.get("status") or cleaner_store.PR_OPEN)
    if not cleaner_store.actions_for(cleaner)["change"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The last change to '{cleaner['cleaner_name']}' is still waiting for "
                "review in Azure DevOps — two open pull requests would both be "
                "changing the same files. Withdraw it first, or have it merged."
            ),
        )
    # RUNNING means the files are on the base branch, so this is an edit. Everything
    # else -- abandoned, failed, or a row whose pull request was never recorded -- has
    # nothing on the branch, so it is a fresh create. Azure DevOps rejects each of
    # those two as the other.
    request_type = (
        "ARTIFACTORY_CLEANER_UPDATE" if state == cleaner_store.RUNNING
        else "ARTIFACTORY_CLEANER_CREATE"
    )

    spec = catalog_forms.form("artifactory_cleaner")
    answers = catalog_forms.apply_defaults(spec, dict(body.answers or {}))
    answers["cleaner_name"] = cleaner["cleaner_name"]
    answers["slug"] = cleaner["slug"]

    errors = catalog_forms.validate_answers(spec, answers)
    if errors:
        raise HTTPException(status_code=400, detail=next(iter(errors.values())))

    payload, _ = _cleaner_payload(answers, allow_slug=cleaner["slug"])
    payload["cleaner_id"] = cleaner["id"]
    payload["slug"] = cleaner["slug"]

    from .approvals import CreateApprovalRequest, create_request

    verb = "change" if request_type == "ARTIFACTORY_CLEANER_UPDATE" else "re-submission"
    result = create_request(
        CreateApprovalRequest(
            request_type=request_type,
            request_title=f"Artifactory cleaner {verb}: {cleaner['cleaner_name']}",
            request_payload=payload,
        ),
        current_user,
    )
    return {"success": True, "kind": "approval", "data": result}


def _submit_to_servicenow(
    spec: Dict[str, Any],
    answers: Dict[str, Any],
    attachments: List[Attachment],
    current_user: AuthUser,
) -> Dict[str, Any]:
    """Record it here, then order the catalog item.

    In that order, deliberately. The answers are the deliverable; if ServiceNow is
    unreachable the submission is still stored, still visible to the person who filed
    it, and carries the reason it never reached the queue -- instead of a 502 that
    loses twenty fields somebody just typed.
    """
    title = str(spec.get("title") or spec["key"])
    row = execute_returning(
        """
        INSERT INTO catalog_submissions (kind, requester_id, requester_email, title, answers)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id, created_at
        """,
        [
            spec["key"],
            str(current_user["id"]),
            str(current_user.get("email") or ""),
            title,
            json.dumps(answers),
        ],
    )
    if not row:
        raise HTTPException(status_code=500, detail="The submission could not be stored.")
    submission_id = row[0]["id"]

    number = ""
    sys_id = ""
    snow_error = ""
    unmapped_fields: List[str] = []
    try:
        result = snow_catalog.submit(
            spec["snow_item"], spec.get("snow_item_env") or "", answers,
            overrides=snow_catalog.variable_overrides(spec["key"]),
            requester_email=str(current_user.get("email") or current_user.get("username") or ""),
        )
        number = result.get("number") or ""
        sys_id = result.get("ritm_sys_id") or result.get("request_sys_id") or ""
        unmapped_fields = list(result.get("unmapped_fields") or [])
        target_table = "sc_req_item" if result.get("ritm_sys_id") else "sc_request"
        for attachment in attachments:
            if not attachment.data_base64 or not sys_id:
                continue
            snow_catalog.attach(
                sys_id, target_table,
                attachment.filename, attachment.content_type, _decode(attachment),
            )
    except HTTPException:
        raise
    except Exception as exc:
        snow_error = f"{type(exc).__name__}: {exc}"
        log.warning("catalog submission %s did not reach ServiceNow: %s", submission_id, snow_error)

    execute_returning(
        """
        UPDATE catalog_submissions
           SET snow_number = %s, snow_sys_id = %s, snow_error = %s
         WHERE id = %s
        RETURNING id
        """,
        [number or None, sys_id or None, snow_error or None, submission_id],
    )

    return {
        "success": True,
        "kind": "servicenow",
        "data": {
            "id": submission_id,
            "number": number,
            "snow_error": snow_error,
            "stored": True,
            # Answers this item has no variable for. Shown rather than logged: the
            # request looks completely successful otherwise, and the only way anybody
            # found out was opening the RITM and noticing a field still held the
            # item's default text.
            "unmapped_fields": unmapped_fields,
        },
    }


# ── Reading them back ────────────────────────────────────────────────────────

@router.get("/submissions")
def list_submissions(
    kind: Optional[str] = None,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Your own submissions, or everybody's for a platform admin.

    Filtered in the query, never in the template: this is the only place that decides
    whose answers a caller can read.
    """
    is_admin = has_effective_admin_access_live(current_user)
    clauses = []
    params: List[Any] = []
    if not is_admin:
        clauses.append("requester_id = %s")
        params.append(str(current_user["id"]))
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = query_all(
        f"""
        SELECT id, kind, requester_email, title, answers, snow_number, snow_error, created_at
          FROM catalog_submissions
          {where}
         ORDER BY created_at DESC
         LIMIT 200
        """,
        params,
    )
    return {"success": True, "data": rows or [], "is_admin": is_admin}
