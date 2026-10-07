"""
The page search index, for admins: its settings, its state, and a Build now.

  GET  /api/devbot/index            the index's state, settings (without credentials) and last runs
  PUT  /api/devbot/index/settings   spaces, credentials, model, on/off -- checked before they are kept
  POST /api/devbot/index/models     embedding models an AI key may use (the key in the body, never the URL)
  POST /api/devbot/index/build      bring the index up to date now, in the background
  POST /api/devbot/index/stop       stop the running build after the entry it is on
  GET  /api/devbot/index/fixes      past fixes as kept: shown, held back, hidden, waiting for review
  POST /api/devbot/index/fixes/hide    hide one fix from everyone (or show it again)
  POST /api/devbot/index/fixes/review  review one fix again on the next build

Every route re-checks admin access itself; the card sitting on an admin page is a
convenience, never the permission. Credentials go in and are never sent back out: the
page is told only whether each one is set.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from devbot import config, knowledge, llm
from security import AuthUser
from common import admin_user, audited_error, failure_text

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("")
def index_status(request: Request, _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    try:
        return {"success": True, "data": knowledge.admin_status()}
    except HTTPException:
        raise
    except Exception:
        log.warning("devbot index: status failed", exc_info=True)
        raise audited_error(request, 503, "The index state could not be read; the details are in the Hub's log.")


class SettingsBody(BaseModel):
    spaces: str = Field("", max_length=2000)
    model: str = Field("", max_length=300)
    enabled: bool = True
    # None keeps the stored value, "" clears it.
    confluence_token: Optional[str] = Field(None, max_length=4000)
    gateway_key: Optional[str] = Field(None, max_length=4000)
    # Past fixes. None keeps what is stored.
    ado_enabled: Optional[bool] = None
    ado_types: Optional[str] = Field(None, max_length=1000)
    ado_field: Optional[str] = Field(None, max_length=300)
    ado_projects: Optional[str] = Field(None, max_length=4000)
    ado_days: Optional[int] = Field(None, ge=30, le=3650)
    ado_collections: Optional[str] = Field(None, max_length=2000)
    # Read a work item's discussion when no fix field is filled.
    ado_comments: Optional[bool] = None
    # The chat model that reviews each past fix before it is kept; "" picks one.
    fix_model: Optional[str] = Field(None, max_length=300)
    snow_enabled: Optional[bool] = None
    snow_table: Optional[str] = Field(None, max_length=80)


class SetupRefused(ValueError):
    """A setting that cannot work, in the Hub's own words for the admin."""


def _check_snow(table: str) -> str:
    """One row of the ticket table, read the way the build will read it."""
    from api.servicenow import _snow_ticket_flow_client

    if not knowledge.snow_available():
        raise SetupRefused("ServiceNow is not configured on this Hub (SNOW_BASE_URL, SNOW_API_USERNAME, SNOW_API_PASSWORD).")
    if not re.fullmatch(r"[a-z0-9_]{1,80}", table):
        raise SetupRefused(f"'{table}' is not a ServiceNow table name.")
    with _snow_ticket_flow_client() as client:
        resp = client.get(f"/api/now/table/{table}", params={
            "sysparm_query": "close_notesISNOTEMPTY", "sysparm_fields": "sys_id", "sysparm_limit": 1})
    if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
        raise SetupRefused(f"ServiceNow refused to list {table} ({resp.status_code}).")
    return f"{table} is readable"


def _check_confluence(token: str, spaces: List[str]) -> List[Dict[str, Any]]:
    base = knowledge.confluence_base()
    if not base:
        return [{"space": s, "ok": False, "detail": "Confluence is not configured on this Hub."} for s in spaces]
    out = []
    with knowledge._confluence(token) as client:
        for key in spaces:
            ok, detail = knowledge.check_space(client, base, key)
            out.append({"space": key, "ok": ok, "detail": detail})
    return out


