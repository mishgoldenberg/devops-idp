"""
Investigations: one question, several systems, one tool call.

A 27B model asked "why did my pipeline fail?" with only single-system tools has to find
the run, read the timeline, read the right log, pull the error out of it, search
Confluence with the right words, look for an existing bug -- five or six model calls,
each paid for against a per-minute limit, each a chance to wander off. So the chain
runs HERE, in code, and the model gets back one compact answer sheet to explain:

  investigate_pipeline_failure  the run -> failed steps -> the log's error lines -> the
                                error's signature -> Confluence pages about it (the best
                                one read) -> open bugs about it -> the package in
                                Artifactory / the quality gate in SonarQube when the
                                error points there -> when it last passed
  investigate_test_failures     failed tests -> were they failing before (new or flaky)
                                -> Confluence -> open bugs
  investigate_work_item         the item -> children -> its pull requests and their
                                builds and reviews -> SonarQube on the PR -> Confluence
  investigate_pull_request      reviews, policies, conflicts, build, quality gate,
                                linked work items: is it ready, and if not, what blocks it
  investigate_project_health    every pipeline's last run and failure rate, open bugs,
                                quality gates
  investigate_artifact          an artifact or image tag -> the run that built it -> its
                                commit and work items

Each step it takes is reported as it happens (ctx.progress), so the person watches it
move from system to system. A system that is not connected is skipped and SAID to be
skipped: "no Confluence page found" and "Confluence was not searched" are different
answers.

The result carries ``findings`` -- facts established by code, not left for the model to
infer -- and a ``note`` telling the model how to answer: quote the fix from a matching
Confluence page and link it, or say none matched and offer its own diagnosis, marked as
its own.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from . import ado, artifactory, confluence, proposals, sonar
from .base import NotConnected, Tool, ToolContext, ToolFailure, register

API = ado.API
FAILED_RESULTS = ("failed", "partiallysucceeded", "canceled")
LOG_TAIL_LINES = 400
MAX_ERROR_LINES = 24

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
_ERROR_LINE = re.compile(r"##\[error\]|\berror\b|\bexception\b|\bfatal\b|\bfailed\b|npm ERR!|Traceback|FAILURE", re.I)
_NOISE = re.compile(r"^\s*(##\[(section|command|group|endgroup|debug)\])|0 error\(s\)|errors?: 0\b", re.I)
_CODE = re.compile(r"\b(?:[A-Z]{1,4}\d{3,5}|TS\d{4}|E\d{4})\b")
_EXCEPTION = re.compile(r"\b[A-Z]\w+(?:Exception|Error)\b")


# ── reading a failure out of a log ───────────────────────────────────────────

def error_lines(text: str) -> List[str]:
    """The lines of a log that say what went wrong, with a little context before the first."""
    lines = [_TIMESTAMP.sub("", line).rstrip() for line in (text or "").splitlines()]
    hits: List[int] = [i for i, line in enumerate(lines) if _ERROR_LINE.search(line) and not _NOISE.search(line)]
    if not hits:
        return [line for line in lines[-12:] if line.strip()]
    keep: List[int] = []
    for n, i in enumerate(hits):
        start = max(0, i - 2) if n == 0 else i
        keep.extend(range(start, i + 1))
    out: List[str] = []
    for i in sorted(set(keep)):
        line = lines[i].strip()
        if line and line not in out:
            out.append(line[:400])
    return out[-MAX_ERROR_LINES:]


def signature(messages: List[str]) -> str:
    """The one line that best names the failure: an error code or an exception beats a
    generic 'exited with code 1', which is what every failure ends with."""
    scored: List[Tuple[int, str]] = []
    for message in messages:
        text = re.sub(r"##\[error\]", "", message).strip()
        if not text:
            continue
        score = 0
        if _CODE.search(text):
            score += 4
        if _EXCEPTION.search(text):
            score += 3
        if re.search(r"not found|unable to|could not|cannot|denied|refused|timed? ?out|missing", text, re.I):
            score += 2
        if re.search(r"exit code|exited with|completed with", text, re.I):
            score -= 3
        scored.append((score, text))
    if not scored:
        return ""
    best = max(scored, key=lambda s: s[0])[1]
    # A path in front of the message ("/src/App.csproj : error NU1101: ...") is noise
    # for searching and for reading.
    return re.sub(r"^\S*[/\\]\S*\s*:\s*", "", best)[:300]


def search_terms(sig: str) -> List[str]:
    """What to search Confluence and work items for, most specific first."""
    if not sig:
        return []
    text = re.sub(r"https?://\S+|\b[0-9a-f]{8,}\b|'[^']*[/\\][^']*'|\S*[/\\]\S+", " ", sig)
    codes = _CODE.findall(text)
    exceptions = _EXCEPTION.findall(text)
    names = [n for n in re.findall(r"\b[A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*)+\b", text)
             if not re.match(r"^\d", n) and len(n) > 4][:2]
    quoted = [q for q in re.findall(r"['\"]([^'\"]{3,60})['\"]", text) if "/" not in q][:1]
    terms: List[str] = []
    first = " ".join(dict.fromkeys(codes[:1] + names[:1] + quoted[:1] + exceptions[:1]))
    if first:
        terms.append(first)
    for single in codes[:1] + names[:1] + exceptions[:1]:
        if single not in terms:
            terms.append(single)
    if not terms:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", text)
                 if w.lower() not in ("error", "errors", "failed", "unable", "process", "completed", "with", "code", "exit")]
        if words:
            terms.append(" ".join(words[:5]))
    return terms[:3]


def missing_package(text: str) -> Optional[Dict[str, str]]:
    """A package or image the build could not get, when the error names one."""
    patterns = [
        ("nuget", r"Unable to find package ([\w.\-]+)(?:\.? No packages exist.*?| with version \(?[>=\s]*([\d][\w.\-]*))?"),
        ("maven", r"Could not (?:find|resolve) artifact [\w.\-]+:([\w.\-]+):(?:\w+:)?([\d][\w.\-]*)"),
        ("npm", r"No matching version found for ([@\w/.\-]+)@([\w.^~\-]+)"),
        ("npm", r"404\s+(?:Not Found)?\s*-?\s*GET\s+\S+/([@\w.\-%]+)\b"),
        ("docker", r"manifest for ([\w./\-]+):([\w.\-]+) not found"),
        ("docker", r"pull access denied for ([\w./\-]+)"),
    ]
    for kind, pattern in patterns:
        found = re.search(pattern, text or "", re.I)
        if found:
            # The sentence's own full stop is not part of the name: "package Contoso.Core. No packages..."
            groups = [g.rstrip(".") for g in found.groups() if g]
            return {"kind": kind, "name": groups[0], "version": groups[1] if len(groups) > 1 else ""}
    return None


def history_note(seen: int, checked: int) -> str:
    if not checked:
        return "no earlier results to compare"
    runs = "the previous run" if checked == 1 else f"the {checked} previous runs"
    if not seen:
        return f"new: passed in {runs}"
    return f"also failed in {runs}" if seen == checked else f"also failed in {seen} of {runs}"


# ── Azure DevOps lookups the investigations share ─────────────────────────────

def find_run(ctx: ToolContext, args: Dict[str, Any], *, want_failed: bool = True,
             need_tests: bool = False) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]:
    """The run the person means, the latest run of that pipeline, and a note.

    Given a run id, that run. Otherwise the newest run matching the project/pipeline
    (the person's own when nothing narrows it), preferring a failed one: "why did my
    pipeline fail" asked after a green re-run still means the red one.
    """
    run_id = str(args.get("run_id") or "").strip().lstrip("#")
    narrowed = bool(args.get("project") or args.get("pipeline"))
    ctx.progress("azure", "Finding the run" + (f" {run_id}" if run_id else ""))
    rows: List[Dict[str, Any]] = []
    for scope in (["mine", "all"] if not narrowed and not run_id else ["all"]):
        result = ado._ado().get_pipelines(project=args.get("project"), collection=args.get("collection"),
                                          scope=scope, current_user=ctx.user)
        rows = list(result.get("data") or [])
        if args.get("pipeline"):
            needle = str(args["pipeline"]).lower()
            rows = [r for r in rows if needle in str(r.get("name") or "").lower()]
        if rows:
            break
    if run_id:
        match = [r for r in rows if run_id in (str(r.get("id")), str(r.get("run_id")))]
        if not match:
            raise ToolFailure(f"Run {run_id} was not found among the recent runs your token can see"
                              + (f" in {args['project']}" if args.get("project") else "") + ".")
        return match[0], rows[0] if rows else None, ""
    if not rows:
        raise ToolFailure("No pipeline runs match that" + (f" in project {args['project']}" if args.get("project") else "") + ".")
    latest = rows[0]
    if not want_failed:
        return latest, latest, ""
    failed = [r for r in rows if str(r.get("result") or "") in FAILED_RESULTS]
    if need_tests:
        failed = failed or rows
    if not failed:
        return latest, latest, "None of the recent matching runs failed; this is the latest one."
    chosen = failed[0]
    note = ""
    if chosen is not latest:
        note = (f"The latest run ({latest.get('name')} {latest.get('run_id')}) is {latest.get('result') or latest.get('status')}; "
                f"this is the latest run that failed.")
    return chosen, latest, note


def run_base(ctx: ToolContext, client: httpx.Client, row: Dict[str, Any]) -> str:
    return ctx.ado_bases_for(client, str(row.get("collection") or ""))[0]


def run_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"pipeline": row.get("name"), "run": row.get("run_id"), "build_id": row.get("id"), "project": row.get("project"),
            "collection": row.get("collection"), "result": row.get("result") or row.get("status"),
            "branch": ado.short_branch(row.get("source_branch")), "requested_for": row.get("requested_for"),
            "finished": row.get("finished_date") or row.get("created_date"), "url": row.get("url")}


def failed_steps(client: httpx.Client, base: str, project: str, build_id: Any) -> List[Dict[str, Any]]:
    resp = client.get(f"{base}/{quote(project, safe='')}/_apis/build/builds/{build_id}/timeline", params=API)
    if resp.status_code != 200:
        return []
    records = resp.json().get("records") or []
    steps = []
    for r in sorted(records, key=lambda r: (r.get("order") or 0)):
        if str(r.get("result") or "") not in ("failed", "partiallySucceeded", "canceled"):
            continue
        if r.get("type") not in ("Task", "Job", "Stage", "Phase", "Checkpoint"):
            continue
        issues = [str(i.get("message") or "")[:400] for i in r.get("issues") or [] if i.get("type") == "error"]
        steps.append({"name": r.get("name"), "type": r.get("type"), "result": r.get("result"),
                      "errors": issues[:5], "log_id": (r.get("log") or {}).get("id")})
    # The task that failed says more than the job and stage around it.
    tasks = [s for s in steps if s["type"] == "Task"]
    return tasks or steps


def read_log(client: httpx.Client, base: str, project: str, build_id: Any, log_id: Any) -> str:
    """The END of a step's log, where the error is; a log can be megabytes long."""
    root = f"{base}/{quote(project, safe='')}/_apis/build/builds/{build_id}/logs"
    params: Dict[str, Any] = dict(API)
    listing = client.get(root, params=API)
    if listing.status_code == 200:
        for log in listing.json().get("value") or []:
            if str(log.get("id")) == str(log_id) and int(log.get("lineCount") or 0) > LOG_TAIL_LINES:
                params["startLine"] = int(log["lineCount"]) - LOG_TAIL_LINES
    resp = client.get(f"{root}/{log_id}", params=params, headers={"Accept": "text/plain"})
    if resp.status_code != 200:
        return ""
    text = resp.text
    if text.lstrip().startswith("{"):
        try:
            text = "\n".join(resp.json().get("value") or [])
        except ValueError:
            pass
    return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])


