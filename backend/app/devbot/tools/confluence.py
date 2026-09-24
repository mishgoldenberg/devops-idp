"""
Confluence (Server / Data Center), read with the person's own personal access token.

Pages are handed to the model as TEXT, not storage-format HTML: the markup would cost
several times the tokens and says nothing the text does not. Code blocks survive as
fenced code, because a fix written on a page is usually a command or a snippet, and
that is exactly the part worth quoting back.

Every value a person gives reaches CQL inside a quoted literal with its backslashes
and quotes escaped.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from resilient_http import tls_verify

from .base import Tool, ToolContext, ToolFailure, register


def cql_literal(value: str) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _client(ctx: ToolContext) -> httpx.Client:
    return httpx.Client(
        verify=tls_verify(),
        timeout=httpx.Timeout(20.0, connect=5.0),
        headers={"Authorization": f"Bearer {ctx.token('confluence')}", "Accept": "application/json"},
    )


def _get(ctx: ToolContext, client: httpx.Client, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
    resp = client.get(f"{ctx.base('confluence')}{path}", params=params)
    if resp.status_code in (401, 403):
        raise ToolFailure("Confluence did not accept your token, or you may not see that page. "
                          "Reconnect it on the Connections page if it has expired.")
    if "html" in (resp.headers.get("content-type") or "").lower() and resp.status_code == 200:
        raise ToolFailure("Confluence answered with a web page instead of data, which usually means it did not "
                          "accept your token. Reconnect it on the Connections page.")
    return resp


def page_url(ctx: ToolContext, item: Dict[str, Any]) -> str:
    links = item.get("_links") or {}
    webui = str(links.get("webui") or links.get("tinyui") or "")
    if webui.startswith(("http://", "https://")):
        return webui
    if webui:
        return ctx.base("confluence").rstrip("/") + (webui if webui.startswith("/") else "/" + webui)
    return f"{ctx.base('confluence')}/pages/viewpage.action?pageId={item.get('id')}"


def storage_to_text(storage: str, limit: int = 3500) -> str:
    """Confluence storage format as readable text, keeping code blocks as code."""
    text = str(storage or "")
    blocks: List[str] = []

    def keep_code(match: "re.Match[str]") -> str:
        body = match.group(2) or ""
        language = re.search(r'ac:name="language"[^>]*>([^<]+)<', match.group(1) or "")
        blocks.append("```" + (language.group(1).strip() if language else "") + "\n" + body.strip() + "\n```")
        return f"\n\u0000{len(blocks) - 1}\u0000\n"

    text = re.sub(r'<ac:structured-macro[^>]*ac:name="(?:code|noformat)"[^>]*>(.*?)<ac:plain-text-body>\s*<!\[CDATA\[(.*?)\]\]>\s*</ac:plain-text-body>.*?</ac:structured-macro>',
                  keep_code, text, flags=re.S | re.I)
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.S)
    text = re.sub(r"<\s*h([1-6])[^>]*>", lambda m: "\n" + "#" * min(4, int(m.group(1)) + 1) + " ", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</\s*t[dh]\s*>", " | ", text, flags=re.I)
    text = re.sub(r"<\s*br\s*/?>|</\s*(p|div|tr|h[1-6]|ul|ol|table)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"\u0000(\d+)\u0000", lambda m: blocks[int(m.group(1))], text)
    return text if len(text) <= limit else text[:limit] + "\n...[the rest of the page is left out]"


def search(ctx: ToolContext, query: str, space: str = "", limit: int = 8) -> List[Dict[str, Any]]:
    """Pages matching ``query``, best first, with Confluence's own excerpt of each."""
    cql = f"type = page AND text ~ {cql_literal(query)}"
    if space:
        cql += f" AND space = {cql_literal(space)}"
    with _client(ctx) as client:
        resp = _get(ctx, client, "/rest/api/search", {"cql": cql, "limit": limit, "excerpt": "highlight"})
        if resp.status_code == 200:
            out = []
            for r in resp.json().get("results") or []:
                content = r.get("content") or {}
                if not content.get("id"):
                    continue
                excerpt = re.sub(r"@@@(end)?hl@@@", "", str(r.get("excerpt") or ""))
                out.append({"id": content.get("id"), "title": content.get("title") or r.get("title"),
                            "space": (r.get("resultGlobalContainer") or {}).get("title"),
                            "excerpt": html.unescape(re.sub(r"<[^>]+>", "", excerpt)).strip()[:300],
                            "updated": str(r.get("lastModified") or "")[:10],
                            "url": page_url(ctx, {**content, "_links": content.get("_links") or {"webui": r.get("url")}})})
            return out
        # Older servers have no /rest/api/search: the content search has no excerpts.
        resp = _get(ctx, client, "/rest/api/content/search", {"cql": cql, "limit": limit, "expand": "space,version"})
        if resp.status_code >= 400:
            raise ToolFailure(f"Confluence refused the search ({resp.status_code}).")
        return [{"id": r.get("id"), "title": r.get("title"), "space": (r.get("space") or {}).get("name"),
                 "excerpt": "", "updated": str((r.get("version") or {}).get("when") or "")[:10], "url": page_url(ctx, r)}
                for r in resp.json().get("results") or []]


