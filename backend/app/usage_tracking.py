"""How much each person actually uses the portal: sessions, active time, pages.

WHAT "ACTIVE" MEANS
-------------------
A tab that is open is not a person using it. The browser reports a second as
active only while the tab is VISIBLE and somebody has moved the mouse, typed or
scrolled in the last two minutes (portal-chrome.html, usage beat). A dashboard left
open over a weekend therefore counts for two minutes, not sixty hours.

WHY THE SERVER CLAMPS WHAT THE BROWSER SAYS
-------------------------------------------
Two tabs both visible on two monitors would each report the same minute, and a
modified client could report anything. So a beat is credited with at most the
wall-clock time since the previous beat in the same session -- whichever tab sent
it -- which makes the total per person bounded by real elapsed time, however many
tabs are open and whatever they claim.

SESSIONS
--------
A session is a run of beats with no gap longer than ``SESSION_GAP_MINUTES``.
Nothing has to "end" one: the next beat after a long gap simply starts another.

STORAGE AND RETENTION
---------------------
``user_sessions`` (one row per session) and ``user_activity_daily`` (seconds and
page views per person, per day, per page). Both are keyed by lower-cased e-mail,
like every other per-person table here. Pruned from the retention loop in main.py:
sessions after ``SESSION_RETENTION_DAYS``, daily rows after ``DAILY_RETENTION_DAYS``
-- the Postgres volume cannot be resized by a redeploy, so nothing may only grow.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from db import execute, get_connection, query_all, query_one

log = logging.getLogger(__name__)

SESSION_GAP_MINUTES = 30
# The browser beats every 60 seconds; anything more in one beat is not credible.
MAX_BEAT_SECONDS = 120
# Slack on the wall-clock clamp: timers drift and a request takes time to arrive.
_CLAMP_SLACK_SECONDS = 5
# A session's first beat has no previous beat to measure against.
_FIRST_BEAT_CAP_SECONDS = 60
SESSION_RETENTION_DAYS = 180
DAILY_RETENTION_DAYS = 400

_PAGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def ensure_usage_tables() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id             BIGSERIAL PRIMARY KEY,
            user_email     VARCHAR(255) NOT NULL,
            started_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            active_seconds INTEGER NOT NULL DEFAULT 0,
            page_views     INTEGER NOT NULL DEFAULT 0,
            entry_page     VARCHAR(40)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_seen "
        "ON user_sessions (user_email, last_seen_at DESC)"
    )
    execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_started ON user_sessions (started_at)")
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_activity_daily (
            user_email     VARCHAR(255) NOT NULL,
            day            DATE NOT NULL,
            page           VARCHAR(40) NOT NULL,
            active_seconds INTEGER NOT NULL DEFAULT 0,
            page_views     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_email, day, page)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_user_activity_daily_day ON user_activity_daily (day)")


def clean_page(page: str) -> str:
    """A page key we are willing to store. Anything else is filed as "other"."""
    value = str(page or "").strip().lower()
    return value if _PAGE_RE.match(value) else "other"


def record_beat(email: str, page: str, active_seconds: int, page_view: bool) -> Dict[str, Any]:
    """Credit one beat to this person's current session, opening one if needed.

    One transaction, with the session row locked, so two tabs beating at the same
    moment cannot both read the same "previous beat" and double the credit.
    """
    who = str(email or "").strip().lower()
    if not who:
        return {"credited": 0}
    where = clean_page(page)
    reported = max(0, min(int(active_seconds or 0), MAX_BEAT_SECONDS))
    views = 1 if page_view else 0
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id,
                       EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - last_seen_at)) AS gap
                  FROM user_sessions
                 WHERE user_email = %s
                   AND last_seen_at > CURRENT_TIMESTAMP - make_interval(mins => %s)
                 ORDER BY last_seen_at DESC
                 LIMIT 1
                 FOR UPDATE
                """,
                [who, SESSION_GAP_MINUTES],
            )
            row = cur.fetchone()
            if row:
                gap = max(0, int(float(row.get("gap") or 0)))
                credited = min(reported, gap + _CLAMP_SLACK_SECONDS)
                cur.execute(
                    """
                    UPDATE user_sessions
                       SET last_seen_at = CURRENT_TIMESTAMP,
                           active_seconds = active_seconds + %s,
                           page_views = page_views + %s
                     WHERE id = %s
                    """,
                    [credited, views, row["id"]],
                )
                new_session = False
            else:
                credited = min(reported, _FIRST_BEAT_CAP_SECONDS)
                cur.execute(
                    """
                    INSERT INTO user_sessions (user_email, active_seconds, page_views, entry_page)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [who, credited, views, where],
                )
                new_session = True
            if credited or views:
                cur.execute(
                    """
                    INSERT INTO user_activity_daily (user_email, day, page, active_seconds, page_views)
                    VALUES (%s, CURRENT_DATE, %s, %s, %s)
                    ON CONFLICT (user_email, day, page) DO UPDATE SET
                        active_seconds = user_activity_daily.active_seconds + EXCLUDED.active_seconds,
                        page_views = user_activity_daily.page_views + EXCLUDED.page_views
                    """,
                    [who, where, credited, views],
                )
        conn.commit()
    return {"credited": credited, "new_session": new_session}


def prune() -> None:
    """Retention. Called from the six-hourly loop in main.py."""
    execute(
        "DELETE FROM user_sessions WHERE last_seen_at < CURRENT_TIMESTAMP - make_interval(days => %s)",
        [SESSION_RETENTION_DAYS],
    )
    execute(
        "DELETE FROM user_activity_daily WHERE day < CURRENT_DATE - %s",
        [DAILY_RETENTION_DAYS],
    )


# ── reads, for the admin Users page ─────────────────────────────────────────

def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _safe(sql: str, params: Optional[list] = None) -> List[Dict[str, Any]]:
    """One section of the page. A failing query empties its section, not the page."""
    try:
        return query_all(sql, params or [])
    except Exception as exc:
        log.warning("usage: query failed (%s): %s", sql.strip().split("\n")[0][:60], exc)
        return []


def tracking_since() -> Optional[str]:
    row = None
    try:
        row = query_one("SELECT MIN(day) AS d FROM user_activity_daily")
    except Exception as exc:
        log.warning("usage: tracking_since failed: %s", exc)
    return _iso((row or {}).get("d"))


# A range is a number of days, or ALL_TIME. Longer than WEEKLY_AFTER_DAYS, the
# charts draw a bar per week: four hundred one-day bars do not fit in a card, and a
# week is the unit anyone reads a long trend in anyway.
ALL_TIME = 0
MAX_RANGE_DAYS = DAILY_RETENTION_DAYS
WEEKLY_AFTER_DAYS = 120


def _clamp_days(days: int) -> int:
    days = int(days or 0)
    return ALL_TIME if days <= 0 else max(7, min(days, MAX_RANGE_DAYS))


def _range(days: int) -> Tuple[Any, int, str]:
    """(first day, length in days, "day" or "week") for a range.

    Computed IN Postgres, like every date the queries compare against: the server's
    CURRENT_DATE and Python's date.today() disagree for part of every day. ALL_TIME
    starts at the first day anything was measured -- but never less than 30 days
    back, so a fortnight of history is not drawn as fourteen bars the width of a card.
    """
    if days == ALL_TIME:
        row = _safe(
            """
            SELECT LEAST(COALESCE(MIN(day), CURRENT_DATE), CURRENT_DATE - 29) AS start,
                   CURRENT_DATE - LEAST(COALESCE(MIN(day), CURRENT_DATE), CURRENT_DATE - 29) + 1 AS span
              FROM user_activity_daily
            """
        )
    else:
        row = _safe("SELECT (CURRENT_DATE - (%s - 1))::date AS start, %s AS span", [days, days])
    first = (row or [{}])[0]
    start = first.get("start")
    span = int(first.get("span") or days or 1)
    if start is None:
        fallback = _safe("SELECT CURRENT_DATE AS start")
        start = (fallback or [{}])[0].get("start")
    return start, span, ("week" if span > WEEKLY_AFTER_DAYS else "day")


def _bucket_series(unit: str, start: Any, who: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per day (or week) from `start` to today: people, seconds, views, new
    accounts. `who` narrows it to one person."""
    step = "1 week" if unit == "week" else "1 day"
    mine = "AND user_email = %s" if who else ""
    params: List[Any] = [unit, start, step, unit, start] + ([who] if who else []) + [unit, start]
    return _safe(
        f"""
        WITH b AS (
            SELECT generate_series(date_trunc(%s, %s::date), CURRENT_DATE, %s::interval)::date AS day
        )
        SELECT b.day,
               COALESCE(a.users, 0) AS active_users,
               COALESCE(a.seconds, 0) AS active_seconds,
               COALESCE(a.views, 0) AS page_views,
               COALESCE(n.new_users, 0) AS new_users
          FROM b
          LEFT JOIN (
                SELECT date_trunc(%s, day)::date AS day, COUNT(DISTINCT user_email) AS users,
                       SUM(active_seconds) AS seconds, SUM(page_views) AS views
                  FROM user_activity_daily
                 WHERE day >= %s {mine}
                 GROUP BY 1
          ) a ON a.day = b.day
          LEFT JOIN (
                SELECT date_trunc(%s, created_at)::date AS day, COUNT(*) AS new_users
                  FROM users
                 WHERE created_at >= %s
                 GROUP BY 1
          ) n ON n.day = b.day
         ORDER BY b.day
        """,
        params,
    )


def summary(days: int = 30) -> Dict[str, Any]:
    """The numbers across everybody: who is here, how often, for how long.

    Today / this week / this month are fixed windows, whatever the range: they answer
    "how many people use it", and a range picker changing what "this week" means would
    be a trap. Everything else -- new accounts, time, sessions and the three charts --
    follows the range, ALL_TIME included.
    """
    days = _clamp_days(days)
    start, span, unit = _range(days)
    totals = (_safe(
        """
        SELECT COUNT(*) AS users,
               COUNT(*) FILTER (WHERE is_active) AS active_accounts,
               COUNT(*) FILTER (WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '7 days') AS new_7d,
               COUNT(*) FILTER (WHERE created_at >= %s) AS new_range
          FROM users
        """,
        [start],
    ) or [{}])[0]
    reach = (_safe(
        """
        SELECT COUNT(DISTINCT user_email) FILTER (WHERE day = CURRENT_DATE) AS dau,
               COUNT(DISTINCT user_email) FILTER (WHERE day > CURRENT_DATE - 7) AS wau,
               COUNT(DISTINCT user_email) FILTER (WHERE day > CURRENT_DATE - 30) AS mau,
               COALESCE(SUM(active_seconds) FILTER (WHERE day > CURRENT_DATE - 7), 0) AS seconds_7d,
               COALESCE(SUM(active_seconds) FILTER (WHERE day >= %s), 0) AS seconds_range,
               COUNT(DISTINCT (user_email, day)) FILTER (WHERE day >= %s) AS person_days_range
          FROM user_activity_daily
         WHERE active_seconds > 0 OR page_views > 0
        """,
        [start, start],
    ) or [{}])[0]
    sessions = (_safe(
        """
        SELECT COUNT(*) AS sessions_range,
               COALESCE(AVG(active_seconds), 0) AS avg_session_seconds_range
          FROM user_sessions
         WHERE started_at >= %s
        """,
        [start],
    ) or [{}])[0]
    series = _bucket_series(unit, start)
    pages = _safe(
        """
        SELECT page,
               SUM(active_seconds) AS active_seconds,
               SUM(page_views) AS page_views,
               COUNT(DISTINCT user_email) AS users
          FROM user_activity_daily
         WHERE day >= %s
         GROUP BY page
         ORDER BY SUM(active_seconds) DESC, SUM(page_views) DESC
         LIMIT 12
        """,
        [start],
    )
    # Session starts by UTC hour; the page shifts them into the viewer's own zone.
    hours = _safe(
        """
        SELECT EXTRACT(HOUR FROM started_at AT TIME ZONE 'UTC')::int AS hour, COUNT(*) AS sessions
          FROM user_sessions
         WHERE started_at >= %s
         GROUP BY 1
        """,
        [start],
    )
    # The newest beat from anybody. A tracker that has stopped counting looks exactly
    # like a quiet day everywhere else on the page; this is where it stops looking so.
    last_beat = (_safe("SELECT MAX(last_seen_at) AS at FROM user_sessions") or [{}])[0]
    person_days = int(reach.get("person_days_range") or 0)
    seconds_range = int(reach.get("seconds_range") or 0)
    return {
        "days": days,
        "range_start": _iso(start),
        "range_days": span,
        "bucket": unit,
        "tracking_since": tracking_since(),
        "last_beat_at": _iso(last_beat.get("at")),
        "users": int(totals.get("users") or 0),
        "active_accounts": int(totals.get("active_accounts") or 0),
        "new_7d": int(totals.get("new_7d") or 0),
        # All time means every account, including the ones made before measuring began.
        "new_range": int(totals.get("users" if days == ALL_TIME else "new_range") or 0),
        "dau": int(reach.get("dau") or 0),
        "wau": int(reach.get("wau") or 0),
        "mau": int(reach.get("mau") or 0),
        "seconds_7d": int(reach.get("seconds_7d") or 0),
        "seconds_range": seconds_range,
        # Per person, per day they came -- "how long a visit to the portal is worth",
        # not diluted by everybody who did not come at all.
        "avg_seconds_per_active_day": int(seconds_range / person_days) if person_days else 0,
        "sessions_range": int(sessions.get("sessions_range") or 0),
        "avg_session_seconds_range": int(float(sessions.get("avg_session_seconds_range") or 0)),
        "series": [
            {
                "day": _iso(r.get("day")),
                "active_users": int(r.get("active_users") or 0),
                "active_seconds": int(r.get("active_seconds") or 0),
                "page_views": int(r.get("page_views") or 0),
                "new_users": int(r.get("new_users") or 0),
            }
            for r in series
        ],
        "pages": [
            {
                "page": str(r.get("page") or ""),
                "active_seconds": int(r.get("active_seconds") or 0),
                "page_views": int(r.get("page_views") or 0),
                "users": int(r.get("users") or 0),
            }
            for r in pages
        ],
        "hours_utc": {int(r["hour"]): int(r.get("sessions") or 0) for r in hours if r.get("hour") is not None},
    }


def users_overview(exclude_email: str = "") -> List[Dict[str, Any]]:
    """Every account, with its role and how much it has used the portal."""
    rows = _safe(
        """
        SELECT u.email,
               COALESCE(NULLIF(u.full_name, ''), u.email) AS display_name,
               r.name AS role_name,
               r.hierarchy_level,
               u.is_active,
               u.created_at,
               u.last_login_at,
               a.total_seconds, a.seconds_7d, a.seconds_30d, a.days_30d, a.views_30d, a.first_day,
               s.last_seen_at, s.sessions_30d, s.avg_session_30d,
               t.top_page
          FROM users u
          JOIN roles r ON u.role_id = r.id
          LEFT JOIN (
                SELECT user_email,
                       SUM(active_seconds) AS total_seconds,
                       SUM(active_seconds) FILTER (WHERE day > CURRENT_DATE - 7) AS seconds_7d,
                       SUM(active_seconds) FILTER (WHERE day > CURRENT_DATE - 30) AS seconds_30d,
                       COUNT(DISTINCT day) FILTER (WHERE day > CURRENT_DATE - 30) AS days_30d,
                       SUM(page_views) FILTER (WHERE day > CURRENT_DATE - 30) AS views_30d,
                       MIN(day) AS first_day
                  FROM user_activity_daily
                 GROUP BY user_email
          ) a ON a.user_email = LOWER(u.email)
          LEFT JOIN (
                SELECT user_email,
                       MAX(last_seen_at) AS last_seen_at,
                       COUNT(*) FILTER (WHERE started_at > CURRENT_TIMESTAMP - INTERVAL '30 days') AS sessions_30d,
                       AVG(active_seconds) FILTER (WHERE started_at > CURRENT_TIMESTAMP - INTERVAL '30 days') AS avg_session_30d
                  FROM user_sessions
                 GROUP BY user_email
          ) s ON s.user_email = LOWER(u.email)
          LEFT JOIN (
                SELECT DISTINCT ON (user_email) user_email, page AS top_page
                  FROM (
                        SELECT user_email, page, SUM(active_seconds) AS secs
                          FROM user_activity_daily
                         WHERE day > CURRENT_DATE - 30
                         GROUP BY user_email, page
                  ) x
                 ORDER BY user_email, secs DESC
          ) t ON t.user_email = LOWER(u.email)
         ORDER BY u.is_active DESC, s.last_seen_at DESC NULLS LAST, u.email ASC
        """
    )
    me = str(exclude_email or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for r in rows:
        email = str(r.get("email") or "")
        # A last sign-in is still evidence of use when the tab never beat (the
        # account predates tracking), so "last active" falls back to it.
        last_seen = r.get("last_seen_at") or r.get("last_login_at")
        out.append({
            "email": email,
            "display_name": str(r.get("display_name") or email),
            "role_name": str(r.get("role_name") or ""),
            "is_admin": int(r.get("hierarchy_level") or 99) == 1,
            "is_active": bool(r.get("is_active")),
            "is_self": email.strip().lower() == me,
            "first_seen": _iso(r.get("created_at")),
            "last_login": _iso(r.get("last_login_at")),
            "last_seen": _iso(last_seen),
            "total_seconds": int(r.get("total_seconds") or 0),
            "seconds_7d": int(r.get("seconds_7d") or 0),
            "seconds_30d": int(r.get("seconds_30d") or 0),
            "days_30d": int(r.get("days_30d") or 0),
            "views_30d": int(r.get("views_30d") or 0),
            "sessions_30d": int(r.get("sessions_30d") or 0),
            "avg_session_30d": int(float(r.get("avg_session_30d") or 0)),
            "top_page": str(r.get("top_page") or ""),
            "tracked": r.get("first_day") is not None,
        })
    return out


def user_detail(email: str, days: int = 30) -> Dict[str, Any]:
    """Everything the portal knows about one person's use of it."""
    who = str(email or "").strip().lower()
    days = _clamp_days(days)
    start, _span, unit = _range(days)
    profile = _safe(
        """
        SELECT u.id::text AS id, u.email, COALESCE(NULLIF(u.full_name, ''), u.email) AS display_name,
               u.username, u.sso_name, r.name AS role_name, u.is_active, u.created_at, u.last_login_at
          FROM users u JOIN roles r ON u.role_id = r.id
         WHERE LOWER(TRIM(u.email)) = %s
        """,
        [who],
    )
    if not profile:
        return {}
    p = profile[0]
    series = _bucket_series(unit, start, who)
    pages = _safe(
        """
        SELECT page, SUM(active_seconds) AS active_seconds, SUM(page_views) AS page_views
          FROM user_activity_daily
         WHERE user_email = %s AND day >= %s
         GROUP BY page ORDER BY SUM(active_seconds) DESC, SUM(page_views) DESC LIMIT 8
        """,
        [who, start],
    )
    sessions = _safe(
        """
        SELECT started_at, last_seen_at, active_seconds, page_views, entry_page
          FROM user_sessions WHERE user_email = %s
         ORDER BY started_at DESC LIMIT 12
        """,
        [who],
    )
    counts = (_safe(
        """
        SELECT (SELECT COUNT(*) FROM approval_requests WHERE requester_id::text = %s) AS requests,
               (SELECT COUNT(*) FROM approval_requests WHERE requester_id::text = %s AND status = 'PENDING') AS requests_pending,
               (SELECT COUNT(*) FROM user_tickets WHERE LOWER(user_email) = %s) AS tickets,
               (SELECT COUNT(*) FROM suggestions WHERE LOWER(user_email) = %s) AS suggestions,
               (SELECT COALESCE(SUM(active_seconds), 0) FROM user_activity_daily WHERE user_email = %s) AS total_seconds,
               (SELECT COUNT(*) FROM user_sessions WHERE user_email = %s) AS sessions
        """,
        [p["id"], p["id"], who, who, who, who],
    ) or [{}])[0]
    connected = _safe(
        "SELECT system, updated_at FROM user_integrations WHERE user_id = %s ORDER BY system",
        [p["id"]],
    )
    actions = _safe(
        """
        SELECT action_type, item_name, created_at
          FROM activity_log WHERE LOWER(user_email) = %s
         ORDER BY created_at DESC LIMIT 8
        """,
        [who],
    )
    return {
        "email": p.get("email"),
        "display_name": p.get("display_name"),
        "username": p.get("username"),
        # The identity provider's name, which tickets carry, beside the display name the
        # person chose (identity.py).
        "sso_name": p.get("sso_name") or "",
        "role_name": p.get("role_name"),
        "is_active": bool(p.get("is_active")),
        "first_seen": _iso(p.get("created_at")),
        "last_login": _iso(p.get("last_login_at")),
        "days": days,
        "bucket": unit,
        "series": [
            {"day": _iso(r.get("day")), "active_seconds": int(r.get("active_seconds") or 0), "page_views": int(r.get("page_views") or 0)}
            for r in series
        ],
        "pages": [
            {"page": str(r.get("page") or ""), "active_seconds": int(r.get("active_seconds") or 0), "page_views": int(r.get("page_views") or 0)}
            for r in pages
        ],
        "sessions": [
            {
                "started_at": _iso(r.get("started_at")),
                "last_seen_at": _iso(r.get("last_seen_at")),
                "active_seconds": int(r.get("active_seconds") or 0),
                "page_views": int(r.get("page_views") or 0),
                "entry_page": str(r.get("entry_page") or ""),
            }
            for r in sessions
        ],
        "counts": {k: int(v or 0) for k, v in counts.items()},
        # Which systems this person has connected a token for. NAMES only -- the
        # token itself never leaves secrets_manager.
        "connected": [{"system": str(r.get("system") or ""), "since": _iso(r.get("updated_at"))} for r in connected],
        "recent_actions": [
            {"action": str(r.get("action_type") or ""), "item": str(r.get("item_name") or ""), "at": _iso(r.get("created_at"))}
            for r in actions
        ],
        "tracking_since": tracking_since(),
    }
