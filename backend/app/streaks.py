"""Daily streaks: how many workdays in a row somebody has done something in the Hub.

WHAT COUNTS
-----------
ONE streak, not four competing ones -- it should sit quietly beside the work, not
become it. A workday counts when the person did any of:

    visit     used the Hub (the usage beat)
    pr        opened a pull request, voted on one or commented on one
    pipeline  a pipeline run they queued SUCCEEDED
    workitem  closed or moved a work item, or changed one from the Hub

The popover shows which of the four happened each day; the number is the streak.

WHICH DAYS
----------
Workdays are Sunday to Thursday, in STREAK_TIMEZONE (Asia/Jerusalem by default).
Friday and Saturday never count and never break a streak.

Every calendar month has BASE_FREEZES freezes. A missed workday uses one
automatically, so a holiday or a sick day does not end a streak; with none left, the
next missed workday does. The first time somebody uses the Hub on a Friday or a
Saturday in a month, that month gets WEEKEND_BONUS more, with a message -- and the
weekend day itself still does not count.

WHERE THE DAYS COME FROM
------------------------
``user_streak_days`` holds one row per person, local day and kind. Hub visits and the
portal's own Azure DevOps actions write it as they happen. The rest of Azure DevOps
history is read in the background with the person's own token (sync_ado), at most
once an hour and incrementally after a 60-day backfill, and never on a page's time.

Visible to the person (GET /api/streaks/me) and to admins (the Users page).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from db import execute, query_all, query_one

log = logging.getLogger(__name__)

KINDS = ("visit", "pr", "pipeline", "workitem")
BASE_FREEZES = 5
WEEKEND_BONUS = 2
BACKFILL_DAYS = 60
SYNC_EVERY = timedelta(hours=1)
HISTORY_DAYS = 400
WEEKEND_NOTICE = "סוגר שבת? חבל, קבל כמה הקפאות חינם שלא תהיה עצוב"

# Python's weekday(): Monday is 0 ... Sunday is 6. Friday (4) and Saturday (5) rest.
_WEEKEND = {4, 5}


def _zone() -> str:
    raw = (os.getenv("STREAK_TIMEZONE") or "").strip()
    return raw if raw and "$(" not in raw else "Asia/Jerusalem"


STREAK_TIMEZONE = _zone()

# Israel's rule written out, for a Postgres without a time-zone database (an embedded or
# minimal build answers "time zone not recognized" to every named zone). Summer time
# starts the Friday before the last Sunday of March at 02:00 and ends the last Sunday of
# October -- the same string glibc's own zone file carries.
_JERUSALEM_POSIX = "IST-2IDT,M3.4.4/26,M10.5.0"
_resolved: Optional[str] = None


def _tz() -> str:
    """The zone Postgres will actually accept, decided once. A zone it rejects would
    make every streak write fail; falling back keeps the days right to the hour."""
    global _resolved
    if _resolved:
        return _resolved
    for candidate in (STREAK_TIMEZONE,
                      _JERUSALEM_POSIX if STREAK_TIMEZONE == "Asia/Jerusalem" else "UTC"):
        try:
            query_one("SELECT CURRENT_TIMESTAMP AT TIME ZONE %s AS t", [candidate])
            if candidate != STREAK_TIMEZONE:
                log.warning("streaks: Postgres does not know the zone %s; using %s instead",
                            STREAK_TIMEZONE, candidate)
            _resolved = candidate
            return candidate
        except Exception:
            continue
    _resolved = "UTC"
    return _resolved


def is_workday(day: date) -> bool:
    return day.weekday() not in _WEEKEND


def ensure_tables() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_streak_days (
            user_email VARCHAR(255) NOT NULL,
            day        DATE NOT NULL,
            kind       VARCHAR(16) NOT NULL,
            count      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_email, day, kind)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_user_streak_days_day ON user_streak_days (day)")
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_streak_sync (
            user_email  VARCHAR(255) PRIMARY KEY,
            synced_at   TIMESTAMP WITH TIME ZONE,
            backfilled  BOOLEAN NOT NULL DEFAULT FALSE,
            last_error  TEXT
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_streak_bonus (
            user_email  VARCHAR(255) NOT NULL,
            month       DATE NOT NULL,
            granted_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            noticed_at  TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (user_email, month)
        )
        """
    )


