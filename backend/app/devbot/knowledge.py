"""
Page search by meaning: an index of chosen Confluence spaces, searched with an
embedding model.

Keyword search finds a page only when it uses the words of the question. A log says
"NU1101: Unable to find package"; the page that fixes it may be titled "Restoring
from the internal feed" and be written in Hebrew. An embedding model turns both into
vectors that land close together because they MEAN the same thing, so the index finds
that page where CQL cannot.

HOW IT IS BUILT
  An admin names the spaces and gives two credentials on the Platform Managing page: a
  read-only Confluence token that can see those spaces, and an AI key for the Hub
  itself (the index is built in the background, when no person's key is there to use,
  and indexing a space is far more than one person's per-minute limits should pay for).
  Both are stored encrypted. The build reads each space's page list, re-reads only the
  pages whose version changed, cuts them into passages, embeds the passages in batches
  at the pace the key's limits allow, and records the run: one row written when it
  starts and finished when it ends, whatever the ending.

HOW IT IS SEARCHED
  No vector database: Postgres here has no pgvector, and nothing new has to be
  installed for it. Each vector is normalised and stored as one signed byte per
  number (a quarter of the size of floats; the ranking it gives is the same to within
  noise), every pod loads them once per build, and a search is one dot product per
  passage in plain Python -- well under a second for tens of thousands of passages.

WHAT ELSE IT HOLDS: PAST FIXES
  The same index holds the fixes people already wrote down elsewhere, because most
  never reach Confluence:
    * Azure DevOps work items of the chosen types in the chosen collections and
      projects that have a resolution written, read with the Hub's admin PAT. The
      resolution field is found by what the process DEFINES (a text field about
      resolution or root cause), not by one assumed name, and an admin can name it.
    * ServiceNow ticket resolution notes, when an admin switches them on.
  A fix is FOUND by the problem (the title and the description) and SHOWN as the fix.
  Resolution notes are written in a hurry, with typos, and sometimes in words nobody
  should read out to the person who asked. So before the index keeps one, the Hub's AI
  key reviews it once: it says in one neutral sentence what the problem was, rewrites
  the fix clearly and professionally with every technical detail kept, and holds back a
  note that has no fix in it ("done", "works now") or nothing but blame. Only that
  reviewed text is stored and shown; the original never leaves the build. A rewrite
  that names a command, path, version or setting the original does not contain is
  held back too (invented_details): a model that fills a gap with a plausible step is
  exactly what nobody would catch. Admins read every kept rewrite on the card, and can
  hide one or send it back for another review.
  Each source is listed on its own; one that cannot be read leaves the others, and
  its own earlier entries, as they were.

WHO SEES WHAT
  The index is built with service credentials, so it may hold pages a given person
  may not open. A Confluence page reaches that person, or the model answering them,
  only after Confluence has confirmed with THEIR OWN token that they can read it; a
  match they cannot see is dropped as if it had never matched. A past fix is shown to
  everyone in its reviewed form -- the problem in a sentence and the fix -- because
  the fix is the useful part and the person asking may not have access to the project
  it was written in; the link to the work item is added only for someone whose own
  PAT can open it.
"""

from __future__ import annotations

import array
import json
import logging
import math
import operator
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from psycopg2 import Binary
from psycopg2.extras import Json

from db import execute, execute_returning, query_all, query_one
from redis_client import get_redis
from resilient_http import tls_verify
from sso_config import decrypt_secret, encrypt_secret

from . import config, llm
from common import failure_text

log = logging.getLogger(__name__)

_LOCK_KEY = "devbot:index:lock"
_GEN_KEY = "devbot:index:gen"
# The build's lock lives two minutes and its heartbeat renews it every 30 seconds, so a
# pod that dies (a redeploy, an eviction) frees it within two minutes.
_LOCK_TTL = 120
_HEARTBEAT = 30
_STALE_SECONDS = 180
_SEEN_TTL = 600
BATCH = 16
PASSAGE_CHARS = 900
MAX_PASSAGES_PER_PAGE = 40
RUNS_KEPT = 30
SCALE = 127.0


# ── schema ───────────────────────────────────────────────────────────────────

