"""
Changes DevBot can put in front of the person -- never make.

A proposal is a card on the page with a Review button. The button opens the same
dialog the widgets use (partials/components/ado-actions.html), filled with DevBot's
draft: the person can change any of it, the target system checks it first, the person
confirms, and the result is read back. Nothing here sends anything anywhere.

``kind`` and ``target`` are exactly what that dialog takes
(``window.portalAdo.open(kind, target)``); ``summary`` is the card's one line.
A proposal marked ``suggested`` came from an investigation rather than from the person
asking: the obvious next steps after a failure, offered as buttons so taking one costs
no model request at all.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

SYSTEM_OF_KIND = {
    "rerun": "azure", "vote": "azure", "pr-comment": "azure", "wi-state": "azure", "wi-comment": "azure",
    "wi-assign": "azure", "wi-description": "azure", "wi-create": "azure",
    "conf-create": "confluence", "conf-append": "confluence",
}


def action(kind: str, title: str, summary: str, target: Dict[str, Any], *, suggested: bool = False) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "system": SYSTEM_OF_KIND.get(kind, "azure"),
        "title": title,
        "summary": summary,
        "target": target,
        "suggested": suggested,
        "status": "proposed",
    }


def rerun(run: Dict[str, Any], *, suggested: bool = False) -> Dict[str, Any]:
    branch = str(run.get("branch") or "")
    return action(
        "rerun", "Run the pipeline again",
        f"A new run of {run.get('pipeline')} on {branch or 'its branch'}, with the same parameters as run {run.get('run')}.",
        {"collection": run.get("collection"), "project": run.get("project"), "id": run.get("build_id"),
         "name": run.get("pipeline"), "source_branch": branch, "run_id": run.get("run")},
        suggested=suggested,
    )


def bug_for_tests(run: Dict[str, Any], failed: List[Dict[str, Any]], *, suggested: bool = True) -> Dict[str, Any]:
    first = failed[0]
    name = str(first.get("name") or "a test").rsplit(".", 1)[-1]
    lines = [f"{len(failed)} test(s) failed in {run.get('pipeline')} run {run.get('run')} on {run.get('branch') or 'its branch'}:", ""]
    for t in failed[:6]:
        lines += [f"- {t.get('name')}: {t.get('error')}", f"  ({t.get('earlier_runs') or ''})"]
    lines += ["", f"Run: {run.get('url')}", "", "Found with DevBot in DevOps Hub."]
    title = f"Test {name} fails: {str(first.get('error') or '').strip()}"[:200]
    return action(
        "wi-create", "Open a bug about it",
        f"A Bug in {run.get('project')} titled “{title}”, listing the failed tests and their errors.",
        {"collection": run.get("collection"), "project": run.get("project"), "type": "Bug", "title": title,
         "description": "\n".join(lines)},
        suggested=suggested,
    )


def bug_for_failure(run: Dict[str, Any], sig: str, excerpt: Optional[Dict[str, Any]],
                    page: Optional[Dict[str, Any]], *, suggested: bool = True) -> Dict[str, Any]:
    """A bug that already says everything the investigation found."""
    lines: List[str] = [
        f"Pipeline {run.get('pipeline')} run {run.get('run')} failed on {run.get('branch') or 'its branch'}"
        + (f" in step \"{excerpt.get('step')}\"." if excerpt else "."),
        "",
        "Error:",
        sig or "(no error message was found in the log)",
    ]
    if excerpt and excerpt.get("lines"):
        lines += ["", "Log excerpt:"] + [str(l) for l in excerpt["lines"][-8:]]
    lines += ["", f"Run: {run.get('url')}"]
    if page:
        lines.append(f"Known fix: {page.get('title')} - {page.get('url')}")
    lines += ["", "Found with DevBot in DevOps Hub."]
    short = (sig or "failed").replace("\n", " ")
    title = f"{run.get('pipeline')} fails: {short}"[:200]
    return action(
        "wi-create", "Open a bug about it",
        f"A Bug in {run.get('project')} titled “{title}”, with the error, the log excerpt and the links.",
        {"collection": run.get("collection"), "project": run.get("project"), "type": "Bug", "title": title,
         "description": "\n".join(lines)},
        suggested=suggested,
    )