def prune() -> None:
    """Retention, from main.py's six-hourly loop."""
    execute("DELETE FROM user_streak_days WHERE day < CURRENT_DATE - %s", [HISTORY_DAYS])
    execute("DELETE FROM user_streak_bonus WHERE month < CURRENT_DATE - %s", [HISTORY_DAYS])


# ── recording ────────────────────────────────────────────────────────────────

def _who(email: str) -> str:
    return str(email or "").strip().lower()


def record(email: str, kind: str, *, increment: bool = True) -> None:
    """Credit today (local) with one activity of this kind. Never raises: a streak is a
    nicety, and nothing it is attached to may fail because of it -- but a failure is
    logged, because a streak that silently stops counting is a broken promise."""
    who = _who(email)
    if not who or kind not in KINDS:
        return
    try:
        execute(
            f"""
            INSERT INTO user_streak_days (user_email, day, kind, count)
            VALUES (%s, (CURRENT_TIMESTAMP AT TIME ZONE %s)::date, %s, 1)
            ON CONFLICT (user_email, day, kind) DO {"UPDATE SET count = user_streak_days.count + 1" if increment else "NOTHING"}
            """,
            [who, _tz(), kind],
        )
    except Exception as exc:
        log.warning("streaks: %s not recorded for %s: %s: %s", kind, who, type(exc).__name__, exc)


def record_visit(email: str) -> None:
    """From the usage beat, once a minute: presence per day, not a count."""
    record(email, "visit", increment=False)


def record_history(email: str, kind: str, stamps: Iterable[str]) -> int:
    """Days from Azure DevOps timestamps (UTC, ISO), converted to local days IN Postgres.
    Presence only -- a sync that runs again must not count the same day twice."""
    who = _who(email)
    values = sorted({str(s) for s in stamps if s})
    if not who or kind not in KINDS or not values:
        return 0
    execute(
        """
        INSERT INTO user_streak_days (user_email, day, kind, count)
        SELECT %s, (s::timestamptz AT TIME ZONE %s)::date, %s, 1
          FROM unnest(%s::text[]) AS s
        ON CONFLICT (user_email, day, kind) DO NOTHING
        """,
        [who, _tz(), kind, values],
    )
    return len(values)


# ── the rules ────────────────────────────────────────────────────────────────

def _month(day: date) -> date:
    return day.replace(day=1)


def compute(days: Dict[date, Set[str]], today: date, bonus_months: Set[date]) -> Dict[str, Any]:
    """The streak, walked forward from the first active day to today.

    A missed workday uses a freeze from its own month while a streak is running; with
    none left the streak ends. Days before the first activity, weekends and today (the
    day is not over) never use one.
    """
    current = longest = 0
    frozen: Set[date] = set()
    used: Dict[date, int] = {}
    active_days = sorted(d for d, kinds in days.items() if kinds and d <= today)
    if active_days:
        day = active_days[0]
        while day <= today:
            if is_workday(day):
                if days.get(day):
                    current += 1
                    longest = max(longest, current)
                elif day != today and current > 0:
                    month = _month(day)
                    allowed = BASE_FREEZES + (WEEKEND_BONUS if month in bonus_months else 0)
                    if used.get(month, 0) < allowed:
                        used[month] = used.get(month, 0) + 1
                        frozen.add(day)
                    else:
                        current = 0
            day += timedelta(days=1)
    month = _month(today)
    allowed = BASE_FREEZES + (WEEKEND_BONUS if month in bonus_months else 0)
    return {
        "current": current,
        "longest": longest,
        "frozen": frozen,
        "freezes": {
            "allowed": allowed,
            "used": used.get(month, 0),
            "left": max(0, allowed - used.get(month, 0)),
            "bonus": month in bonus_months,
        },
    }


