"""Everything the portal asks SonarQube, in one place.

WHY THIS IS A MODULE AND NOT FIVE ENDPOINTS
-------------------------------------------
Five dashboard widgets want SonarQube. Written the obvious way that is five
project listings and five rounds of measure lookups every time somebody opens the
home page, against a server that is now actually reachable and therefore actually
costs time. Four of the five want the same thing: every project, with its quality
gate and its numbers. So that is read ONCE, cached, and the widgets are views over
it -- gates, new code, hotspots and the project list differ in what they show, not
in what they fetch.

TWO THINGS ABOUT SONARQUBE 9.9 THAT SHAPE THIS FILE
---------------------------------------------------
``Authorization: Bearer`` arrived in SonarQube 10. On 9.x it is worse than
unsupported: BasicAuthentication only reads a header that starts with "Basic", and
an unrecognised one is not an error -- it is ignored, and the request is served
ANONYMOUSLY. So a Bearer attempt against 9.9 returns HTTP 200 carrying whatever an
anonymous user may see, and any code that treats "not 401" as "this scheme works"
concludes Bearer is correct and keeps using it. On an instance with anonymous
Browse that looks like success while silently dropping every private project the
token existed to reach.

Bearer is therefore not attempted at all. The token goes where 9.x reads it: as the
HTTP Basic username with an empty password.

``api/projects/search`` needs Administer System. It is the natural-looking way to
list projects and it answers 403 for everybody who is not an administrator, which
on this instance is nearly everybody. ``api/components/search_projects`` needs only
Browse and is the one that works.

ANONYMOUS READS ARE A FEATURE HERE
----------------------------------
Where an instance grants Browse to anonymous users, every read below works with no
token at all, so the widgets have something to show before anybody connects
anything. A token is still used when the user has one -- it is what makes a
private project visible, and what makes ``author`` searches match their own
commits. ``_request`` therefore takes an OPTIONAL token, and "no token" is a
normal path rather than an error.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from resilient_http import tls_verify

_log = logging.getLogger(__name__)

# api/measures/search refuses more than 100 component keys in one call. Fifty,
# not a hundred: the keys go into a GET query string, and a hundred `group:artifact`
# keys is a 6KB request line before the metric list -- at or over what an
# intermediate proxy will carry.
_MEASURE_CHUNK = 50
# How many of those calls run at once. The crawl used to make them one after
# another, so 800 projects meant sixteen server round trips back to back -- longer
# than the widget's own 20-second deadline. Six keeps a busy SonarQube comfortable.
_PARALLEL = 6
# Measures are reused until the project is analysed again (see _measures). The cap
# bounds memory on an instance far bigger than this one; the age bounds how long a
# change nobody could have predicted -- a gate edited by an admin -- stays unseen.
_MEASURE_CACHE_MAX = 5000
_MEASURE_MAX_AGE_S = 6 * 3600
# A project the listing gave no analysis date for has nothing to compare against.
_UNDATED_MAX_AGE_S = 60
# api/components/search_projects pages at 500.
_PAGE_SIZE = 500
# Enough pages to cover any realistic instance without looping forever on a
# server that keeps claiming there is more.
_MAX_PAGES = 20

# What one snapshot reads. Overall and new-code metrics travel together because
# they cost the same single call, and three widgets divide them up afterwards.
#
# EVERY KEY HERE MUST EXIST ON 9.9. measures/search answers 404 for the whole call
# when one metric key is unknown -- it does not skip it -- so a key from a newer
# version empties every SonarQube widget at once. The test metrics are core
# metrics and simply come back absent for a project that imports no test report.
SNAPSHOT_METRICS: Tuple[str, ...] = (
    "alert_status",
    "quality_gate_details",
    "bugs",
    "vulnerabilities",
    "code_smells",
    "security_hotspots",
    "security_hotspots_reviewed",
    "coverage",
    "duplicated_lines_density",
    "ncloc",
    "sqale_index",
    "reliability_rating",
    "security_rating",
    "security_review_rating",
    "sqale_rating",
    "tests",
    "test_failures",
    "test_errors",
    "skipped_tests",
    "new_violations",
    "new_bugs",
    "new_vulnerabilities",
    "new_code_smells",
    "new_security_hotspots",
    "new_coverage",
    "new_duplicated_lines_density",
    "new_lines",
)

# What a pull request's own analysis is read with. `coverage` and
# `duplicated_lines_density` on a pull request are the branch as it would be after
# the merge, which is what SonarQube's own decoration calls "estimated after merge".
PULL_REQUEST_METRICS: Tuple[str, ...] = (
    "new_bugs",
    "new_vulnerabilities",
    "new_code_smells",
    "new_security_hotspots",
    "new_coverage",
    "new_duplicated_lines_density",
    "new_lines",
    "new_lines_to_cover",
    "coverage",
    "duplicated_lines_density",
    "bugs",
    "vulnerabilities",
    "code_smells",
    "security_hotspots",
    "tests",
    "test_failures",
    "test_errors",
    "skipped_tests",
)

# The set round 118 shipped and proved against this server. If the server rejects
# the fuller set above -- one key it does not know is a 404 for the WHOLE call --
# the snapshot falls back to this rather than leaving four widgets with an error.
_PROVEN_METRICS: Tuple[str, ...] = (
    "alert_status", "quality_gate_details", "bugs", "vulnerabilities", "code_smells",
    "security_hotspots", "security_hotspots_reviewed", "coverage",
    "duplicated_lines_density", "ncloc", "reliability_rating", "security_rating",
    "sqale_rating", "new_violations", "new_bugs", "new_vulnerabilities",
    "new_security_hotspots", "new_coverage", "new_duplicated_lines_density", "new_lines",
)

# Which of those are "new code" measures. SonarQube returns these under a period
# rather than as a plain value, and the two are not interchangeable.
_NEW_CODE_METRICS = frozenset(m for m in SNAPSHOT_METRICS if m.startswith("new_"))

_RATING_LETTER = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E",
                  "1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}

# The auth scheme this server accepted, remembered per base URL. See the module
# docstring: on 9.x every Bearer attempt is a wasted round trip.
_scheme_lock = threading.Lock()
_scheme_by_base: Dict[str, str] = {}


class SonarUnavailable(RuntimeError):
    """The server could not be reached or would not answer."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        # The transport error underneath, kept because the portal classifies a
        # failure by its TYPE (refused / DNS / TLS / timeout) to decide what to tell
        # the user. Wrapping without this turns every one of them into "unknown".
        self.cause = cause


