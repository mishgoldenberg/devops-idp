"""
SonarQube, read with the person's token when they have one and anonymously when they
do not -- this instance lets anybody browse public projects, which is how its widgets
work without a token (api/integrations._optional_token).

Requests go through the Hub's _sonar_request, which tries Basic before Bearer: 9.x
silently ignores a Bearer header and answers as anonymous, so the order decides
whether private projects are visible at all.

A new-code number is read from the measure's ``period``, never its ``value`` -- they
are different fields, and the wrong one renders a clean zero for a project full of
new problems.
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote

from .base import Tool, ToolContext, ToolFailure, register

METRIC_NAMES = {
    "alert_status": "quality gate",
    "bugs": "bugs",
    "vulnerabilities": "vulnerabilities",
    "code_smells": "code smells",
    "security_hotspots": "security hotspots",
    "coverage": "coverage %",
    "duplicated_lines_density": "duplicated lines %",
    "ncloc": "lines of code",
    "reliability_rating": "reliability rating",
    "security_rating": "security rating",
    "sqale_rating": "maintainability rating",
    "security_review_rating": "security review rating",
    "new_bugs": "new bugs",
    "new_vulnerabilities": "new vulnerabilities",
    "new_code_smells": "new code smells",
    "new_coverage": "coverage on new code %",
    "new_duplicated_lines_density": "duplicated lines on new code %",
    "new_security_hotspots_reviewed": "hotspots reviewed on new code %",
    "new_reliability_rating": "reliability rating on new code",
    "new_security_rating": "security rating on new code",
    "new_maintainability_rating": "maintainability rating on new code",
}
RATING = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E", "1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}
COMPARATOR = {"GT": "above", "LT": "below", "EQ": "equal to", "NE": "not equal to"}


def _get(ctx: ToolContext, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    from api.integrations import _sonar_request

    base = ctx.base("sonarqube")
    resp = _sonar_request(f"{base}{path}", ctx.optional_token("sonarqube"), params=params, timeout=15.0)
    if resp.status_code == 404:
        raise ToolFailure("SonarQube does not know that project, branch or pull request.")
    if resp.status_code in (401, 403):
        raise ToolFailure("SonarQube refused the request: the project is private and your token cannot see it "
                          "(or no token is connected). Connect a SonarQube token on the Connections page.")
    if resp.status_code >= 400:
        try:
            errors = "; ".join(e.get("msg", "") for e in resp.json().get("errors") or [])
        except ValueError:
            errors = ""
        raise ToolFailure(f"SonarQube answered {resp.status_code}: {errors or 'no details'}")
    return resp.json()


def dashboard_url(ctx: ToolContext, key: str, branch: str = "", pull_request: str = "") -> str:
    url = f"{ctx.base('sonarqube')}/dashboard?id={quote(key, safe='')}"
    if pull_request:
        url += f"&pullRequest={quote(str(pull_request), safe='')}"
    elif branch:
        url += f"&branch={quote(branch, safe='')}"
    return url


def _scope(args: Dict[str, Any]) -> Dict[str, str]:
    if args.get("pull_request"):
        return {"pullRequest": str(args["pull_request"])}
    if args.get("branch"):
        return {"branch": args["branch"]}
    return {}


def _value(metric: str, raw: Any) -> Any:
    if raw is None:
        return None
    text = str(raw)
    if metric.endswith("_rating"):
        return RATING.get(text, text)
    return text


def gate(ctx: ToolContext, key: str, branch: str = "", pull_request: str = "") -> Dict[str, Any]:
    """The quality gate of one project (or branch, or pull request), failed conditions first."""
    params = {"projectKey": key, **_scope({"branch": branch, "pull_request": pull_request})}
    body = _get(ctx, "/api/qualitygates/project_status", params).get("projectStatus") or {}
    conditions = []
    for c in body.get("conditions") or []:
        metric = str(c.get("metricKey") or "")
        conditions.append({
            "metric": METRIC_NAMES.get(metric, metric),
            "status": c.get("status"),
            "actual": _value(metric, c.get("actualValue")),
            "threshold": f"{COMPARATOR.get(c.get('comparator'), c.get('comparator'))} {_value(metric, c.get('errorThreshold'))}",
            "on_new_code": metric.startswith("new_") or bool(c.get("periodIndex")),
        })
    conditions.sort(key=lambda c: c["status"] != "ERROR")
    return {"project": key, "status": body.get("status"), "conditions": conditions,
            "url": dashboard_url(ctx, key, branch, pull_request)}


def find_projects(ctx: ToolContext, query: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"ps": limit}
    if query:
        params["filter"] = f'query = "{query.replace(chr(34), "")}"'
    rows = _get(ctx, "/api/components/search_projects", params).get("components") or []
    return [{"key": r.get("key"), "name": r.get("name")} for r in rows if r.get("key")]


# ── tools ────────────────────────────────────────────────────────────────────

def sonar_projects(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rows = find_projects(ctx, args.get("query", ""), 40)
    if rows:
        keys = ",".join(r["key"] for r in rows)
        measures = _get(ctx, "/api/measures/search", {"projectKeys": keys, "metricKeys": "alert_status,coverage,bugs"})
        by_key: Dict[str, Dict[str, Any]] = {}
        for m in measures.get("measures") or []:
            by_key.setdefault(m.get("component"), {})[m.get("metric")] = m.get("value")
        for r in rows:
            got = by_key.get(r["key"], {})
            r["gate"] = got.get("alert_status") or "none"
            r["coverage"] = got.get("coverage")
            r["bugs"] = got.get("bugs")
            r["url"] = dashboard_url(ctx, r["key"])
    failing = [r for r in rows if r.get("gate") == "ERROR"]
    note = "" if ctx.optional_token("sonarqube") else "No SonarQube token is connected, so only public projects are listed."
    return {
        "data": {"projects": rows, "count": len(rows), "failing_gate": len(failing)},
        "note": note,
        "summary": f"{len(rows)} project{'s' if len(rows) != 1 else ''}" + (f", {len(failing)} failing the gate" if failing else ""),
        "links": [{"title": f"{r['name']} ({r['gate']})", "url": r["url"]} for r in (failing or rows)[:5]],
    }


def sonar_quality_gate(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    result = gate(ctx, args["project_key"], args.get("branch", ""), args.get("pull_request", ""))
    failed = [c for c in result["conditions"] if c["status"] == "ERROR"]
    return {
        "data": result,
        "summary": f"{args['project_key']}: gate {result['status']}" + (f", {len(failed)} condition{'s' if len(failed) != 1 else ''} failing" if failed else ""),
        "links": [{"title": f"{args['project_key']} quality gate", "url": result["url"]}],
    }


def sonar_issues(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    top = max(1, min(int(args.get("top") or 15), 40))
    params: Dict[str, Any] = {"componentKeys": args["project_key"], "resolved": "false", "ps": top,
                              "s": "SEVERITY", "asc": "false", "facets": "severities,types", **_scope(args)}
    if args.get("severities"):
        params["severities"] = ",".join(args["severities"]) if isinstance(args["severities"], list) else args["severities"]
    if args.get("types"):
        params["types"] = ",".join(args["types"]) if isinstance(args["types"], list) else args["types"]
    if args.get("new_code"):
        params["inNewCodePeriod"] = "true"
    body = _get(ctx, "/api/issues/search", params)
    base = ctx.base("sonarqube")
    issues = []
    for i in body.get("issues") or []:
        component = str(i.get("component") or "")
        issues.append({
            "severity": i.get("severity"), "type": i.get("type"), "message": i.get("message"),
            "file": component.split(":", 1)[-1], "line": i.get("line"), "rule": i.get("rule"),
            "created": str(i.get("creationDate") or "")[:10],
            "url": f"{base}/project/issues?id={quote(args['project_key'], safe='')}&open={quote(str(i.get('key') or ''), safe='')}",
        })
    facets = {f.get("property"): {v.get("val"): v.get("count") for v in f.get("values") or [] if v.get("count")}
              for f in body.get("facets") or []}
    total = (body.get("paging") or {}).get("total") or body.get("total") or len(issues)
    return {
        "data": {"project": args["project_key"], "total_open": total, "by_severity": facets.get("severities"),
                 "by_type": facets.get("types"), "issues": issues},
        "summary": f"{total} open issue{'s' if total != 1 else ''} in {args['project_key']}",
        "links": [{"title": f"{args['project_key']} issues",
                   "url": f"{base}/project/issues?id={quote(args['project_key'], safe='')}&resolved=false"}],
    }


MEASURES = ("alert_status,bugs,vulnerabilities,code_smells,security_hotspots,coverage,duplicated_lines_density,ncloc,"
            "reliability_rating,security_rating,sqale_rating,new_bugs,new_vulnerabilities,new_code_smells,new_coverage,"
            "new_duplicated_lines_density")


def measures(ctx: ToolContext, key: str, branch: str = "", pull_request: str = "") -> Dict[str, Any]:
    body = _get(ctx, "/api/measures/component",
                {"component": key, "metricKeys": MEASURES, "additionalFields": "period",
                 **_scope({"branch": branch, "pull_request": pull_request})})
    overall: Dict[str, Any] = {}
    new_code: Dict[str, Any] = {}
    for m in (body.get("component") or {}).get("measures") or []:
        metric = str(m.get("metric") or "")
        name = METRIC_NAMES.get(metric, metric)
        if metric.startswith("new_"):
            period = m.get("period") or (m.get("periods") or [{}])[0]
            new_code[name] = _value(metric, (period or {}).get("value"))
        else:
            overall[name] = _value(metric, m.get("value"))
    period = body.get("period") or (body.get("periods") or [{}])[0] or {}
    return {"project": key, "overall": overall, "new_code": new_code,
            "new_code_since": str(period.get("date") or "")[:10] or None, "url": dashboard_url(ctx, key, branch, pull_request)}


def sonar_measures(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    result = measures(ctx, args["project_key"], args.get("branch", ""), args.get("pull_request", ""))
    return {"data": result, "summary": f"Measures of {args['project_key']}",
            "links": [{"title": f"{args['project_key']} in SonarQube", "url": result["url"]}]}


KEY = {"type": "string", "description": "SonarQube project key (from sonar_projects)"}
BRANCH = {"type": "string", "description": "Branch name; omit for the main branch"}
PR = {"type": "string", "description": "Pull request id, for its analysis"}

register(
    Tool("sonar_projects", "sonarqube", "read", "Listing SonarQube projects",
         "SonarQube projects with their quality gate status. query matches the name or key.",
         {"query": {"type": "string"}}, sonar_projects),
    Tool("sonar_quality_gate", "sonarqube", "read", "Checking the quality gate",
         "Quality gate of a project, branch or pull request, with the failing conditions.",
         {"project_key": KEY, "branch": BRANCH, "pull_request": PR}, sonar_quality_gate, required=["project_key"]),
    Tool("sonar_issues", "sonarqube", "read", "Reading code issues",
         "Open issues of a project, most severe first. severities: BLOCKER,CRITICAL,MAJOR,MINOR,INFO. "
         "types: BUG,VULNERABILITY,CODE_SMELL.",
         {"project_key": KEY, "severities": {"type": "string"}, "types": {"type": "string"},
          "new_code": {"type": "boolean", "description": "Only issues in new code"}, "branch": BRANCH,
          "pull_request": PR, "top": {"type": "integer"}},
         sonar_issues, required=["project_key"]),
    Tool("sonar_measures", "sonarqube", "read", "Reading code measures",
         "Coverage, bugs, vulnerabilities, smells, duplication and ratings, overall and on new code.",
         {"project_key": KEY, "branch": BRANCH, "pull_request": PR}, sonar_measures, required=["project_key"]),
)
