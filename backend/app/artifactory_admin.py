"""
Artifactory operations that need admin rights.

A per-user Artifactory token cannot do either of the things the quota self-service
needs: /api/storageinfo is admin-only (which is why the storage widget is restricted
to Platform Admins), and a project's storage_quota_bytes can only be written by an
administrator. So this uses a service account -- ARTIFACTORY_ADMIN_USERNAME with
ARTIFACTORY_ADMIN_PASSWORD (or ARTIFACTORY_ADMIN_TOKEN) if they are set, and
otherwise the ARTIFACTORY_BACKUP pair that already exists in the cluster for the
database backup upload.

A PASSWORD AND A TOKEN ARE NOT INTERCHANGEABLE

Both arrive here as "the secret", and how they must be sent differs:

    password        Basic username:password, and ONLY that. A password in an
                    Authorization: Bearer header is not an alternative to try -- it
                    is meaningless to the server and pointless to send.
    access token    Bearer. JFrog Access tokens are JWTs.
    API key         X-JFrog-Art-Api.

So the shapes attempted are chosen from what the secret LOOKS like, best first, and
the one that works is remembered. api/integrations.py needed the same ladder for
user tokens; the difference here is that a password must not climb it.

TWO SERVICES

The Projects API is not part of Artifactory. It lives in JFrog Access, at /access --
a sibling of /artifactory, not a child of it -- and the two do not necessarily accept
the same credential. An account that reads /artifactory happily can still be refused
by Access, which is why every probe records its own status against its own URL.

Everything here returns RAW BYTES. Formatting belongs to whatever is about to show
the number: a request that says "512 GB" while writing 549755813888 is a request
nobody can check.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from resilient_http import tls_verify

log = logging.getLogger(__name__)

_UNLIMITED = 0  # JFrog writes 0 (older versions -1) for "no quota"

# Which auth shape last worked, so the ladder is climbed once rather than per call.
_working_shape: Optional[str] = None


class ArtifactoryProbeError(RuntimeError):
    """One failed call, with everything needed to act on it.

    The status is the whole message. A 401 is the wrong credentials or the wrong
    credential SHAPE, a 403 is the right credentials without admin rights, a 404 is an
    endpoint this version does not route, and a connection error is the firewall --
    four different jobs for whoever is reading it. Collapsing them into "Artifactory
    did not answer" throws away the only part anyone can act on.
    """

    def __init__(
        self, label: str, url: str, status: int, body: str = "",
        account: str = "", shapes: str = "",
    ):
        self.label = label
        self.url = url
        self.status = status
        self.body = (body or "")[:200]
        self.account = account
        self.shapes = shapes
        super().__init__(str(self))

    def __str__(self) -> str:
        who = self.account or "the configured account"
        if self.status == 401:
            tried = f" (tried: {self.shapes})" if self.shapes else ""
            return f"{self.label}: Artifactory rejected {who} with 401{tried}."
        if self.status == 403:
            return (
                f"{self.label}: {who} is authenticated but not allowed to read this "
                f"(403). It needs Artifactory admin rights."
            )
        if self.status == 404:
            return f"{self.label}: this Artifactory does not have {self.url} (404)."
        if self.status == 0:
            return f"{self.label}: Artifactory could not be reached ({self.body})."
        return f"{self.label}: Artifactory answered HTTP {self.status}."


# ── Configuration ────────────────────────────────────────────────────────────

def _base() -> str:
    return (os.getenv("ARTIFACTORY_BASE_URL") or "").strip().rstrip("/")


def _access_root(base: str) -> str:
    return re.sub(r"/artifactory/?$", "", (base or "").rstrip("/"))


def _credentials() -> Tuple[str, str, str]:
    """(username, secret, which variables it came from).

    A DEDICATED admin credential is preferred, because the backup account only needs
    deploy rights on one repository and making it an Artifactory administrator to read
    a quota is a bigger grant than the feature deserves. The backup pair stays as the
    fallback so an instance where that account IS an admin needs no new configuration.

    PASSWORD and TOKEN are both accepted for the admin account, because the person
    configuring it has whichever one they have -- a UI login is a password, and asking
    them to mint a token first is a reason for the feature to sit unconfigured.

    When BOTH are set the TOKEN wins, and that order is not arbitrary. A password
    authenticates to Artifactory; an access token authenticates to Artifactory AND to
    JFrog Access, which is the service the Projects API lives behind. Preferring the
    password meant that following this feature's own advice -- "Access needs an admin
    access token, set ARTIFACTORY_ADMIN_TOKEN" -- changed nothing, because the token
    that was just added was never the one being sent.
    """
    admin_user = (os.getenv("ARTIFACTORY_ADMIN_USERNAME") or "").strip()
    admin_password = (os.getenv("ARTIFACTORY_ADMIN_PASSWORD") or "").strip()
    admin_token = (os.getenv("ARTIFACTORY_ADMIN_TOKEN") or "").strip()
    if admin_user and admin_token:
        return admin_user, admin_token, "ARTIFACTORY_ADMIN_USERNAME/TOKEN"
    if admin_user and admin_password:
        return admin_user, admin_password, "ARTIFACTORY_ADMIN_USERNAME/PASSWORD"
    return (
        (os.getenv("ARTIFACTORY_BACKUP_USERNAME") or "").strip(),
        (os.getenv("ARTIFACTORY_BACKUP_TOKEN") or "").strip(),
        "ARTIFACTORY_BACKUP_USERNAME/TOKEN",
    )


def account() -> str:
    return _credentials()[0]


def credential_source() -> str:
    return _credentials()[2]


def auth_shape() -> str:
    return _working_shape or "not established yet"


def is_configured() -> bool:
    user, secret, _ = _credentials()
    return bool(_base() and user and secret)


# ── One call, however it has to be authenticated ─────────────────────────────

def _looks_like_token(secret: str) -> bool:
    """Whether the secret can meaningfully be sent as a bearer token or API key.

    A JFrog access token is a JWT (three dot-separated segments, starting "eyJ"); an
    API key is a long opaque string. A UI password is neither, and sending one as a
    bearer token is not a fallback worth trying -- it cannot succeed, and it puts the
    password in a header it was never meant for.
    """
    value = (secret or "").strip()
    if value.startswith("eyJ") and value.count(".") == 2:
        return True
    return len(value) >= 40 and " " not in value


def _shapes_for(secret: str) -> Tuple[str, ...]:
    if _looks_like_token(secret):
        return ("bearer", "apikey", "basic")
    return ("basic",)


def _headers(shape: str, secret: str) -> Dict[str, str]:
    # Accept */* rather than application/json: some endpoints answer text/plain and
    # demanding JSON makes Artifactory reply 406.
    headers = {"Accept": "*/*"}
    if shape == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif shape == "apikey":
        headers["X-JFrog-Art-Api"] = secret
    return headers


def _call(
    method: str,
    url: str,
    label: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
) -> Any:
    """Perform one call, trying each plausible auth shape, and record every status.

    Only a 401 moves on to the next shape. A 403 means the credential was ACCEPTED
    and the account simply is not allowed -- trying another header would collect three
    identical refusals and report the last one, hiding the fact that the credential
    itself is fine.
    """
    global _working_shape

    user, secret, source = _credentials()
    if not is_configured():
        raise RuntimeError(
            "Artifactory admin access is not configured. Set ARTIFACTORY_BASE_URL and "
            "either ARTIFACTORY_ADMIN_USERNAME with ARTIFACTORY_ADMIN_PASSWORD, or "
            "ARTIFACTORY_BACKUP_USERNAME/TOKEN."
        )

    shapes = _shapes_for(secret)
    order = ([_working_shape] if _working_shape in shapes else []) + [
        s for s in shapes if s != _working_shape
    ]
    last: Optional[ArtifactoryProbeError] = None

    for shape in order:
        auth = httpx.BasicAuth(user, secret) if shape == "basic" else None
        try:
            with httpx.Client(
                verify=tls_verify(),
                auth=auth,
                headers=_headers(shape, secret),
                timeout=httpx.Timeout(timeout, connect=5.0),
                follow_redirects=True,
            ) as client:
                resp = client.request(method, url, json=json_body)
        except Exception as exc:
            log.warning("Artifactory %s: %s unreachable: %s", label, url, exc)
            raise ArtifactoryProbeError(
                label, url, 0, f"{type(exc).__name__}: {exc}", user, ", ".join(order)
            ) from exc

        log.warning(
            "Artifactory %s: %s %s auth=%s status=%s account=%s source=%s",
            label, method, url, shape, resp.status_code, user, source,
        )
        if resp.status_code < 400:
            _working_shape = shape
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        last = ArtifactoryProbeError(
            label, url, resp.status_code, resp.text, user, ", ".join(order)
        )
        if resp.status_code != 401:
            break

    raise last or ArtifactoryProbeError(label, url, 0, "no attempt was made", user)


def _probe(url: str, label: str) -> Any:
    return _call("GET", url, label)


def _count_of(payload: Any) -> Optional[int]:
    """How many things a probe came back with, for the diagnostics.

    None means "not a countable answer" rather than zero: /api/storageinfo returns one
    object, and calling that 0 would read as a failure it is not.
    """
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("projects", "results", "members", "repositories"):
            if isinstance(payload.get(key), list):
                return len(payload[key])
    return None


# ── Sizes ────────────────────────────────────────────────────────────────────

def format_bytes(value: float) -> str:
    """Human-readable size, for labels only -- never for what gets written."""
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def parse_size(text: str) -> int:
    """"250 GB" / "250gb" / "250" (GB assumed) -> bytes.

    Typed sizes are how a person expresses a quota; bytes are how Artifactory stores
    one. Doing the conversion in one place means the confirmation the requester reads
    and the number the executor writes cannot disagree.
    """
    raw = str(text or "").strip().upper().replace(",", "")
    if not raw:
        raise ValueError("A size is required.")
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)?$", raw)
    if not match:
        raise ValueError(f"'{text}' is not a size. Use something like '250 GB'.")
    amount = float(match.group(1))
    unit = match.group(2) or "GB"
    factor = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[unit]
    total = int(amount * factor)
    if total <= 0:
        raise ValueError("Size must be greater than zero.")
    return total


# ── Projects ─────────────────────────────────────────────────────────────────

def _storage() -> Dict[str, Any]:
    """One read of /api/storageinfo, giving both numbers it reports.

    They are NOT the same number and the difference is not a rounding error:

      * per repository (``usedSpaceInBytes``) is LOGICAL size -- what the repositories
        contain. This is what a project quota is measured against, so it is the right
        number for "how full is this project".
      * ``fileStoreSummary`` is PHYSICAL disk, after de-duplication. Artifactory keeps
        one copy of a binary however many repositories reference it, so the logical
        total is routinely larger -- which is how an estate reported 7.2 TB "used" on
        a 6 TB disk. Neither number was wrong; putting them on one axis was.

    The filestore also knows the disk's real size, which is a better answer than a
    configured one that nobody updates.
    """
    payload = _probe(f"{_base()}/api/storageinfo", "storage usage") or {}
    repo_bytes: Dict[str, float] = {}
    for repo in payload.get("repositoriesSummaryList") or []:
        key = str(repo.get("repoKey") or "").strip()
        if not key or key.upper() == "TOTAL":
            continue
        try:
            repo_bytes[key] = float(repo.get("usedSpaceInBytes") or 0)
        except (TypeError, ValueError):
            repo_bytes[key] = 0.0

    from api.integrations import _parse_size_to_bytes, _strip_percent_suffix

    store = payload.get("fileStoreSummary") or {}
    return {
        "repo_bytes": repo_bytes,
        "physical_used_bytes": int(_parse_size_to_bytes(_strip_percent_suffix(store.get("usedSpace")))),
        "physical_total_bytes": int(_parse_size_to_bytes(_strip_percent_suffix(store.get("totalSpace")))),
    }


def _repo_bytes() -> Dict[str, float]:
    return _storage()["repo_bytes"]


def _raw_projects() -> List[Dict[str, Any]]:
    raw = _probe(f"{_access_root(_base())}/access/api/v1/projects", "project list")
    if not isinstance(raw, list):
        raw = (raw or {}).get("projects") or (raw or {}).get("results") or []
    return raw or []


def projects_with_usage() -> List[Dict[str, Any]]:
    """Cached for a minute. See _live_projects_with_usage for what it does."""
    return _cached("projects-with-usage", _live_projects_with_usage, ttl=60)


def _live_projects_with_usage() -> List[Dict[str, Any]]:
    """Every JFrog project with its quota, its usage and what is left of it.

    Usage is summed from the repositories whose key is prefixed <project-key>-, which
    is how JFrog names a project's repositories -- /api/storageinfo reports per
    repository and never per project.
    """
    # The project list is the one this cannot do without. Usage is a separate
    # admin-only endpoint: an account that can list projects but not read storage
    # still gives a usable picker with quotas in it, and "usage unknown" beats an
    # empty form.
    raw = _raw_projects()
    try:
        repo_bytes = _repo_bytes()
        usage_known = True
    except ArtifactoryProbeError as exc:
        log.warning("Artifactory usage unavailable, quotas only: %s", exc)
        repo_bytes, usage_known = {}, False

    projects: List[Dict[str, Any]] = []
    for proj in raw:
        key = str(proj.get("project_key") or proj.get("projectKey") or "").strip()
        if not key:
            continue
        quota = int(proj.get("storage_quota_bytes") or proj.get("storageQuotaBytes") or 0)
        used = int(sum(v for k, v in repo_bytes.items() if k.startswith(f"{key}-")))
        unlimited = quota <= _UNLIMITED
        projects.append({
            "key": key,
            "name": str(proj.get("display_name") or proj.get("displayName") or key),
            "quota_bytes": quota,
            "used_bytes": used,
            "usage_known": usage_known,
            # "free" is only meaningful against a real quota AND a known usage. An
            # unlimited project has no number here rather than a made-up one.
            "free_bytes": None if (unlimited or not usage_known) else max(0, quota - used),
            "unlimited": unlimited,
            "percentage": (
                0.0 if (unlimited or quota <= 0 or not usage_known)
                else round(min(100.0, used / quota * 100), 1)
            ),
        })
    projects.sort(key=lambda p: p["used_bytes"], reverse=True)
    return projects


def project(key: str) -> Optional[Dict[str, Any]]:
    wanted = str(key or "").strip().lower()
    if not wanted:
        return None
    for proj in projects_with_usage():
        if proj["key"].lower() == wanted:
            return proj
    return None


def set_project_quota(key: str, quota_bytes: int) -> Dict[str, Any]:
    """Raise (or set) a project's storage quota, then READ IT BACK.

    The read-back is the point: a 200 from an Access API PUT means the request was
    accepted, not that the field holds the value now, and a quota nobody verified is
    exactly the kind of best-effort side effect that gets reported as done and isn't.
    """
    key = str(key or "").strip()
    quota_bytes = int(quota_bytes)
    if not key:
        raise ValueError("A project key is required.")
    if quota_bytes <= 0:
        raise ValueError("A quota must be greater than zero.")

    url = f"{_access_root(_base())}/access/api/v1/projects/{key}"
    try:
        body = _probe(url, f"project {key}") or {}
    except ArtifactoryProbeError as exc:
        if exc.status == 404:
            raise ValueError(f"Artifactory has no project '{key}'.") from exc
        raise

    before = int(body.get("storage_quota_bytes") or body.get("storageQuotaBytes") or 0)
    # PUT replaces the project, so send back what was read with only the quota
    # changed -- a partial body silently clears display_name, the admin privileges
    # and the rest of the project's configuration.
    body["storage_quota_bytes"] = quota_bytes
    body.pop("storageQuotaBytes", None)

    _call("PUT", url, f"quota write for {key}", json_body=body)

    after_body = _probe(url, f"quota read-back for {key}") or {}
    after = int(after_body.get("storage_quota_bytes") or 0)
    if after != quota_bytes:
        raise RuntimeError(
            f"Artifactory accepted the change for '{key}' but the quota reads "
            f"{format_bytes(after)}, not the requested {format_bytes(quota_bytes)}."
        )

    # The cached project list now holds the old quota. Dropped rather than left to
    # expire: the next thing anybody does after a quota change is look at the quota.
    try:
        from integrations_cache import invalidate_owner

        invalidate_owner("artifactory", "system")
    except Exception as exc:  # a stale cache must not fail a completed write
        log.warning("Artifactory cache not invalidated after quota write: %s", exc)

    return {
        "project_key": key,
        "quota_before_bytes": before,
        "quota_after_bytes": after,
        "quota_before": format_bytes(before) if before > 0 else "Unlimited",
        "quota_after": format_bytes(after),
        "verified": True,
    }


def repositories() -> List[Dict[str, Any]]:
    """Every repository key, for the cleaner's repository picker.

    Read with the admin account rather than the requester's token on purpose: the
    cleaner deletes from a repository the requester may not be able to list, and a
    picker that silently omits it sends them to type a key by hand -- which is the one
    field in a delete spec that must not be typed by hand.
    """
    raw = _probe(f"{_base()}/api/repositories", "repository list") or []
    out: List[Dict[str, Any]] = []
    for repo in raw:
        key = str(repo.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "type": str(repo.get("type") or ""),
            "package_type": str(repo.get("packageType") or ""),
        })
    out.sort(key=lambda r: r["key"].lower())
    return out


# ── Who may ask for what ─────────────────────────────────────────────────────

def _cached(suffix: str, producer, ttl: int = 300):
    """Remember an answer that changes rarely and costs a round trip to fetch.

    Project membership is read one project at a time -- JFrog Access has no endpoint
    that lists it in bulk -- so scoping a picker to twenty projects is forty sequential
    calls across a slow internal link, which is exactly the pause the quota form had before
    its list appeared. Membership changes a few times a year; a five-minute answer is
    indistinguishable from a live one and arrives immediately.

    Keyed on the thing, not the viewer: what is in a project is the same fact for
    everyone who asks.
    """
    from integrations_cache import cached_external

    return cached_external("artifactory", "system", suffix, producer, ttl=ttl)


def _members_of(key: str, kind: str) -> List[Dict[str, str]]:
    """One project's users, or its groups, from the Access API.

    Read with the admin account because a member cannot list the membership of their
    own project on this instance. Returns [] when the endpoint refuses -- the status
    is already in the log, and the caller decides what an empty list means.
    """
    return _cached(f"project-{kind}:{key}", lambda: _members_of_live(key, kind))


def _members_of_live(key: str, kind: str) -> List[Dict[str, str]]:
    url = f"{_access_root(_base())}/access/api/v1/projects/{key}/{kind}"
    try:
        raw = _probe(url, f"{kind} of {key}") or {}
    except ArtifactoryProbeError:
        return []
    members = raw.get("members") if isinstance(raw, dict) else raw
    out: List[Dict[str, str]] = []
    for member in members or []:
        if isinstance(member, str):
            out.append({"name": member, "email": ""})
            continue
        out.append({
            "name": str(member.get("name") or member.get("username") or ""),
            "email": str(member.get("email") or ""),
        })
    return out


def project_members(key: str) -> List[Dict[str, str]]:
    return _members_of(key, "users")


def project_groups(key: str) -> List[str]:
    return [m["name"].lower() for m in _members_of(key, "groups") if m["name"]]


def groups_of_user(identities: List[str]) -> List[str]:
    """The Artifactory groups this person is in, tried under each of their identities.

    Membership of a JFrog project is normally granted to a GROUP, not to a person, so
    a check that only looks at the project's user list finds nobody and concludes the
    requester owns nothing. Trying every identity matters for the same reason it does
    everywhere else here: the account exists under exactly one of them and there is no
    way to know which from the outside.
    """
    return _cached(
        "user-groups:" + ",".join(sorted(str(i or "") for i in identities)),
        lambda: _groups_of_user_live(identities),
    )


def _groups_of_user_live(identities: List[str]) -> List[str]:
    for identity in identities:
        name = str(identity or "").strip()
        if not name:
            continue
        try:
            raw = _probe(f"{_base()}/api/security/users/{name}", f"groups of {name}") or {}
        except ArtifactoryProbeError:
            continue
        groups = raw.get("groups") if isinstance(raw, dict) else None
        if groups:
            return [str(g).strip().lower() for g in groups if str(g or "").strip()]
    return []


def projects_for_user(identities: List[str]) -> Dict[str, Any]:
    """The projects this person belongs to, matched on any of their identities.

    Identities are every form the same person can appear under -- the portal
    username, the e-mail, and the e-mail's local part -- because Artifactory members
    are named by whichever of those the account was created with. One form is not
    enough; it either finds nobody or finds the wrong subject. Membership counts
    whether it was granted to them directly or to a group they are in.

    Returns ``{"projects", "scoped", "note", "total"}``. THE LIST IS NEVER NARROWED TO
    NOTHING: an empty picker is a form that cannot be filled in, and every one of the
    reasons it could be empty -- membership not readable, membership granted through
    something this cannot see, the person genuinely being in no project -- is better
    answered by showing everything with a line saying why. ``scoped`` is False in
    those cases, so the interface can say "these are all projects" rather than
    implying they are yours. The request still goes to an approver either way.
    """
    wanted = {str(i).strip().lower() for i in identities if str(i or "").strip()}
    everything = projects_with_usage()
    if not everything:
        # Nothing to scope. Whether that is "this instance has no projects" or "the
        # admin account cannot see them" is answered by the diagnostics, which record
        # the status alongside the count.
        return {"projects": [], "scoped": False, "note": "", "total": 0}
    if not wanted:
        return {"projects": everything, "scoped": False,
                "note": "Showing every project.", "total": len(everything)}

    my_groups = set(groups_of_user(list(identities)))
    mine: List[Dict[str, Any]] = []
    readable = 0
    for proj in everything:
        members = project_members(proj["key"])
        groups = project_groups(proj["key"])
        if not members and not groups:
            continue
        readable += 1
        if any(m["name"].lower() in wanted or m["email"].lower() in wanted for m in members):
            mine.append(proj)
        elif my_groups.intersection(groups):
            mine.append(proj)

    if readable == 0:
        return {
            "projects": everything, "scoped": False, "total": len(everything),
            "note": "Your project membership could not be read, so every project is listed.",
        }
    if not mine:
        return {
            "projects": everything, "scoped": False, "total": len(everything),
            "note": (
                "You are not listed as a member of any Artifactory project, so every "
                "project is shown. Pick the one the request is for — the approver sees "
                "which project it was."
            ),
        }
    return {"projects": mine, "scoped": True, "note": "", "total": len(everything)}


# ── The whole estate, for the approver ───────────────────────────────────────

def unlimited_project_keys() -> List[str]:
    """Projects deliberately left without a quota, excluded from the rollup.

    DevOps is the one that matters here: it is unlimited on purpose, so counting it
    would make the committed total meaningless.
    """
    raw = os.getenv("ARTIFACTORY_UNLIMITED_PROJECTS", "DevOps")
    return [p.strip() for p in raw.split(",") if p.strip()]


def disk_total_bytes(fallback: int = 0) -> int:
    """Artifactory's real capacity, from ARTIFACTORY_DISK_TOTAL.

    Parsed by the SAME reader the storage widget uses, imported rather than copied:
    that one refuses a value with no unit, which is what stops an unexpanded pipeline
    macro or a bare "6" from being read as six bytes and drawing a bar against
    nonsense. Imported inside the function because api.integrations is a router module
    the api package loads at start-up.

    ``fallback`` is the size Artifactory reports for its own filestore, used when the
    variable is unset -- a number the server measured beats one nobody has revisited
    since it was typed.
    """
    from api.integrations import _parse_admin_size

    configured = int(_parse_admin_size((os.getenv("ARTIFACTORY_DISK_TOTAL") or "").strip()) or 0)
    return configured or int(fallback or 0)


def quota_rollup(exclude: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every project's quota added up, against the disk it all has to fit on.

    This is the number an approver needs and the one nobody has: each project's quota
    looks reasonable on its own, and the question at approval time is whether the SUM
    of what has been promised still fits. Projects with no quota are excluded -- an
    unlimited project cannot be added to a total.
    """
    skip = {p.lower() for p in (exclude if exclude is not None else unlimited_project_keys())}
    projects = projects_with_usage()
    counted = [p for p in projects if not p["unlimited"] and p["key"].lower() not in skip]
    excluded = [p for p in projects if p["unlimited"] or p["key"].lower() in skip]

    # Physical disk, read from Artifactory itself. This is the only number that
    # belongs on the same axis as the disk size -- the per-project figures are
    # logical, and comparing those to a disk says an estate is 120% full when it is
    # half empty.
    try:
        store = _storage()
        physical_used = store["physical_used_bytes"]
        physical_total = store["physical_total_bytes"]
    except ArtifactoryProbeError as exc:
        log.warning("Artifactory filestore unreadable: %s", exc)
        physical_used = physical_total = 0

    committed = sum(p["quota_bytes"] for p in counted)
    used = sum(p["used_bytes"] for p in counted)
    disk = disk_total_bytes(physical_total)
    return {
        "committed_bytes": committed,
        "committed": format_bytes(committed),
        # Logical: the sum of what the counted projects hold. Kept because it is what
        # the quotas themselves are measured against.
        "used_bytes": used,
        "used": format_bytes(used),
        "usage_known": all(p.get("usage_known", True) for p in counted),
        # Physical: what is actually on the disk, after de-duplication.
        "physical_used_bytes": physical_used,
        "physical_used": format_bytes(physical_used) if physical_used else "",
        "physical_known": physical_used > 0,
        "disk_total_bytes": disk,
        "disk_total": format_bytes(disk) if disk > 0 else "",
        "disk_known": disk > 0,
        "disk_source": (
            "ARTIFACTORY_DISK_TOTAL"
            if (os.getenv("ARTIFACTORY_DISK_TOTAL") or "").strip() else "Artifactory filestore"
        ),
        "counted": len(counted),
        "excluded": [p["key"] for p in excluded],
        "percentage": round(min(100.0, committed / disk * 100), 1) if disk > 0 else 0.0,
    }


