"""
Portal log.

An append-only record of what the portal did, who asked for it, and whether it
worked. It backs the admin-only Logs page.

WHAT GETS IN HERE
-----------------
Three sources, and the distinction matters when you are reading the page:

  * ``app``    — business events, named by hand at the point they happen:
                 a request approved, a Terraform run, a login. These are the
                 events an auditor cares about.
  * ``http``   — every state-changing API call, recorded automatically by the
                 middleware in request_audit.py. This is what "log everything"
                 actually means in practice.
  * ``system`` — WARNING and above from the application's own Python loggers,
                 mirrored in by the handler in request_audit.py. This is where an
                 Artifactory timeout or a ServiceNow rejection shows up, and it is
                 why the page can now answer "why did that fail" without SSHing
                 into a pod to read stdout.

GETs are deliberately NOT recorded. Every dashboard widget polls; logging reads
would bury the writes under millions of rows that say nothing happened. The log
is for things that CHANGED, and for things that BROKE.

LEVELS
------
The standard five. Levels are derived, not guessed: a 2xx is INFO, a 4xx is a
WARNING (someone was refused — that is worth seeing), a 5xx is an ERROR, and
CRITICAL is reserved for the application saying so itself.

Why a separate table from ``audit_logs``: that one's ``action`` is a Postgres
ENUM with a fixed set of values, so a new label needs a coordinated ALTER TYPE —
fragile on a constrained deployment. ``audit_logs`` is left alone for legacy writers.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from db import execute, query_all, query_one


_log = logging.getLogger(__name__)


class Level:
    """The standard levels, in severity order."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


ALL_LEVELS = [Level.DEBUG, Level.INFO, Level.WARNING, Level.ERROR, Level.CRITICAL]

# Anything we don't recognise is stored as INFO rather than rejected: a log that
# drops events it doesn't understand is worse than one with a mislabelled row.
_VALID_LEVELS = set(ALL_LEVELS)


class Source:
    APP = "app"        # named business events
    HTTP = "http"      # state-changing API calls
    SYSTEM = "system"  # mirrored application log records


class Action:
    """Common action labels. Free-form strings are also accepted."""

    # Self-service
    SELF_SERVICE_REQUEST_CREATED = "self_service.request_created"
    REQUEST_APPROVED = "self_service.request_approved"
    REQUEST_REJECTED = "self_service.request_rejected"

    # Terraform lifecycle
    TERRAFORM_STARTED = "terraform.started"
    TERRAFORM_COMPLETED = "terraform.completed"
    TERRAFORM_FAILED = "terraform.failed"

    # ServiceNow
    TICKET_CREATED = "servicenow.ticket_created"

    # Artifactory self-service. Named for what changed, not for the code path:
    # "settings updated" told an auditor nothing about which project's quota moved.
    ARTIFACTORY_QUOTA_CHANGED = "artifactory.quota_changed"
    ARTIFACTORY_CLEANER_SUBMITTED = "artifactory.cleaner_submitted"
    ARTIFACTORY_CLEANER_PR_CLOSED = "artifactory.cleaner_pr_closed"
    # A person asserting that the CronJob and ConfigMap are off the cluster. The
    # portal cannot verify it, which is precisely why the claim is worth recording
    # with a name against it.
    ARTIFACTORY_CLEANER_CLUSTER_CLEARED = "artifactory.cleaner_cluster_cleared"

    # Safe Mode
    SAFE_MODE_ENABLED = "admin.safe_mode_enabled"
    SAFE_MODE_DISABLED = "admin.safe_mode_disabled"

    # Authentication. A portal with no record of who signed in — and of who TRIED
    # and failed — is missing the first thing anyone asks for after an incident.
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"


