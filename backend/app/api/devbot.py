"""
DevBot: the chat assistant's API. The page is /ui/devbot.

  GET    /api/devbot/status                    set up? key connected? models, systems, limits
  GET    /api/devbot/usage                     tokens and requests per model; the key's limits and budget
  POST   /api/devbot/models/check              can this model read live data (one small request)
  GET    /api/devbot/conversations             this person's conversations, newest first
  GET    /api/devbot/conversations/{id}        one conversation's questions and answers
  PATCH  /api/devbot/conversations/{id}        rename it
  DELETE /api/devbot/conversations/{id}        delete it
  POST   /api/devbot/conversations/{id}/actions/{action}  how a proposed change ended
  POST   /api/devbot/conversations/{id}/messages/{m}/feedback  thumbs up or down on one answer
  POST   /api/devbot/chat                      ask; the answer streams back as events

The person's model key is stored like every other token, in user_integrations under
the system "devbot", and is set on the Connections page or on the DevBot page through
the same /api/integrations/devbot/token endpoint. It never leaves the backend.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from security import AuthUser, get_current_user

from devbot import config, knowledge, llm, models, monitor, orchestrator, store
from common import audited_error

log = logging.getLogger(__name__)
router = APIRouter()


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str = Field("", max_length=64)
    model: str = Field("", max_length=200)


class CheckBody(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class OutcomeBody(BaseModel):
    message_id: int = Field(..., ge=1)
    status: str = Field(..., pattern="^(done|simulated|declined)$")
    result: str = Field("", max_length=1000)
    url: str = Field("", max_length=2000)


def require_user_id(user: AuthUser) -> str:
    """The signed-in person's id, or 401 (also used by api/adminbot.py)."""
    uid = orchestrator.user_id(user)
    if not uid:
        raise HTTPException(status_code=401, detail="User id missing")
    return uid