def failed_tests(client: httpx.Client, base: str, project: str, build_id: Any, top: int = 10) -> Dict[str, Any]:
    """Failed test results of one run, and the totals."""
    root = f"{base}/{quote(project, safe='')}/_apis/test"
    resp = client.get(f"{root}/runs", params={**API, "buildUri": f"vstfs:///Build/Build/{build_id}"})
    if resp.status_code != 200:
        return {"available": False}
    runs = resp.json().get("value") or []
    total = sum(int(r.get("totalTests") or 0) for r in runs)
    passed = sum(int(r.get("passedTests") or 0) for r in runs)
    tests: List[Dict[str, Any]] = []
    for run in runs:
        if int(run.get("totalTests") or 0) <= int(run.get("passedTests") or 0):
            continue
        got = client.get(f"{root}/Runs/{run.get('id')}/results", params={**API, "outcomes": "Failed", "$top": str(top)})
        if got.status_code != 200:
            continue
        for t in got.json().get("value") or []:
            if str(t.get("outcome") or "").lower() != "failed":
                continue
            tests.append({"name": t.get("automatedTestName") or t.get("testCaseTitle"),
                          "error": str(t.get("errorMessage") or "")[:300],
                          "stack": str(t.get("stackTrace") or "")[:240]})
    return {"available": bool(runs), "total": total, "passed": passed, "failed_count": len(tests), "failed": tests[:top]}


