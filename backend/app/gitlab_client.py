"""
GitLab, read and written with the signed-in person's OWN token.

One self-managed instance, at GITLAB_BASE_URL (the web address; the REST API is under
/api/v4 of it). Each person connects a Personal Access Token on the Connections page;
it is stored with every other token, encrypted (api/integrations.py, system "gitlab"),
and sent only as the PRIVATE-TOKEN header -- never in a URL, never to the browser.

  read_api   everything the widgets, Needs You, search and the streak read
  api        the actions: approve, comment, retry, run (api/gitlab_actions.py)

WHERE AZURE DEVOPS HAS A COLLECTION, A PROJECT AND A REPOSITORY, GITLAB HAS A GROUP AND
A PROJECT. The widgets are the Azure DevOps widgets with a provider setting, so the row
shapes here are theirs: a merge request's ``project`` is its top-level group and its
``repository`` is the GitLab project, which is where the code lives.

Which edition the server runs is never asked: Free and Premium answer the same
approvals endpoint, and only Premium fills in ``approvals_required``. Whatever the
answer carries is what the Hub shows.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status

import resilient_http

log = logging.getLogger(__name__)

SYSTEM = "gitlab"


def base_url() -> str:
    """The instance's web address, or "" when this Hub has none.

    Tolerates the forms people paste: no scheme, a trailing slash, the API path, and
    an Azure DevOps macro nobody defined."""
    raw = (os.getenv("GITLAB_BASE_URL") or "").strip()
    if not raw or raw.startswith("$("):
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    raw = raw.rstrip("/")
    for suffix in ("/api/v4", "/api"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw


def configured() -> bool:
    return bool(base_url())


def require_base() -> str:
    url = base_url()
    if not url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="GitLab is not set up on this Hub (GITLAB_BASE_URL). Ask a platform admin.")
    return url


NOT_CONNECTED = "GitLab is not connected. Please add your token."


def user_token(user: Dict[str, Any]) -> str:
    """The person's own token, or "" -- never the portal's: there is none."""
    from api.integrations import _get_token

    uid = str(user.get("id") or "").strip()
    if not uid:
        return ""
    try:
        return _get_token(uid, SYSTEM) or ""
    except Exception as exc:
        log.warning("gitlab: could not read the token of %s: %s", uid, exc)
        return ""


def require_token(user: Dict[str, Any]) -> str:
    token = user_token(user)
    if not token:
        raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail=NOT_CONNECTED)
    return token


def client(token: str, read: float = 20.0) -> httpx.Client:
    """A client on the API root, carrying the token as GitLab expects it."""
    return resilient_http.Client(
        base_url=require_base() + "/api/v4",
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},

        timeout=httpx.Timeout(read, connect=5.0),
    )


def owner(user: Dict[str, Any]) -> str:
    """The cache owner, as the other integrations key it."""
    return str(user.get("email") or user.get("username") or user.get("id") or "").lower()


def pid(value: Any) -> str:
    """A project or group reference for a URL path: an id as it is, a path encoded."""
    text = str(value or "").strip()
    return text if text.isdigit() else quote(text, safe="")


def describe(code: int) -> str:
    """A GitLab status in words that say whose problem it is."""
    if code == 401:
        return "GitLab did not accept your token (HTTP 401). It may have expired or been revoked: reconnect it on the Connections page."
    if code == 403:
        return "GitLab refused (HTTP 403): your token lacks the scope, or your account lacks access."
    if code == 404:
        return "GitLab could not find it (HTTP 404). It may have been deleted, or your account cannot see it."
    if code == 429:
        return "GitLab is limiting requests right now (HTTP 429). Try again in a minute."
    return f"GitLab answered HTTP {code}."


def json_of(resp: httpx.Response) -> Any:
    """The JSON body -- or a refusal when GitLab answered with a web page (a sign-in
    page is what a proxy or an expired session hands back), which a 200 does not show."""
    if "html" in (resp.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=502, detail="GitLab answered with a web page instead of data. "
                                                    "Check GITLAB_BASE_URL, or reconnect your token.")
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="GitLab answered with something that is not JSON.")


def refusal(resp: httpx.Response) -> HTTPException:
    """GitLab's refusal as the Hub's answer. Never a 401: the Hub reads a 401 as ITS OWN
    session ending and signs the person out, for a GitLab token that merely expired.
    424 (the far system refused) carries GitLab's 401 and 403 instead."""
    code = resp.status_code
    if code in (401, 403):
        return HTTPException(status_code=424, detail=describe(code))
    if code == 404:
        return HTTPException(status_code=404, detail=describe(code))
    return HTTPException(status_code=502, detail=describe(code))