def _auth_kwargs(scheme: str, token: str) -> Dict[str, Any]:
    if not token or scheme == "anonymous":
        return {"headers": {"Accept": "application/json"}}
    if scheme == "bearer":
        return {"headers": {"Authorization": f"Bearer {token}", "Accept": "application/json"}}
    return {"auth": (token, ""), "headers": {"Accept": "application/json"}}


def _schemes_to_try(base: str, token: str) -> List[str]:
    if not token:
        return ["anonymous"]
    # NO BEARER. See the module docstring: 9.x ignores the header and answers 200
    # anonymously, so trying it cannot fail loudly -- it can only succeed wrongly.
    # Anonymous stays as the last resort so a dead token still shows public projects
    # rather than an error, but it is never REMEMBERED as the working scheme.
    return ["basic", "anonymous"]


def answered_scheme(base: str) -> str:
    """Which scheme this server last ACCEPTED, or "" if only anonymous ever has.

    The difference between "a token is stored" and "the token is being used" is
    invisible everywhere else: with anonymous Browse granted, a dead token still
    returns a full-looking project list, just a smaller one.
    """
    with _scheme_lock:
        return _scheme_by_base.get(base, "")


def _remember(base: str, scheme: str) -> None:
    if scheme == "anonymous":
        return
    with _scheme_lock:
        if _scheme_by_base.get(base) != scheme:
            _scheme_by_base[base] = scheme
            _log.info("SonarQube at %s accepts %s auth", base, scheme)


_client_lock = threading.Lock()
_clients: Dict[Tuple[str, bool], httpx.Client] = {}


def _client(base: str) -> httpx.Client:
    """One pooled client per server, shared by every caller on this pod.

    A client per call paid a new TCP and TLS handshake for every page of every
    crawl; a pooled one keeps the connection and pays it once.

    COOKIES ARE REFUSED. This client is shared between USERS, and a cookie one
    person's request was answered with must never ride along on somebody else's.
    Credentials travel per request (_auth_kwargs), never on the client.
    """
    verify = tls_verify()
    key = (base.rstrip("/"), bool(verify))
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            client = httpx.Client(
                verify=verify,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
                limits=httpx.Limits(
                    max_connections=_PARALLEL * 3,
                    max_keepalive_connections=_PARALLEL,
                    keepalive_expiry=60.0,
                ),
            )
            _clients[key] = client
        return client