def open_bugs(ctx: ToolContext, client: httpx.Client, project: str, terms: List[str], collection: str = "") -> List[Dict[str, Any]]:
    words = [t for t in terms if t][:3]
    if not words:
        return []
    ctx.progress("azure", "Looking for open bugs about " + words[0])
    where = [f"[System.TeamProject] = {ado.literal(project)}", "[System.WorkItemType] = 'Bug'",
             "[System.State] NOT IN (" + ", ".join(ado.literal(s) for s in ado.CLOSED_STATES) + ")",
             "(" + " OR ".join(f"[System.Title] CONTAINS {ado.literal(w)}" for w in words) + ")"]
    try:
        return ado.wiql(ctx, client, where, 5, collection)
    except httpx.HTTPError:
        return []


def confluence_help(ctx: ToolContext, terms: List[str], read_best: bool = True) -> Dict[str, Any]:
    """Confluence pages about an error: searched most-specific first, the best one read."""
    if not ctx.systems.get("confluence"):
        return {"searched": False, "why": "Confluence is not connected for this person, so it was not searched."}
    tried: List[str] = []
    for term in terms:
        tried.append(term)
        ctx.progress("confluence", f"Searching Confluence for '{term}'")
        try:
            pages = confluence.search(ctx, term, limit=4)
        except (ToolFailure, NotConnected, httpx.HTTPError) as exc:
            return {"searched": False, "why": f"The Confluence search failed: {exc}"}
        if not pages:
            continue
        out: Dict[str, Any] = {"searched": True, "searched_for": tried,
                               "pages": [{k: p.get(k) for k in ("title", "space", "excerpt", "url")} for p in pages[:3]]}
        key = term.split()[0].lower()
        best = next((p for p in pages if key in (str(p.get("title")) + " " + str(p.get("excerpt"))).lower()), pages[0])
        if read_best:
            ctx.progress("confluence", f"Reading '{best.get('title')}'")
            try:
                page = confluence.read_page(ctx, str(best.get("id")), limit=1600)
                out["best_page"] = {"title": page["title"], "url": page["url"], "updated": page["updated"], "text": page["text"]}
            except (ToolFailure, httpx.HTTPError):
                pass
        return out
    return {"searched": True, "searched_for": tried, "pages": []}


def sonar_for(ctx: ToolContext, names: List[str], branch: str = "", pull_request: str = "") -> Optional[Dict[str, Any]]:
    """The SonarQube project that belongs to a pipeline or repository, found by name."""
    if not ctx.systems.get("sonarqube"):
        return None
    for name in names:
        guess = re.sub(r"[-_.](ci|cd|build|pipeline|release)$", "", str(name or ""), flags=re.I)
        if len(guess) < 3:
            continue
        ctx.progress("sonarqube", f"Looking for the SonarQube project of '{guess}'")
        try:
            found = sonar.find_projects(ctx, guess, 5)
        except (ToolFailure, httpx.HTTPError):
            return None
        exact = [p for p in found if guess.lower() in (p["key"].lower(), p["name"].lower())] or found[:1]
        if exact:
            try:
                return sonar.gate(ctx, exact[0]["key"], branch, pull_request)
            except ToolFailure:
                try:
                    return sonar.gate(ctx, exact[0]["key"])
                except (ToolFailure, httpx.HTTPError):
                    return None
    return None