@router.put("/settings")
def save_settings(body: SettingsBody, request: Request, admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    """Keep the settings only after they are shown to work: every space readable with
    the token, and the model one the key may use. A setting that fails here would
    otherwise fail at 3 a.m. in a build nobody is watching."""
    spaces = knowledge.parse_spaces(body.spaces)
    current = knowledge.settings(secrets=True)
    ado = {k: v for k, v in {
        "enabled": body.ado_enabled, "field": (body.ado_field or "").strip() if body.ado_field is not None else None,
        "types": knowledge.parse_list(body.ado_types) if body.ado_types is not None else None,
        "projects": knowledge.parse_list(body.ado_projects) if body.ado_projects is not None else None,
        "collections": knowledge.parse_list(body.ado_collections) if body.ado_collections is not None else None,
        "comments": body.ado_comments,
        "days": body.ado_days}.items() if v is not None}
    snow = {k: v for k, v in {"enabled": body.snow_enabled,
                              "table": (body.snow_table or "").strip() if body.snow_table is not None else None}.items()
            if v is not None}
    ado_after = {**current["ado"], **ado}
    snow_after = {**current["snow"], **snow}
    token = current["confluence_token"] if body.confluence_token is None else body.confluence_token.strip()
    key = current["gateway_key"] if body.gateway_key is None else body.gateway_key.strip()
    if body.enabled and spaces and not token:
        raise audited_error(request, 400, "Give a Confluence token that can read those spaces.")
    if body.enabled and (spaces or ado_after["enabled"] or snow_after["enabled"]) and not key:
        raise audited_error(request, 400, "Give an AI key for the Hub to build the index with.")
    if ado_after["enabled"] and not ado_after["types"]:
        raise audited_error(request, 400, "Name at least one work item type to take past fixes from (for example Bug).")
    checks: Dict[str, Any] = {"spaces": [], "model": "", "ado": "", "snow": "", "fix_model": ""}
    fix_model = current["fix_model"] if body.fix_model is None else body.fix_model.strip()
    if ado_after["enabled"]:
        checks["ado"] = ("Azure DevOps fixes are read with the Hub's admin token" if knowledge.ado_available() else
                         "Azure DevOps fixes are on, but the Hub has no admin token (AZURE_DEVOPS_ADMIN_PAT), so they are skipped")
    if snow_after["enabled"]:
        try:
            checks["snow"] = _check_snow(snow_after["table"])
        except SetupRefused as exc:
            raise audited_error(request, 400, f"Ticket fixes: {exc}")
        except httpx.HTTPError as exc:
            raise audited_error(request, 502, failure_text("ServiceNow", exc))
    if token and spaces:
        try:
            checks["spaces"] = _check_confluence(token, spaces)
        except httpx.HTTPError as exc:
            raise audited_error(request, 502, failure_text("Confluence", exc))
        bad = [c for c in checks["spaces"] if not c["ok"]]
        if bad:
            raise audited_error(request, 400, "; ".join(f"{c['space']}: {c['detail']}" for c in bad))
    model = body.model.strip()
    if key:
        if not config.enabled():
            raise audited_error(request, 409, "DevBot is not set up on this Hub (DEVBOT_LLM_BASE_URL), so there is no AI "
                                      "service to build the index with.")
        try:
            available = [m["id"] for m in llm.list_models(key, embedding=True)]
        except llm.LLMError as exc:
            raise audited_error(request, 400, f"The AI key: {exc.message}")
        if not available:
            raise audited_error(request, 400, "The AI key may not use any embedding model. Ask for access to one "
                                      "(a multilingual e5 works best here).")
        if model and model not in available:
            raise audited_error(request, 400, f"The AI key may not use {model}. It may use: {', '.join(available)}.")
        chosen = model or knowledge.pick_model(available)
        try:
            vectors, _ = llm.embed(key, chosen, [knowledge.as_passage(chosen, "Check the index can be built.")])
        except llm.LLMError as exc:
            raise audited_error(request, 400, f"{chosen} did not embed a test sentence: {exc.message}")
        checks["model"] = f"{chosen} ({len(vectors[0])} numbers per passage)"
        if (ado_after["enabled"] and knowledge.ado_available()) or snow_after["enabled"]:
            # Past fixes are reviewed by a chat model before they are kept: prove one
            # answers now, not in the first build.
            try:
                chat = [m["id"] for m in llm.list_models(key)]
            except llm.LLMError as exc:
                raise audited_error(request, 400, f"The AI key: {exc.message}")
            if fix_model and fix_model not in chat:
                raise audited_error(request, 400, f"The AI key may not use {fix_model} to review fixes. "
                                          f"It may use: {', '.join(chat) or 'no chat model'}.")
            reviewer = knowledge.pick_chat_model(chat, fix_model)
            if not reviewer:
                raise audited_error(request, 400, "Past fixes are reviewed by a chat model before they are kept, and the AI "
                                          "key may not use any. Ask for access to one, or switch past fixes off.")
            try:
                llm.complete(key, reviewer, [{"role": "user", "content": "Answer with the word OK."}], max_tokens=200)
            except llm.LLMError as exc:
                raise audited_error(request, 400, f"{reviewer} did not answer a test question: {exc.message}")
            checks["fix_model"] = f"fixes are reviewed by {reviewer}"
    knowledge.save_settings(spaces, model, body.enabled, str(admin.get("username") or admin.get("email") or ""),
                            confluence_token=body.confluence_token if body.confluence_token is None else body.confluence_token.strip(),
                            gateway_key=body.gateway_key if body.gateway_key is None else body.gateway_key.strip(),
                            ado=ado, snow=snow, fix_model=fix_model)
    return {"success": True, "data": {"checks": checks, "status": knowledge.admin_status()}}


class KeyBody(BaseModel):
    gateway_key: Optional[str] = Field(None, max_length=4000)


@router.post("/models")
def embedding_models(body: KeyBody, request: Request, _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    key = (body.gateway_key or "").strip() or knowledge.settings(secrets=True)["gateway_key"]
    if not key:
        raise audited_error(request, 400, "Give the AI key first.")
    if not config.enabled():
        raise audited_error(request, 409, "DevBot is not set up on this Hub (DEVBOT_LLM_BASE_URL).")
    try:
        available = [m["id"] for m in llm.list_models(key, embedding=True)]
        chat = [m["id"] for m in llm.list_models(key)]
    except llm.LLMError as exc:
        raise audited_error(request, 400, f"The AI key: {exc.message}")
    return {"success": True, "data": {"models": available, "suggested": knowledge.pick_model(available),
                                      "chat_models": chat, "suggested_chat": knowledge.pick_chat_model(chat)}}


@router.post("/build")
def build_now(request: Request, _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    result = knowledge.start("admin")
    if not result["started"]:
        raise audited_error(request, 409, result["why"])
    return {"success": True, "data": result}


@router.get("/fixes")
def past_fixes(request: Request, source: str = Query("", pattern="^(ado|snow|)$"),
               state: str = Query("", pattern="^(shown|held|hidden|waiting|)$"), q: str = Query("", max_length=200),
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    try:
        return {"success": True, "data": knowledge.list_fixes(source, state, q, limit, offset)}
    except Exception:
        log.warning("devbot index: the past fixes could not be listed", exc_info=True)
        raise audited_error(request, 503, "The past fixes could not be read; the details are in the Hub's log.")


class FixBody(BaseModel):
    # In the body: the id holds colons and a ticket's sys_id.
    id: str = Field(..., min_length=3, max_length=300)
    hidden: bool = True


@router.post("/fixes/hide")
def hide_fix(body: FixBody, request: Request, admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    if not knowledge.set_hidden(body.id, body.hidden, str(admin.get("username") or admin.get("email") or "")):
        raise audited_error(request, 404, "There is no such past fix in the index.")
    return {"success": True}


@router.post("/fixes/review")
def review_fix_again(body: FixBody, request: Request, _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    if not knowledge.request_review(body.id):
        raise audited_error(request, 404, "There is no such past fix in the index.")
    return {"success": True}


@router.post("/stop")
def stop_build(request: Request, _admin: AuthUser = Depends(admin_user)) -> Dict[str, Any]:
    """Stop the build within seconds, whatever it is doing. What it indexed so far
    stays, and the next build carries on from there (only what is missing or changed is
    read). A build left "running" by a pod that is gone is marked stopped at once."""
    outcome = knowledge.request_stop()
    if outcome == "none":
        raise audited_error(request, 409, "No build is running.")
    return {"success": True, "data": {"outcome": outcome}}