def ensure_tables() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_index_settings (
            id                INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            spaces            TEXT NOT NULL DEFAULT '',
            confluence_token  TEXT NOT NULL DEFAULT '',
            gateway_key       TEXT NOT NULL DEFAULT '',
            model             TEXT NOT NULL DEFAULT '',
            enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            updated_by        TEXT NOT NULL DEFAULT '',
            updated_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_index_pages (
            page_id     TEXT PRIMARY KEY,
            space_key   TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL DEFAULT '',
            url         TEXT NOT NULL DEFAULT '',
            version     INTEGER,
            modified    TEXT NOT NULL DEFAULT '',
            passages    INTEGER NOT NULL DEFAULT 0,
            error       TEXT NOT NULL DEFAULT '',
            indexed_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_index_chunks (
            id       BIGSERIAL PRIMARY KEY,
            page_id  TEXT NOT NULL REFERENCES devbot_index_pages(page_id) ON DELETE CASCADE,
            seq      INTEGER NOT NULL,
            text     TEXT NOT NULL,
            vector   BYTEA NOT NULL
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_devbot_index_chunks_page ON devbot_index_chunks (page_id, seq)")
    # Past fixes. Existing rows are Confluence pages, which is what the column defaults say.
    for column, kind in (("source", "TEXT NOT NULL DEFAULT 'confluence'"), ("ref", "TEXT NOT NULL DEFAULT ''"),
                         ("origin", "TEXT NOT NULL DEFAULT ''"),
                         # Past fixes: the reviewed fix and problem, whether the review
                         # ran, and why a note was held back. A fix indexed before the
                         # review existed has vetted = FALSE and is reviewed next build.
                         ("fix", "TEXT NOT NULL DEFAULT ''"), ("summary", "TEXT NOT NULL DEFAULT ''"),
                         ("vetted", "BOOLEAN NOT NULL DEFAULT FALSE"), ("held", "TEXT NOT NULL DEFAULT ''"),
                         # Hidden by an admin: kept, never shown, and kept hidden across
                         # rebuilds until an admin shows it again.
                         ("hidden", "BOOLEAN NOT NULL DEFAULT FALSE"), ("hidden_by", "TEXT NOT NULL DEFAULT ''"),
                         # Who can apply the fix: "anyone", or "support" when it needs
                         # rights the person asking does not have. Empty until reviewed.
                         ("audience", "TEXT NOT NULL DEFAULT ''")):
        execute(f"ALTER TABLE devbot_index_pages ADD COLUMN IF NOT EXISTS {column} {kind}")
    for column, kind in (("ado_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                         ("ado_types", "TEXT NOT NULL DEFAULT 'Bug, GrafanaAlert'"),
                         ("ado_field", "TEXT NOT NULL DEFAULT ''"),
                         ("ado_projects", "TEXT NOT NULL DEFAULT ''"),
                         ("ado_days", "INTEGER NOT NULL DEFAULT 730"),
                         ("snow_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
                         ("snow_table", "TEXT NOT NULL DEFAULT 'incident'"),
                         ("ado_collections", "TEXT NOT NULL DEFAULT ''"),
                         ("ado_comments", "BOOLEAN NOT NULL DEFAULT TRUE"),
                         ("fix_model", "TEXT NOT NULL DEFAULT ''")):
        execute(f"ALTER TABLE devbot_index_settings ADD COLUMN IF NOT EXISTS {column} {kind}")
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_index_runs (
            id             BIGSERIAL PRIMARY KEY,
            started_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            finished_at    TIMESTAMP WITH TIME ZONE,
            status         TEXT NOT NULL DEFAULT 'running',
            trigger        TEXT NOT NULL DEFAULT '',
            pages_seen     INTEGER NOT NULL DEFAULT 0,
            pages_indexed  INTEGER NOT NULL DEFAULT 0,
            pages_removed  INTEGER NOT NULL DEFAULT 0,
            pages_failed   INTEGER NOT NULL DEFAULT 0,
            passages       INTEGER NOT NULL DEFAULT 0,
            error          TEXT NOT NULL DEFAULT '',
            detail         JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    # The last sign of life of a running build: a row still "running" whose heartbeat
    # stopped belongs to a pod that is gone.
    execute("ALTER TABLE devbot_index_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITH TIME ZONE "
            "DEFAULT CURRENT_TIMESTAMP")


# ── settings ─────────────────────────────────────────────────────────────────

def parse_spaces(text: str) -> List[str]:
    """Space keys from what an admin typed: commas, spaces or new lines between them."""
    out: List[str] = []
    for key in re.split(r"[\s,;]+", str(text or "")):
        key = key.strip()
        if key and key not in out and re.fullmatch(r"~?[\w.-]{1,255}", key):
            out.append(key)
    return out


def parse_list(text: str) -> List[str]:
    """Names separated by commas or new lines (a work item type or project may have spaces)."""
    out: List[str] = []
    for name in re.split(r"[,;\n]+", str(text or "")):
        name = " ".join(name.split())
        if name and name not in out and len(name) <= 200:
            out.append(name)
    return out


def ado_available() -> bool:
    """Can the Hub read Azure DevOps on its own (a base URL and the admin PAT)?"""
    try:
        from api import azure_devops as ado_api

        return bool(ado_api.ADO_BASE and ado_api._ENV_ADMIN_PAT)
    except Exception:
        return False


def snow_available() -> bool:
    return all((os.getenv(name) or "").strip() for name in ("SNOW_BASE_URL", "SNOW_API_USERNAME", "SNOW_API_PASSWORD"))


def _decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return decrypt_secret(value)
    except ValueError:
        log.warning("devbot index: a stored credential no longer decrypts (JWT_SECRET changed?)")
        return ""


def settings(secrets: bool = False) -> Dict[str, Any]:
    """The admin's settings. Credentials only with secrets=True, and never to a page:
    the page is told whether each one is SET, which is all it needs."""
    row = query_one("SELECT * FROM devbot_index_settings WHERE id = 1") or {}
    out = {
        "spaces": parse_spaces(row.get("spaces") or ""),
        "model": str(row.get("model") or ""),
        "fix_model": str(row.get("fix_model") or ""),
        "enabled": bool(row.get("enabled", True)) if row else True,
        "has_confluence_token": bool(row.get("confluence_token")),
        "has_gateway_key": bool(row.get("gateway_key")),
        "updated_by": str(row.get("updated_by") or ""),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else "",
        "ado": {
            "enabled": bool(row.get("ado_enabled", True)) if row else True,
            "types": parse_list(row.get("ado_types") if row else "Bug, GrafanaAlert") or ["Bug", "GrafanaAlert"],
            "field": str(row.get("ado_field") or ""),
            "projects": parse_list(row.get("ado_projects") or ""),
            "collections": parse_list(row.get("ado_collections") or ""),
            "comments": bool(row.get("ado_comments", True)) if row else True,
            "days": int(row.get("ado_days") or 730),
            "available": ado_available(),
        },
        "snow": {
            "enabled": bool(row.get("snow_enabled", False)),
            "table": str(row.get("snow_table") or "incident"),
            "available": snow_available(),
        },
    }
    if secrets:
        out["confluence_token"] = _decrypt(str(row.get("confluence_token") or ""))
        out["gateway_key"] = _decrypt(str(row.get("gateway_key") or ""))
    return out


def save_settings(spaces: List[str], model: str, enabled: bool, by: str,
                  confluence_token: Optional[str] = None, gateway_key: Optional[str] = None,
                  ado: Optional[Dict[str, Any]] = None, snow: Optional[Dict[str, Any]] = None,
                  fix_model: Optional[str] = None) -> None:
    """Credentials left as None keep their stored value; "" clears them. ``ado``,
    ``snow`` and ``fix_model`` left as None keep what is stored."""
    current = query_one("SELECT * FROM devbot_index_settings WHERE id = 1") or {}
    token = current.get("confluence_token") or "" if confluence_token is None else (
        encrypt_secret(confluence_token) if confluence_token else "")
    key = current.get("gateway_key") or "" if gateway_key is None else (
        encrypt_secret(gateway_key) if gateway_key else "")
    was = settings()
    a = {**was["ado"], **(ado or {})}
    n = {**was["snow"], **(snow or {})}
    reviewer = was["fix_model"] if fix_model is None else fix_model.strip()
    execute(
        """
        INSERT INTO devbot_index_settings (id, spaces, confluence_token, gateway_key, model, enabled, updated_by, updated_at,
                                           ado_enabled, ado_types, ado_field, ado_projects, ado_days, snow_enabled, snow_table,
                                           ado_collections, fix_model, ado_comments)
        VALUES (1, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET spaces = EXCLUDED.spaces, confluence_token = EXCLUDED.confluence_token,
            gateway_key = EXCLUDED.gateway_key, model = EXCLUDED.model, enabled = EXCLUDED.enabled,
            updated_by = EXCLUDED.updated_by, updated_at = CURRENT_TIMESTAMP,
            ado_enabled = EXCLUDED.ado_enabled, ado_types = EXCLUDED.ado_types, ado_field = EXCLUDED.ado_field,
            ado_projects = EXCLUDED.ado_projects, ado_days = EXCLUDED.ado_days,
            snow_enabled = EXCLUDED.snow_enabled, snow_table = EXCLUDED.snow_table,
            ado_collections = EXCLUDED.ado_collections, fix_model = EXCLUDED.fix_model,
            ado_comments = EXCLUDED.ado_comments
        """,
        [" ".join(spaces), token, key, model, bool(enabled), by,
         bool(a["enabled"]), ", ".join(a["types"]) or "Bug, GrafanaAlert", str(a["field"] or "").strip(),
         ", ".join(a["projects"]), max(30, min(int(a["days"] or 730), 3650)),
         bool(n["enabled"]), str(n["table"] or "incident").strip(), ", ".join(a["collections"]), reviewer,
         bool(a.get("comments", True))],
    )


def sources_on(s: Dict[str, Any]) -> List[str]:
    """The sources this index is set to read, and CAN read."""
    out = []
    if s["spaces"] and s["has_confluence_token"]:
        out.append("confluence")
    if s["ado"]["enabled"] and s["ado"]["available"]:
        out.append("ado")
    if s["snow"]["enabled"] and s["snow"]["available"]:
        out.append("snow")
    return out


def configured(s: Optional[Dict[str, Any]] = None) -> bool:
    s = s or settings()
    return bool(s["has_gateway_key"] and config.enabled() and sources_on(s))


# ── the embedding model ──────────────────────────────────────────────────────

def pick_model(available: List[str], wanted: str = "") -> str:
    """The model to embed with: the admin's choice, then DEVBOT_EMBED_MODEL, then a
    multilingual e5, then any embedding model the key may use."""
    for choice in (wanted, config.embed_model()):
        if choice and choice in available:
            return choice
    for pattern in (r"multilingual-e5", r"(^|[/_-])e5([-_]|$)", r"bge-m3", r"embed"):
        found = next((m for m in available if re.search(pattern, m, re.I)), "")
        if found:
            return found
    return available[0] if available else ""


def _instruct(model: str) -> bool:
    return bool(re.search(r"e5.*instruct|instruct.*e5", model, re.I))


def as_query(model: str, text: str) -> str:
    """e5 was trained to tell a question from a passage by a prefix; without it the
    question lands among the passages that merely look like it."""
    if _instruct(model):
        return ("Instruct: Given an error message or a question from a DevOps engineer, retrieve the "
                "documentation page that explains or fixes it\nQuery: " + text)
    if re.search(r"(^|[/_-])e5([-_]|$)", model, re.I):
        return "query: " + text
    return text


def as_passage(model: str, text: str) -> str:
    if re.search(r"(^|[/_-])e5([-_]|$)", model, re.I) and not _instruct(model):
        return "passage: " + text
    return text


def pack(vector: List[float]) -> bytes:
    """A vector as one signed byte per number, after scaling it to length 1."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array.array("b", (max(-127, min(127, int(round(x / norm * SCALE)))) for x in vector)).tobytes()


def unit(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


# ── passages ─────────────────────────────────────────────────────────────────

def passages(title: str, text: str, size: int = PASSAGE_CHARS) -> List[str]:
    """A page cut into passages of about ``size`` characters along its paragraphs.

    Each passage starts with the page title, because "Steps" or "Fix" means nothing
    without the page it is on. A paragraph longer than a passage is cut at a sentence.
    """
    head = f"{title}\n"
    blocks: List[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        while len(para) > size:
            cut = max(para.rfind(". ", 0, size), para.rfind("\n", 0, size))
            cut = cut + 1 if cut > size // 3 else size
            blocks.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            blocks.append(para)
    out: List[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > size:
            out.append(head + current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current or not out:
        out.append(head + current)
    return out[:MAX_PASSAGES_PER_PAGE]


# ── Confluence, with the index's own token ───────────────────────────────────

def confluence_base() -> str:
    from api.integrations import _base_url
    from fastapi import HTTPException

    try:
        return _base_url("confluence")
    except HTTPException:
        return ""


def _confluence(token: str) -> httpx.Client:
    return httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(30.0, connect=10.0),
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _web_url(base: str, item: Dict[str, Any]) -> str:
    webui = str((item.get("_links") or {}).get("webui") or "")
    if webui.startswith(("http://", "https://")):
        return webui
    if webui:
        return base.rstrip("/") + (webui if webui.startswith("/") else "/" + webui)
    return f"{base}/pages/viewpage.action?pageId={item.get('id')}"


def check_space(client: httpx.Client, base: str, key: str) -> Tuple[bool, str]:
    """Can this token read the space? With the reason in words when it cannot."""
    try:
        resp = client.get(f"{base}/rest/api/space/{quote(str(key), safe='')}")
    except httpx.HTTPError as exc:
        log.warning("devbot index: Confluence space %s: %s: %s", key, type(exc).__name__, exc)
        return False, failure_text("Confluence", exc)
    if resp.status_code == 200 and "json" in (resp.headers.get("content-type") or ""):
        return True, str(resp.json().get("name") or key)
    if resp.status_code in (401,) or "html" in (resp.headers.get("content-type") or ""):
        return False, "Confluence did not accept the token"
    if resp.status_code in (403, 404):
        return False, f"there is no space {key}, or the token may not see it"
    return False, f"Confluence answered {resp.status_code}"


def space_pages(client: httpx.Client, base: str, key: str) -> List[Dict[str, Any]]:
    """Every current page of a space: id, title, version, link."""
    out: List[Dict[str, Any]] = []
    start = 0
    while True:
        _check_stop()
        resp = client.get(f"{base}/rest/api/content", params={
            "spaceKey": key, "type": "page", "status": "current", "expand": "version",
            "limit": 100, "start": start})
        if resp.status_code != 200:
            raise RuntimeError(f"Confluence refused the page list of {key} ({resp.status_code})")
        rows = resp.json().get("results") or []
        for item in rows:
            version = item.get("version") or {}
            out.append({"id": str(item.get("id")), "title": str(item.get("title") or ""), "space": key,
                        "version": int(version.get("number") or 0), "modified": str(version.get("when") or "")[:19],
                        "url": _web_url(base, item)})
        if len(rows) < 100 or not (resp.json().get("_links") or {}).get("next"):
            return out
        start += len(rows)


def page_text(client: httpx.Client, base: str, page_id: str) -> str:
    from .tools.confluence import storage_to_text

    resp = client.get(f"{base}/rest/api/content/{page_id}", params={"expand": "body.storage"})
    if resp.status_code != 200:
        raise RuntimeError(f"Confluence refused page {page_id} ({resp.status_code})")
    storage = ((resp.json().get("body") or {}).get("storage") or {}).get("value") or ""
    return storage_to_text(storage, limit=PASSAGE_CHARS * MAX_PASSAGES_PER_PAGE)


# ── Azure DevOps: past fixes on work items, with the Hub's admin PAT ─────────

# A field a fix is written in, by its NAME: teams call it Solution as often as
# Resolution, and in Hebrew פתרון or תיקון. The first group is where people are asked to
# write the fix; the second is related and read after it.
_FIX_FIRST = re.compile(r"solution|resolution|\u05e4\u05ea\u05e8\u05d5\u05df|\u05ea\u05d9\u05e7\u05d5\u05df", re.I)
_FIX_ALSO = re.compile(r"root.?cause|workaround|corrective", re.I)
# ...and what only looks like one: who and when it was resolved, which build had it.
_NOT_FIX = re.compile(r"reason|resolved.?(by|date)|date|build|version|found.?in|fixed.?in|integrat", re.I)
# Read last, when nothing else is filled: people sometimes write the fix into System
# Info. The review holds back what turns out to be environment details.
_FIX_FALLBACK = ("Microsoft.VSTS.TCM.SystemInfo",)
MAX_ADO_ITEMS = 5000


def _wiql_text(value: str) -> str:
    from api.azure_devops import _wiql_literal

    return "'" + _wiql_literal(value) + "'"


def resolution_fields(fields: List[Dict[str, Any]], override: str = "") -> List[str]:
    """The fields a fix is written in, in the order they are read: the admin's (one or
    several, comma-separated, in order), or every text field NAMED like a solution or
    resolution, then root cause / workaround, then System Info as a last resort. Custom
    fields are often referenced by a GUID, so the display name is what is matched.
    Picklists and dates are not where anybody writes a fix: only text fields count."""
    if override.strip():
        return parse_list(override.replace(" ", ","))
    first, also, last = [], [], []
    for f in fields:
        ref = str(f.get("referenceName") or "")
        name = str(f.get("name") or "")
        kind = str(f.get("type") or "").lower()
        if kind not in ("html", "plaintext", "string"):
            continue
        if ref in _FIX_FALLBACK:
            last.append(ref)
        elif _NOT_FIX.search(name) or _NOT_FIX.search(ref.rsplit(".", 1)[-1]):
            continue
        elif _FIX_FIRST.search(name) or _FIX_FIRST.search(ref):
            first.append(ref)
        elif _FIX_ALSO.search(name) or _FIX_ALSO.search(ref):
            also.append(ref)
    return first + also + last


def field_names(fields: List[Dict[str, Any]]) -> Dict[str, str]:
    return {str(f.get("referenceName") or ""): str(f.get("name") or "") for f in fields}


_NO_FIX_KEY = "devbot:index:nofix:"


def _discussion(client: httpx.Client, base: str, project: str, wi_id: Any) -> str:
    """The last few comments of a work item, oldest first, as plain text: where a fix
    lands when nobody filled the field for it. Bounded, because some discussions run
    to dozens of comments and the review needs the end of it, not all of it."""
    from .tools.ado import plain

    resp = client.get(f"{base}/{quote(project, safe='')}/_apis/wit/workItems/{wi_id}/comments",
                      params={"api-version": "7.0-preview.3", "$top": "6", "order": "desc"})
    if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
        return ""
    texts = [plain(c.get("text"), 600) for c in (resp.json().get("comments") or []) if not c.get("isDeleted")]
    return "\n---\n".join(t for t in reversed(texts) if t.strip())[:2500]


def _fix_from_discussion(client: httpx.Client, base: str, project: str, wi_id: Any, page_id: str, rev: int,
                         known: Dict[str, Any]) -> str:
    """A work item's discussion, when no field holds its fix -- read only when the item
    changed. An item indexed from its discussion before, and unchanged since, needs no
    read (its reviewed fix is stored; a marker keeps it in the listing), and one whose
    discussion held nothing at this revision is remembered for 30 days."""
    before = known.get(page_id) or {}
    if before.get("version") == rev and (before.get("audience") or before.get("held")):
        return "(unchanged discussion)"
    try:
        if _text(get_redis().get(_NO_FIX_KEY + page_id)) == str(rev):
            return ""
    except Exception:
        pass
    text = _discussion(client, base, project, wi_id)
    if not text:
        try:
            get_redis().setex(_NO_FIX_KEY + page_id, 30 * 86400, str(rev))
        except Exception:
            pass
        return ""
    return "From the discussion:\n" + text


def list_ado(s: Dict[str, Any], detail: Dict[str, Any], known: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Work items of the chosen types changed in the last N days that have a fix written,
    in the chosen collections (every one the admin PAT reaches when none is named)."""
    from api import azure_devops as ado_api
    from .tools.ado import plain

    settings_ = s["ado"]
    api = {"api-version": "7.0"}
    where = [f"[System.WorkItemType] IN ({', '.join(_wiql_text(t) for t in settings_['types'])})",
             f"[System.ChangedDate] >= @Today - {int(settings_['days'])}"]
    if settings_["projects"]:
        where.append(f"[System.TeamProject] IN ({', '.join(_wiql_text(p) for p in settings_['projects'])})")
    query = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(where) + " ORDER BY [System.ChangedDate] DESC"
    out: List[Dict[str, Any]] = []
    info: Dict[str, Any] = {"collections": {}, "without_fix": 0}
    wanted = {c.lower() for c in settings_.get("collections") or []}
    known = known or {}
    with ado_api._admin_ado_client() as client:
        bases = ado_api._discover_ado_bases(client)
        found_names = {base.rstrip("/").rsplit("/", 1)[-1].lower() for base in bases}
        missing = sorted(c for c in wanted if c not in found_names)
        if missing and len(missing) == len(wanted):
            raise RuntimeError("The admin PAT reaches no collection called " + ", ".join(missing) + ".")
        if missing:
            info["missing_collections"] = missing
        for base in bases:
            _check_stop()
            collection = base.rstrip("/").rsplit("/", 1)[-1]
            if wanted and collection.lower() not in wanted:
                continue
            fields_resp = client.get(f"{base}/_apis/wit/fields", params=api)
            if fields_resp.status_code in (400, 401, 403, 404):
                continue
            fields_resp.raise_for_status()
            all_fields = fields_resp.json().get("value") or []
            candidates = resolution_fields(all_fields, settings_["field"])
            names = field_names(all_fields)
            coll: Dict[str, Any] = {"fields": candidates, "names": {r: names.get(r, "") for r in candidates},
                                    "filled": {r: 0 for r in candidates}, "items": 0, "without_fix": 0,
                                    "from_discussion": 0}
            info["collections"][collection] = coll
            if not candidates:
                continue
            resp = client.post(f"{base}/_apis/wit/wiql", params={**api, "$top": MAX_ADO_ITEMS}, json={"query": query})
            if resp.status_code in (400, 401, 403, 404):
                coll["error"] = f"the query was refused ({resp.status_code})"
                continue
            resp.raise_for_status()
            ids = [int(w["id"]) for w in resp.json().get("workItems") or []][:MAX_ADO_ITEMS]
            for i in range(0, len(ids), 200):
                _check_stop()
                chunk = ids[i:i + 200]
                got = client.get(f"{base}/_apis/wit/workitems",
                                 params={**api, "ids": ",".join(str(x) for x in chunk), "errorPolicy": "omit"})
                got.raise_for_status()
                for wi in got.json().get("value") or []:
                    if not wi:
                        continue
                    f = wi.get("fields") or {}
                    filled = [ref for ref in candidates if plain(f.get(ref), 10).strip()]
                    for ref in filled:
                        coll["filled"][ref] += 1
                    fix = plain(f.get(filled[0]), 3000) if filled else ""
                    project = str(f.get("System.TeamProject") or "")
                    kind = str(f.get("System.WorkItemType") or "Work item")
                    rev = int(f.get("System.Rev") or wi.get("rev") or 0)
                    page_id = f"ado:{collection}:{wi.get('id')}"
                    if not fix and settings_.get("comments", True):
                        fix = _fix_from_discussion(client, base, project, wi.get("id"), page_id, rev, known)
                        if fix:
                            coll["from_discussion"] += 1
                    if not fix:
                        coll["without_fix"] += 1
                        continue
                    # The problem is in the Description, the Repro Steps, or both.
                    problem = "\n\n".join(t for t in (plain(f.get("System.Description"), 2000),
                                                         plain(f.get("Microsoft.VSTS.TCM.ReproSteps"), 2000)) if t.strip())[:3000]
                    out.append({
                        "id": page_id, "source": "ado", "space": project,
                        "title": str(f.get("System.Title") or ""), "ref": f"{kind} #{wi.get('id')}",
                        "origin": base, "url": f"{base}/{quote(project, safe='')}/_workitems/edit/{wi.get('id')}",
                        "version": rev,
                        "modified": str(f.get("System.ChangedDate") or "")[:19],
                        # Searched by the problem; the fix is reviewed, then shown.
                        "text": problem, "fix_raw": fix,
                    })
                    coll["items"] += 1
            info["without_fix"] += coll["without_fix"]
    detail["ado"] = info
    return out


# ── ServiceNow: resolution notes, only when an admin opts in ─────────────────

MAX_SNOW_ITEMS = 5000


def list_snow(s: Dict[str, Any], detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tickets with resolution notes: the short description and the notes, nothing about
    who raised them or who fixed them."""
    from api.servicenow import _snow_ticket_flow_client

    table = s["snow"]["table"]
    if not re.fullmatch(r"[a-z0-9_]{1,80}", table):
        raise RuntimeError(f"'{table}' is not a ServiceNow table name.")
    out: List[Dict[str, Any]] = []
    offset = 0
    with _snow_ticket_flow_client() as client:
        while offset < MAX_SNOW_ITEMS:
            _check_stop()
            resp = client.get(f"/api/now/table/{table}", params={
                "sysparm_query": "close_notesISNOTEMPTY^ORDERBYDESCsys_updated_on",
                "sysparm_fields": "sys_id,number,short_description,description,close_notes,sys_updated_on,sys_mod_count",
                "sysparm_limit": 500, "sysparm_offset": offset, "sysparm_exclude_reference_link": "true"})
            if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
                raise RuntimeError(f"ServiceNow refused the {table} list ({resp.status_code}).")
            rows = resp.json().get("result") or []
            for row in rows:
                notes = str(row.get("close_notes") or "").strip()
                if not notes:
                    continue
                out.append({
                    "id": f"snow:{row.get('sys_id')}", "source": "snow", "space": table,
                    "title": str(row.get("short_description") or row.get("number") or "")[:300],
                    "ref": str(row.get("number") or ""), "origin": "", "url": "",
                    "version": int(row.get("sys_mod_count") or 0), "modified": str(row.get("sys_updated_on") or "")[:19],
                    "text": str(row.get("description") or "").strip()[:2500], "fix_raw": notes[:3000],
                })
            if len(rows) < 500:
                break
            offset += len(rows)
    detail["snow"] = {"table": table, "items": len(out)}
    return out


# ── reviewing a fix before it is kept ────────────────────────────────────────

_REVIEW = (
    "You review resolution notes that a DevOps support team wrote when it closed a bug or a support ticket. "
    "Engineers who later hit the same problem will read your version, so it must be correct, clear and professional.\n"
    "Answer with ONE JSON object and nothing else: {\"usable\": true or false, \"problem\": \"...\", \"fix\": \"...\", "
    "\"who\": \"anyone\" or \"support\"}\n"
    "- problem: one neutral sentence saying what went wrong, from the title and the description.\n"
    "- fix: the fix, rewritten: correct the spelling and grammar, keep every technical detail exactly (commands, "
    "names, versions, paths, settings, links), as short steps when there are several. Leave out blame, insults, "
    "sarcasm, jokes, remarks about the person who reported it and the names of people. Never add a step the note "
    "does not contain. Write it in the language the note is written in.\n"
    "- usable: false when the note holds no fix another engineer could apply (for example 'fixed', 'done', "
    "'works now', 'duplicate', 'no answer from the user', 'closed'), or holds nothing but blame; then leave fix empty.\n"
    "- who: \"support\" when applying the fix needs rights a regular developer does not have: on servers or "
    "infrastructure (restarting services on servers, freeing disk or adding storage on servers, changing server or "
    "network configuration, granting permissions, admin consoles). \"anyone\" when the person who hit the problem "
    "can apply it themselves (in their code, pipeline, repository, settings or their own machine).\n"
    "The notes may be a work item's discussion rather than a resolution: then take the fix from what the "
    "discussion says was done, and set usable false when it never says."
)


def pick_chat_model(available: List[str], wanted: str = "") -> str:
    """The model that reviews fixes: the admin's choice, then DevBot's default, then any."""
    for choice in (wanted, config.default_model()):
        if choice and choice in available:
            return choice
    return available[0] if available else ""


def _json_object(text: str) -> Dict[str, Any]:
    found = re.search(r"\{.*\}", text or "", re.S)
    if not found:
        return {}
    try:
        value = json.loads(found.group(0))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


_TOKEN = re.compile(r"[^\s,;()\[\]{}<>\"'`]+")


def _technical(token: str) -> bool:
    """A token a fix's correctness hangs on: a path, a flag, a file, a setting, a
    version, an error code. Plain words -- in any language -- and bare numbers are not:
    a rewrite is supposed to change the words."""
    if len(token) < 3 or re.fullmatch(r"[\d.,:%]+", token):
        return False
    if re.search(r"[/\\=_]|^--?[A-Za-z]|\w\.\w|\w:\w", token):
        return True
    return bool(re.search(r"\d", token) and re.search(r"[A-Za-z]", token))


def invented_details(rewrite: str, original: str) -> List[str]:
    """The technical tokens of ``rewrite`` that ``original`` does not contain."""
    source = (original or "").lower()
    out: List[str] = []
    for raw in _TOKEN.findall(rewrite or ""):
        token = raw.strip(".:!?*")
        if _technical(token) and token.lower() not in source and token not in out:
            out.append(token)
    return out


def review_fix(key: str, model: str, item: Dict[str, Any], pacer: "_Pacer") -> Dict[str, Any]:
    """{"usable", "problem", "fix"} for one past fix, reviewed by the model. An answer
    that is not the JSON asked for is asked once more, then treated as a failure: a
    note is never kept unreviewed."""
    prompt = (f"Title: {item.get('title') or ''}\n\nDescription:\n{str(item.get('text') or '')[:1500] or '(none)'}"
              f"\n\nResolution notes:\n{str(item.get('fix_raw') or '')[:2500]}")
    messages = [{"role": "system", "content": _REVIEW}, {"role": "user", "content": prompt}]
    for attempt in range(6):
        try:
            text, limits = llm.complete(key, model, messages, max_tokens=900)
            pacer.after(limits, 1500)
        except llm.LLMError as exc:
            if exc.kind in ("limit", "server", "timeout", "network") and attempt < 5:
                pacer.wait(float(min(90, exc.retry_after or (15 * (attempt + 1)))))
                continue
            raise
        pacer.reviews += 1
        got = _json_object(text)
        if "usable" in got:
            usable = got["usable"] is True or str(got["usable"]).strip().lower() == "true"
            fix = re.sub(r"\n{3,}", "\n\n", str(got.get("fix") or "")).strip()[:1500]
            held = "" if usable and fix else "no fix another engineer could apply"
            if not held:
                added = invented_details(fix, " ".join(str(item.get(k) or "") for k in ("title", "text", "fix_raw")))
                if added:
                    held = "the review added details the note does not have: " + ", ".join(added[:5])
                    log.warning("devbot index: the review of %s added %s; held back", item.get("id"), added[:5])
            who = "support" if str(got.get("who") or "").strip().lower() == "support" else "anyone"
            return {"usable": not held, "problem": str(got.get("problem") or "").strip()[:300],
                    "fix": fix if not held else "", "held": held, "audience": who if not held else ""}
        if attempt >= 1:
            break
    raise RuntimeError("The AI service did not answer the fix review in the form asked for.")


# ── building ─────────────────────────────────────────────────────────────────

class _Pacer:
    """Keeps an indexing run inside the key's per-minute limits, from the figures the
    gateway reports on each answer, and waits out a 429 instead of failing on it."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self.sleep = sleep
        self.waited = 0.0
        # What the build spent on the Hub's key, for the monitoring page.
        self.embeds = 0
        self.reviews = 0

    def after(self, limits: Dict[str, Any], next_tokens: int) -> None:
        left_requests = limits.get("rpm_left")
        left_tokens = limits.get("tpm_left")
        if (left_requests is not None and left_requests <= 1) or (left_tokens is not None and left_tokens < next_tokens):
            self.wait(20.0)

    def wait(self, seconds: float) -> None:
        """Wait, in slices of two seconds, so a Stop is not held up by a minute's wait."""
        self.waited += seconds
        left = seconds
        while left > 0:
            _check_stop()
            self.sleep(min(2.0, left))
            left -= 2.0


def _embed_with_retries(key: str, model: str, texts: List[str], pacer: _Pacer) -> List[List[float]]:
    for attempt in range(6):
        try:
            vectors, limits = llm.embed(key, model, texts)
            pacer.embeds += 1
            pacer.after(limits, sum(len(t) for t in texts) // 3)
            return vectors
        except llm.LLMError as exc:
            if exc.kind in ("limit", "server", "timeout", "network") and attempt < 5:
                pacer.wait(float(min(90, exc.retry_after or (15 * (attempt + 1)))))
                continue
            raise
    raise llm.LLMError("limit", "The AI service kept refusing the embeddings requests.")


def _run_update(run_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{name} = %s" for name in fields)
    values = [Json(v) if isinstance(v, dict) else v for v in fields.values()]
    execute(f"UPDATE devbot_index_runs SET {sets} WHERE id = %s", values + [run_id])


def _alive_in_db() -> bool:
    row = query_one("SELECT 1 AS x FROM devbot_index_runs WHERE status = 'running' "
                    "AND heartbeat_at > NOW() - (%s * INTERVAL '1 second') LIMIT 1", [_STALE_SECONDS])
    return bool(row)


def running() -> bool:
    """Is a build alive anywhere? The lock says one was started; the heartbeat says its
    pod is still there. A lock older than a few minutes with no heartbeat behind it was
    left by a pod that died (or by the code before heartbeats, whose lock lived six
    hours), and is cleared here rather than blocking every build until it expires."""
    try:
        held = _text(get_redis().get(_LOCK_KEY))
    except Exception:
        return _alive_in_db()
    if not held:
        return False
    try:
        age = time.time() - float(held)
    except ValueError:
        age = _STALE_SECONDS + 1
    if age < _STALE_SECONDS or _alive_in_db():
        return True
    log.warning("devbot index: clearing the lock of a build whose pod is gone")
    try:
        get_redis().delete(_LOCK_KEY)
    except Exception:
        pass
    return False


def bury_orphans() -> int:
    """Mark as died every run still "running" whose heartbeat stopped: its pod is gone."""
    rows = execute_returning(
        "UPDATE devbot_index_runs SET status = 'died', finished_at = CURRENT_TIMESTAMP, "
        "error = 'The pod building it stopped before it finished (a redeploy or a restart). The next build carries on.' "
        "WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < NOW() - (%s * INTERVAL '1 second')) "
        "RETURNING id", [_STALE_SECONDS])
    return len(rows or [])


_STOP_KEY = "devbot:index:stop"


class _Stopped(Exception):
    """An admin pressed Stop. Raised where the build checks, and ends it as "stopped"."""


def request_stop() -> str:
    """Stop the build. "stopping": a live one will stop at its next check (within
    seconds, whatever it is doing). "cleared": none was alive, and a run left
    "running" by a dead pod is marked so. "none": there was nothing to stop."""
    if running():
        try:
            get_redis().setex(_STOP_KEY, 3600, "1")
            return "stopping"
        except Exception:
            return "none"
    return "cleared" if bury_orphans() else "none"


def _stop_requested() -> bool:
    try:
        return bool(get_redis().exists(_STOP_KEY))
    except Exception:
        return False


def _check_stop() -> None:
    if _stop_requested():
        raise _Stopped()


def _heartbeat(run_id: int, token: str, done: threading.Event) -> None:
    """Keep the lock and the run's heartbeat alive while this pod builds."""
    while not done.wait(_HEARTBEAT):
        try:
            if _text(get_redis().get(_LOCK_KEY)) == token:
                get_redis().expire(_LOCK_KEY, _LOCK_TTL)
        except Exception:
            pass
        try:
            execute("UPDATE devbot_index_runs SET heartbeat_at = CURRENT_TIMESTAMP WHERE id = %s", [run_id])
        except Exception:
            log.warning("devbot index: heartbeat not recorded", exc_info=True)


def start(trigger: str) -> Dict[str, Any]:
    """Build in a background thread. Returns at once, saying whether it started."""
    s = settings()
    if not configured(s):
        return {"started": False, "why": "Set the AI key and at least one source (spaces with a Confluence token, "
                                         "Azure DevOps fixes, or ticket fixes) first."}
    if running():
        return {"started": False, "why": "A build is already running."}
    bury_orphans()
    threading.Thread(target=build, args=(trigger,), name="devbot-index", daemon=True).start()
    return {"started": True}


def build(trigger: str, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Bring the index up to date with the spaces. Only changed pages are re-read.

    One run at a time across every pod (a Redis lock); the run's row is written when it
    starts and finished in `finally`, so "running", "finished" and "died" stay distinct.
    """
    token = f"{time.time()}"
    try:
        if not get_redis().set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL):
            return {"started": False, "why": "A build is already running."}
    except Exception:
        log.warning("devbot index: Redis unavailable, building without the cross-pod lock")
    rows = execute_returning("INSERT INTO devbot_index_runs (trigger) VALUES (%s) RETURNING id", [trigger])
    if not rows:
        raise RuntimeError("the index run was not recorded")
    run_id = int(rows[0]["id"])
    # This run holds the lock, so any other run still marked running lost its pod
    # half-way. Say so, or it reads "running" for ever.
    execute("UPDATE devbot_index_runs SET status = 'died', finished_at = CURRENT_TIMESTAMP, "
            "error = 'The pod building it stopped before it finished (a redeploy or a restart). The next build carries on.' "
            "WHERE status = 'running' AND id <> %s", [run_id])
    try:
        get_redis().delete(_STOP_KEY)
    except Exception:
        pass
    beating = threading.Event()
    threading.Thread(target=_heartbeat, args=(run_id, token, beating), name="devbot-index-heartbeat",
                     daemon=True).start()
    counts = {"pages_seen": 0, "pages_indexed": 0, "pages_removed": 0, "pages_failed": 0, "passages": 0}
    status, error, detail = "failed", "", {}
    pacer = _Pacer(sleep)
    try:
        s = settings(secrets=True)
        wanted = sources_on({**s, "has_confluence_token": bool(s["confluence_token"])})
        if not s["gateway_key"] or not wanted:
            raise RuntimeError("The AI key, or every source, is not set.")
        model = s["model"] or pick_model([m["id"] for m in llm.list_models(s["gateway_key"], embedding=True)])
        if not model:
            raise RuntimeError("The AI key may not use any embedding model.")
        detail["model"] = model
        reviewer = ""
        if "ado" in wanted or "snow" in wanted:
            # Past fixes are reviewed by a chat model before they are kept; without one
            # they are not read at all, and the reason is shown with the sources.
            reviewer = pick_chat_model([m["id"] for m in llm.list_models(s["gateway_key"])], s["fix_model"])
            detail["fix_model"] = reviewer
        stored_model = query_one("SELECT detail->>'model' AS model FROM devbot_index_runs "
                                 "WHERE status = 'done' ORDER BY id DESC LIMIT 1") or {}
        if stored_model.get("model") and stored_model["model"] != model:
            # Vectors from two models are not comparable: start over.
            execute("DELETE FROM devbot_index_pages")
        base = confluence_base()
        seen: Dict[str, Dict[str, Any]] = {}
        read_ok: List[str] = []
        detail["sources"] = {}
        for source in wanted:
            _check_stop()
            detail["phase"] = {"confluence": "Listing the Confluence pages", "ado": "Listing the Azure DevOps work items",
                               "snow": "Listing the tickets"}[source]
            _run_update(run_id, detail=detail)
            try:
                if source in ("ado", "snow") and not reviewer:
                    raise RuntimeError("The AI key may not use any chat model to review the fixes with.")
                if source == "confluence":
                    if not base:
                        raise RuntimeError("Confluence is not configured on this Hub (CONFLUENCE_BASE_URL).")
                    with _confluence(s["confluence_token"]) as client:
                        for key in s["spaces"]:
                            ok, why = check_space(client, base, key)
                            if not ok:
                                raise RuntimeError(f"Space {key}: {why}.")
                            for page in space_pages(client, base, key):
                                seen[page["id"]] = {**page, "source": "confluence"}
                elif source == "ado":
                    known_ado = {r["page_id"]: r for r in query_all(
                        "SELECT page_id, version, audience, held FROM devbot_index_pages WHERE source = 'ado' AND vetted")}
                    for item in list_ado(s, detail, known_ado):
                        seen[item["id"]] = item
                elif source == "snow":
                    for item in list_snow(s, detail):
                        seen[item["id"]] = item
                read_ok.append(source)
                detail["sources"][source] = {"items": sum(1 for v in seen.values() if v["source"] == source)}
            except _Stopped:
                raise
            except Exception as exc:
                # One source that cannot be read leaves the others, and its own
                # earlier entries, exactly as they were.
                why = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
                detail["sources"][source] = {"error": str(why)[:300]}
                log.warning("devbot index: source %s could not be read: %s", source, why)
        if not read_ok:
            raise RuntimeError("; ".join(f"{k}: {v.get('error')}" for k, v in detail["sources"].items()))
        counts["pages_seen"] = len(seen)
        known = {r["page_id"]: r for r in query_all(
            "SELECT page_id, version, error, source, vetted, held, audience FROM devbot_index_pages")}
        # Gone: no longer in a source that WAS read, or in a source switched off.
        gone = [pid for pid, row in known.items()
                if (row["source"] in read_ok and pid not in seen) or row["source"] not in wanted]
        for pid in gone:
            execute("DELETE FROM devbot_index_pages WHERE page_id = %s", [pid])
        counts["pages_removed"] = len(gone)
        total = int((query_one("SELECT COUNT(*) AS n FROM devbot_index_chunks") or {}).get("n") or 0)
        todo = [p for p in seen.values()
                if (known.get(p["id"]) or {}).get("version") != p["version"] or (known.get(p["id"]) or {}).get("error")
                or (p["source"] in ("ado", "snow") and not (known.get(p["id"]) or {}).get("vetted"))
                # A kept fix reviewed before "who can apply it" was asked is asked again.
                or (p["source"] in ("ado", "snow") and (known.get(p["id"]) or {}).get("vetted")
                    and not (known.get(p["id"]) or {}).get("held") and not (known.get(p["id"]) or {}).get("audience"))]
        detail["to_read"] = len(todo)
        detail["phase"] = "Reading, reviewing and indexing what changed"
        _run_update(run_id, **counts, detail=detail)
        conf_client = _confluence(s["confluence_token"]) if "confluence" in read_ok else None
        try:
            for n, page in enumerate(todo, 1):
                if _stop_requested():
                    raise _Stopped(f"Stopped by an admin after {n - 1} of {len(todo)} entries.")
                try:
                    text = page.get("text")
                    if text is None:
                        text = page_text(conf_client, base, page["id"])
                    if page.get("fix_raw") is not None:
                        review = review_fix(s["gateway_key"], reviewer, page, pacer)
                        page = {**page, "fix": review["fix"], "summary": review["problem"], "vetted": True,
                                "held": review["held"], "audience": review.get("audience", "")}
                    # A note held back is recorded with no passages: nothing finds it, and
                    # it is not reviewed again until the work item or ticket changes.
                    parts = [] if page.get("held") else passages(
                        (page.get("ref") + ": " if page.get("ref") else "") + page["title"], text)
                    old = int((query_one("SELECT passages FROM devbot_index_pages WHERE page_id = %s", [page["id"]]) or {}).get("passages") or 0)
                    if total - old + len(parts) > config.index_max_chunks():
                        detail["stopped"] = (f"The index is full ({config.index_max_chunks()} passages, "
                                             "DEVBOT_INDEX_MAX_CHUNKS). The remaining entries were left out.")
                        break
                    vectors: List[List[float]] = []
                    for i in range(0, len(parts), BATCH):
                        vectors += _embed_with_retries(s["gateway_key"], model,
                                                       [as_passage(model, p) for p in parts[i:i + BATCH]], pacer)
                    _store_page(page, parts, vectors)
                    total += len(parts) - old
                    counts["pages_indexed"] += 1
                except llm.LLMError as exc:
                    if exc.kind in ("key", "model_denied", "model_missing", "budget", "not_configured"):
                        raise RuntimeError(f"The AI service: {exc.message}")
                    _page_failed(page, exc.message)
                    counts["pages_failed"] += 1
                except (RuntimeError, httpx.HTTPError) as exc:
                    _page_failed(page, str(exc))
                    counts["pages_failed"] += 1
                if n % 10 == 0:
                    _run_update(run_id, **counts)
        finally:
            if conf_client is not None:
                conf_client.close()
        counts["passages"] = int((query_one("SELECT COUNT(*) AS n FROM devbot_index_chunks") or {}).get("n") or 0)
        status = "done"
    except _Stopped as exc:
        # What was indexed so far stays; the next build carries on from there.
        status = "stopped"
        detail["stopped"] = str(exc) or "Stopped by an admin while " + str(detail.get("phase") or "starting").lower() + "."
        counts["passages"] = int((query_one("SELECT COUNT(*) AS n FROM devbot_index_chunks") or {}).get("n") or 0)
    except Exception as exc:
        error = str(exc)[:500] or type(exc).__name__
        log.warning("devbot index: the build failed: %s", error)
    finally:
        beating.set()
        detail.pop("phase", None)
        detail["waited_seconds"] = int(pacer.waited)
        detail["requests"] = {"embed": pacer.embeds, "review": pacer.reviews}
        try:
            _run_update(run_id, status=status, error=error, detail=detail, **counts)
            execute("UPDATE devbot_index_runs SET finished_at = CURRENT_TIMESTAMP WHERE id = %s", [run_id])
            execute("DELETE FROM devbot_index_runs WHERE id NOT IN "
                    "(SELECT id FROM devbot_index_runs ORDER BY id DESC LIMIT %s)", [RUNS_KEPT])
        except Exception:
            log.warning("devbot index: the run's outcome was not recorded", exc_info=True)
        try:
            get_redis().set(_GEN_KEY, str(run_id))
            get_redis().delete(_STOP_KEY)
            if _text(get_redis().get(_LOCK_KEY)) == token:
                get_redis().delete(_LOCK_KEY)
        except Exception:
            pass
    return {"run": run_id, "status": status, "error": error, **counts}


def _store_page(page: Dict[str, Any], parts: List[str], vectors: List[List[float]]) -> None:
    execute(
        """
        INSERT INTO devbot_index_pages (page_id, space_key, title, url, version, modified, passages, error, indexed_at,
                                        source, ref, origin, fix, summary, vetted, held, audience)
        VALUES (%s, %s, %s, %s, NULL, %s, 0, '', CURRENT_TIMESTAMP, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET space_key = EXCLUDED.space_key, title = EXCLUDED.title,
            url = EXCLUDED.url, version = NULL, modified = EXCLUDED.modified, source = EXCLUDED.source,
            ref = EXCLUDED.ref, origin = EXCLUDED.origin, fix = EXCLUDED.fix, summary = EXCLUDED.summary,
            vetted = EXCLUDED.vetted, held = EXCLUDED.held, audience = EXCLUDED.audience, error = ''
        """,
        [page["id"], page["space"], page["title"], page["url"], page["modified"], page.get("source") or "confluence",
         page.get("ref") or "", page.get("origin") or "", page.get("fix") or "", page.get("summary") or "",
         bool(page.get("vetted")), page.get("held") or "", page.get("audience") or ""],
    )
    execute("DELETE FROM devbot_index_chunks WHERE page_id = %s", [page["id"]])
    for seq, (text, vector) in enumerate(zip(parts, vectors)):
        execute("INSERT INTO devbot_index_chunks (page_id, seq, text, vector) VALUES (%s, %s, %s, %s)",
                [page["id"], seq, text, Binary(pack(vector))])
    # The version last, so a run that dies half-way re-reads this page next time.
    execute("UPDATE devbot_index_pages SET version = %s, passages = %s, indexed_at = CURRENT_TIMESTAMP "
            "WHERE page_id = %s", [page["version"], len(parts), page["id"]])


def _page_failed(page: Dict[str, Any], why: str) -> None:
    log.warning("devbot index: page %s (%s) was not indexed: %s", page["id"], page["title"], why)
    execute(
        """
        INSERT INTO devbot_index_pages (page_id, space_key, title, url, version, modified, error, source, ref, origin)
        VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET error = EXCLUDED.error, version = NULL
        """,
        [page["id"], page["space"], page["title"], page["url"], page["modified"], why[:300],
         page.get("source") or "confluence", page.get("ref") or "", page.get("origin") or ""],
    )


def due(now: Optional[float] = None) -> bool:
    """Is a scheduled catch-up due? Never when not configured or switched off."""
    s = settings()
    if not configured(s) or not s["enabled"]:
        return False
    row = query_one("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) AS age FROM devbot_index_runs") or {}
    age = row.get("age")
    return age is None or float(age) >= config.index_interval_hours() * 3600


def schedule_loop(stop: Optional[threading.Event] = None) -> None:
    """The background catch-up: checks hourly, builds when one is due."""
    stop = stop or threading.Event()
    stop.wait(120)
    while not stop.is_set():
        try:
            if due() and not running():
                build("schedule")
        except Exception as exc:
            log.warning("devbot index: the scheduled build could not start: %s", exc)
        stop.wait(3600)


# ── searching ────────────────────────────────────────────────────────────────

_loaded: Dict[str, Any] = {"gen": None, "ids": [], "vectors": [], "pages": {}}
_load_lock = threading.Lock()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _generation() -> str:
    try:
        value = _text(get_redis().get(_GEN_KEY))
        if value:
            return value
    except Exception:
        pass
    row = query_one("SELECT MAX(id) AS id FROM devbot_index_runs WHERE finished_at IS NOT NULL") or {}
    return str(row.get("id") or "")


def _vectors() -> Tuple[List[Tuple[str, int]], List[array.array], Dict[str, Dict[str, Any]]]:
    """Every passage's vector, loaded once per build into this pod."""
    gen = _generation()
    with _load_lock:
        if _loaded["gen"] != gen:
            rows = query_all("SELECT page_id, seq, vector FROM devbot_index_chunks ORDER BY page_id, seq")
            pages = {r["page_id"]: r for r in query_all(
                "SELECT page_id, space_key, title, url, modified, source, ref, origin, fix, summary, audience "
                "FROM devbot_index_pages WHERE version IS NOT NULL AND (source = 'confluence' OR vetted) AND NOT hidden")}
            ids, vectors = [], []
            for r in rows:
                if r["page_id"] not in pages:
                    continue
                ids.append((r["page_id"], int(r["seq"])))
                vectors.append(array.array("b", bytes(r["vector"])))
            _loaded.update(gen=gen, ids=ids, vectors=vectors, pages=pages)
        return _loaded["ids"], _loaded["vectors"], _loaded["pages"]


def ready(source: str = "") -> bool:
    """Is anything searchable (from ``source``, when named)?"""
    try:
        s = settings()
        if not configured(s) or not s["enabled"]:
            return False
        # Searchable means it has passages: a past fix held back by the review is
        # recorded (so it is not reviewed again) but has nothing to find.
        if source:
            row = query_one("SELECT COUNT(*) AS n FROM devbot_index_pages WHERE version IS NOT NULL AND passages > 0 "
                            "AND source = %s", [source])
        else:
            row = query_one("SELECT COUNT(*) AS n FROM devbot_index_pages WHERE version IS NOT NULL AND passages > 0") or {}
        return int((row or {}).get("n") or 0) > 0
    except Exception:
        return False


def public_status() -> Dict[str, Any]:
    """What a person's page is told: is meaning search on, over which spaces, and are
    past fixes searched."""
    if not ready():
        return {"ready": False}
    s = settings()
    return {"ready": True, "spaces": s["spaces"] if ready("confluence") else [],
            "past_fixes": ready("ado") or ready("snow")}


def rank(query: List[float], vectors: List[array.array]) -> List[Tuple[float, int]]:
    """(similarity, index) for every passage, best first. Similarity is the cosine."""
    q = unit(query)
    mul = operator.mul
    scored = [(sum(map(mul, q, v)) / SCALE, i) for i, v in enumerate(vectors)]
    scored.sort(reverse=True)
    return scored


def search(query: str, limit: int = 5, space: str = "", sources: Optional[Tuple[str, ...]] = None) -> List[Dict[str, Any]]:
    """Indexed pages most like ``query``, one entry per page with its best passage.

    A Confluence page is NOT yet checked against the asking person: call visible_to()
    before showing one. A past fix carries its reviewed ``fix`` and ``summary``.
    """
    if not ready():
        return []
    s = settings(secrets=True)
    ids, vectors, pages = _vectors()
    if not vectors:
        return []
    model = (query_one("SELECT detail->>'model' AS model FROM devbot_index_runs WHERE status = 'done' "
                       "ORDER BY id DESC LIMIT 1") or {}).get("model") or s["model"]
    if not model:
        return []
    qv = llm.embed(s["gateway_key"], model, [as_query(model, query[:2000])], timeout=20.0)[0][0]
    floor = config.index_min_score()
    votes = _votes()
    best: Dict[str, Tuple[float, int]] = {}
    for score, i in rank(qv, vectors):
        if score < floor or len(best) >= limit * 3:
            break
        page_id, seq = ids[i]
        page = pages[page_id]
        if sources and (page.get("source") or "confluence") not in sources:
            continue
        if space and page["space_key"].lower() != space.lower():
            continue
        if page_id not in best:
            # What people said helped counts a little; it never lifts an unrelated page
            # over the bar, because it is only added to what already cleared it.
            net = max(-2, min(3, votes.get(page.get("url") or "", 0)))
            best[page_id] = (score + 0.015 * net, seq)
    out = []
    for page_id, (score, seq) in sorted(best.items(), key=lambda kv: -kv[1][0])[:limit * 2]:
        text = (query_one("SELECT text FROM devbot_index_chunks WHERE page_id = %s AND seq = %s", [page_id, seq]) or {}).get("text") or ""
        page = pages[page_id]
        out.append({"id": page_id, "title": page["title"], "space": page["space_key"], "url": page["url"],
                    "updated": str(page.get("modified") or "")[:10], "score": min(100, round(score * 100)),
                    "passage": text.split("\n", 1)[-1][:700], "source": page.get("source") or "confluence",
                    "ref": page.get("ref") or "", "origin": page.get("origin") or "",
                    "fix": page.get("fix") or "", "summary": page.get("summary") or "",
                    "audience": page.get("audience") or ""})
    return out


def _votes() -> Dict[str, int]:
    try:
        from . import monitor

        return monitor.source_votes()
    except Exception:
        return {}


def visible(found: List[Dict[str, Any]], user_id: str, limit: int,
            check: Callable[[Dict[str, Any]], Optional[bool]]) -> List[Dict[str, Any]]:
    """Only the hits ``check`` says this person may open; an answer of None (could not
    tell) drops the hit without remembering it. Yes and no are remembered ten minutes."""
    out: List[Dict[str, Any]] = []
    for item in found:
        if len(out) >= limit:
            break
        cache_key = f"devbot:index:seen:{user_id}:{item['id']}"
        allowed: Optional[bool] = None
        try:
            cached = get_redis().get(cache_key)
            if cached is not None:
                allowed = _text(cached) == "1"
        except Exception:
            pass
        if allowed is None:
            allowed = check(item)
            if allowed is None:
                continue
            try:
                get_redis().setex(cache_key, _SEEN_TTL, "1" if allowed else "0")
            except Exception:
                pass
        if allowed:
            out.append(item)
    return out


def ado_check(pat: str) -> Callable[[Dict[str, Any]], Optional[bool]]:
    """Can the owner of ``pat`` open this work item? Asked of the work item itself."""
    def check(item: Dict[str, Any]) -> Optional[bool]:
        wi_id = str(item["id"]).rsplit(":", 1)[-1]
        try:
            with httpx.Client(verify=tls_verify(), timeout=httpx.Timeout(15.0, connect=5.0),
                              auth=httpx.BasicAuth("", pat)) as client:
                resp = client.get(f"{item['origin']}/_apis/wit/workitems/{wi_id}",
                                  params={"fields": "System.Id", "api-version": "7.0"})
        except httpx.HTTPError:
            return None
        if resp.status_code == 200 and "json" in (resp.headers.get("content-type") or ""):
            return True
        return False if resp.status_code in (401, 403, 404) or resp.status_code == 200 else None
    return check


def visible_to(user_token: str, base: str, found: List[Dict[str, Any]], user_id: str, limit: int) -> List[Dict[str, Any]]:
    """Only the matches this person may open, checked with their own token."""
    out: List[Dict[str, Any]] = []
    if not user_token or not base:
        return out
    with _confluence(user_token) as client:
        for item in found:
            if len(out) >= limit:
                break
            cache_key = f"devbot:index:seen:{user_id}:{item['id']}"
            allowed: Optional[bool] = None
            try:
                cached = get_redis().get(cache_key)
                if cached is not None:
                    allowed = _text(cached) == "1"
            except Exception:
                pass
            if allowed is None:
                try:
                    resp = client.get(f"{base}/rest/api/content/{item['id']}", params={"expand": "version"})
                    allowed = resp.status_code == 200 and "json" in (resp.headers.get("content-type") or "")
                except httpx.HTTPError:
                    continue  # unknown is not yes; not cached either
                try:
                    get_redis().setex(cache_key, _SEEN_TTL, "1" if allowed else "0")
                except Exception:
                    pass
            if allowed:
                out.append(item)
    return out


# ── the admin's review of past fixes ─────────────────────────────────────────

FIX_STATES = ("shown", "held", "hidden", "waiting")


def list_fixes(source: str = "", state: str = "", q: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Past fixes as the index holds them: what everyone is shown, what was held back
    and why, what an admin hid, what is waiting for review. The original notes are not
    here -- they are never stored -- so each row links to where it came from."""
    where = ["source <> 'confluence'"]
    params: List[Any] = []
    if source in ("ado", "snow"):
        where.append("source = %s")
        params.append(source)
    where.append({"shown": "vetted AND held = '' AND NOT hidden AND passages > 0", "held": "vetted AND held <> ''",
                  "hidden": "hidden", "waiting": "NOT vetted"}.get(state, "TRUE"))
    if q.strip():
        where.append("(title ILIKE %s OR summary ILIKE %s OR fix ILIKE %s OR ref ILIKE %s)")
        params += [f"%{q.strip()}%"] * 4
    sql_where = " AND ".join(where)
    total = int((query_one(f"SELECT COUNT(*) AS n FROM devbot_index_pages WHERE {sql_where}", params) or {}).get("n") or 0)
    rows = query_all(
        f"""
        SELECT page_id, source, ref, title, space_key, url, summary, fix, held, vetted, hidden, hidden_by, audience,
               modified, indexed_at, error
          FROM devbot_index_pages WHERE {sql_where}
         ORDER BY indexed_at DESC NULLS LAST, page_id LIMIT %s OFFSET %s
        """,
        params + [max(1, min(int(limit), 200)), max(0, int(offset))],
    )
    counts = query_one(
        "SELECT COUNT(*) FILTER (WHERE vetted AND held = '' AND NOT hidden AND passages > 0) AS shown, "
        "COUNT(*) FILTER (WHERE vetted AND held <> '') AS held, COUNT(*) FILTER (WHERE hidden) AS hidden, "
        "COUNT(*) FILTER (WHERE NOT vetted) AS waiting FROM devbot_index_pages WHERE source <> 'confluence'") or {}
    out = []
    for r in rows:
        state_ = "hidden" if r["hidden"] else "waiting" if not r["vetted"] else "held" if r["held"] else "shown"
        out.append({"id": r["page_id"], "source": r["source"], "ref": r["ref"], "title": r["title"],
                    "where": r["space_key"], "url": r["url"] if r["source"] == "ado" else "",
                    "problem": r["summary"], "fix": r["fix"], "held": r["held"], "state": state_,
                    "hidden_by": r["hidden_by"], "audience": r.get("audience") or "",
                    "changed": str(r.get("modified") or "")[:10],
                    "error": r.get("error") or ""})
    return {"items": out, "total": total, "counts": {k: int(counts.get(k) or 0) for k in FIX_STATES}}


def _new_generation() -> None:
    """Tell every pod its in-memory vectors are stale (they reload on the next search)."""
    try:
        get_redis().set(_GEN_KEY, f"edit-{time.time()}")
    except Exception:
        pass


def set_hidden(page_id: str, hidden: bool, by: str) -> bool:
    rows = execute_returning(
        "UPDATE devbot_index_pages SET hidden = %s, hidden_by = %s WHERE page_id = %s AND source <> 'confluence' "
        "RETURNING page_id", [bool(hidden), by if hidden else "", page_id])
    _new_generation()
    return bool(rows)


def request_review(page_id: str) -> bool:
    """Review this fix again on the next build. Until then it is not shown: a fix an
    admin doubted should not go on being answered with."""
    rows = execute_returning(
        "UPDATE devbot_index_pages SET vetted = FALSE WHERE page_id = %s AND source <> 'confluence' RETURNING page_id",
        [page_id])
    _new_generation()
    return bool(rows)


# ── the admin's view ─────────────────────────────────────────────────────────

def admin_status() -> Dict[str, Any]:
    s = settings()
    pages = query_one(
        "SELECT COUNT(*) FILTER (WHERE version IS NOT NULL AND held = '') AS indexed, COUNT(*) FILTER (WHERE error <> '') AS failed, "
        "COALESCE(SUM(passages), 0) AS passages FROM devbot_index_pages") or {}
    runs = query_all("SELECT id, started_at, finished_at, status, trigger, pages_seen, pages_indexed, pages_removed, "
                     "pages_failed, passages, error, detail FROM devbot_index_runs ORDER BY id DESC LIMIT 6")
    failed_pages = query_all("SELECT page_id, title, url, error, source, ref FROM devbot_index_pages WHERE error <> '' "
                             "ORDER BY indexed_at DESC LIMIT 10")
    dims = 0
    sample = query_one("SELECT LENGTH(vector) AS dims FROM devbot_index_chunks LIMIT 1") or {}
    if sample.get("dims"):
        dims = int(sample["dims"])
    passages_n = int(pages.get("passages") or 0)
    by_source = {r["source"]: {"indexed": int(r["indexed"] or 0), "failed": int(r["failed"] or 0),
                               "held": int(r["held"] or 0), "unreviewed": int(r["unreviewed"] or 0)} for r in query_all(
        "SELECT source, COUNT(*) FILTER (WHERE version IS NOT NULL AND held = '' AND NOT hidden) AS indexed, "
        "COUNT(*) FILTER (WHERE error <> '') AS failed, COUNT(*) FILTER (WHERE held <> '') AS held, "
        "COUNT(*) FILTER (WHERE source <> 'confluence' AND NOT vetted) AS unreviewed "
        "FROM devbot_index_pages GROUP BY source")}
    busy = running()
    if not busy and any(r["status"] == "running" for r in runs):
        bury_orphans()
        runs = query_all("SELECT id, started_at, finished_at, status, trigger, pages_seen, pages_indexed, pages_removed, "
                         "pages_failed, passages, error, detail FROM devbot_index_runs ORDER BY id DESC LIMIT 6")
    for r in runs:
        for name in ("started_at", "finished_at"):
            r[name] = r[name].isoformat() if r.get(name) else ""
        if r["status"] == "running" and not busy:
            r["status"], r["error"] = "died", "The pod building it stopped before it finished."
    return {
        "settings": s,
        "configured": configured(s),
        "hub_ready": config.enabled(),
        "confluence_configured": bool(confluence_base()),
        "running": busy,
        "pages": int(pages.get("indexed") or 0),
        "pages_failed": int(pages.get("failed") or 0),
        "passages": passages_n,
        "memory_mb": round(passages_n * (dims + 120) / 1_000_000, 1),
        "max_passages": config.index_max_chunks(),
        "interval_hours": config.index_interval_hours(),
        "runs": runs,
        "failed_pages": failed_pages,
        "by_source": by_source,
        "sources_on": sources_on(s),
    }