def ensure_table() -> None:
    """Idempotent DDL for audit_events."""
    try:
        execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id          BIGSERIAL PRIMARY KEY,
                user_email  VARCHAR(255),
                action      VARCHAR(128) NOT NULL,
                metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Added after the table shipped, so they arrive as ALTERs. Existing rows get
        # the defaults, which is the truth about them: they were all app-level INFO.
        for ddl in (
            "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS level VARCHAR(16) NOT NULL DEFAULT 'INFO'",
            "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'app'",
        ):
            execute(ddl)

        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created "
            "ON audit_events (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_events_user_created "
            "ON audit_events (user_email, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_events_action_created "
            "ON audit_events (action, created_at DESC)",
            # The Logs page opens on "problems first", so this is the hot path.
            "CREATE INDEX IF NOT EXISTS idx_audit_events_level_created "
            "ON audit_events (level, created_at DESC)",
        ):
            execute(ddl)
    except Exception as exc:
        _log.warning("audit: ensure_table failed: %s", exc)


def log_event(
    action: str,
    *,
    level: str = Level.INFO,
    source: str = Source.APP,
    user_email: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort write. NEVER raises, and never logs through the stdlib logger
    on failure — the system handler mirrors WARNING+ back into this table, so a
    warning here would try to write the row that just failed to write."""
    if not action:
        return
    lvl = str(level or Level.INFO).upper()
    if lvl not in _VALID_LEVELS:
        lvl = Level.INFO
    try:
        execute(
            """
            INSERT INTO audit_events (user_email, action, level, source, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                (user_email or "").strip().lower()[:255] or None,
                str(action)[:128],
                lvl,
                str(source or Source.APP)[:16],
                metadata or {},
            ],
        )
    except Exception:
        # Deliberately silent. See the docstring: anything louder recurses.
        pass


# Convenience alias so callers can write ``audit.log(action, ...)``.
log = log_event