@router.get("/status")
def status(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Everything the page needs to decide what to show before the first question."""
    uid = require_user_id(current_user)
    out: Dict[str, Any] = {
        "enabled": config.enabled(),
        "key_connected": False,
        "models": [],
        "model_error": None,
        "default_model": "",
        "systems": orchestrator.system_states(uid),
        "limits": orchestrator.last_limits(uid),
        "key_info": {},
        "key_help_url": config.key_help_url(),
        "history_days": config.history_days(),
        "page_search": {"ready": False},
    }
    if not out["enabled"]:
        return {"success": True, "data": out}
    try:
        out["page_search"] = knowledge.public_status()
    except Exception:
        log.warning("devbot: the page search status could not be read", exc_info=True)
    key = orchestrator.model_key(uid)
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


@router.get("/usage")
def usage(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """What this person's questions have cost, per model, today and over 30 days, beside
    what the gateway says about the key itself (its limits, budget and spend). The two
    are different numbers on purpose: the Hub counts what DevBot asked; the gateway
    counts everything the key was used for, anywhere."""
    uid = require_user_id(current_user)
    key = orchestrator.model_key(uid)
    try:
        summary = store.usage_summary(uid)
    except Exception as exc:
        log.warning("devbot: usage could not be read: %s", exc)
        raise HTTPException(status_code=503, detail="Your usage could not be read right now. Try again in a minute.")
    return {"success": True, "data": {
        **summary,
        "limits": orchestrator.last_limits(uid),
        "key_info": models.key_info(key) if key else {},
        "key_connected": bool(key),
    }}


@router.post("/models/check")
async def check_model(body: CheckBody, request: Request, current_user: AuthUser = Depends(get_current_user)):
    """Find out whether a model can read live data. Costs the person one request."""
    uid = require_user_id(current_user)
    key = orchestrator.model_key(uid)
    if not key:
        raise audited_error(request, 428, "Connect your AI model key first.")
    try:
        result = await llm.probe_tools(key, body.model)
    except llm.LLMError as exc:
        raise audited_error(request, 429 if exc.kind == "limit" else 502, exc.message)
    models.remember(body.model, result.get("tools"), result.get("detail") or "")
    return {"success": True, "data": {"model": body.model, **result}}


def _visible(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The questions and answers, without the lookups the model made on the way."""
    out = []
    for row in rows:
        meta = row.get("meta") or {}
        if row["role"] == "user" or (row["role"] == "assistant" and not meta.get("internal")):
            out.append({"id": row["id"], "role": row["role"], "content": row["content"],
                        "meta": meta, "created_at": row.get("created_at")})
    return out


@router.get("/conversations")
def conversations(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    return {"success": True, "data": store.list_conversations(require_user_id(current_user))}


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, request: Request, current_user: AuthUser = Depends(get_current_user)):
    uid = require_user_id(current_user)
    row = store.get_conversation(uid, conversation_id)
    if not row:
        raise audited_error(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True, "data": {**row, "messages": _visible(store.messages(uid, conversation_id))}}


@router.patch("/conversations/{conversation_id}")
def rename(conversation_id: str, body: RenameBody, request: Request,
           current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    if not store.rename_conversation(require_user_id(current_user), conversation_id, body.title.strip()):
        raise audited_error(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True}


@router.delete("/conversations/{conversation_id}")
def delete(conversation_id: str, request: Request, current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    if not store.delete_conversation(require_user_id(current_user), conversation_id):
        raise audited_error(request, 404, "That conversation does not exist, or it is not yours.")
    return {"success": True}


@router.post("/conversations/{conversation_id}/actions/{action_id}")
def action_outcome(conversation_id: str, action_id: str, body: OutcomeBody, request: Request,
                   current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """How a proposed change ended: confirmed (done, or simulated under Safe Mode) or
    declined. Recorded on the answer that proposed it, so the page shows it and the model
    reads it with the next question. The change itself was made by the dialog, as the
    person; this only writes down what the dialog reported."""
    # The url is shown as a link on the card: only an address a browser may follow.
    url = body.url.strip()
    if url and not url.lower().startswith(("https://", "http://", "/")) or url.startswith("//"):
        url = ""
    patch = {"status": body.status, "result": body.result.strip(), "url": url}
    updated = store.update_action(require_user_id(current_user), conversation_id, body.message_id, action_id[:32], patch)
    if not updated:
        raise audited_error(request, 404, "That proposal does not exist in this conversation.")
    return {"success": True, "data": updated}


class FeedbackBody(BaseModel):
    rating: str = Field("", pattern="^(up|down|)$")
    note: str = Field("", max_length=1000)


@router.post("/conversations/{conversation_id}/messages/{message_id}/feedback")
def feedback(conversation_id: str, message_id: int, body: FeedbackBody, request: Request,
             current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """A thumbs up or down on one answer, with an optional note. Sent with the question
    and the answer it is about, which is what makes it useful to whoever reads it: the
    page says so before the person sends it. An empty rating takes it back."""
    uid = require_user_id(current_user)
    rows = store.messages(uid, conversation_id)
    index = next((i for i, m in enumerate(rows) if int(m["id"]) == message_id and m["role"] == "assistant"), None)
    if index is None:
        raise audited_error(request, 404, "That answer does not exist in this conversation.")
    answer = rows[index]
    question = next((m["content"] for m in reversed(rows[:index]) if m["role"] == "user"), "")
    try:
        monitor.set_feedback(uid, conversation_id, message_id, body.rating, body.note.strip(), question,
                             answer.get("content") or "", answer.get("meta") or {})
        store.update_meta(uid, conversation_id, message_id,
                          {"feedback": {"rating": body.rating, "note": body.note.strip()} if body.rating else None})
    except HTTPException:
        raise
    except Exception:
        log.warning("devbot: feedback was not saved", exc_info=True)
        raise audited_error(request, 503, "Your feedback was not saved. Try again in a minute.")
    return {"success": True, "data": {"rating": body.rating}}


@router.post("/chat")
async def chat(body: ChatBody, current_user: AuthUser = Depends(get_current_user)):
    """Ask a question. The answer streams back as server-sent events (see orchestrator)."""
    require_user_id(current_user)
    return StreamingResponse(
        orchestrator.stream_turn(current_user, body.message, body.conversation_id.strip(), body.model.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers proxied answers by default, which would hold the whole
            # answer back and deliver it at the end. This header turns that off for
            # this response only.
            "X-Accel-Buffering": "no",
        },
    )
