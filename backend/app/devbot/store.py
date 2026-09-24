"""
Conversations, their messages, and what each person's questions cost.

Every read and write here takes the OWNER as well as the id. A conversation id is not
a secret -- it is in the page's address bar -- so "this id exists" must never be
enough to read it.

Messages keep their roles as the model saw them, so a conversation can be replayed to
the model exactly: ``user``, ``assistant`` (with ``meta.tool_calls`` when it asked for
lookups, and ``meta.internal`` so the page does not show those), and ``tool`` (the
compact result it was handed). The page itself reads only the person's questions and
the final answers, with the steps and sources that led to each stored on the answer.

Kept for DEVBOT_HISTORY_DAYS after a conversation was last used, then removed by the
backend's retention loop (every six hours). Usage rows are kept for 90 days: they are
what the Usage view adds up.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from db import execute, execute_returning, query_all, query_one

USAGE_DAYS = 90


def ensure_tables() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_conversations (
            id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_id     TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_devbot_conversations_user "
        "ON devbot_conversations (user_id, updated_at DESC)"
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES devbot_conversations(id) ON DELETE CASCADE,
            role            VARCHAR(16) NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_devbot_messages_conversation "
        "ON devbot_messages (conversation_id, id)"
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_usage (
            id                BIGSERIAL PRIMARY KEY,
            user_id           TEXT NOT NULL,
            model             TEXT NOT NULL,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            requests          INTEGER NOT NULL DEFAULT 0,
            created_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_devbot_usage_user ON devbot_usage (user_id, created_at)")


def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    out = dict(row)
    for key in ("id", "conversation_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    if isinstance(out.get("meta"), str):
        try:
            out["meta"] = json.loads(out["meta"])
        except ValueError:
            out["meta"] = {}
    return out


# ── conversations ────────────────────────────────────────────────────────────

def create_conversation(user_id: str, title: str, model: str) -> Dict[str, Any]:
    rows = execute_returning(
        "INSERT INTO devbot_conversations (user_id, title, model) VALUES (%s, %s, %s) "
        "RETURNING id, title, model, created_at, updated_at",
        [user_id, title[:200], model[:200]],
    )
    if not rows:
        raise RuntimeError("The new conversation was not saved.")
    return _row(rows[0]) or {}


def get_conversation(user_id: str, conversation_id: str) -> Optional[Dict[str, Any]]:
    return _row(query_one(
        "SELECT id, title, model, created_at, updated_at FROM devbot_conversations "
        "WHERE id::text = %s AND user_id = %s",
        [conversation_id, user_id],
    ))


def list_conversations(user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    rows = query_all(
        "SELECT id, title, model, created_at, updated_at FROM devbot_conversations "
        "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
        [user_id, limit],
    )
    return [r for r in (_row(x) for x in rows) if r]


def rename_conversation(user_id: str, conversation_id: str, title: str) -> bool:
    rows = execute_returning(
        "UPDATE devbot_conversations SET title = %s WHERE id::text = %s AND user_id = %s RETURNING id",
        [title[:200], conversation_id, user_id],
    )
    return bool(rows)


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    rows = execute_returning(
        "DELETE FROM devbot_conversations WHERE id::text = %s AND user_id = %s RETURNING id",
        [conversation_id, user_id],
    )
    return bool(rows)


def touch_conversation(user_id: str, conversation_id: str, model: str) -> None:
    execute(
        "UPDATE devbot_conversations SET updated_at = CURRENT_TIMESTAMP, model = %s "
        "WHERE id::text = %s AND user_id = %s",
        [model[:200], conversation_id, user_id],
    )


# ── messages ─────────────────────────────────────────────────────────────────

def add_message(conversation_id: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    rows = execute_returning(
        "INSERT INTO devbot_messages (conversation_id, role, content, meta) VALUES (%s, %s, %s, %s) RETURNING id",
        [conversation_id, role, content or "", Json(meta or {})],
    )
    if not rows:
        raise RuntimeError("The message was not saved.")
    return int(rows[0]["id"])


def messages(user_id: str, conversation_id: str, limit: int = 400) -> List[Dict[str, Any]]:
    """The newest ``limit`` messages, oldest first, owner-checked."""
    rows = query_all(
        """
        SELECT m.id, m.role, m.content, m.meta, m.created_at
          FROM devbot_messages m
          JOIN devbot_conversations c ON c.id = m.conversation_id
         WHERE c.id::text = %s AND c.user_id = %s
         ORDER BY m.id DESC
         LIMIT %s
        """,
        [conversation_id, user_id, limit],
    )
    out = [r for r in (_row(x) for x in reversed(rows)) if r]
    # A cut that lands mid-turn would hand the model a tool result without the call
    # that asked for it. Start at a question.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def update_meta(user_id: str, conversation_id: str, message_id: int, patch: Dict[str, Any]) -> bool:
    rows = execute_returning(
        """
        UPDATE devbot_messages m SET meta = m.meta || %s
          FROM devbot_conversations c
         WHERE m.id = %s AND m.conversation_id = c.id
           AND c.id::text = %s AND c.user_id = %s
        RETURNING m.id
        """,
        [Json(patch), message_id, conversation_id, user_id],
    )
    return bool(rows)


def update_action(user_id: str, conversation_id: str, message_id: int, action_id: str,
                  patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record how a proposed change ended, on the answer that proposed it.

    The whole list is rewritten: the proposals of one answer are a handful of small
    objects, and a JSONB path update keyed on a position would record the outcome on the
    wrong card the moment the list changed shape.
    """
    row = query_one(
        """
        SELECT m.meta FROM devbot_messages m
          JOIN devbot_conversations c ON c.id = m.conversation_id
         WHERE m.id = %s AND c.id::text = %s AND c.user_id = %s AND m.role = 'assistant'
        """,
        [message_id, conversation_id, user_id],
    )
    if not row:
        return None
    meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
    actions = list(meta.get("actions") or [])
    found = None
    for item in actions:
        if item.get("id") == action_id:
            item.update(patch)
            found = item
    if found is None:
        return None
    update_meta(user_id, conversation_id, message_id, {"actions": actions})
    return found


