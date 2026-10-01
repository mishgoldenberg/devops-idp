"""
AdminBot: DevBot's loop over the Hub's own records, for admins. The page is /ui/adminbot.

  GET    /api/adminbot/status                    set up? the Hub key's models and limits
  GET    /api/adminbot/conversations             this admin's AdminBot conversations
  GET    /api/adminbot/conversations/{id}        one conversation
  PATCH  /api/adminbot/conversations/{id}        rename it
  DELETE /api/adminbot/conversations/{id}        delete it
  POST   /api/adminbot/conversations/{id}/actions/{action}      record a declined proposal
  POST   /api/adminbot/conversations/{id}/actions/{action}/run  confirm a proposal: perform it
  POST   /api/adminbot/chat                      ask; the answer streams back as events

Every route re-checks admin access (has_effective_admin_access_live): a menu item that
only admins see is a convenience, never the permission. Answers use the Hub's AI key,
set on Platform Managing, so no admin needs a key of their own.

A confirmed proposal is performed from the proposal as STORED on the answer, never from
what the page sends back, and through the admin pages' own endpoint functions
(tools.hub.run_action), so every guard they have applies here too.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import audit
from security import AuthUser, get_current_user, has_effective_admin_access_live

from devbot import config, llm, models, orchestrator, store
from devbot.tools import hub

log = logging.getLogger(__name__)
router = APIRouter()
BOT = "admin"


def _admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AdminBot is for the Hub's admins.")
    return user


def _uid(user: AuthUser) -> str:
    uid = orchestrator.user_id(user)
    if not uid:
        raise HTTPException(status_code=401, detail="User id missing")
    return uid


def _fail(request: Request, code: int, detail: str) -> HTTPException:
    try:
        request.state.audit_detail = detail
    except Exception:
        pass
    return HTTPException(status_code=code, detail=detail)


@router.get("/status")
def bot_status(admin: AuthUser = Depends(_admin)) -> Dict[str, Any]:
    """The same shape DevBot's page reads, for the Hub's key: the page is shared."""
    out: Dict[str, Any] = {
        "enabled": config.enabled(),
        "key_connected": False,
        "models": [],
        "model_error": None,
        "default_model": "",
        "systems": {"hub": "connected"},
        "limits": orchestrator.last_limits("hub"),
        "key_info": {},
        "history_days": config.history_days(),
        "page_search": {"ready": False},
    }
    if not out["enabled"]:
        return {"success": True, "data": out}
    key = orchestrator.hub_key()
    out["key_connected"] = bool(key)
    if not key:
        return {"success": True, "data": out}
    try:
        available = models.available(key)
        out["models"] = available
        chosen = models.choose(available)
        out["default_model"] = chosen["id"] if chosen else ""
    except llm.LLMError as exc:
        out["model_error"] = {"kind": exc.kind, "message": exc.message}
    out["key_info"] = models.key_info(key)
    return {"success": True, "data": out}


class CheckBody(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)


@router.post("/models/check")
async def check_model(body: CheckBody, request: Request, admin: AuthUser = Depends(_admin)):
    key = orchestrator.hub_key()
    if not key:
        raise _fail(request, 428, "The Hub's AI key is not set (Platform Managing, DevBot search index).")
    try:
        result = await llm.probe_tools(key, body.model)
    except llm.LLMError as exc:
        raise _fail(request, 429 if exc.kind == "limit" else 502, exc.message)
    models.remember(body.model, result.get("tools"), result.get("detail") or "")
    return {"success": True, "data": {"model": body.model, **result}}


def _visible(rows):
    out = []
    for row in rows:
        meta = row.get("meta") or {}
        if row["role"] == "user" or (row["role"] == "assistant" and not meta.get("internal")):
            out.append({"id": row["id"], "role": row["role"], "content": row["content"],
                        "meta": meta, "created_at": row.get("created_at")})
    return out


@router.get("/conversations")
def conversations(admin: AuthUser = Depends(_admin)) -> Dict[str, Any]:
    return {"success": True, "data": store.list_conversations(_uid(admin), bot=BOT)}


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, request: Request, admin: AuthUser = Depends(_admin)):
    uid = _uid(admin)
    row = store.get_conversation(uid, conversation_id, BOT)
    if not row:
        raise _fail(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True, "data": {**row, "messages": _visible(store.messages(uid, conversation_id, bot=BOT))}}


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.patch("/conversations/{conversation_id}")
def rename(conversation_id: str, body: RenameBody, request: Request, admin: AuthUser = Depends(_admin)):
    if not store.rename_conversation(_uid(admin), conversation_id, body.title.strip(), BOT):
        raise _fail(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True}


@router.delete("/conversations/{conversation_id}")
def delete(conversation_id: str, request: Request, admin: AuthUser = Depends(_admin)):
    if not store.delete_conversation(_uid(admin), conversation_id, BOT):
        raise _fail(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True}


class OutcomeBody(BaseModel):
    message_id: int = Field(..., ge=1)
    status: str = Field(..., pattern="^(declined)$")
    result: str = Field("", max_length=1000)
    url: str = Field("", max_length=2000)


@router.post("/conversations/{conversation_id}/actions/{action_id}")
def action_outcome(conversation_id: str, action_id: str, body: OutcomeBody, request: Request,
                   admin: AuthUser = Depends(_admin)) -> Dict[str, Any]:
    """Only a DECLINE is recorded this way: a confirmation goes through /run, which
    performs it, so "done" is never something the page merely claims."""
    updated = store.update_action(_uid(admin), conversation_id, body.message_id, action_id[:32],
                                  {"status": "declined", "result": "", "url": ""}, BOT)
    if not updated:
        raise _fail(request, 404, "That proposal does not exist in this conversation.")
    return {"success": True, "data": updated}


class RunBody(BaseModel):
    message_id: int = Field(..., ge=1)
    # The one thing the admin may change in the dialog: the note to the requester.
    comment: Optional[str] = Field(None, max_length=500)


@router.post("/conversations/{conversation_id}/actions/{action_id}/run")
def run_action(conversation_id: str, action_id: str, body: RunBody, request: Request,
               admin: AuthUser = Depends(_admin)) -> Dict[str, Any]:
    uid = _uid(admin)
    action = store.find_action(uid, conversation_id, body.message_id, action_id[:32], BOT)
    if not action:
        raise _fail(request, 404, "That proposal does not exist in this conversation.")
    if action.get("status") not in (None, "proposed"):
        raise _fail(request, 409, f"That proposal was already {action.get('status')}.")
    record = {"kind": action.get("kind"), "target": action.get("target"), "comment": body.comment,
              "conversation_id": conversation_id}
    try:
        outcome = hub.run_action(admin, action, body.comment)
    except HTTPException as exc:
        audit.log(audit.Action.ADMINBOT_ACTION, level=audit.Level.WARNING, user_email=str(admin.get("email") or ""),
                  metadata={**record, "refused": str(exc.detail)})
        raise _fail(request, exc.status_code, str(exc.detail))
    except Exception as exc:
        log.warning("adminbot: %s failed", action.get("kind"), exc_info=True)
        raise _fail(request, 500, f"It was not done: {type(exc).__name__}. The details are in the Hub's log.")
    audit.log(audit.Action.ADMINBOT_ACTION, user_email=str(admin.get("email") or ""),
              metadata={**record, "result": outcome["result"]})
    updated = store.update_action(uid, conversation_id, body.message_id, action_id[:32],
                                  {"status": "done", "result": outcome["result"], "url": outcome.get("url") or ""}, BOT)
    return {"success": True, "data": updated or {**action, "status": "done", **outcome}}


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str = Field("", max_length=64)
    model: str = Field("", max_length=200)


@router.post("/chat")
async def chat(body: ChatBody, admin: AuthUser = Depends(_admin)):
    _uid(admin)
    return StreamingResponse(
        orchestrator.stream_turn(admin, body.message, body.conversation_id.strip(), body.model.strip(), bot=BOT),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