def artifact_check(ctx: ToolContext, wanted: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Is the package the build could not get in Artifactory, and which versions are?"""
    if not ctx.systems.get("artifactory"):
        return {"checked": False, "why": "Artifactory is not connected for this person, so it was not checked."}
    ctx.progress("artifactory", f"Looking for {wanted['name']} in Artifactory")
    try:
        if wanted["kind"] == "docker":
            name = wanted["name"].split("/", 1)[-1] if "/" in wanted["name"] else wanted["name"]
            rows = artifactory.aql(ctx, f'items.find({{"name":"manifest.json","path":{{"$match":"*{artifactory.literal(name)}*"}}}})'
                                        '.include("repo","path","modified").sort({"$desc":["modified"]}).limit(15)')
            found = [f"{r.get('repo')}/{r.get('path')}" for r in rows]
        else:
            rows = artifactory.aql(ctx, f'items.find({{"name":{{"$match":"*{artifactory.literal(wanted["name"])}*"}},"type":"file"}})'
                                        '.include("repo","path","name","modified").sort({"$desc":["modified"]}).limit(15)')
            found = [f"{r.get('repo')}/{(r.get('path') or '').strip('.').strip('/')}/{r.get('name')}".replace("//", "/") for r in rows]
    except (ToolFailure, httpx.HTTPError) as exc:
        return {"checked": False, "why": f"The Artifactory search failed: {exc}"}
    version = wanted.get("version") or ""
    has_version = bool(version) and any(version in f for f in found)
    return {"checked": True, "looked_for": wanted, "found": found[:8],
            "requested_version_present": has_version if version else None}


def last_success(client: httpx.Client, base: str, project: str, definition_id: Any) -> Optional[Dict[str, Any]]:
    if not definition_id:
        return None
    resp = client.get(f"{base}/{quote(project, safe='')}/_apis/build/builds",
                      params={**API, "definitions": str(definition_id), "resultFilter": "succeeded", "$top": "1"})
    if resp.status_code != 200:
        return None
    rows = resp.json().get("value") or []
    if not rows:
        return None
    b = rows[0]
    return {"run": b.get("buildNumber"), "finished": b.get("finishTime"), "commit": str(b.get("sourceVersion") or "")[:10],
            "url": ado.run_url(base, project, b.get("id"))}


def _links(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group in groups:
        for link in group:
            if link and link.get("url") and all(l["url"] != link["url"] for l in out):
                out.append(link)
    return out


def _answer_note(help_: Dict[str, Any], findings: List[str]) -> str:
    if help_.get("best_page"):
        return ("A Confluence page matches this failure: explain the error, give the fix FROM that page (quote its "
                "commands) and link the page. Mention the other findings briefly.")
    if help_.get("searched"):
        return ("No Confluence page matched this failure. Say so, show the error lines, then suggest a fix yourself, "
                "clearly labelled as DevBot's own suggestion" + (", using the findings." if findings else "."))
    return ("Confluence was not searched (" + str(help_.get("why") or "") + "). Show the error lines and suggest a fix, "
            "labelled as DevBot's own suggestion.")


# ── investigations ───────────────────────────────────────────────────────────

def investigate_pipeline_failure(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    row, latest, note = find_run(ctx, args)
    run = run_summary(row)
    with ctx.ado_client(read=30.0) as client:
        base = run_base(ctx, client, row)
        project = str(row.get("project"))
        ctx.progress("azure", f"Reading the timeline of {run['pipeline']} {run['run']}")
        steps = failed_steps(client, base, project, row.get("id"))
        excerpt: Dict[str, Any] = {}
        messages: List[str] = [e for s in steps for e in s["errors"]]
        for step in steps[:2]:
            if not step.get("log_id"):
                continue
            ctx.progress("azure", f"Reading the log of '{step['name']}'")
            lines = error_lines(read_log(client, base, project, row.get("id"), step["log_id"]))
            if lines:
                excerpt = excerpt or {"step": step["name"], "lines": lines}
                messages += lines
                break
        sig = signature(messages)
        terms = search_terms(sig)
        findings: List[str] = []

        tests: Dict[str, Any] = {}
        if any(re.search(r"test", s["name"] or "", re.I) for s in steps):
            ctx.progress("azure", "Reading the failed tests")
            tests = failed_tests(client, base, project, row.get("id"), 5)
            if tests.get("failed_count"):
                findings.append(f"{tests['failed_count']} test(s) failed out of {tests.get('total')}.")

        extra: Dict[str, Any] = {}
        package = missing_package("\n".join(messages))
        if package:
            check = artifact_check(ctx, package)
            extra["artifactory"] = check
            if check and check.get("checked"):
                if check.get("requested_version_present") is False:
                    findings.append(f"{package['name']} {package['version']} is not in Artifactory; found: "
                                    + (", ".join(check["found"][:4]) or "nothing") + ".")
                elif not check.get("found"):
                    findings.append(f"Artifactory has nothing named {package['name']}.")
                else:
                    findings.append(f"Artifactory has: {', '.join(check['found'][:4])}.")
        if any(re.search(r"sonar|quality gate", (s["name"] or "") + " ".join(s["errors"]), re.I) for s in steps):
            gate = sonar_for(ctx, [run["pipeline"], project], branch=run["branch"])
            if gate:
                extra["sonarqube"] = gate
                failing = [c for c in gate["conditions"] if c["status"] == "ERROR"]
                if failing:
                    findings.append("Quality gate failed on " + "; ".join(f"{c['metric']} {c['actual']} ({c['threshold']})" for c in failing) + ".")

        help_ = confluence_help(ctx, terms)
        bugs = open_bugs(ctx, client, project, terms, str(row.get("collection") or ""))
        if bugs:
            findings.append(f"{len(bugs)} open bug(s) already mention this.")
        passed = last_success(client, base, project, row.get("definition_id"))
        if passed:
            findings.append(f"It last passed in run {passed['run']} ({str(passed['finished'])[:10]}, commit {passed['commit']}).")

    data = {
        "run": run,
        "which_run": note or None,
        "failed_steps": [{k: s[k] for k in ("name", "errors")} for s in steps[:4]],
        "log_excerpt": excerpt or None,
        "error_signature": sig or None,
        "tests": tests or None,
        "findings": findings,
        "confluence": help_,
        "open_bugs": [{k: b[k] for k in ("id", "title", "state", "assigned_to", "url")} for b in bugs],
        "last_success": passed,
        **extra,
    }
    pages = (help_.get("pages") or []) if isinstance(help_, dict) else []
    # The two obvious next steps, as buttons: taking one costs no model request.
    suggested = [proposals.rerun(run, suggested=True),
                 proposals.bug_for_failure(run, sig, excerpt or None, help_.get("best_page"))]
    if bugs:
        suggested = suggested[:1]
    return {
        "data": data,
        "actions": suggested,
        "note": _answer_note(help_, findings),
        "summary": (sig or "No error message found")[:140] + (" · fix found in Confluence" if help_.get("best_page") else ""),
        "links": _links([{"title": f"{run['pipeline']} {run['run']}", "url": run["url"], "system": "azure"}],
                        [{"title": p["title"], "url": p["url"], "system": "confluence"} for p in pages],
                        [{"title": f"Bug #{b['id']} {b['title']}", "url": b["url"], "system": "azure"} for b in bugs]),
    }


def investigate_test_failures(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    row, latest, note = find_run(ctx, args, need_tests=True)
    run = run_summary(row)
    with ctx.ado_client(read=30.0) as client:
        base = run_base(ctx, client, row)
        project = str(row.get("project"))
        ctx.progress("azure", f"Reading the test results of {run['pipeline']} {run['run']}")
        tests = failed_tests(client, base, project, row.get("id"), 10)
        if not tests.get("available"):
            raise ToolFailure(f"{run['pipeline']} {run['run']} published no test results to Azure DevOps, so there are "
                              "no test failures to read. (The pipeline needs a Publish Test Results step.)")
        history: Counter = Counter()
        checked = 0
        if row.get("definition_id") and tests.get("failed"):
            ctx.progress("azure", "Checking whether these tests failed in earlier runs")
            resp = client.get(f"{base}/{quote(project, safe='')}/_apis/build/builds",
                              params={**API, "definitions": str(row["definition_id"]), "$top": "6"})
            earlier = [b for b in (resp.json().get("value") or []) if b.get("id") != row.get("id")][:5] if resp.status_code == 200 else []
            for b in earlier:
                got = failed_tests(client, base, project, b.get("id"), 50)
                if got.get("available"):
                    checked += 1
                    history.update({t["name"] for t in got.get("failed") or []})
        for t in tests.get("failed") or []:
            t["earlier_runs"] = history_note(history.get(t["name"], 0), checked)
        first = (tests.get("failed") or [{}])[0]
        sig = signature([first.get("error", ""), first.get("name", "")])
        terms = [t for t in search_terms(sig) + [str(first.get("name") or "").rsplit(".", 1)[-1]] if t][:3]
        help_ = confluence_help(ctx, terms)
        bugs = open_bugs(ctx, client, project, terms, str(row.get("collection") or ""))
    findings = []
    new = [t["name"] for t in tests.get("failed") or [] if str(t.get("earlier_runs", "")).startswith("new")]
    repeat = [t["name"] for t in tests.get("failed") or [] if str(t.get("earlier_runs", "")).startswith("also")]
    if new:
        findings.append("New failures in this run: " + ", ".join(new[:4]) + ".")
    if repeat:
        findings.append("Failing before this run too (likely flaky or long-broken): " + ", ".join(repeat[:4]) + ".")
    data = {"run": run, "which_run": note or None, "tests": tests, "findings": findings, "confluence": help_,
            "open_bugs": [{k: b[k] for k in ("id", "title", "state", "url")} for b in bugs]}
    pages = help_.get("pages") or []
    suggested = [proposals.bug_for_tests(run, tests["failed"])] if tests.get("failed") and not bugs else []
    return {
        "data": data,
        "actions": suggested,
        "note": _answer_note(help_, findings),
        "summary": f"{tests.get('failed_count', 0)} failed test(s) in {run['pipeline']} {run['run']}",
        "links": _links([{"title": f"{run['pipeline']} {run['run']} tests", "url": (run["url"] or "") + "&view=ms.vss-test-web.build-test-results-tab", "system": "azure"}],
                        [{"title": p["title"], "url": p["url"], "system": "confluence"} for p in pages],
                        [{"title": f"Bug #{b['id']} {b['title']}", "url": b["url"], "system": "azure"} for b in bugs]),
    }


def pull_request_state(ctx: ToolContext, client: httpx.Client, base: str, pr: Dict[str, Any]) -> Dict[str, Any]:
    """Reviews, policies, conflicts, build and comments of one pull request."""
    repo = pr.get("repository") or {}
    project = (repo.get("project") or {}).get("name") or pr.get("project") or ""
    project_id = (repo.get("project") or {}).get("id") or ""
    pr_id = pr.get("pullRequestId")
    root = f"{base}/{quote(str(project), safe='')}/_apis/git/repositories/{quote(str(repo.get('id') or ''), safe='')}/pullrequests/{pr_id}"
    reviewers = [{"name": ado.who(r), "vote": {10: "approved", 5: "approved with suggestions", 0: "no vote",
                                               -5: "waiting for author", -10: "rejected"}.get(int(r.get("vote") or 0), r.get("vote")),
                  "required": bool(r.get("isRequired"))} for r in pr.get("reviewers") or []]
    policies = []
    build = None
    resp = client.get(f"{base}/{quote(str(project), safe='')}/_apis/policy/evaluations",
                      params={"api-version": "7.0-preview.1", "artifactId": f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}"})
    if resp.status_code == 200:
        for ev in resp.json().get("value") or []:
            conf = ev.get("configuration") or {}
            name = ((conf.get("type") or {}).get("displayName") or "policy") + (
                f" ({(conf.get('settings') or {}).get('displayName')})" if (conf.get("settings") or {}).get("displayName") else "")
            policies.append({"policy": name, "status": ev.get("status"), "blocking": bool(conf.get("isBlocking"))})
            ctx_ = ev.get("context") or {}
            if ctx_.get("buildId"):
                build = {"build_id": ctx_["buildId"], "pipeline": ctx_.get("buildDefinitionName"),
                         "url": ado.run_url(base, str(project), ctx_["buildId"])}
    threads = client.get(f"{root}/threads", params=API)
    active = 0
    if threads.status_code == 200:
        active = sum(1 for t in threads.json().get("value") or []
                     if t.get("status") == "active" and any(c.get("commentType") in (1, "text") for c in t.get("comments") or []))
    items = client.get(f"{root}/workitems", params=API)
    wi_ids = [int(w["id"]) for w in (items.json().get("value") or [])] if items.status_code == 200 else []
    blockers = [f"{p['policy']} is {p['status']}" for p in policies if p["blocking"] and p["status"] not in ("approved", "notApplicable")]
    if str(pr.get("mergeStatus") or "") == "conflicts":
        blockers.append("it has merge conflicts")
    if any(r["required"] and r["vote"] in ("no vote", "waiting for author", "rejected") for r in reviewers):
        blockers.append("a required reviewer has not approved")
    if any(r["vote"] == "rejected" for r in reviewers):
        blockers.append("a reviewer rejected it")
    if active:
        blockers.append(f"{active} comment thread(s) are still active")
    if pr.get("isDraft"):
        blockers.append("it is a draft")
    return {
        "id": pr_id, "title": pr.get("title"), "status": pr.get("status"), "created_by": ado.who(pr.get("createdBy")),
        "source": ado.short_branch(pr.get("sourceRefName")), "target": ado.short_branch(pr.get("targetRefName")),
        "repository": repo.get("name"), "project": project, "merge_status": pr.get("mergeStatus"),
        "reviewers": reviewers, "policies": policies, "active_threads": active, "build": build,
        "work_item_ids": wi_ids[:10], "ready": not blockers and pr.get("status") == "active", "blocked_by": blockers,
        "url": f"{base}/{quote(str(project), safe='')}/_git/{quote(str(repo.get('name') or ''), safe='')}/pullrequest/{pr_id}",
    }


def find_pull_request(ctx: ToolContext, client: httpx.Client, pr_id: int, collection: str = "") -> Tuple[str, Dict[str, Any]]:
    for base in ctx.ado_bases_for(client, collection):
        resp = client.get(f"{base}/_apis/git/pullrequests/{pr_id}", params=API)
        if resp.status_code == 200:
            return base, resp.json()
        if resp.status_code not in ado.NOT_MINE:
            resp.raise_for_status()
    raise ToolFailure(f"Pull request {pr_id} was not found in any collection your token can read.")


def investigate_pull_request(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client(read=30.0) as client:
        ctx.progress("azure", f"Reading pull request {args['pr_id']}")
        base, pr = find_pull_request(ctx, client, int(args["pr_id"]), args.get("collection", ""))
        ctx.progress("azure", "Checking its reviews, policies and build")
        state = pull_request_state(ctx, client, base, pr)
        linked = ado.work_item_rows(client, base, state["work_item_ids"]) if state["work_item_ids"] else []
        gate = sonar_for(ctx, [state["repository"]], pull_request=str(state["id"]))
        build_detail = None
        if state["build"]:
            ctx.progress("azure", f"Reading its build {state['build']['pipeline'] or ''}")
            resp = client.get(f"{base}/{quote(str(state['project']), safe='')}/_apis/build/builds/{state['build']['build_id']}", params=API)
            if resp.status_code == 200:
                b = resp.json()
                build_detail = {**state["build"], "run": b.get("buildNumber"), "result": b.get("result") or b.get("status")}
    if gate and gate.get("status") == "ERROR":
        state["blocked_by"].append("its SonarQube quality gate fails")
        state["ready"] = False
    findings = (["Ready to merge."] if state["ready"] else ["Not ready: " + "; ".join(state["blocked_by"]) + "."])
    if build_detail and build_detail.get("result") == "failed":
        findings.append(f"Its build {build_detail.get('pipeline')} {build_detail.get('run')} failed: "
                        "investigate_pipeline_failure with that run explains why.")
    data = {"pull_request": state, "build": build_detail, "sonarqube": gate, "findings": findings,
            "work_items": [{k: w[k] for k in ("id", "type", "title", "state", "url")} for w in linked]}
    return {
        "data": data,
        "note": "Say first whether it is ready to merge; then list exactly what blocks it, most blocking first.",
        "summary": f"PR {state['id']}: " + ("ready to merge" if state["ready"] else f"{len(state['blocked_by'])} thing(s) block it"),
        "links": _links([{"title": f"PR {state['id']}: {state['title']}", "url": state["url"], "system": "azure"}],
                        [{"title": f"{build_detail.get('pipeline')} {build_detail.get('run')}", "url": build_detail["url"], "system": "azure"}] if build_detail else [],
                        [{"title": "Quality gate", "url": gate["url"], "system": "sonarqube"}] if gate else [],
                        [{"title": f"#{w['id']} {w['title']}", "url": w["url"], "system": "azure"} for w in linked]),
    }


def investigate_work_item(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with ctx.ado_client(read=30.0) as client:
        ctx.progress("azure", f"Reading work item #{args['id']}")
        info = ado.describe_work_item(ctx, client, int(args["id"]), args.get("collection", ""))
        base = info.pop("base")
        item = info["item"]
        prs = []
        for link in info.get("pull_request_links")[:3]:
            ctx.progress("azure", f"Reading its pull request {link['id']}")
            try:
                _, pr = find_pull_request(ctx, client, int(link["id"]), item["collection"])
            except (ToolFailure, ValueError):
                continue
            state = pull_request_state(ctx, client, base, pr)
            prs.append({k: state[k] for k in ("id", "title", "status", "repository", "ready", "blocked_by", "build", "url")})
        open_children = [c for c in info["children"] if c.get("state") not in ado.CLOSED_STATES]
    help_ = confluence_help(ctx, [f"{item['id']}", " ".join(str(item["title"]).split()[:5])], read_best=False) \
        if ctx.systems.get("confluence") else {"searched": False, "why": "Confluence is not connected."}
    gate = sonar_for(ctx, [prs[0]["repository"]], pull_request=str(prs[0]["id"])) if prs else None
    findings = []
    if info["children"]:
        findings.append(f"{len(info['children']) - len(open_children)} of {len(info['children'])} child items are done.")
    for pr in prs:
        findings.append(f"PR {pr['id']} is {pr['status']}" + ("" if pr["ready"] else ": blocked by " + "; ".join(pr["blocked_by"])) + ".")
    if gate:
        findings.append(f"SonarQube on PR {prs[0]['id']}: {gate['status']}.")
    data = {**info, "pull_requests": prs, "sonarqube": gate, "findings": findings, "confluence": help_}
    pages = help_.get("pages") or []
    return {
        "data": data,
        "note": "Give its state in one line, then what is left to do (open children, PR blockers), then related pages.",
        "summary": f"#{item['id']} {item['title']} ({item['state']})",
        "links": _links([{"title": f"#{item['id']} {item['title']}", "url": item["url"], "system": "azure"}],
                        [{"title": f"PR {p['id']}: {p['title']}", "url": p["url"], "system": "azure"} for p in prs],
                        [{"title": p["title"], "url": p["url"], "system": "confluence"} for p in pages]),
    }


def investigate_project_health(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    project = args["project"]
    ctx.progress("azure", f"Reading the recent runs of {project}")
    result = ado._ado().get_pipelines(project=project, collection=args.get("collection"), scope="all", current_user=ctx.user)
    rows = list(result.get("data") or [])
    if not rows:
        raise ToolFailure(f"No pipeline runs found in project '{project}' (or the project does not exist).")
    pipelines: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        p = pipelines.setdefault(str(r.get("name")), {"pipeline": r.get("name"), "runs": 0, "failed": 0, "last": None})
        p["runs"] += 1
        p["failed"] += 1 if str(r.get("result")) in FAILED_RESULTS else 0
        if p["last"] is None:
            p["last"] = {"run": r.get("run_id"), "result": r.get("result") or r.get("status"),
                         "finished": r.get("finished_date"), "url": r.get("url")}
    with ctx.ado_client() as client:
        ctx.progress("azure", "Counting open bugs")
        where = [f"[System.TeamProject] = {ado.literal(project)}", "[System.WorkItemType] = 'Bug'",
                 "[System.State] NOT IN (" + ", ".join(ado.literal(s) for s in ado.CLOSED_STATES) + ")"]
        bugs = ado.wiql(ctx, client, where, 30, args.get("collection", ""), order="[Microsoft.VSTS.Common.Priority] ASC")
    by_priority = Counter(str(b.get("priority") or "?") for b in bugs)
    gates = []
    if ctx.systems.get("sonarqube"):
        ctx.progress("sonarqube", f"Checking the quality gates of {project}")
        try:
            for p in sonar.find_projects(ctx, project, 6):
                g = sonar.gate(ctx, p["key"])
                gates.append({"project": p["key"], "status": g["status"], "url": g["url"],
                              "failing": [c["metric"] for c in g["conditions"] if c["status"] == "ERROR"]})
        except (ToolFailure, httpx.HTTPError):
            pass
    red = [p for p in pipelines.values() if p["last"] and p["last"]["result"] in FAILED_RESULTS]
    findings = [f"{len(red)} of {len(pipelines)} pipelines are red on their last run." if red else "Every pipeline's last run passed.",
                f"{len(bugs)} open bug(s)" + (f", {by_priority.get('1', 0)} at priority 1." if bugs else ".")]
    findings += [f"Quality gate failing in {g['project']}: {', '.join(g['failing'])}." for g in gates if g["status"] == "ERROR"]
    data = {"project": project, "pipelines": sorted(pipelines.values(), key=lambda p: -p["failed"])[:15],
            "open_bugs": {"count": len(bugs), "by_priority": dict(by_priority),
                          "top": [{k: b[k] for k in ("id", "title", "priority", "assigned_to", "url")} for b in bugs[:5]]},
            "quality_gates": gates, "findings": findings}
    return {
        "data": data,
        "note": "Lead with the overall health in one sentence, then what needs attention first.",
        "summary": f"{project}: {len(red)} red pipeline(s), {len(bugs)} open bug(s)",
        "links": _links([{"title": f"{p['pipeline']} {p['last']['run']} ({p['last']['result']})", "url": p["last"]["url"], "system": "azure"} for p in red[:4]],
                        [{"title": f"{g['project']} quality gate", "url": g["url"], "system": "sonarqube"} for g in gates[:3]]),
    }


def investigate_artifact(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    repo = args["repo"]
    path = str(args.get("path") or "").strip("/")
    if not path and args.get("image") and args.get("tag"):
        path = f"{str(args['image']).strip('/')}/{args['tag']}/manifest.json"
    if not path:
        raise ToolFailure("Give the artifact's path, or a Docker image and tag.")
    ctx.progress("artifactory", f"Reading {repo}/{path}")
    info = artifactory.item_info(ctx, repo, path)
    props = info.get("properties") or {}
    build_name = str(props.get("build.name") or "")
    build_number = str(props.get("build.number") or "")
    revision = str(props.get("vcs.revision") or props.get("git.commit") or "")
    findings = []
    run = None
    items: List[Dict[str, Any]] = []
    if build_name and build_number and ctx.systems.get("azure"):
        ctx.progress("azure", f"Finding run {build_number} of {build_name}")
        try:
            result = ado._ado().get_pipelines(project=None, collection=None, scope="all", current_user=ctx.user)
            match = [r for r in result.get("data") or []
                     if str(r.get("run_id")) == build_number and build_name.lower() in str(r.get("name") or "").lower()]
        except Exception:
            match = []
        if match:
            run = run_summary(match[0])
            with ctx.ado_client() as client:
                base = run_base(ctx, client, match[0])
                ctx.progress("azure", "Reading the work items of that run")
                resp = client.get(f"{base}/{quote(str(run['project']), safe='')}/_apis/build/builds/{run['build_id']}/workitems",
                                  params={**API, "$top": "20"})
                if resp.status_code == 200:
                    items = ado.work_item_rows(client, base, [int(w["id"]) for w in resp.json().get("value") or []][:20])
            findings.append(f"Built by {run['pipeline']} run {run['run']} on {run['branch']} ({run['result']}).")
        else:
            findings.append(f"It says it was built by {build_name} {build_number}, but that run is not among the recent "
                            "runs your token can see.")
    elif not build_name:
        findings.append("It carries no build.name/build.number properties, so the run that built it cannot be traced. "
                        "(The pipeline would need to publish build info to Artifactory.)")
    data = {"artifact": info, "commit": revision or None, "run": run,
            "work_items": [{k: w[k] for k in ("id", "type", "title", "state", "url")} for w in items], "findings": findings}
    return {
        "data": data,
        "note": "Trace it in order: artifact -> run -> commit -> work items; say plainly where the trail stops.",
        "summary": f"{repo}/{path}" + (f" <- {run['pipeline']} {run['run']}" if run else ""),
        "links": _links([{"title": f"{repo}/{path}", "url": info["url"], "system": "artifactory"}],
                        [{"title": f"{run['pipeline']} {run['run']}", "url": run["url"], "system": "azure"}] if run else [],
                        [{"title": f"#{w['id']} {w['title']}", "url": w["url"], "system": "azure"} for w in items[:5]]),
    }


RUN = {"project": {"type": "string"}, "pipeline": {"type": "string", "description": "Part of the pipeline name"},
       "run_id": {"type": "string", "description": "Run number or build id; omit for the latest"},
       "collection": {"type": "string"}}

register(
    Tool("investigate_pipeline_failure", "azure", "investigate", "Investigating the pipeline failure",
         "Why a pipeline run failed, across systems: the failed step, the error lines from its log, Confluence pages "
         "with a fix (the best one read), open bugs about it, the missing package in Artifactory or the failing "
         "SonarQube gate when the error points there, and when it last passed. Omit everything for the user's latest "
         "failed run.",
         RUN, investigate_pipeline_failure, max_chars=5200, also=["confluence", "artifactory", "sonarqube"]),
    Tool("investigate_test_failures", "azure", "investigate", "Investigating the failed tests",
         "Failed tests of a run with their errors, whether each also failed in earlier runs (new or flaky), Confluence "
         "pages and open bugs about them. Omit everything for the user's latest run with failures.",
         RUN, investigate_test_failures, max_chars=4800, also=["confluence"]),
    Tool("investigate_work_item", "azure", "investigate", "Investigating the work item",
         "Everything about one work item: its state, open children, its pull requests and what blocks them, their "
         "build and SonarQube result, and related Confluence pages.",
         {"id": {"type": "integer"}, "collection": {"type": "string"}}, investigate_work_item, required=["id"],
         max_chars=5000, also=["confluence", "sonarqube"]),
    Tool("investigate_pull_request", "azure", "investigate", "Checking the pull request",
         "Is a pull request ready to merge, and if not what blocks it: reviews, required reviewers, policies, "
         "conflicts, comments, its build and its SonarQube quality gate, plus linked work items.",
         {"pr_id": {"type": "integer"}, "collection": {"type": "string"}}, investigate_pull_request,
         required=["pr_id"], max_chars=4200, also=["sonarqube"]),
    Tool("investigate_project_health", "azure", "investigate", "Checking the project's health",
         "Health of an Azure DevOps project: each pipeline's last run and failure rate, open bugs by priority, and "
         "the SonarQube quality gates of matching projects.",
         {"project": {"type": "string"}, "collection": {"type": "string"}}, investigate_project_health,
         required=["project"], max_chars=4200, also=["sonarqube"]),
    Tool("investigate_artifact", "artifactory", "investigate", "Tracing the artifact",
         "Where an artifact or Docker image tag came from: its build properties, the pipeline run that built it, the "
         "commit, and that run's work items.",
         {"repo": {"type": "string"}, "path": {"type": "string"}, "image": {"type": "string"}, "tag": {"type": "string"}},
         investigate_artifact, required=["repo"], max_chars=3600, also=["azure"]),
)