def page_id_from(value: str) -> str:
    value = str(value or "").strip()
    if value.isdigit():
        return value
    parts = urlsplit(value)
    got = parse_qs(parts.query).get("pageId")
    if got and got[0].isdigit():
        return got[0]
    found = re.search(r"/pages/(\d+)", parts.path)
    if found:
        return found.group(1)
    raise ToolFailure("Give the page id, or a link to the page (one with pageId= or /pages/<number>/ in it).")


def read_page(ctx: ToolContext, page: str, limit: int = 3500) -> Dict[str, Any]:
    page_id = page_id_from(page)
    with _client(ctx) as client:
        resp = _get(ctx, client, f"/rest/api/content/{page_id}", {"expand": "body.storage,space,version,ancestors"})
    if resp.status_code == 404:
        raise ToolFailure(f"There is no Confluence page {page_id}, or you may not see it.")
    if resp.status_code >= 400:
        raise ToolFailure(f"Confluence refused to open page {page_id} ({resp.status_code}).")
    item = resp.json()
    version = item.get("version") or {}
    return {
        "id": item.get("id"), "title": item.get("title"), "space": (item.get("space") or {}).get("name"),
        "space_key": (item.get("space") or {}).get("key"),
        "version": version.get("number"), "updated": str(version.get("when") or "")[:10],
        "updated_by": ((version.get("by") or {}).get("displayName")),
        "breadcrumb": " / ".join(a.get("title", "") for a in (item.get("ancestors") or [])[-3:]),
        "text": storage_to_text(((item.get("body") or {}).get("storage") or {}).get("value") or "", limit),
        "url": page_url(ctx, item),
    }


# ── tools ────────────────────────────────────────────────────────────────────

def conf_search(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    rows = search(ctx, args["query"], args.get("space", ""), max(1, min(int(args.get("top") or 8), 15)))
    return {
        "data": {"pages": rows, "count": len(rows)},
        "summary": f"{len(rows)} page{'s' if len(rows) != 1 else ''} matching '{args['query']}'",
        "links": [{"title": r["title"], "url": r["url"]} for r in rows[:6]],
    }


def conf_page(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    page = read_page(ctx, args["page"])
    return {"data": page, "summary": page["title"], "links": [{"title": page["title"], "url": page["url"]}]}


def conf_recent(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from api.integrations import confluence_recent

    ctx.token("confluence")
    rows = [{"id": p.get("id"), "title": p.get("title"), "space": p.get("space_name"), "why": p.get("interaction"),
             "updated": str(p.get("last_updated") or "")[:10], "url": p.get("url")}
            for p in (confluence_recent(current_user=ctx.user).get("data") or [])]
    return {"data": {"pages": rows}, "summary": f"{len(rows)} recent page{'s' if len(rows) != 1 else ''}",
            "links": [{"title": r["title"], "url": r["url"]} for r in rows[:5] if r.get("url")]}


def conf_spaces(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    with _client(ctx) as client:
        resp = _get(ctx, client, "/rest/api/space", {"limit": 100, "type": "global"})
    if resp.status_code >= 400:
        raise ToolFailure(f"Confluence refused to list spaces ({resp.status_code}).")
    base = ctx.base("confluence")
    rows = [{"key": s.get("key"), "name": s.get("name"), "url": f"{base}/display/{s.get('key')}"}
            for s in resp.json().get("results") or []]
    return {"data": {"spaces": rows}, "summary": f"{len(rows)} space{'s' if len(rows) != 1 else ''}"}


register(
    Tool("conf_search", "confluence", "read", "Searching Confluence",
         "Search Confluence pages by words (full text), best match first, with an excerpt. Optional space key.",
         {"query": {"type": "string"}, "space": {"type": "string", "description": "Space key"}, "top": {"type": "integer"}},
         conf_search, required=["query"]),
    Tool("conf_page", "confluence", "read", "Reading a Confluence page",
         "Read one Confluence page as text (code blocks kept). page is its id or its link.",
         {"page": {"type": "string"}}, conf_page, required=["page"], max_chars=4200),
    Tool("conf_recent", "confluence", "read", "Finding your recent pages",
         "Pages the user recently viewed or edited.", {}, conf_recent),
    Tool("conf_spaces", "confluence", "read", "Listing Confluence spaces",
         "The Confluence spaces (key and name).", {}, conf_spaces),
)