def replace_content(user_id: str, conversation_id: str, message_id: int, content: str) -> bool:
    rows = execute_returning(
        """
        UPDATE devbot_messages m SET content = %s
          FROM devbot_conversations c
         WHERE m.id = %s AND m.conversation_id = c.id
           AND c.id::text = %s AND c.user_id = %s
        RETURNING m.id
        """,
        [content, message_id, conversation_id, user_id],
    )
    return bool(rows)


# ── usage ────────────────────────────────────────────────────────────────────

def record_usage(user_id: str, model: str, prompt_tokens: int, completion_tokens: int, requests: int) -> None:
    execute(
        "INSERT INTO devbot_usage (user_id, model, prompt_tokens, completion_tokens, requests) "
        "VALUES (%s, %s, %s, %s, %s)",
        [user_id, model[:200], int(prompt_tokens or 0), int(completion_tokens or 0), int(requests or 0)],
    )


def usage_summary(user_id: str) -> Dict[str, Any]:
    """Tokens and requests per model, today and over the last 30 days."""
    rows = query_all(
        """
        SELECT model,
               COALESCE(SUM(prompt_tokens)     FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS today_in,
               COALESCE(SUM(completion_tokens) FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS today_out,
               COALESCE(SUM(requests)          FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS today_requests,
               COALESCE(SUM(prompt_tokens), 0)     AS month_in,
               COALESCE(SUM(completion_tokens), 0) AS month_out,
               COALESCE(SUM(requests), 0)          AS month_requests,
               COUNT(*)                            AS questions
          FROM devbot_usage
         WHERE user_id = %s AND created_at >= now() - interval '30 days'
         GROUP BY model
         ORDER BY SUM(prompt_tokens) + SUM(completion_tokens) DESC
        """,
        [user_id],
    )
    return {"models": [{k: (int(v) if k != "model" else v) for k, v in dict(r).items()} for r in rows]}


def purge(days: int) -> None:
    execute(
        "DELETE FROM devbot_conversations WHERE updated_at < now() - (%s * interval '1 day')",
        [int(days)],
    )
    execute(
        "DELETE FROM devbot_usage WHERE created_at < now() - (%s * interval '1 day')",
        [USAGE_DAYS],
    )
