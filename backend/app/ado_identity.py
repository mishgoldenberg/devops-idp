"""
Identity and permission writes for Azure DevOps Server, on the endpoints this
server actually has.

WHY THIS MODULE EXISTS
──────────────────────
Three rounds of provisioning grants failed silently, and the reason turned out to be
in a comment in scripts/ado_security_migration.py, written against this same server:

    the Graph API (/_apis/graph/...) is not routed on this on-prem 25H2 server;
    /_apis/resourceAreas is empty

So every grant path built on ``_apis/graph/users``, ``_apis/graph/groups``,
``_apis/graph/memberships`` or ``_apis/graph/descriptors`` was returning 404 — and
because a grant is best-effort, a 404 looked exactly like "user not found", which
looked exactly like nothing being wrong. The project got created, the admin PAT's
owner was made a project admin by Azure DevOps itself, and the requested user got
nothing.

WHAT REPLACES IT
────────────────
The legacy ``/_api/_identity/`` endpoints — the ones the Azure DevOps web UI itself
calls, and the ones the working migration scripts used on this server:

    GET  {collection}/{project}/_api/_identity/ReadScopedApplicationGroupsJson?__v=5
    GET  {collection}/{project}/_api/_identity/ReadGroupMembers?__v=5&scope=<tfid>&readMembers=true
    POST {collection}/{project}/_api/_identity/AddIdentities?__v=5

``AddIdentities`` is the important one: given a ``DOMAIN\\user`` in ``newUsersJson`` it
BINDS that Active Directory account into the collection and adds it to the group in
``groupsToJoinJson`` — one call that both materialises the identity and grants the
membership. That is precisely the operation every previous attempt was trying to
assemble out of Graph calls that do not exist here.

Permissions (the process ACL) go through ``_apis/accesscontrollists``, with the
``acesDictionary`` payload shape from the migration script — NOT
``_apis/accesscontrolentries``, which is a different endpoint with a different body.

Everything here is best-effort and returns a status string plus a detail; nothing
raises into the provisioning flow.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# The legacy endpoints are the web UI's own, so they want to look like the web UI.
# X-TFS-FedAuthRedirect stops the server answering a sign-in page with HTTP 200,
# which is how these calls fail confusingly when auth is not accepted.
_LEGACY_HEADERS = {
    "Accept": "application/json",
    "X-TFS-FedAuthRedirect": "Suppress",
    "X-Requested-With": "XMLHttpRequest",
}

_GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _legacy_base(base_url: str, project: str) -> str:
    return f"{base_url}/{quote(project, safe='')}/_api/_identity"


def bare_group_name(identity: Dict[str, Any]) -> str:
    """Project-independent group name.

    The legacy API reports a project group as ``[MyProject]\\Project Administrators``
    in DisplayName and ``Project Administrators`` in FriendlyDisplayName, and which
    fields are populated varies. Strip the bracketed project so a caller can match on
    the name alone.
    """
    name = identity.get("FriendlyDisplayName") or identity.get("DisplayName") or ""
    if name.startswith("[") and "\\" in name:
        name = name.split("\\", 1)[1]
    return name.strip()


def read_scoped_groups(
    client: httpx.Client, base_url: str, project: str
) -> Tuple[List[Dict[str, Any]], str]:
    """Application groups of one project. Returns ``(groups, status)``.

    Two endpoint spellings exist across Server versions; both are tried before
    concluding anything, because a 404 on one of them is not an answer.
    """
    last = ""
    for endpoint in ("ReadScopedApplicationGroupsJson", "ReadScopedGroupsJson"):
        url = f"{_legacy_base(base_url, project)}/{endpoint}?__v=5"
        try:
            r = client.get(url, headers=_LEGACY_HEADERS)
        except Exception as exc:
            last = f"{endpoint}:{type(exc).__name__}"
            continue
        if not r.is_success:
            last = f"{endpoint}:HTTP {r.status_code}"
            continue
        try:
            body = r.json()
        except Exception:
            # A sign-in page answered with 200 is HTML, not JSON — a real and
            # otherwise very confusing failure mode for these endpoints.
            last = f"{endpoint}:non-JSON response (auth?)"
            continue
        groups = body.get("identities") or []
        return groups, f"{endpoint}:{len(groups)} groups"
    log.warning("legacy group read failed for %s: %s", project, last)
    return [], last


def find_group(groups: List[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
    """The group whose project-independent name matches ``wanted``, case-insensitively."""
    target = wanted.strip().lower()
    for group in groups:
        if bare_group_name(group).lower() == target:
            return group
    return None


def read_group_members(
    client: httpx.Client, base_url: str, project: str, group_tfid: str
) -> List[Dict[str, Any]]:
    """Direct members of a group. Best-effort: [] on any failure."""
    url = (
        f"{_legacy_base(base_url, project)}/ReadGroupMembers"
        f"?__v=5&scope={quote(group_tfid, safe='')}&readMembers=true"
    )
    try:
        r = client.get(url, headers=_LEGACY_HEADERS)
        if not r.is_success:
            return []
        return (r.json() or {}).get("identities") or []
    except Exception:
        return []


def _verification_token(client: httpx.Client, base_url: str, project: str) -> str:
    """Scrape the anti-forgery token the legacy POST may demand.

    These endpoints are the web UI's, so on some configurations they enforce the
    ASP.NET request-verification token even for a token-authenticated caller. Fetching
    an admin page both yields the field value and sets the matching cookie on the
    shared client, which is the pair the server checks.
    """
    for path in (f"/{quote(project, safe='')}/_admin/_security", "/_admin/_security", "/"):
        try:
            r = client.get(f"{base_url}{path}", headers={"X-TFS-FedAuthRedirect": "Suppress"})
        except Exception:
            continue
        if not r.is_success:
            continue
        m = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text
        ) or re.search(
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', r.text
        )
        if m:
            return m.group(1)
    return ""


def add_identities(
    client: httpx.Client,
    base_url: str,
    project: str,
    group_tfid: str,
    new_users: Optional[List[str]] = None,
    existing_ids: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Add members to a group — binding AD accounts into the collection as needed.

    ``new_users`` are names to resolve against the identity providers (``DOMAIN\\user``
    is the form an AD-backed server resolves). ``existing_ids`` are TeamFoundationIds of
    identities the collection already knows.

    This is the call that makes a never-signed-in domain user real here: the server
    resolves the name against AD, creates the collection identity, and joins the group,
    in one request.
    """
    payload = {
        "newUsersJson": json.dumps(new_users or []),
        "existingUsersJson": json.dumps(existing_ids or []),
        "groupsToJoinJson": json.dumps([group_tfid]),
    }
    url = f"{_legacy_base(base_url, project)}/AddIdentities?__v=5"

    def _post(extra: Optional[Dict[str, str]] = None) -> httpx.Response:
        data = dict(payload)
        if extra:
            data.update(extra)
        return client.post(
            url,
            data=data,
            headers={
                **_LEGACY_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )

    attempts: List[str] = []
    try:
        r = _post()
    except Exception as exc:
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    # Retry once with the anti-forgery token if the first attempt was refused.
    if r.status_code in (400, 403) or not r.is_success:
        attempts.append(f"no-token:HTTP {r.status_code} {r.text[:100]}")
        token = _verification_token(client, base_url, project)
        if token:
            try:
                r = _post({"__RequestVerificationToken": token})
            except Exception as exc:
                return {
                    "status": "failed",
                    "detail": "; ".join(attempts + [f"with-token:{type(exc).__name__}: {exc}"]),
                }
            attempts.append(f"with-token:HTTP {r.status_code}")
        else:
            attempts.append("no __RequestVerificationToken found on any page")

    if not r.is_success:
        return {"status": "failed", "detail": "; ".join(attempts) or f"HTTP {r.status_code} {r.text[:150]}"}

    # A 200 is not success on its own: this endpoint reports failure in the body.
    try:
        body = r.json()
    except Exception:
        return {
            "status": "failed",
            "detail": "; ".join(attempts + ["200 but non-JSON response (auth redirect?)"]),
        }
    if body.get("HasErrors") or body.get("hasErrors"):
        message = (
            body.get("ErrorMessage")
            or body.get("errorMessage")
            or json.dumps(body)[:200]
        )
        return {"status": "failed", "detail": f"server reported HasErrors: {message}"}

    return {"status": "granted", "detail": "; ".join(attempts + ["AddIdentities OK"]) }


def find_member(
    members: List[Dict[str, Any]], principal: str
) -> Optional[Dict[str, Any]]:
    """The member matching ``principal`` by account, domain\\account, or mail."""
    want = principal.strip().lower()
    bare = want.split("\\")[-1].split("@")[0]
    for member in members:
        domain = (member.get("Domain") or "").strip()
        account = (member.get("AccountName") or "").strip()
        candidates = {
            account.lower(),
            f"{domain}\\{account}".lower() if domain and account else "",
            (member.get("MailAddress") or "").strip().lower(),
            (member.get("SignInAddress") or "").strip().lower(),
        }
        candidates.discard("")
        if want in candidates or bare == account.lower():
            return member
    return None


def descriptor_for_tfid(
    client: httpx.Client, base_url: str, team_foundation_id: str
) -> str:
    """Legacy identity descriptor for a TeamFoundationId, via ``_apis/identities``.

    ACLs are keyed by descriptor (``Microsoft.TeamFoundation.Identity;S-1-9-…``) while
    the legacy identity endpoints speak TeamFoundationIds, so one translation is needed
    between adding someone to a group and giving them a permission. ``_apis/identities``
    is a normal REST route and is unaffected by Graph being absent.
    """
    for api in ("6.0", "7.0", "5.0", "1.0"):
        try:
            r = client.get(
                f"{base_url}/_apis/identities"
                f"?identityIds={quote(team_foundation_id, safe='')}&api-version={api}"
            )
        except Exception:
            continue
        if not r.is_success:
            continue
        values = (r.json() or {}).get("value") or []
        if values:
            return values[0].get("descriptor") or ""
    return ""


def discover_acl_token(
    client: httpx.Client, base_url: str, namespace_id: str, object_id: str
) -> Tuple[str, str]:
    """Work out the real security token for ``object_id`` in a namespace.

    The token format for the Process namespace is the one thing that could not be
    confirmed from documentation, and guessing it is what left the process grant
    failing. So instead of guessing: read the tokens the server ALREADY uses in that
    namespace and copy their shape, swapping in our object's GUID — the same trick the
    migration script uses when rewriting tokens between projects.

    Returns ``(token, how)`` where ``how`` is "existing", "inferred" or "default".
    """
    tokens: List[str] = []
    for api in ("6.0", "7.0", "5.0"):
        try:
            r = client.get(
                f"{base_url}/_apis/accesscontrollists/{namespace_id}"
                f"?api-version={api}&recurse=True"
            )
        except Exception:
            continue
        if not r.is_success:
            continue
        tokens = [
            str(acl.get("token") or "") for acl in ((r.json() or {}).get("value") or [])
        ]
        break

    # Already scoped to this object — nothing to infer.
    for token in tokens:
        if object_id.lower() in token.lower():
            return token, "existing"

    # Same shape, our GUID. Prefer the shortest matching token so we copy the most
    # specific-but-simple form rather than a deeply nested one.
    with_guid = sorted((t for t in tokens if _GUID.search(t)), key=len)
    if with_guid:
        template = with_guid[0]
        match = _GUID.search(template)
        if match:
            return template[: match.start()] + object_id + template[match.end() :], "inferred"

    return f"$PROCESS:{object_id}:", "default"


def set_acl(
    client: httpx.Client,
    base_url: str,
    namespace_id: str,
    token: str,
    descriptor: str,
    allow: int,
) -> Dict[str, str]:
    """Write one allow-ACE, using the payload shape that works on this server.

    ``_apis/accesscontrollists`` with an ``acesDictionary`` — not
    ``_apis/accesscontrolentries`` with an ``accessControlEntries`` list, which is a
    different endpoint that earlier rounds used.
    """
    body = {
        "value": [
            {
                "inheritPermissions": True,
                "token": token,
                "acesDictionary": {
                    descriptor: {
                        "descriptor": descriptor,
                        "allow": allow,
                        "deny": 0,
                    }
                },
            }
        ]
    }
    tried: List[str] = []
    for api in ("6.0", "7.0", "5.0"):
        try:
            r = client.post(
                f"{base_url}/_apis/accesscontrollists/{namespace_id}?api-version={api}",
                json=body,
            )
        except Exception as exc:
            tried.append(f"{api}:{type(exc).__name__}")
            continue
        if r.status_code in (200, 201, 204):
            return {"status": "granted", "detail": f"accesscontrollists@{api} token={token}"}
        tried.append(f"{api}:HTTP {r.status_code} {r.text[:100]}")
        if r.status_code in (401, 403):
            break
    return {"status": "failed", "detail": " | ".join(tried)}