def _today() -> date:
    row = query_one("SELECT (CURRENT_TIMESTAMP AT TIME ZONE %s)::date AS d", [_tz()])
    return row["d"] if row and row.get("d") else datetime.now(timezone.utc).date()


def _days_for(emails: Optional[List[str]] = None) -> Dict[str, Dict[date, Set[str]]]:
    params: List[Any] = [HISTORY_DAYS]
    where = "day >= CURRENT_DATE - %s"
    if emails is not None:
        where += " AND user_email = ANY(%s)"
        params.append([_who(e) for e in emails])
    out: Dict[str, Dict[date, Set[str]]] = {}
    for row in query_all(f"SELECT user_email, day, kind FROM user_streak_days WHERE {where}", params) or []:
        out.setdefault(row["user_email"], {}).setdefault(row["day"], set()).add(row["kind"])
    return out


def _bonus_for(emails: Optional[List[str]] = None) -> Dict[str, Set[date]]:
    params: List[Any] = []
    where = "TRUE"
    if emails is not None:
        where = "user_email = ANY(%s)"
        params.append([_who(e) for e in emails])
    out: Dict[str, Set[date]] = {}
    for row in query_all(f"SELECT user_email, month FROM user_streak_bonus WHERE {where}", params) or []:
        out.setdefault(row["user_email"], set()).add(row["month"])
    return out


def _grant_weekend_bonus(email: str, today: date) -> bool:
    """First Hub use on a Friday or Saturday this month: two more freezes, once."""
    if is_workday(today):
        return False
    row = query_one(
        """
        INSERT INTO user_streak_bonus (user_email, month) VALUES (%s, %s)
        ON CONFLICT (user_email, month) DO NOTHING
        RETURNING month
        """,
        [_who(email), _month(today)],
    )
    return bool(row)


def status(email: str) -> Dict[str, Any]:
    """Everything the banner flame and its popover show, for one person."""
    who = _who(email)
    today = _today()
    granted = False
    try:
        granted = _grant_weekend_bonus(who, today)
    except Exception as exc:
        log.warning("streaks: weekend bonus not granted to %s: %s", who, exc)
    if granted:
        from api.notifications import notify_once

        notify_once(
            user_email=who,
            message=f"{WEEKEND_NOTICE} (+{WEEKEND_BONUS} הקפאות החודש)",
            group_key=f"streak-bonus:{_month(today).isoformat()}",
            notif_type="info",
        )
    days = _days_for([who]).get(who, {})
    bonus = _bonus_for([who]).get(who, set())
    result = compute(days, today, bonus)
    notice = query_one(
        "SELECT month FROM user_streak_bonus WHERE user_email = %s AND month = %s AND noticed_at IS NULL",
        [who, _month(today)],
    )
    strip = []
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        kinds = sorted(days.get(day, set()))
        strip.append({
            "day": day.isoformat(),
            "workday": is_workday(day),
            "kinds": kinds,
            "frozen": day in result["frozen"],
            "today": day == today,
        })
    sync = query_one("SELECT synced_at, backfilled, last_error FROM user_streak_sync WHERE user_email = %s", [who]) or {}
    return {
        "current": result["current"],
        "longest": result["longest"],
        "today": {
            "day": today.isoformat(),
            "workday": is_workday(today),
            "kinds": sorted(days.get(today, set())),
        },
        "freezes": result["freezes"],
        "days": strip,
        "notice": ({"text": WEEKEND_NOTICE, "bonus": WEEKEND_BONUS} if notice else None),
        "sync": {
            "synced_at": sync.get("synced_at").isoformat() if sync.get("synced_at") else None,
            "backfilled": bool(sync.get("backfilled")),
            "error": sync.get("last_error") or "",
            "running": who in _running,
        },
    }