def _request(
    base: str,
    token: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """One GET, returning parsed JSON, or raising SonarUnavailable with a reason."""
    url = f"{base.rstrip('/')}{path}"
    last: Optional[httpx.Response] = None
    for scheme in _schemes_to_try(base, token):
        try:
            response = _client(base).get(
                url, params=params, timeout=httpx.Timeout(timeout, connect=5.0),
                **_auth_kwargs(scheme, token),
            )
        except Exception as exc:
            raise SonarUnavailable(f"{type(exc).__name__}: {exc}", cause=exc) from exc
        last = response
        if response.status_code in {401, 403}:
            continue
        if response.status_code >= 400:
            raise SonarUnavailable(
                f"SonarQube answered HTTP {response.status_code} for {path}",
                response.status_code,
            )
        _remember(base, scheme)
        try:
            return response.json()
        except ValueError as exc:
            raise SonarUnavailable(f"SonarQube returned a non-JSON body for {path}") from exc

    status_code = last.status_code if last is not None else None
    raise SonarUnavailable(
        f"SonarQube refused every authentication scheme for {path} "
        f"(HTTP {status_code}). The token may have expired, or the project may be private.",
        status_code,
    )


# ── the snapshot ────────────────────────────────────────────────────────────
# One crawl per cache owner at a time, and none at all while a cached answer is
# young enough: integrations_cache.cached_external_swr, which /overview reads through.


def _all_projects(base: str, token: str) -> List[Dict[str, str]]:
    """Every project this caller may Browse.

    NOT api/projects/search -- see the module docstring. That one needs Administer
    System and answers 403 to ordinary users, which is the whole population here.
    """
    out: List[Dict[str, Any]] = []
    page = 1
    while page <= _MAX_PAGES:
        payload = _request(base, token, "/api/components/search_projects", {
            "ps": _PAGE_SIZE,
            "p": page,
            # `f` is a closed list on 9.9 (analysisDate, leakPeriodDate, _all); both
            # are in it. They are what "analysed 3 days ago" and "new code since"
            # are read from, and they cost nothing extra on the same call.
            "f": "analysisDate,leakPeriodDate",
        })
        components = payload.get("components") or []
        for item in components:
            key = str(item.get("key") or "")
            if key:
                out.append({
                    "key": key,
                    "name": str(item.get("name") or key),
                    "analysis_date": str(item.get("analysisDate") or ""),
                    "new_code_since": str(item.get("leakPeriodDate") or ""),
                    "visibility": str(item.get("visibility") or ""),
                    "tags": [str(t) for t in (item.get("tags") or []) if t],
                })
        total = int((payload.get("paging") or {}).get("total") or 0)
        if len(out) >= total or not components:
            break
        page += 1
    return out


def _measure_value(measure: Dict[str, Any]) -> str:
    """The value of one measure, whichever shape this version reports it in.

    A new-code measure has no ``value``; it carries a ``period`` (9.9) or a
    ``periods`` list (older). Reading only ``value`` is how a new-code widget ends
    up showing zeros for a project that has plenty wrong with it.
    """
    if measure.get("value") is not None:
        return str(measure.get("value"))
    period = measure.get("period")
    if isinstance(period, dict) and period.get("value") is not None:
        return str(period.get("value"))
    periods = measure.get("periods")
    if isinstance(periods, list) and periods:
        first = periods[0]
        if isinstance(first, dict) and first.get("value") is not None:
            return str(first.get("value"))
    return ""


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# The metric set each server has accepted. One that refused the extended set is
# asked for the core set from then on, rather than twice per chunk on every crawl.
_metrics_by_base: Dict[str, Tuple[str, ...]] = {}

_measure_lock = threading.Lock()
# (base, project key) -> (analysis version, stored at, {metric: value})
_measure_cache: "OrderedDict[Tuple[str, str], Tuple[str, float, Dict[str, str]]]" = OrderedDict()


def _measure_chunk(base: str, token: str, chunk: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """{project_key: {metric: value}} for up to _MEASURE_CHUNK keys, in one call."""
    metrics = _metrics_by_base.get(base, SNAPSHOT_METRICS)
    params = {"projectKeys": ",".join(chunk), "metricKeys": ",".join(metrics)}
    try:
        payload = _request(base, token, "/api/measures/search", params)
    except SonarUnavailable as exc:
        if exc.status_code != 404 or metrics is _PROVEN_METRICS:
            raise
        # A metric key this server does not know. Say which set was refused, then
        # carry on with the one that is known to work: fewer numbers on the card
        # beats no card at all.
        if _metrics_by_base.get(base) is not _PROVEN_METRICS:
            _metrics_by_base[base] = _PROVEN_METRICS
            _log.warning(
                "SonarQube at %s refused the extended metric set (HTTP 404); "
                "falling back to the core set. Unknown key among: %s",
                base, ", ".join(sorted(set(SNAPSHOT_METRICS) - set(_PROVEN_METRICS))),
            )
        params["metricKeys"] = ",".join(_PROVEN_METRICS)
        payload = _request(base, token, "/api/measures/search", params)
    out: Dict[str, Dict[str, str]] = {}
    for measure in payload.get("measures") or []:
        component = str(measure.get("component") or "")
        metric = str(measure.get("metric") or "")
        if not component or not metric:
            continue
        out.setdefault(component, {})[metric] = _measure_value(measure)
    return out


def _analysis_version(project: Dict[str, Any]) -> str:
    """What has to change for a project's measures to change: a new analysis, or a
    new start for its new-code period."""
    return f"{project.get('analysis_date', '')}|{project.get('new_code_since', '')}"


def _measures(base: str, token: str, projects: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{project_key: {metric: value}} for every project, asking only about the ones
    that changed.

    A project's measures change when it is ANALYSED, and search_projects already
    says when that last happened. So each project's measures are kept (on this pod)
    against its analysis date and fetched again only when the date moves -- which
    turns a crawl of every measure of every project, once a minute, into a crawl of
    the handful analysed since.

    SHARED BETWEEN USERS, SAFELY. A project's measures are the same whoever reads
    them; what differs per person is WHICH projects they may see, and that comes
    from their own search_projects call on every snapshot. A kept entry is only ever
    handed out for a key the caller's own listing just returned.
    """
    base = base.rstrip("/")
    out: Dict[str, Dict[str, str]] = {}
    wanted: List[Dict[str, Any]] = []
    # A listing that carries dates tells us which projects were never analysed: they
    # have no measures to fetch. A listing without any dates tells us nothing.
    dated = any(p.get("analysis_date") for p in projects)
    now = time.monotonic()
    with _measure_lock:
        for project in projects:
            key = project["key"]
            if dated and not project.get("analysis_date"):
                out[key] = {}
                continue
            hit = _measure_cache.get((base, key))
            max_age = _MEASURE_MAX_AGE_S if project.get("analysis_date") else _UNDATED_MAX_AGE_S
            if hit and hit[0] == _analysis_version(project) and now - hit[1] < max_age:
                out[key] = hit[2]
                _measure_cache.move_to_end((base, key))
            else:
                wanted.append(project)
    if not wanted:
        return out

    chunks = list(_chunks([p["key"] for p in wanted], _MEASURE_CHUNK))
    if len(chunks) == 1:
        results = [_measure_chunk(base, token, chunks[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(_PARALLEL, len(chunks))) as pool:
            results = list(pool.map(lambda chunk: _measure_chunk(base, token, chunk), chunks))
    fetched: Dict[str, Dict[str, str]] = {}
    for part in results:
        fetched.update(part)

    with _measure_lock:
        for project in wanted:
            key = project["key"]
            values = fetched.get(key, {})
            out[key] = values
            _measure_cache[(base, key)] = (_analysis_version(project), now, values)
            _measure_cache.move_to_end((base, key))
        while len(_measure_cache) > _MEASURE_CACHE_MAX:
            _measure_cache.popitem(last=False)
    return out


def _gate_conditions(details_json: str) -> List[Dict[str, str]]:
    """Every condition of the project's gate, passed or not, from quality_gate_details.

    This is why the gate widget needs no second call per project: SonarQube
    already ships the reason as a JSON string in a measure.
    """
    if not details_json:
        return []
    try:
        details = json.loads(details_json)
    except ValueError:
        return []
    if not isinstance(details, dict):
        return []
    return [
        format_condition(
            str(condition.get("metric") or ""),
            str(condition.get("actual") or ""),
            str(condition.get("op") or ""),
            str(condition.get("error") or condition.get("warning") or ""),
            str(condition.get("level") or ""),
        )
        for condition in details.get("conditions") or []
        if isinstance(condition, dict)
    ]


# Metrics SonarQube reports as a percentage. Anything else numeric is a count, and
# anything ending in _rating is a 1-5 grade that people know as a letter.
_PERCENT_METRICS = frozenset({
    "coverage", "new_coverage", "line_coverage", "new_line_coverage",
    "branch_coverage", "new_branch_coverage",
    "duplicated_lines_density", "new_duplicated_lines_density",
    "security_hotspots_reviewed", "new_security_hotspots_reviewed",
    "test_success_density", "sqale_debt_ratio", "new_sqale_debt_ratio",
})


def format_value(metric: str, raw: str) -> str:
    """A measure the way a person reads it: "42.8%", "D", "1,204" -- never "42.8477".

    The gate details carry the raw number, so without this a failing condition
    read "Duplication on new code -- 42.8477 > 3": correct, and a sum to do in your
    head before you know whether it is bad.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ""
    if metric.endswith("_rating"):
        return _RATING_LETTER.get(text, text)
    try:
        number = float(text)
    except ValueError:
        return text
    if metric in _PERCENT_METRICS:
        return f"{number:.1f}%"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def format_condition(metric: str, actual: str, op: str, threshold: str, level: str) -> Dict[str, str]:
    """One gate condition, with the requirement said in words.

    ``op`` is the comparison that FAILS the condition: GT fails above the
    threshold, LT fails below it. So the requirement is the opposite -- "at most"
    for GT and "at least" for LT -- and for a rating, where 1 is A and a higher
    number is worse, GT 1 means "needs A".
    """
    op = str(op or "").upper()
    level = str(level or "").upper()
    shown_threshold = format_value(metric, threshold)
    if metric.endswith("_rating"):
        needs = f"needs {shown_threshold}" + ("" if shown_threshold == "A" else " or better")
    elif op == "LT":
        needs = f"needs at least {shown_threshold}"
    elif op == "GT":
        needs = f"must be at most {shown_threshold}"
    else:
        needs = ""
    return {
        "metric": metric,
        "label": _humanise_metric(metric),
        "actual": actual,
        "actual_display": format_value(metric, actual) or "no value",
        "op": {"GT": ">", "LT": "<"}.get(op, ""),
        "threshold": threshold,
        "threshold_display": shown_threshold,
        "needs": needs,
        "level": level,
        "passed": level == "OK",
    }


def _humanise_metric(metric: str) -> str:
    known = {
        "new_coverage": "Coverage on new code",
        "new_duplicated_lines_density": "Duplication on new code",
        "new_violations": "Issues on new code",
        "new_bugs": "Bugs on new code",
        "new_vulnerabilities": "Vulnerabilities on new code",
        "new_security_hotspots": "Hotspots on new code",
        "new_security_hotspots_reviewed": "Hotspots reviewed on new code",
        "new_reliability_rating": "Reliability on new code",
        "new_security_rating": "Security on new code",
        "new_security_review_rating": "Security review on new code",
        "new_maintainability_rating": "Maintainability on new code",
        "new_code_smells": "Code smells on new code",
        "new_line_coverage": "Line coverage on new code",
        "new_branch_coverage": "Branch coverage on new code",
        "coverage": "Coverage",
        "duplicated_lines_density": "Duplicated lines",
        "sqale_rating": "Maintainability",
        "reliability_rating": "Reliability",
        "security_rating": "Security",
        "security_review_rating": "Security review",
        "security_hotspots_reviewed": "Hotspots reviewed",
    }
    if metric in known:
        return known[metric]
    return metric.replace("_", " ").strip().capitalize()


_CATEGORY_LABEL = {
    "sql-injection": "SQL injection",
    "xss": "Cross-site scripting",
    "insecure-conf": "Insecure configuration",
    "weak-cryptography": "Weak cryptography",
    "auth": "Authentication",
    "log-injection": "Log injection",
    "path-traversal-injection": "Path traversal",
    "command-injection": "Command injection",
    "dos": "Denial of service",
    "ssrf": "Server-side request forgery",
    "others": "Other",
}


def _humanise_category(category: str) -> str:
    """A hotspot's security category. NOT _humanise_metric: these are hyphenated,
    so the metric map's underscore handling leaves "Sql-injection"."""
    key = str(category or "").strip().lower()
    return _CATEGORY_LABEL.get(key) or key.replace("-", " ").title() or "Other"


def _num(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _int(raw: str) -> int:
    # _num returns a float, and float("nan") / float("inf") both PARSE -- int() then
    # raises, out of a helper whose whole job is not to.
    try:
        return int(_num(raw))
    except (ValueError, OverflowError):
        return 0


def snapshot(base: str, token: str) -> List[Dict[str, Any]]:
    """Every project, with its gate and its numbers, in a handful of calls.

    One listing, plus measure calls for only the projects analysed since the last
    snapshot (see _measures). Four widgets read this; none of them fetches anything
    of its own.
    """
    projects = _all_projects(base, token)
    if not projects:
        return []
    measures = _measures(base, token, projects)

    rows: List[Dict[str, Any]] = []
    for project in projects:
        m = measures.get(project["key"], {})
        status = str(m.get("alert_status") or "").upper()
        conditions = _gate_conditions(m.get("quality_gate_details", ""))
        rows.append({
            "key": project["key"],
            "project_key": project["key"],
            "name": project["name"],
            "analysis_date": project.get("analysis_date", ""),
            "new_code_since": project.get("new_code_since", ""),
            "visibility": project.get("visibility", ""),
            "tags": project.get("tags", []),
            "gate": status or "NONE",
            "gate_ok": status == "OK",
            "gate_failing": status == "ERROR",
            "gate_unknown": status not in {"OK", "ERROR", "WARN"},
            # Every condition, so an expanded project can show what it PASSED as
            # well; the failing ones separately because three widgets rank on them.
            "conditions": conditions,
            "failing_conditions": [c for c in conditions if not c["passed"]],
            "bugs": _int(m.get("bugs", "")),
            "vulnerabilities": _int(m.get("vulnerabilities", "")),
            "code_smells": _int(m.get("code_smells", "")),
            # Minutes of estimated remediation effort, as SonarQube stores it.
            "debt_minutes": _int(m.get("sqale_index", "")),
            "security_review": _RATING_LETTER.get(str(m.get("security_review_rating", "")), ""),
            # None, not 0, when the project imports no test report: "no tests" and
            # "no test data" are different findings and only one is a problem.
            "tests": _int(m["tests"]) if m.get("tests") not in (None, "") else None,
            "test_failures": _int(m.get("test_failures", "")),
            "test_errors": _int(m.get("test_errors", "")),
            "skipped_tests": _int(m.get("skipped_tests", "")),
            "new_code_smells": _int(m.get("new_code_smells", "")),
            "hotspots": _int(m.get("security_hotspots", "")),
            "hotspots_reviewed": _num(m.get("security_hotspots_reviewed", "")),
            # What is actually LEFT to review. `security_hotspots` is the total and
            # `security_hotspots_reviewed` is a percentage, so a project with 200
            # hotspots all reviewed outranks one with 3 untouched unless the two are
            # combined. The widget ranks on this.
            "hotspots_to_review": max(0, round(
                _int(m.get("security_hotspots", ""))
                * (1.0 - min(100.0, max(0.0, _num(m.get("security_hotspots_reviewed", "")))) / 100.0)
            )),
            "coverage": _num(m.get("coverage", "")),
            "duplications": _num(m.get("duplicated_lines_density", "")),
            "ncloc": _int(m.get("ncloc", "")),
            "reliability": _RATING_LETTER.get(str(m.get("reliability_rating", "")), ""),
            "security": _RATING_LETTER.get(str(m.get("security_rating", "")), ""),
            "maintainability": _RATING_LETTER.get(str(m.get("sqale_rating", "")), ""),
            "new_violations": _int(m.get("new_violations", "")),
            "new_bugs": _int(m.get("new_bugs", "")),
            "new_vulnerabilities": _int(m.get("new_vulnerabilities", "")),
            "new_hotspots": _int(m.get("new_security_hotspots", "")),
            "new_coverage": _num(m.get("new_coverage", "")),
            "new_duplications": _num(m.get("new_duplicated_lines_density", "")),
            "new_lines": _int(m.get("new_lines", "")),
            "has_measures": bool(m),
        })
    return rows


# ── issues, by the person who wrote the line ────────────────────────────────

def issues_for_authors(base: str, token: str, authors: Sequence[str],
                       *, limit: int = 25) -> List[Dict[str, Any]]:
    """Open issues on lines these SCM identities last touched.

    NOT by assignee. On an instance where nobody assigns anything -- which is the
    normal state of SonarQube unless a team has deliberately adopted assignment --
    an assignee search is permanently empty, and an empty widget teaches people to
    ignore the widget. The SCM author is recorded by the analysis itself, so it is
    populated whether or not anybody has ever opened SonarQube.
    """
    authors = [a for a in {str(a or "").strip() for a in authors} if a]
    if not authors:
        return []
    payload = _request(base, token, "/api/issues/search", {
        # `author`, REPEATED -- not `authors`, and not comma-separated. 9.9 reads it
        # with multiParam and does not split on commas, and it silently ignores a
        # parameter it does not declare: "authors" would be dropped on the floor and
        # the search would quietly widen to every issue on the instance.
        "author": list(authors),
        "resolved": "false",
        "statuses": "OPEN,CONFIRMED,REOPENED",
        "s": "SEVERITY",
        "asc": "false",
        "ps": max(1, min(int(limit), 100)),
        # NO additionalFields: 9.9 validates it against a closed set that does not
        # include "components", and an illegal value is a 400, not an ignored
        # parameter. The components block is returned by default anyway.
    })
    components = {
        str(c.get("key") or ""): str(c.get("longName") or c.get("name") or "")
        for c in payload.get("components") or []
    }
    out: List[Dict[str, Any]] = []
    for issue in payload.get("issues") or []:
        component = str(issue.get("component") or "")
        out.append({
            "key": str(issue.get("key") or ""),
            "project": str(issue.get("project") or ""),
            "component": component,
            "file": components.get(component, component.split(":")[-1]),
            "line": issue.get("line"),
            "message": str(issue.get("message") or ""),
            "severity": str(issue.get("severity") or "").upper(),
            "type": str(issue.get("type") or "").replace("_", " ").title(),
            "rule": str(issue.get("rule") or ""),
            "effort": str(issue.get("effort") or issue.get("debt") or ""),
            "tags": [str(t) for t in (issue.get("tags") or []) if t],
            "status": str(issue.get("status") or "").upper(),
            "author": str(issue.get("author") or ""),
            "created": str(issue.get("creationDate") or ""),
            "updated": str(issue.get("updateDate") or ""),
        })
    return out


# ── hotspots, for one project, on demand ────────────────────────────────────

def hotspots_to_review(base: str, token: str, project_key: str,
                       *, limit: int = 20) -> List[Dict[str, Any]]:
    """Security hotspots still awaiting review on one project.

    Per project, because SonarQube 9.9 has no instance-wide hotspot search: the
    endpoint requires a project. The widget therefore ranks projects from the
    snapshot's counts and fetches the list only for the one somebody opens.
    """
    payload = _request(base, token, "/api/hotspots/search", {
        "projectKey": project_key,
        "status": "TO_REVIEW",
        "ps": max(1, min(int(limit), 100)),
    })
    out: List[Dict[str, Any]] = []
    for hotspot in payload.get("hotspots") or []:
        out.append({
            "key": str(hotspot.get("key") or ""),
            "message": str(hotspot.get("message") or ""),
            "probability": str(hotspot.get("vulnerabilityProbability") or "").upper(),
            "category": _humanise_category(str(hotspot.get("securityCategory") or "")),
            "component": str(hotspot.get("component") or ""),
            "file": str(hotspot.get("component") or "").split(":")[-1],
            "line": hotspot.get("line"),
        })
    return out


# ── pull requests ───────────────────────────────────────────────────────────

_SPLIT = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lowercased, punctuation collapsed. ``My-Repo.API`` and ``my repo api`` match."""
    return _SPLIT.sub(" ", str(text or "").lower()).strip()


def match_projects_to_repos(
    projects: Sequence[Dict[str, Any]],
    repos: Sequence[str],
) -> Dict[str, str]:
    """{repo name: sonar project key}, for the repos a Sonar project plausibly scans.

    THERE IS NO AUTHORITATIVE LINK AVAILABLE HERE. ``api/alm_settings/get_binding``
    knows which Azure DevOps repository a project is bound to and needs Administer
    on that project, which almost nobody has on this instance -- so asking would
    answer 403 for the people the widget is for. Name matching is what is left.

    It is therefore deliberately CONSERVATIVE: an exact normalised match on the
    project key or its name, or a key that ends with the repo name after a
    separator. A fuzzy match would attach one team's quality gate to another team's
    pull request, and a wrong answer here is worse than no answer -- so anything
    ambiguous is dropped rather than guessed, and the widget says how many it could
    not place.
    """
    by_norm: Dict[str, List[str]] = {}
    for project in projects:
        key = str(project.get("key") or "")
        if not key:
            continue
        for candidate in (key, str(project.get("name") or "")):
            norm = normalise(candidate)
            if norm:
                by_norm.setdefault(norm, [])
                if key not in by_norm[norm]:
                    by_norm[norm].append(key)
        # `group:artifact` and `org_repo` both end with the repository's name.
        tail = normalise(re.split(r"[:_/]", key)[-1])
        if tail:
            by_norm.setdefault(tail, [])
            if key not in by_norm[tail]:
                by_norm[tail].append(key)

    out: Dict[str, str] = {}
    for repo in repos:
        norm = normalise(repo)
        candidates = by_norm.get(norm) or []
        if len(candidates) == 1:
            out[str(repo)] = candidates[0]
        elif len(candidates) > 1:
            _log.info(
                "SonarQube: %r matches %d projects (%s); left unmatched rather than guessed",
                repo, len(candidates), ", ".join(candidates[:4]),
            )
    return out


class PullRequestsUnsupported(RuntimeError):
    """This SonarQube has no pull-request analysis at all."""


def pull_request_gates(base: str, token: str, project_key: str) -> List[Dict[str, Any]]:
    """Analysed pull requests for one project, with each one's gate.

    NOT AVAILABLE ON EVERY SONARQUBE. api/project_pull_requests ships in the Branch
    plugin, which is part of Developer Edition and above -- a Community 9.9 does not
    serve the route at all and answers 404 "Unknown url". That is a fact about the
    licence, not a fault, and it must reach the user as "this edition does not
    analyse pull requests" rather than as an error they could act on.
    """
    try:
        payload = _request(base, token, "/api/project_pull_requests/list", {"project": project_key})
    except SonarUnavailable as exc:
        if exc.status_code == 404:
            raise PullRequestsUnsupported(
                "This SonarQube does not analyse pull requests (the Branch plugin, "
                "Developer Edition and above, is what provides them)."
            ) from exc
        raise
    out: List[Dict[str, Any]] = []
    for pr in payload.get("pullRequests") or []:
        pr_status = pr.get("status") or {}
        status = str(pr_status.get("qualityGateStatus") or "").upper()
        out.append({
            "id": str(pr.get("key") or ""),
            "title": str(pr.get("title") or ""),
            "branch": str(pr.get("branch") or ""),
            # 9.9 carries both; protobuf omits unset optionals, so either may be
            # the one that is present.
            "target": str(pr.get("target") or pr.get("base") or ""),
            "gate": status or "NONE",
            "gate_ok": status == "OK",
            "gate_failing": status == "ERROR",
            # The list already carries the three issue counts, so a collapsed row
            # can say "2 bugs" without a call per pull request.
            "bugs": _int(pr_status.get("bugs", "")),
            "vulnerabilities": _int(pr_status.get("vulnerabilities", "")),
            "code_smells": _int(pr_status.get("codeSmells", "")),
            "analysed_at": str(pr.get("analysisDate") or ""),
            "url": str(pr.get("url") or ""),
            "project_key": project_key,
        })
    return out


def _single_measures(base: str, token: str, project_key: str, pull_request: str,
                     metrics: Sequence[str]) -> Dict[str, str]:
    payload = _request(base, token, "/api/measures/component", {
        "component": project_key,
        "pullRequest": pull_request,
        "metricKeys": ",".join(metrics),
    })
    component = payload.get("component") or {}
    return {
        str(m.get("metric") or ""): _measure_value(m)
        for m in component.get("measures") or []
        if m.get("metric")
    }


def pull_request_details(base: str, token: str, project_key: str,
                         pull_request: str) -> Dict[str, Any]:
    """One pull request's own analysis: its gate, every condition, its numbers.

    Two calls, both Browse-only: the gate with its conditions from
    qualitygates/project_status, and the measures from measures/component. Asked
    for one pull request at a time, when somebody opens it -- never for the list.
    """
    gate_payload = _request(base, token, "/api/qualitygates/project_status", {
        "projectKey": project_key,
        "pullRequest": pull_request,
    })
    project_status = gate_payload.get("projectStatus") or {}
    status = str(project_status.get("status") or "").upper()
    conditions = [
        format_condition(
            str(c.get("metricKey") or ""),
            str(c.get("actualValue") or ""),
            str(c.get("comparator") or ""),
            str(c.get("errorThreshold") or c.get("warningThreshold") or ""),
            str(c.get("status") or ""),
        )
        for c in project_status.get("conditions") or []
        if isinstance(c, dict)
    ]
    try:
        m = _single_measures(base, token, project_key, pull_request, PULL_REQUEST_METRICS)
    except SonarUnavailable as exc:
        # The gate call above already found this pull request, so a 404 HERE is a
        # metric key the server does not know, not a missing analysis. Keep the
        # card with the numbers every 9.x has rather than saying "not analysed".
        if exc.status_code != 404:
            raise
        _log.warning("SonarQube at %s refused the pull-request metric set (HTTP 404); using the core set", base)
        m = _single_measures(base, token, project_key, pull_request, (
            "new_bugs", "new_vulnerabilities", "new_code_smells", "new_security_hotspots",
            "new_coverage", "new_duplicated_lines_density", "new_lines",
            "coverage", "duplicated_lines_density",
        ))

    def count(new_key: str, overall_key: str) -> int:
        # A pull request's issues ARE its new issues; older servers report them
        # under the plain key only, so fall back rather than show a false zero.
        raw = m.get(new_key)
        return _int(raw if raw not in (None, "") else m.get(overall_key, ""))

    def pct(key: str) -> Optional[float]:
        raw = m.get(key)
        return None if raw in (None, "") else _num(raw)

    return {
        "project_key": project_key,
        "pull_request": pull_request,
        "gate": status or "NONE",
        "gate_ok": status == "OK",
        "gate_failing": status == "ERROR",
        "conditions": conditions,
        "bugs": count("new_bugs", "bugs"),
        "vulnerabilities": count("new_vulnerabilities", "vulnerabilities"),
        "code_smells": count("new_code_smells", "code_smells"),
        "hotspots": count("new_security_hotspots", "security_hotspots"),
        # None where SonarQube has nothing to say (no coverage report imported, or
        # nothing coverable changed) -- the card says so instead of "0.0%".
        "new_coverage": pct("new_coverage"),
        "coverage_after_merge": pct("coverage"),
        "new_duplications": pct("new_duplicated_lines_density"),
        "duplications_after_merge": pct("duplicated_lines_density"),
        "new_lines": _int(m.get("new_lines", "")),
        "new_lines_to_cover": _int(m.get("new_lines_to_cover", "")),
        "tests": _int(m["tests"]) if m.get("tests") not in (None, "") else None,
        "test_failures": _int(m.get("test_failures", "")),
        "test_errors": _int(m.get("test_errors", "")),
        "skipped_tests": _int(m.get("skipped_tests", "")),
    }
