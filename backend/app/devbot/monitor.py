"""
How DevBot is doing, for admins: one row per question, and the feedback people give.

WHAT IS RECORDED, AND WHAT IS NOT
  Every question leaves one devbot_events row: who asked, which model, how it ended
  (answered, answered from what was found, failed with which kind of error, stopped),
  what it cost, how long it took, and each lookup it made with whether it worked and
  how long it took. NOT the question or the answer: a person's conversations are
  theirs, and the monitoring page must not become a way to read them.

  The only question and answer text an admin sees is what a person SENT with a thumbs
  up or down, and the page they send it from says so before they do.

AdminBot's questions are recorded in the same table with bot = 'admin': every figure
below is DevBot's alone, and AdminBot's show only in what the Hub's own AI key spent
(hub_key_spend), beside what the index builds spent with it.

Question rows are kept for two years (a few hundred bytes each, numbers only), so
"All time" means something; feedback, which carries text, for 180 days. Purged by the
backend's retention loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from db import execute, execute_returning, query_all, query_one

log = logging.getLogger(__name__)

KEEP_DAYS = 730
FEEDBACK_DAYS = 180
OUTCOMES = ("answered", "found_only", "failed", "stopped")


def ensure_tables() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_events (
            id                 BIGSERIAL PRIMARY KEY,
            user_id            TEXT NOT NULL,
            conversation_id    TEXT NOT NULL DEFAULT '',
            message_id         BIGINT,
            model              TEXT NOT NULL DEFAULT '',
            outcome            TEXT NOT NULL,
            error_kind         TEXT NOT NULL DEFAULT '',
            requests           INTEGER NOT NULL DEFAULT 0,
            prompt_tokens      INTEGER NOT NULL DEFAULT 0,
            completion_tokens  INTEGER NOT NULL DEFAULT 0,
            duration_ms        INTEGER NOT NULL DEFAULT 0,
            limit_waits        INTEGER NOT NULL DEFAULT 0,
            tools              JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_devbot_events_time ON devbot_events (created_at)")
    execute("CREATE INDEX IF NOT EXISTS idx_devbot_events_user ON devbot_events (user_id, created_at)")
    # Which assistant was asked (Round 134): rows written before were all DevBot's.
    execute("ALTER TABLE devbot_events ADD COLUMN IF NOT EXISTS bot TEXT NOT NULL DEFAULT 'devbot'")
    execute(
        """
        CREATE TABLE IF NOT EXISTS devbot_feedback (
            id               BIGSERIAL PRIMARY KEY,
            user_id          TEXT NOT NULL,
            conversation_id  TEXT NOT NULL,
            message_id       BIGINT NOT NULL,
            rating           TEXT NOT NULL,
            note             TEXT NOT NULL DEFAULT '',
            question         TEXT NOT NULL DEFAULT '',
            answer           TEXT NOT NULL DEFAULT '',
            model            TEXT NOT NULL DEFAULT '',
            tools            JSONB NOT NULL DEFAULT '[]'::jsonb,
            sources          JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, message_id)
        )
        """
    )


# ── recording ────────────────────────────────────────────────────────────────

def record(user_id: str, *, conversation_id: str, message_id: Optional[int], model: str, outcome: str,
           error_kind: str, requests: int, prompt_tokens: int, completion_tokens: int, duration_ms: int,
           limit_waits: int, steps: List[Dict[str, Any]], bot: str = "devbot") -> None:
    """One question's outcome. Never raises: it only RECORDS finished work."""
    try:
        tools = [{"tool": str(s.get("tool") or ""), "system": str(s.get("system") or ""),
                  "ok": s.get("status") == "done", "ms": int(s.get("ms") or 0),
                  **({"error": str(s.get("detail") or "")[:200]} if s.get("status") != "done" else {})}
                 for s in steps]
        execute(
            """
            INSERT INTO devbot_events (user_id, conversation_id, message_id, model, outcome, error_kind, requests,
                                       prompt_tokens, completion_tokens, duration_ms, limit_waits, tools, bot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [user_id, conversation_id or "", message_id, (model or "")[:200], outcome, (error_kind or "")[:40],
             int(requests or 0), int(prompt_tokens or 0), int(completion_tokens or 0), int(duration_ms or 0),
             int(limit_waits or 0), Json(tools), bot],
        )
    except Exception:
        log.warning("devbot: the question's outcome was not recorded for monitoring", exc_info=True)


def set_feedback(user_id: str, conversation_id: str, message_id: int, rating: str, note: str,
                 question: str, answer: str, meta: Dict[str, Any]) -> None:
    """A thumbs up or down, with what it was about. ``rating`` "" takes it back."""
    if not rating:
        execute("DELETE FROM devbot_feedback WHERE user_id = %s AND message_id = %s", [user_id, message_id])
        return
    tools = [{"tool": s.get("tool"), "ok": s.get("status") == "done"} for s in meta.get("steps") or []]
    sources = [{"title": s.get("title"), "url": s.get("url"), "system": s.get("system")} for s in meta.get("sources") or []]
    execute(
        """
        INSERT INTO devbot_feedback (user_id, conversation_id, message_id, rating, note, question, answer, model, tools, sources)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, message_id) DO UPDATE SET rating = EXCLUDED.rating, note = EXCLUDED.note,
            updated_at = CURRENT_TIMESTAMP
        """,
        [user_id, conversation_id, message_id, rating, (note or "")[:1000], (question or "")[:2000],
         (answer or "")[:6000], str(meta.get("model") or "")[:200], Json(tools), Json(sources)],
    )


# ── what people found useful, for the page search ────────────────────────────

_votes: Dict[str, Any] = {"at": 0.0, "by_url": {}}
_votes_lock = threading.Lock()


def source_votes() -> Dict[str, int]:
    """Net thumbs (up minus down) per source link over the last 180 days, cached ten
    minutes: a page that answered well is worth a little more the next time."""
    with _votes_lock:
        if time.time() - _votes["at"] < 600:
            return _votes["by_url"]
    try:
        rows = query_all(
            """
            SELECT s->>'url' AS url,
                   SUM(CASE WHEN f.rating = 'up' THEN 1 ELSE -1 END) AS net
              FROM devbot_feedback f, jsonb_array_elements(f.sources) s
             WHERE f.updated_at > now() - interval '180 days' AND s->>'url' IS NOT NULL
             GROUP BY s->>'url'
            """
        )
        by_url = {str(r["url"]): int(r["net"] or 0) for r in rows}
    except Exception:
        log.warning("devbot: feedback votes could not be read", exc_info=True)
        by_url = {}
    with _votes_lock:
        _votes.update(at=time.time(), by_url=by_url)
    return by_url


# ── the admin's view ─────────────────────────────────────────────────────────

def _days(days: int) -> int:
    """The period in days. 0 is "All time": from the first question recorded (at most
    KEEP_DAYS, which is everything that is kept)."""
    if int(days or 0) <= 0:
        row = query_one("SELECT EXTRACT(DAY FROM now() - MIN(created_at)) AS d FROM devbot_events "
                        "WHERE bot = 'devbot'") or {}
        return max(1, min(int(row.get("d") or 0) + 1, KEEP_DAYS))
    return max(1, min(int(days), KEEP_DAYS))


def hub_key_spend(d: int) -> Dict[str, Any]:
    """What the Hub's own AI key spent in the period: AdminBot's questions, and the
    requests the index builds made (embeddings, and reviews of past fixes)."""
    admin = query_one(
        "SELECT COUNT(*) AS questions, COALESCE(SUM(requests), 0) AS requests, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens, COUNT(DISTINCT user_id) AS admins "
        "FROM devbot_events WHERE bot = 'admin' AND created_at > now() - (%s * interval '1 day')", [d]) or {}
    index = query_one(
        "SELECT COUNT(*) AS builds, COALESCE(SUM((detail->'requests'->>'embed')::int), 0) AS embed, "
        "COALESCE(SUM((detail->'requests'->>'review')::int), 0) AS review "
        "FROM devbot_index_runs WHERE started_at > now() - (%s * interval '1 day')", [d]) or {}
    return {"adminbot_questions": int(admin.get("questions") or 0), "adminbot_requests": int(admin.get("requests") or 0),
            "adminbot_tokens": int(admin.get("tokens") or 0), "admins": int(admin.get("admins") or 0),
            "index_builds": int(index.get("builds") or 0), "index_embed_requests": int(index.get("embed") or 0),
            "index_reviews": int(index.get("review") or 0)}


def overview(days: int = 30) -> Dict[str, Any]:
    d = _days(days)
    totals = query_one(
        """
        SELECT COUNT(*) AS questions,
               COUNT(DISTINCT user_id) AS people,
               COUNT(*) FILTER (WHERE outcome = 'answered') AS answered,
               COUNT(*) FILTER (WHERE outcome = 'found_only') AS found_only,
               COUNT(*) FILTER (WHERE outcome = 'failed') AS failed,
               COUNT(*) FILTER (WHERE outcome = 'stopped') AS stopped,
               COALESCE(SUM(requests), 0) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(limit_waits), 0) AS limit_waits,
               COUNT(*) FILTER (WHERE error_kind IN ('limit', 'budget')) AS limit_failures,
               COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_ms), 0) AS median_ms,
               COALESCE(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY duration_ms), 0) AS p90_ms
          FROM devbot_events
         WHERE bot = 'devbot' AND created_at > now() - (%s * interval '1 day')
        """,
        [d],
    ) or {}
    feedback = query_one(
        "SELECT COUNT(*) FILTER (WHERE rating = 'up') AS up, COUNT(*) FILTER (WHERE rating = 'down') AS down "
        "FROM devbot_feedback WHERE updated_at > now() - (%s * interval '1 day')", [min(d, FEEDBACK_DAYS)]) or {}
    # A long period is drawn in weeks: two years of daily bars is a solid block.
    unit = "day" if d <= 120 else "week"
    buckets = d if unit == "day" else d // 7 + 1
    keys = query_one("SELECT COUNT(DISTINCT user_id) AS n FROM user_integrations WHERE system = 'devbot'") or {}
    daily = query_all(
        """
        SELECT to_char(day, 'YYYY-MM-DD') AS day,
               COUNT(e.id) AS questions,
               COUNT(e.id) FILTER (WHERE e.outcome = 'failed') AS failed,
               COUNT(DISTINCT e.user_id) AS people
          FROM generate_series(date_trunc(%s, now()) - ((%s - 1) * ('1 ' || %s)::interval), date_trunc(%s, now()),
                               ('1 ' || %s)::interval) AS day
          LEFT JOIN devbot_events e ON e.bot = 'devbot' AND e.created_at >= day
                                   AND e.created_at < day + ('1 ' || %s)::interval
         GROUP BY day ORDER BY day
        """,
        [unit, buckets, unit, unit, unit, unit],
    )
    models = query_all(
        """
        SELECT model, COUNT(*) AS questions, COUNT(*) FILTER (WHERE outcome = 'failed') AS failed,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
               COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_ms), 0) AS median_ms
          FROM devbot_events WHERE bot = 'devbot' AND created_at > now() - (%s * interval '1 day')
         GROUP BY model ORDER BY COUNT(*) DESC
        """,
        [d],
    )
    errors = query_all(
        """
        SELECT error_kind, COUNT(*) AS n FROM devbot_events
         WHERE bot = 'devbot' AND created_at > now() - (%s * interval '1 day') AND outcome = 'failed'
         GROUP BY error_kind ORDER BY COUNT(*) DESC
        """,
        [d],
    )
    tools = query_all(
        """
        SELECT t->>'tool' AS tool, MAX(t->>'system') AS system, COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE (t->>'ok')::boolean IS NOT TRUE) AS failed,
               COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (t->>'ms')::int), 0) AS median_ms,
               COALESCE(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY (t->>'ms')::int), 0) AS p90_ms
          FROM devbot_events e, jsonb_array_elements(e.tools) t
         WHERE e.bot = 'devbot' AND e.created_at > now() - (%s * interval '1 day')
         GROUP BY t->>'tool' ORDER BY COUNT(*) DESC
        """,
        [d],
    )
    tool_errors = query_all(
        """
        SELECT t->>'tool' AS tool, t->>'error' AS error, COUNT(*) AS n, MAX(e.created_at) AS last_at
          FROM devbot_events e, jsonb_array_elements(e.tools) t
         WHERE e.bot = 'devbot' AND e.created_at > now() - (%s * interval '1 day') AND (t->>'ok')::boolean IS NOT TRUE
         GROUP BY t->>'tool', t->>'error' ORDER BY MAX(e.created_at) DESC LIMIT 15
        """,
        [d],
    )

    def num(row: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in dict(row).items():
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            elif isinstance(v, (int, float)) or (v is not None and type(v).__name__ == "Decimal"):
                out[k] = int(round(float(v)))
            else:
                out[k] = v
        return out

    return {
        "days": d,
        "all_time": int(days or 0) <= 0,
        "bucket": unit,
        "feedback_days": min(d, FEEDBACK_DAYS),
        "hub_key": hub_key_spend(d),
        "totals": {**num(totals), "up": int(feedback.get("up") or 0), "down": int(feedback.get("down") or 0),
                   "keys_connected": int(keys.get("n") or 0)},
        "daily": [num(r) for r in daily],
        "models": [num(r) for r in models],
        "errors": [num(r) for r in errors],
        "tools": [num(r) for r in tools],
        "tool_errors": [num(r) for r in tool_errors],
    }


def people(days: int = 30) -> List[Dict[str, Any]]:
    """Everyone who has an AI key connected or asked anything in the period."""
    d = _days(days)
    rows = query_all(
        """
        WITH ev AS (
            SELECT user_id, COUNT(*) AS questions,
                   COUNT(*) FILTER (WHERE outcome = 'failed') AS failed,
                   COUNT(*) FILTER (WHERE error_kind IN ('limit', 'budget')) AS limited,
                   COALESCE(SUM(limit_waits), 0) AS limit_waits,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                   COALESCE(SUM(requests), 0) AS requests,
                   MAX(created_at) AS last_at,
                   MODE() WITHIN GROUP (ORDER BY model) AS model
              FROM devbot_events WHERE bot = 'devbot' AND created_at > now() - (%s * interval '1 day')
             GROUP BY user_id
        ), fb AS (
            SELECT user_id, COUNT(*) FILTER (WHERE rating = 'up') AS up, COUNT(*) FILTER (WHERE rating = 'down') AS down
              FROM devbot_feedback WHERE updated_at > now() - (%s * interval '1 day') GROUP BY user_id
        ), keys AS (
            SELECT DISTINCT user_id FROM user_integrations WHERE system = 'devbot'
        ), everyone AS (
            SELECT user_id FROM ev UNION SELECT user_id FROM keys
        )
        SELECT p.user_id, u.username, u.email, u.full_name,
               (k.user_id IS NOT NULL) AS key_connected,
               COALESCE(ev.questions, 0) AS questions, COALESCE(ev.failed, 0) AS failed,
               COALESCE(ev.limited, 0) AS limited, COALESCE(ev.limit_waits, 0) AS limit_waits,
               COALESCE(ev.tokens, 0) AS tokens, COALESCE(ev.requests, 0) AS requests,
               ev.last_at, ev.model, COALESCE(fb.up, 0) AS up, COALESCE(fb.down, 0) AS down,
               (SELECT MAX(created_at) FROM devbot_events x WHERE x.user_id = p.user_id AND x.bot = 'devbot') AS ever_at
          FROM everyone p
          LEFT JOIN users u ON u.id::text = p.user_id
          LEFT JOIN ev ON ev.user_id = p.user_id
          LEFT JOIN fb ON fb.user_id = p.user_id
          LEFT JOIN keys k ON k.user_id = p.user_id
         ORDER BY ev.questions DESC NULLS LAST, u.username
        """,
        [d, d],
    )
    out = []
    for r in rows:
        row = dict(r)
        for k in ("last_at", "ever_at"):
            row[k] = row[k].isoformat() if row.get(k) else ""
        for k in ("questions", "failed", "limited", "limit_waits", "tokens", "requests", "up", "down"):
            row[k] = int(row.get(k) or 0)
        out.append(row)
    return out


def feedback(rating: str = "", days: int = 30, limit: int = 50) -> List[Dict[str, Any]]:
    d = _days(days)
    params: List[Any] = [d]
    where = "f.updated_at > now() - (%s * interval '1 day')"
    if rating in ("up", "down"):
        where += " AND f.rating = %s"
        params.append(rating)
    rows = query_all(
        f"""
        SELECT f.id, f.rating, f.note, f.question, f.answer, f.model, f.tools, f.sources, f.updated_at,
               u.username, u.full_name, u.email
          FROM devbot_feedback f LEFT JOIN users u ON u.id::text = f.user_id
         WHERE {where}
         ORDER BY f.updated_at DESC LIMIT %s
        """,
        params + [max(1, min(int(limit), 200))],
    )
    out = []
    for r in rows:
        row = dict(r)
        row["updated_at"] = row["updated_at"].isoformat() if row.get("updated_at") else ""
        for k in ("tools", "sources"):
            if isinstance(row.get(k), str):
                row[k] = json.loads(row[k] or "[]")
        out.append(row)
    return out


def purge() -> None:
    execute("DELETE FROM devbot_events WHERE created_at < now() - (%s * interval '1 day')", [KEEP_DAYS])
    execute("DELETE FROM devbot_feedback WHERE updated_at < now() - (%s * interval '1 day')", [FEEDBACK_DAYS])