def diagnose() -> Dict[str, Any]:
    """Try each call this feature needs and report what happened to each one.

    Exists because every failure here is invisible from the outside: the picker is
    empty either way, and "no projects", "not an admin", "wrong credential shape" and
    "wrong URL" need four different people to fix them. This is the read-only version
    an operator can run before anyone has to read a pod log.
    """
    global _working_shape

    base = _base()
    if not is_configured():
        return {
            "configured": False,
            "account": account(),
            "source": credential_source(),
            "auth_shape": "",
            "base_url": base,
            "checks": [],
            "summary": (
                "No credentials. Set ARTIFACTORY_ADMIN_USERNAME with "
                "ARTIFACTORY_ADMIN_PASSWORD (preferred), or ARTIFACTORY_BACKUP_USERNAME"
                "/TOKEN, on the backend."
            ),
        }

    # Start from nothing: the point of a diagnostic is to find out what works NOW, not
    # to confirm what happened to work earlier.
    _working_shape = None

    checks = [
        ("project list", f"{_access_root(base)}/access/api/v1/projects", "access"),
        ("storage usage", f"{base}/api/storageinfo", "artifactory"),
        ("repository list", f"{base}/api/repositories", "artifactory"),
    ]
    results = []
    for label, url, service in checks:
        try:
            payload = _probe(url, label)
            results.append({
                "check": label, "url": url, "service": service, "status": 200,
                "ok": True, "auth": _working_shape or "", "detail": "",
                # A 200 that returns nothing is its own failure, and it is the one an
                # empty picker actually looks like. Recording only the status made
                # "this account sees no projects" indistinguishable from success.
                "count": _count_of(payload),
            })
        except ArtifactoryProbeError as exc:
            results.append({
                "check": label, "url": url, "service": service, "status": exc.status,
                "ok": False, "auth": "", "detail": str(exc), "count": None,
            })

    failed = [r for r in results if not r["ok"]]
    projects = next((r for r in results if r["check"] == "project list"), None)
    artifactory_ok = all(r["ok"] for r in results if r["service"] == "artifactory")
    access_401 = [r for r in results if r["service"] == "access" and r["status"] == 401]

    if not failed and projects and projects["count"] == 0:
        # The case the user hits after fixing the credential: everything answers, and
        # the picker is still empty. Nothing above this line is wrong, so nothing above
        # this line can explain it -- and "no projects" and "no permission" are two
        # different people's jobs.
        summary = (
            f"Every call succeeds, but the Projects API returned no projects at all "
            f"for {account()}. Either this Artifactory has no JFrog Projects defined, "
            "or that account is not an administrator (a non-admin sees an empty list "
            "here rather than a refusal). Check Administration > Projects in the "
            "Artifactory UI while signed in as this account."
        )
    elif not failed:
        summary = (
            f"{account()} can read everything this needs ({auth_shape()} auth): "
            f"{projects['count'] if projects else '?'} project(s)."
        )
    elif access_401 and artifactory_ok:
        # The single most useful thing this function can say. Artifactory accepted the
        # credential and Access refused it, which is not a password problem -- Access
        # wants a token from an account it recognises.
        summary = (
            f"Artifactory accepts {account()}, but JFrog Access (the Projects API) "
            "answers 401 to the same credential. Access needs an admin ACCESS TOKEN: "
            "generate one in the Artifactory UI (Edit Profile / Generate Token) and "
            "set it as ARTIFACTORY_ADMIN_TOKEN."
        )
    elif all(r["status"] == 403 for r in failed):
        summary = (
            f"{account()} is authenticated but not an Artifactory administrator. "
            "Grant it admin, or point ARTIFACTORY_ADMIN_USERNAME at an account that is."
        )
    elif all(r["status"] == 401 for r in failed):
        summary = (
            f"Artifactory rejected {account()} everywhere (401). Check the value in "
            f"{credential_source()} -- an expired, mistyped or unexpanded secret looks "
            "exactly like this."
        )
    else:
        summary = "; ".join(r["detail"] for r in failed)

    return {
        "configured": True,
        "account": account(),
        "source": credential_source(),
        "auth_shape": _working_shape or "",
        "base_url": base,
        "checks": results,
        "summary": summary,
    }