def acknowledge_notice(email: str) -> None:
    execute(
        "UPDATE user_streak_bonus SET noticed_at = CURRENT_TIMESTAMP WHERE user_email = %s AND noticed_at IS NULL",
        [_who(email)],
    )


def overview() -> Dict[str, Dict[str, int]]:
    """Every person's current and longest streak, for the admin Users page."""
    today = _today()
    days = _days_for(None)
    bonus = _bonus_for(None)
    out: Dict[str, Dict[str, int]] = {}
    for who, history in days.items():
        result = compute(history, today, bonus.get(who, set()))
        out[who] = {"current": result["current"], "longest": result["longest"]}
    return out


# ── Azure DevOps history, in the background ──────────────────────────────────

_running: Set[str] = set()
_running_lock = threading.Lock()


def maybe_sync(user: Dict[str, Any]) -> bool:
    """Start one background read of this person's Azure DevOps history if the last one
    is older than SYNC_EVERY. Returns whether one is running now."""
    who = _who(user.get("email"))
    if not who:
        return False
    row = query_one("SELECT synced_at FROM user_streak_sync WHERE user_email = %s", [who]) or {}
    last = row.get("synced_at")
    if last and datetime.now(timezone.utc) - (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) < SYNC_EVERY:
        return who in _running
    with _running_lock:
        if who in _running:
            return True
        _running.add(who)

    def run() -> None:
        try:
            sync_ado(user)
        except Exception as exc:
            log.warning("streaks: Azure DevOps history not read for %s: %s: %s", who, type(exc).__name__, exc)
            _mark(who, error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            with _running_lock:
                _running.discard(who)

    threading.Thread(target=run, name="streak-sync", daemon=True).start()
    return True


def _mark(who: str, *, error: str = "", backfilled: Optional[bool] = None) -> None:
    execute(
        """
        INSERT INTO user_streak_sync (user_email, synced_at, backfilled, last_error)
        VALUES (%s, CURRENT_TIMESTAMP, %s, %s)
        ON CONFLICT (user_email) DO UPDATE
           SET synced_at = CURRENT_TIMESTAMP,
               backfilled = user_streak_sync.backfilled OR EXCLUDED.backfilled,
               last_error = EXCLUDED.last_error
        """,
        [who, bool(backfilled), error or None],
    )


def sync_ado(user: Dict[str, Any]) -> Dict[str, int]:
    """Read the days this person opened, voted on or commented on a pull request, had a
    pipeline run succeed, or closed or moved a work item -- with THEIR token."""
    import httpx

    from api.azure_devops import (
        _ADO_NOT_MINE,
        _ado_identity,
        _discover_ado_bases,
        _is_mine,
        _my_account_forms,
        _person_forms,
    )
    from resilient_http import tls_verify
    from secrets_manager import get_user_azure_devops_pat

    who = _who(user.get("email"))
    pat = get_user_azure_devops_pat(str(user.get("id") or ""))
    if not pat:
        _mark(who, error="Azure DevOps is not connected.")
        return {}
    state = query_one("SELECT synced_at, backfilled FROM user_streak_sync WHERE user_email = %s", [who]) or {}
    window = BACKFILL_DAYS
    if state.get("backfilled") and state.get("synced_at"):
        last = state["synced_at"] if state["synced_at"].tzinfo else state["synced_at"].replace(tzinfo=timezone.utc)
        window = min(BACKFILL_DAYS, max(2, (datetime.now(timezone.utc) - last).days + 2))
    since = datetime.now(timezone.utc) - timedelta(days=window)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {"pr": 0, "pipeline": 0, "workitem": 0}
    stamps: Dict[str, List[str]] = {"pr": [], "pipeline": [], "workitem": []}

    def recent(stamp: Any) -> bool:
        try:
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= since
        except ValueError:
            return False

    with httpx.Client(verify=tls_verify(), auth=httpx.BasicAuth("", pat),
                      timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        for base in _discover_ado_bases(client):
            me = _ado_identity(client, base)
            forms = _my_account_forms(me, user, str(user.get("username") or ""))
            projects = client.get(f"{base}/_apis/projects", params={"api-version": "7.0", "$top": "500"})
            if projects.status_code in _ADO_NOT_MINE:
                continue
            reviewed: List[Tuple[str, str, int]] = []
            for proj in (projects.json() or {}).get("value") or []:
                pid = proj.get("id")
                prs = client.get(f"{base}/{pid}/_apis/git/pullrequests",
                                 params={"api-version": "7.0", "searchCriteria.status": "all", "$top": "200"})
                if prs.status_code == 200:
                    for pr in prs.json().get("value") or []:
                        if _person_forms(pr.get("createdBy")) & forms and recent(pr.get("creationDate")):
                            stamps["pr"].append(pr["creationDate"])
                        me_as_reviewer = next((r for r in pr.get("reviewers") or [] if _person_forms(r) & forms), None)
                        touched = pr.get("closedDate") or pr.get("creationDate")
                        if me_as_reviewer and int(me_as_reviewer.get("vote") or 0) != 0 and (
                                str(pr.get("status")) == "active" or recent(touched)):
                            reviewed.append((str(pid), str((pr.get("repository") or {}).get("id")), int(pr["pullRequestId"])))
                builds = client.get(f"{base}/{pid}/_apis/build/builds",
                                    params={"api-version": "7.0", "minTime": since_iso, "resultFilter": "succeeded",
                                            "queryOrder": "finishTimeDescending", "$top": "200"})
                if builds.status_code == 200:
                    for build in builds.json().get("value") or []:
                        if _is_mine(build, me, str(user.get("email") or "")) and build.get("finishTime"):
                            stamps["pipeline"].append(build["finishTime"])
            # When they VOTED is only in the pull request's threads. Bounded: the most
            # recent 30 they reviewed.
            for pid, repo_id, pr_id in reviewed[:30]:
                threads = client.get(f"{base}/{pid}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads",
                                     params={"api-version": "7.0"})
                if threads.status_code != 200:
                    continue
                for thread in threads.json().get("value") or []:
                    for comment in thread.get("comments") or []:
                        if _person_forms(comment.get("author")) & forms and recent(comment.get("publishedDate")):
                            stamps["pr"].append(comment["publishedDate"])
            # Work items they closed, or moved while being the last to change them.
            for wiql, field in (
                ("[Microsoft.VSTS.Common.ClosedBy] = @Me AND [Microsoft.VSTS.Common.ClosedDate] >= @Today - {n}",
                 "Microsoft.VSTS.Common.ClosedDate"),
                ("[System.ChangedBy] = @Me AND [Microsoft.VSTS.Common.StateChangeDate] >= @Today - {n}",
                 "Microsoft.VSTS.Common.StateChangeDate"),
            ):
                q = client.post(f"{base}/_apis/wit/wiql", params={"api-version": "7.0", "$top": "400"},
                                json={"query": "SELECT [System.Id] FROM WorkItems WHERE " + wiql.format(n=window)})
                if q.status_code != 200:
                    continue
                ids = [str(w["id"]) for w in q.json().get("workItems") or []]
                for i in range(0, len(ids), 200):
                    items = client.get(f"{base}/_apis/wit/workitems",
                                       params={"api-version": "7.0", "ids": ",".join(ids[i:i + 200]), "fields": field})
                    if items.status_code == 200:
                        for wi in items.json().get("value") or []:
                            stamp = (wi.get("fields") or {}).get(field)
                            if stamp and recent(stamp):
                                stamps["workitem"].append(stamp)
    for kind, values in stamps.items():
        counts[kind] = record_history(who, kind, values)
    _mark(who, backfilled=True)
    return counts