def get(cl: httpx.Client, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = cl.get(path, params=params)
    if resp.status_code != 200:
        raise refusal(resp)
    return json_of(resp)


def paged(cl: httpx.Client, path: str, params: Optional[Dict[str, Any]] = None, *, limit: int = 300) -> List[Dict[str, Any]]:
    """Every page of a list, up to ``limit`` rows, following GitLab's X-Next-Page."""
    out: List[Dict[str, Any]] = []
    query = {"per_page": 100, **(params or {})}
    page = 1
    while len(out) < limit:
        resp = cl.get(path, params={**query, "page": page})
        if resp.status_code != 200:
            if out:
                break
            raise refusal(resp)
        rows = json_of(resp)
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        nxt = (resp.headers.get("x-next-page") or "").strip()
        if not nxt.isdigit():
            break
        page = int(nxt)
    return out[:limit]


def me(cl: httpx.Client) -> Dict[str, Any]:
    """Who the token belongs to. GitLab says it plainly, so "which of these are mine"
    is answered by id, never by matching names across two systems."""
    data = get(cl, "/user")
    return {"id": data.get("id"), "username": str(data.get("username") or ""),
            "name": str(data.get("name") or ""), "email": str(data.get("email") or data.get("public_email") or "")}


def test_token(token: str) -> Dict[str, Any]:
    """The check Connections makes before saving a token: the same /user call every
    reader starts from, so "accepted here" and "the widgets work" cannot disagree."""
    with client(token, read=8.0) as cl:
        resp = cl.get("/user")
        if resp.status_code in (401, 403):
            raise HTTPException(status_code=401, detail=f"GitLab rejected the token (HTTP {resp.status_code}). "
                                                        "It needs the read_api scope at least, and api for the actions.")
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=describe(resp.status_code))
        data = json_of(resp)
        if not isinstance(data, dict) or not data.get("id"):
            raise HTTPException(status_code=400, detail="GitLab did not say who this token belongs to.")
        return {"username": data.get("username"), "name": data.get("name")}


# ── shapes the widgets share with Azure DevOps ──────────────────────────────

def group_of(path: str) -> str:
    """The top-level group of ``group/sub/project``."""
    return str(path or "").split("/", 1)[0]


def project_path_of(mr: Dict[str, Any]) -> str:
    """``group/sub/project`` of a merge request, from what every version returns."""
    full = str(((mr.get("references") or {}).get("full")) or "")
    if "!" in full:
        return full.rsplit("!", 1)[0]
    url = str(mr.get("web_url") or "")
    if "/-/merge_requests/" in url:
        return url.split("://", 1)[-1].split("/", 1)[-1].split("/-/merge_requests/", 1)[0]
    return ""


def run_status(raw: Any) -> str:
    """GitLab's pipeline status as the one word the pipelines widget colours."""
    value = str(raw or "").lower()
    return {
        "success": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
        "skipped": "canceled",
        "running": "running",
        "pending": "pending",
        "created": "pending",
        "waiting_for_resource": "pending",
        "preparing": "pending",
        "scheduled": "pending",
        "manual": "pending",
        "waiting_for_callback": "pending",
    }.get(value, value or "unknown")


FINISHED = {"success", "failed", "canceled", "skipped"}


def short(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def unique(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return seen