def list_events(
    *,
    user_email: Optional[str] = None,
    action: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return {data, total, limit, offset}.

    ``level`` accepts a comma-separated list ("ERROR,CRITICAL") and is treated as
    "any of these", because that is the question people actually ask of a log.
    ``q`` is a substring match across the action, the user and the metadata — one
    box, because when you are searching a log you rarely know which column the
    word you remember is in.
    """
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))

    clauses: List[str] = []
    params: List[Any] = []

    if user_email:
        clauses.append("LOWER(user_email) = %s")
        params.append(str(user_email).strip().lower())
    if action:
        clauses.append("action = %s")
        params.append(str(action).strip()[:128])
    if level:
        wanted = [
            lv.strip().upper()
            for lv in str(level).split(",")
            if lv.strip().upper() in _VALID_LEVELS
        ]
        if wanted:
            clauses.append("level = ANY(%s)")
            params.append(wanted)
    if source:
        clauses.append("source = %s")
        params.append(str(source).strip()[:16])
    if q:
        needle = f"%{str(q).strip().lower()}%"
        clauses.append(
            "(LOWER(action) LIKE %s OR LOWER(COALESCE(user_email, '')) LIKE %s "
            "OR LOWER(metadata::text) LIKE %s)"
        )
        params.extend([needle, needle, needle])
    if date_from:
        clauses.append("created_at >= %s")
        params.append(str(date_from))
    if date_to:
        clauses.append("created_at <= %s")
        params.append(str(date_to))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    try:
        total_row = query_one(
            f"SELECT COUNT(*)::bigint AS n FROM audit_events{where}",
            params,
        )
        total = int((total_row or {}).get("n") or 0)

        rows = query_all(
            f"""
            SELECT id, user_email, action, level, source, metadata, created_at
              FROM audit_events
              {where}
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
    except Exception as exc:
        _log.debug("audit.list_events failed: %s", exc)
        return {"data": [], "total": 0, "limit": limit, "offset": offset}

    return {"data": rows, "total": total, "limit": limit, "offset": offset}


def _filter_sql(
    *,
    user_email: Optional[str] = None,
    action: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple:
    """The WHERE clause shared by listing, counting and deleting.

    One builder, deliberately: a Clear button that deletes by slightly different
    rules than the table it is looking at removes rows the operator never saw.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if user_email:
        clauses.append("LOWER(user_email) = %s")
        params.append(str(user_email).strip().lower())
    if action:
        clauses.append("action = %s")
        params.append(str(action).strip()[:128])
    if level:
        wanted = [
            lv.strip().upper()
            for lv in str(level).split(",")
            if lv.strip().upper() in _VALID_LEVELS
        ]
        if wanted:
            clauses.append("level = ANY(%s)")
            params.append(wanted)
    if source:
        clauses.append("source = %s")
        params.append(str(source).strip()[:16])
    if q:
        needle = f"%{str(q).strip().lower()}%"
        clauses.append(
            "(LOWER(action) LIKE %s OR LOWER(COALESCE(user_email, '')) LIKE %s "
            "OR LOWER(metadata::text) LIKE %s)"
        )
        params.extend([needle, needle, needle])
    if date_from:
        clauses.append("created_at >= %s")
        params.append(str(date_from))
    if date_to:
        clauses.append("created_at <= %s")
        params.append(str(date_to))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, bool(clauses)


def delete_events(**filters: Any) -> int:
    """Delete the rows matching these filters. Returns how many went.

    Deliberately NOT guarded against deleting everything — an admin looking at an
    unfiltered page and pressing Clear means the whole log, and a Clear button
    that silently declines to do that is worse than not having one. The guard is
    in the API layer, where an unfiltered delete has to be asked for explicitly.
    """
    where, params, _has = _filter_sql(**filters)
    try:
        row = query_one(
            f"WITH deleted AS (DELETE FROM audit_events{where} RETURNING 1) "
            f"SELECT COUNT(*)::bigint AS n FROM deleted",
            params,
        )
        return int((row or {}).get("n") or 0)
    except Exception as exc:
        _log.warning("audit.delete_events failed: %s", exc)
        return 0


def count_events(**filters: Any) -> int:
    """How many rows match — what the Clear confirmation quotes before deleting."""
    where, params, _has = _filter_sql(**filters)
    try:
        row = query_one(f"SELECT COUNT(*)::bigint AS n FROM audit_events{where}", params)
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def level_counts(**filters: Any) -> Dict[str, int]:
    """How many rows sit at each level, under the CURRENT filters.

    The page shows these on the level chips, so "ERROR 3" tells you whether it is
    worth clicking before you click it. Filters mirror list_events so the counts
    describe what you are looking at, not the whole table.
    """
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    q = filters.get("q")
    source = filters.get("source")
    user_email = filters.get("user_email")

    clauses: List[str] = []
    params: List[Any] = []
    if user_email:
        clauses.append("LOWER(user_email) = %s")
        params.append(str(user_email).strip().lower())
    if source:
        clauses.append("source = %s")
        params.append(str(source).strip()[:16])
    if q:
        needle = f"%{str(q).strip().lower()}%"
        clauses.append(
            "(LOWER(action) LIKE %s OR LOWER(COALESCE(user_email, '')) LIKE %s "
            "OR LOWER(metadata::text) LIKE %s)"
        )
        params.extend([needle, needle, needle])
    if date_from:
        clauses.append("created_at >= %s")
        params.append(str(date_from))
    if date_to:
        clauses.append("created_at <= %s")
        params.append(str(date_to))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    counts = {lv: 0 for lv in ALL_LEVELS}
    try:
        rows = query_all(
            f"SELECT level, COUNT(*)::bigint AS n FROM audit_events{where} GROUP BY level",
            params,
        )
    except Exception as exc:
        _log.debug("audit.level_counts failed: %s", exc)
        return counts
    for row in rows:
        lv = str(row.get("level") or "").upper()
        if lv in counts:
            counts[lv] = int(row.get("n") or 0)
    return counts


def distinct_actions() -> List[str]:
    """Every action string present in the table (for the filter dropdown).

    Capped: with HTTP calls logged, this is one row per route rather than the
    handful it used to be, and an unbounded dropdown is not a filter.
    """
    try:
        rows = query_all(
            "SELECT action, COUNT(*)::bigint AS n FROM audit_events "
            "GROUP BY action ORDER BY n DESC, action ASC LIMIT 200"
        )
        return [str(r["action"]) for r in rows if r.get("action")]
    except Exception:
        return []


# ── Retention ──────────────────────────────────────────────────────────────
# Two cutoffs, on purpose. INFO is the routine traffic of a working portal and is
# only interesting while it is recent — it answers "what just happened". WARNING
# and above answer "why is this broken", and that question gets asked weeks later,
# so they are kept far longer. One cutoff for both would mean either drowning in
# old INFO rows or throwing away the errors that explain a recurring failure.

AUDIT_RETENTION_DAYS = 7        # INFO / DEBUG
PROBLEM_RETENTION_DAYS = 30     # WARNING / ERROR / CRITICAL


# How big the database is allowed to get before the log starts saying so. The
# Postgres volume is 1Gi and a StatefulSet's volumeClaimTemplate is IMMUTABLE —
# the size cannot be raised by editing the chart and redeploying, it needs a
# deliberate PVC expansion (docs/RUNBOOK.md). That makes running out of space a
# thing you have to see coming, because you cannot fix it in the thirty seconds
# after Postgres starts refusing writes.
DB_SIZE_WARN_MB = int(os.getenv("DB_SIZE_WARN_MB", "600"))
DB_VOLUME_MB = int(os.getenv("DB_VOLUME_MB", "1024"))


def database_size_mb() -> Optional[int]:
    """Size of the portal's own database, in MB. None if it cannot be measured."""
    try:
        row = query_one(
            "SELECT (pg_database_size(current_database()) / 1024 / 1024)::bigint AS mb"
        )
        return int((row or {}).get("mb") or 0)
    except Exception as exc:
        _log.debug("audit.database_size_mb failed: %s", exc)
        return None


def check_database_size() -> Optional[int]:
    """Log a warning while there is still time to do something about it.

    Called from the same background loop as the retention sweep, so it costs one
    cheap query every six hours. The warning names the number and the volume,
    because "the database is large" is not an instruction and "780 MB of a 1024 MB
    volume" is.
    """
    size = database_size_mb()
    if size is None or DB_SIZE_WARN_MB <= 0:
        return size
    if size >= DB_SIZE_WARN_MB:
        _log.warning(
            "portal database is %d MB of a %d MB volume (warning threshold %d MB). "
            "The volume cannot be enlarged by redeploying — see docs/RUNBOOK.md. "
            "Shrink it first by clearing old rows from the Logs page.",
            size, DB_VOLUME_MB, DB_SIZE_WARN_MB,
        )
    return size


def delete_older_than(
    days: int = AUDIT_RETENTION_DAYS,
    problem_days: int = PROBLEM_RETENTION_DAYS,
) -> int:
    """Purge expired rows. Returns rows deleted; never raises.

    Only positive day counts are accepted, as a guard against a bad config value
    turning the sweeper into a table truncation.
    """
    try:
        days = int(days)
        problem_days = int(problem_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0 or problem_days <= 0:
        _log.warning(
            "audit.delete_older_than: refusing days=%s problem_days=%s (must be > 0)",
            days, problem_days,
        )
        return 0
    try:
        row = query_one(
            """
            WITH deleted AS (
                DELETE FROM audit_events
                 WHERE (
                        level IN ('DEBUG', 'INFO')
                        AND created_at < NOW() - (%s || ' days')::interval
                       )
                    OR (
                        level IN ('WARNING', 'ERROR', 'CRITICAL')
                        AND created_at < NOW() - (%s || ' days')::interval
                       )
                 RETURNING 1
            )
            SELECT COUNT(*)::bigint AS n FROM deleted
            """,
            [days, problem_days],
        )
        deleted = int((row or {}).get("n") or 0)
        if deleted:
            _log.info(
                "audit.delete_older_than(info=%s, problems=%s): purged %d row(s)",
                days, problem_days, deleted,
            )
        return deleted
    except Exception as exc:
        _log.warning("audit.delete_older_than(%s) failed: %s", days, exc)
        return 0
