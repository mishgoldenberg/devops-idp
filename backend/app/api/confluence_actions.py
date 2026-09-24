"""
Confluence actions the portal performs on the signed-in person's behalf.

  POST /api/confluence/actions/page-create   create a page in a space (optionally under a parent)
  POST /api/confluence/actions/page-append   add a section to the end of an existing page

Built exactly like the Azure DevOps actions (api/ado_actions.py), and driven by the same
dialog (partials/components/ado-actions.html):

AS THE PERSON, NEVER AS THE PORTAL -- every call uses the person's own Confluence token,
so the page is created by them, with their permissions, and Confluence says so.

CHECKED TWICE -- the dialog first calls with ``dry_run``: the space exists, the title is
free, the parent exists; the page to extend exists and its current version is read. The
person reads exactly what will happen and confirms. Only then does the write run, and
its result is READ BACK before the portal says it happened.

APPENDED, NEVER REPLACED -- adding to a page puts a new section after what is there;
nothing already on the page is touched. A page changed by someone between the check and
the write is refused (Confluence's own version check), never overwritten.

The content arrives as Markdown and is converted to Confluence's storage format
(devbot/markup.py), escaped on the way. Safe Mode is re-checked inside every action,
right before the write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import safe_mode
from resilient_http import tls_verify
from security import AuthUser, get_current_user

from devbot.markup import markdown_to_storage

from .integrations import _base_url, _require_token

log = logging.getLogger(__name__)
router = APIRouter()


class _Action(BaseModel):
    dry_run: bool = False


class CreateBody(_Action):
    space: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1, max_length=60000)
    parent_id: str = Field("", max_length=32)


class AppendBody(_Action):
    page_id: str = Field(..., min_length=1, max_length=32)
    heading: str = Field("", max_length=255)
    content: str = Field(..., min_length=1, max_length=60000)


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        verify=tls_verify(),
        timeout=httpx.Timeout(20.0, connect=5.0),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )


def _fail(resp: httpx.Response, what: str) -> HTTPException:
    code = resp.status_code
    if code == 401:
        return HTTPException(status_code=401, detail="Confluence did not accept your token. It may have expired: "
                                                     "reconnect it on the Connections page.")
    if code == 403:
        return HTTPException(status_code=403, detail=f"Confluence does not allow you to {what}. Ask a space admin "
                                                     "for the permission, or do it in Confluence.")
    if code == 404:
        return HTTPException(status_code=404, detail="Confluence could not find it. It may have been deleted, or "
                                                     "you may not see it.")
    if code == 409:
        return HTTPException(status_code=409, detail="Someone changed the page in the meantime. Open it, check the "
                                                     "change, and try again.")
    try:
        message = str(resp.json().get("message") or "")[:300]
    except ValueError:
        message = (resp.text or "")[:300]
    return HTTPException(status_code=502, detail=f"Confluence answered {code}: {message or 'no details'}")


def _json(resp: httpx.Response) -> Dict[str, Any]:
    if "html" in (resp.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=502, detail="Confluence answered with a web page instead of data, which usually "
                                                    "means it did not accept your token. Reconnect it on Connections.")
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Confluence answered with something that is not JSON.")
    return body if isinstance(body, dict) else {}


def _page_url(base: str, item: Dict[str, Any]) -> str:
    webui = str((item.get("_links") or {}).get("webui") or "")
    if webui.startswith(("http://", "https://")):
        return webui
    return base.rstrip("/") + (webui if webui.startswith("/") else f"/pages/viewpage.action?pageId={item.get('id')}")


def _dry(summary: str, checks: List[str], **extra: Any) -> Dict[str, Any]:
    return {"success": True, "dry_run": True, "summary": summary, "checks": checks, **extra}


def _simulated(summary: str, **extra: Any) -> Dict[str, Any]:
    return {"success": True, "simulated": True, "summary": summary,
            "result": "Safe Mode is on: nothing was sent to Confluence.", **extra}


@router.post("/page-create")
def page_create(body: CreateBody, current_user: AuthUser = Depends(get_current_user)):
    token = _require_token(current_user, "confluence")
    base = _base_url("confluence")
    space = body.space.strip()
    title = " ".join(body.title.split())
    with _client(token) as client:
        got = client.get(f"{base}/rest/api/space/{space}")
        if got.status_code == 404:
            raise HTTPException(status_code=409, detail=f"There is no Confluence space with the key '{space}'.")
        if got.status_code != 200:
            raise _fail(got, "read that space")
        space_name = str(_json(got).get("name") or space)
        same = client.get(f"{base}/rest/api/content", params={"spaceKey": space, "title": title, "type": "page"})
        if same.status_code == 200 and (_json(same).get("results") or []):
            existing = _json(same)["results"][0]
            raise HTTPException(status_code=409, detail=f"A page called “{title}” already exists in {space_name}: "
                                                        f"{_page_url(base, existing)}. Pick another title, or add to that page instead.")
        parent_title = ""
        if body.parent_id.strip():
            parent = client.get(f"{base}/rest/api/content/{body.parent_id.strip()}")
            if parent.status_code != 200:
                raise _fail(parent, "read the parent page")
            parent_title = str(_json(parent).get("title") or body.parent_id)
        summary = f"Create the page “{title}” in {space_name}" + (f", under “{parent_title}”" if parent_title else "")
        checks = [f"The space {space_name} exists.", f"No page there is called “{title}” yet.",
                  "The content is converted to Confluence formatting; code blocks stay code blocks."]
        if body.dry_run:
            return _dry(summary, checks)
        if safe_mode.is_enabled():
            return _simulated(summary)
        payload: Dict[str, Any] = {
            "type": "page", "title": title, "space": {"key": space},
            "body": {"storage": {"value": markdown_to_storage(body.content), "representation": "storage"}},
        }
        if body.parent_id.strip():
            payload["ancestors"] = [{"id": body.parent_id.strip()}]
        made = client.post(f"{base}/rest/api/content", json=payload)
        if made.status_code >= 300:
            raise _fail(made, "create pages in this space")
        created = _json(made)
        back = client.get(f"{base}/rest/api/content/{created.get('id')}")
        if back.status_code != 200 or str(_json(back).get("title") or "") != title:
            raise HTTPException(status_code=502, detail="Confluence accepted the page but it cannot be read back. Check the space there.")
        url = _page_url(base, _json(back))
    return {"success": True, "summary": summary, "result": f"The page “{title}” is in {space_name}.", "url": url}


@router.post("/page-append")
def page_append(body: AppendBody, current_user: AuthUser = Depends(get_current_user)):
    token = _require_token(current_user, "confluence")
    base = _base_url("confluence")
    page_id = body.page_id.strip()
    with _client(token) as client:
        got = client.get(f"{base}/rest/api/content/{page_id}", params={"expand": "body.storage,version,space"})
        if got.status_code != 200:
            raise _fail(got, "read this page")
        page = _json(got)
        title = str(page.get("title") or page_id)
        version = int((page.get("version") or {}).get("number") or 0)
        current = str(((page.get("body") or {}).get("storage") or {}).get("value") or "")
        section = (f"## {body.heading.strip()}\n\n" if body.heading.strip() else "") + body.content
        summary = f"Add a section to the end of “{title}”"
        checks = [f"The page exists (version {version}).",
                  "Everything already on the page stays exactly as it is; the section goes at the end.",
                  "If someone changes the page before you confirm, nothing is written."]
        url = _page_url(base, page)
        if body.dry_run:
            return _dry(summary, checks, url=url)
        if safe_mode.is_enabled():
            return _simulated(summary, url=url)
        addition = markdown_to_storage(section)
        put = client.put(f"{base}/rest/api/content/{page_id}", json={
            "id": page_id, "type": "page", "title": title,
            "version": {"number": version + 1, "message": "Added from DevOps Hub"},
            "body": {"storage": {"value": current + addition, "representation": "storage"}},
        })
        if put.status_code >= 300:
            raise _fail(put, "edit this page")
        back = client.get(f"{base}/rest/api/content/{page_id}", params={"expand": "version"})
        new_version = int((_json(back).get("version") or {}).get("number") or 0) if back.status_code == 200 else 0
        if new_version <= version:
            raise HTTPException(status_code=502, detail="Confluence did not keep the new section. Check the page there.")
    return {"success": True, "summary": summary, "result": f"“{title}” now ends with your section (version {new_version}).", "url": url}
