from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from uuid import uuid4

from ..security import AuthUser, get_current_user


router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    conversationId: Optional[str] = None
    userId: Optional[str] = None


conversations: Dict[str, Dict] = {}


@router.post("/chat")
def chat(
    body: ChatRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    if not body.message:
        return {
            "success": False,
            "error": "Message required",
        }

    user_id = body.userId or current_user.get("id")

    user_message = {
        "id": str(uuid4()),
        "role": "user",
        "content": body.message,
        "timestamp": _now_iso(),
    }
    assistant_message = {
        "id": str(uuid4()),
        "role": "assistant",
        "content": _generate_mock_response(body.message),
        "timestamp": _now_iso(),
    }

    if body.conversationId and body.conversationId in conversations:
        conv = conversations[body.conversationId]
        conv["messages"].append(user_message)
        conv["messages"].append(assistant_message)
        conv["updated_at"] = _now_iso()
    else:
        conv_id = body.conversationId or str(uuid4())
        conversations[conv_id] = {
            "id": conv_id,
            "user_id": user_id,
            "title": body.message[:50],
            "messages": [user_message, assistant_message],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    conv_id = body.conversationId or conversations[next(reversed(conversations))]["id"]

    return {
        "success": True,
        "data": {
            "message": assistant_message,
            "conversationId": conv_id,
        },
        "timestamp": _now_iso(),
    }


@router.get("/conversations")
def get_conversations(
    userId: Optional[str] = None,
    current_user: AuthUser = Depends(get_current_user),
):
    effective_user_id = userId or current_user.get("id")
    user_convs = [
        conv for conv in conversations.values() if conv.get("user_id") == effective_user_id
    ]
    user_convs.sort(key=lambda c: c["updated_at"], reverse=True)
    return {
        "success": True,
        "data": user_convs,
        "timestamp": _now_iso(),
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    conv = conversations.get(conversation_id)
    if not conv:
        return {
            "success": False,
            "error": "Conversation not found",
        }
    return {
        "success": True,
        "data": conv,
        "timestamp": _now_iso(),
    }


def _generate_mock_response(message: str) -> str:
    lower = message.lower()
    if "how" in lower or "what" in lower:
        return (
            "Based on the context, the DevOps Control Center provides centralized access "
            "to your development tools and workflows. Would you like details on a specific feature?"
        )
    if "error" in lower or "issue" in lower:
        return (
            "I see you're experiencing an issue. Please share the error message or steps you took, "
            "and I'll help you troubleshoot."
        )
    if "deploy" in lower:
        return (
            "For deployment, you can review pipeline status and recent runs in the Azure DevOps widgets. "
            "I can also outline best practices if you like."
        )
    return (
        "I'm here to help with questions about the DevOps Control Center, your projects, "
        "pipelines, code quality, and more. What would you like to know?"
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


