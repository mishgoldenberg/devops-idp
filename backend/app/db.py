"""
Postgres access layer.

Responsibilities
----------------
* Own the application's single ``psycopg2.pool.ThreadedConnectionPool``.
  Threaded, not Simple: nearly every route here is a synchronous ``def``, and
  those run in worker threads, so the pool is shared across threads by design.
  It is created lazily so importing this module never fails when the DB is
  temporarily unreachable (matters during pod startup / CrashLoopBackOff).
* Provide small, explicit helpers (``query_all``, ``query_one``,
  ``execute``, ``execute_returning``) instead of pulling in a full ORM — the
  schema and query surface are small enough to keep direct SQL honest.
* Own all DDL: every table the app writes to is created by an
  ``ensure_*()`` helper called at startup. These helpers are idempotent
  (``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE ... IF NOT EXISTS``) so
  schema drift between installs heals itself at the next boot.
* Register the ``dict → Json`` adapter so endpoints can write JSONB columns
  without wrapping every value in ``Json(...)`` by hand.

Anything that reads/writes the DB goes through here; don't open raw
connections from API routers.
"""

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2 import errorcodes, pool
from psycopg2.extensions import register_adapter
from psycopg2.extras import Json, RealDictCursor
from config import get_settings

# Imported at module scope on purpose. security.py touches db only from inside
# function bodies, so there is no cycle — and a lazy import here would turn a
# packaging mistake into a silently skipped migration instead of a startup error.
from security import PLATFORM_ADMIN_LEVEL, REGULAR_USER_LEVEL

# Globally teach psycopg2 how to serialize Python dicts into Postgres JSON /
# JSONB columns. Without this, any `execute(sql, [some_dict])` raises
# `ProgrammingError: can't adapt type 'dict'`, which bites endpoints that
# store JSONB (approval_requests.request_payload, audit_logs.details, etc.).
# We intentionally don't adapt `list` — psycopg2's default list→ARRAY
# adapter is still correct for any Postgres text/uuid array columns.
register_adapter(dict, Json)

# THREADED, not Simple. psycopg2's own words for SimpleConnectionPool are "a
# connection pool that can't be shared across different threads" — it has no lock,
# and its free-connection list is mutated by every getconn/putconn.
#
# This application is served by that exact forbidden arrangement. Practically every
# route here is a plain `def` (over three hundred of them, against one `async def`),
# and Starlette runs every plain `def` endpoint in a worker thread. So concurrent
# requests call getconn/putconn on an unsynchronised pool, and two of them can be
# handed the SAME connection — which then interleaves two transactions on one
# session. That does not fail cleanly; it produces wrong data and "connection
# already closed" at random under load.
#
# It survives today because concurrency is low. It is precisely the bug that appears
# when the portal is put in front of hundreds of people, and it would look like
# random unexplainable errors rather than like a pool problem, so it is fixed here
# rather than diagnosed later.
#
# ThreadedConnectionPool is the same pool with a lock around it.
_db_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None

# One pod's ceiling on Postgres connections. Sized against the server's
# max_connections, NOT against expected traffic: every replica keeps its own pool,
# so the real total is POOL_MAX x replicas, and exceeding max_connections takes the
# database down for everything rather than slowing one pod down.
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

# Guard against a lazily-initialised global being created twice when two threads
# arrive at once — otherwise the loser's pool is silently orphaned along with its
# open connections, which leaks a handful of connections on every cold start.
_pool_lock = threading.Lock()


# How long a single connection attempt may take. Without this, libpq waits for the
# OS TCP timeout — minutes — whenever the DB host silently drops packets, which is
# the normal behaviour of a firewall in this network rather than an exotic failure.
#
# That wait is what turns "Postgres is unreachable" into CrashLoopBackOff: the
# startup DDL blocks on connect, uvicorn never starts answering, the liveness probe
# gets connection-refused and the kubelet kills a container that was only waiting.
# Five seconds turns the same outage into a logged error and a NotReady pod.
_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))


def _dsn_with_timeouts(dsn: str) -> str:
    """
    Add connect_timeout and TCP keepalives to the DSN unless they are already set.

    Keepalives matter for the same reason: a connection idling in the pool across a
    firewall/NAT rebalance is dead but looks open, and the first query on it hangs
    for the OS timeout instead of failing.
    """
    if not dsn:
        return dsn
    params = {
        "connect_timeout": str(_CONNECT_TIMEOUT),
        "keepalives": "1",
        "keepalives_idle": "30",
        "keepalives_interval": "10",
        "keepalives_count": "3",
    }
    # Works for both URL DSNs (postgresql://…) and keyword DSNs (host=… dbname=…).
    is_url = "://" in dsn
    missing = [(k, v) for k, v in params.items() if k not in dsn]
    if not missing:
        return dsn
    if is_url:
        sep = "&" if "?" in dsn else "?"
        return dsn + sep + "&".join(f"{k}={v}" for k, v in missing)
    return dsn + " " + " ".join(f"{k}={v}" for k, v in missing)


# When pool construction fails, every caller must NOT immediately try again.
# Building the pool opens DB_POOL_MIN connections, so one failed attempt costs
# min x connect_timeout seconds — and while it runs it holds _pool_lock, so a
# hundred queued requests each wait for all the attempts ahead of them. That is
# how "Postgres is down" turns into "the whole pod is wedged". One thread retries
# per cooldown window; everyone else fails fast and the UI shows a real error.
_pool_failed_at = 0.0
_POOL_RETRY_COOLDOWN = float(os.getenv("DB_POOL_RETRY_COOLDOWN", "5"))


class DatabaseUnavailable(psycopg2.OperationalError):
    """Raised instead of queueing behind a connection attempt that is failing."""


def _get_pool() -> "psycopg2.pool.ThreadedConnectionPool":
    global _db_pool, _pool_failed_at
    if _db_pool is not None:
        return _db_pool
    if time.monotonic() - _pool_failed_at < _POOL_RETRY_COOLDOWN:
        raise DatabaseUnavailable(
            "database is unreachable (a connection attempt failed moments ago)"
        )
    if not _pool_lock.acquire(timeout=1):
        raise DatabaseUnavailable("a database connection attempt is already in progress")
    try:
        if _db_pool is None:
            settings = get_settings()
            try:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    _POOL_MIN, _POOL_MAX, _dsn_with_timeouts(settings.database_url)
                )
                _pool_failed_at = 0.0
            except Exception:
                _pool_failed_at = time.monotonic()
                raise
    finally:
        _pool_lock.release()
    return _db_pool

@contextmanager
def get_connection():
    """Context manager yielding a PostgreSQL connection from the pool."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        pool.putconn(conn)

def query_all(sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """Run a SELECT query and return all rows as dictionaries."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = cur.fetchall()
    return [dict(row) for row in rows]


def query_one(sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
    """Run a SELECT query and return a single row as a dictionary, or None."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or [])
            row = cur.fetchone()
    return dict(row) if row is not None else None


def execute(sql: str, params: Optional[Sequence[Any]] = None) -> None:
    """Run a write query (INSERT/UPDATE/DELETE)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
        conn.commit()


def execute_returning(
    sql: str, params: Optional[Sequence[Any]] = None
) -> List[Dict[str, Any]]:
    """Run a write query with RETURNING and return all rows."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = cur.fetchall()
        conn.commit()
    return [dict(row) for row in rows]


_health_lock = threading.Lock()
_health_cache: Tuple[float, bool] = (0.0, False)
_HEALTH_TTL = 2.0


def health_check() -> bool:
    """
    Database health, answered fast enough to be a probe.

    Called by the readiness probe every few seconds. The naive version — run
    `SELECT 1`, let it block — is what makes a database outage escalate: each
    probe takes as long as a connection attempt, probes overlap, every one of
    them occupies a Starlette worker thread, and once the pool is exhausted the
    *liveness* endpoint stops answering too. The pod is then restarted for a
    fault that a restart cannot fix.

    So: cache the answer briefly, and if a check is already in flight report the
    last known result instead of starting a second one.
    """
    global _health_cache
    ts, last = _health_cache
    now = time.monotonic()
    if now - ts < _HEALTH_TTL:
        return last
    if not _health_lock.acquire(blocking=False):
        return last
    try:
        try:
            query_one("SELECT 1")
            ok = True
        except Exception:
            ok = False
        _health_cache = (time.monotonic(), ok)
        return ok
    finally:
        _health_lock.release()


def _pg_exception_chain(exc: BaseException):
    """Walk psycopg2 / wrapper chain (__cause__, .orig)."""
    seen: set[int] = set()
    stack: List[Optional[BaseException]] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.append(getattr(e, "__cause__", None))
        stack.append(getattr(e, "orig", None))


def _is_undefined_table(exc: BaseException) -> bool:
    try:
        from psycopg2 import errors as pg_errors
    except ImportError:
        pg_errors = None  # type: ignore
    for e in _pg_exception_chain(exc):
        if pg_errors is not None and isinstance(e, pg_errors.UndefinedTable):
            return True
        code = getattr(e, "pgcode", None)
        if code == errorcodes.UNDEFINED_TABLE or code == "42P01":
            return True
        diag = getattr(e, "diag", None)
        st = getattr(diag, "sqlstate", None) if diag is not None else None
        if st == "42P01":
            return True
    msg = str(exc).lower()
    if "does not exist" in msg and "relation" in msg:
        return True
    # Localized server messages (e.g. Russian PostgreSQL)
    if "не существует" in msg:
        return True
    return False


_obs_tables_lock = threading.Lock()
_obs_tables_ensured = False
# Set when CREATE TABLE for observability failed (e.g. DB user lacks privilege); cleared on any successful obs SELECT.
_observability_ddl_unavailable = False


def observability_schema_unavailable() -> bool:
    """True if observability reads are degraded (missing tables, denied access, or DDL failed) in this process."""
    return _observability_ddl_unavailable


def ensure_observability_tables_once() -> None:
    """
    Run observability DDL at most once per process (thread-safe).
    Startup may skip failed ensure_tables(); this repairs schema on first admin API use.
    """
    global _obs_tables_ensured
    if _obs_tables_ensured:
        return
    with _obs_tables_lock:
        if _obs_tables_ensured:
            return
        ensure_observability_tables()
        _obs_tables_ensured = True


def ensure_observability_tables() -> None:
    """
    Create observability analytics tables (idempotent).
    Separate from ensure_tables so API routes can repair schema if startup DDL was skipped.
    """
    import logging

    log = logging.getLogger(__name__)
    stmts = [
        # Genuinely dead schema from earlier iterations. These two are safe to drop
        # repeatedly because nothing has written to them for a long time.
        (
            "drop_legacy_widget_user_views",
            "DROP TABLE IF EXISTS widget_user_views",
        ),
        (
            "drop_legacy_home_widget_prefs",
            "DROP TABLE IF EXISTS home_widget_prefs",
        ),
        # widget_usage is NOT dropped. It used to be, immediately above the
        # CREATE TABLE IF NOT EXISTS below — which made that guard meaningless and
        # emptied the table every time this function ran.
        #
        # This function runs at BACKEND STARTUP, so it fired on every pod start. With
        # two replicas and autoscaling that is several times a day, and every one of
        # them silently reset "Suggested for you", "Recently used" and every count on
        # the Observability page for all users. The same statement existed in the
        # deploy-time SQL job and has been removed there too.
        #
        # It is the current schema, not legacy. Replacing it one day is a migration
        # written for that change, not a drop that runs forever.
        # Event-based widget usage: one row per widget view.
        (
            "widget_usage",
            """
        CREATE TABLE IF NOT EXISTS widget_usage (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            widget_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            session_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """,
        ),
        (
            "idx_widget_usage_key",
            "CREATE INDEX IF NOT EXISTS idx_widget_usage_key ON widget_usage (widget_key)",
        ),
        (
            "idx_widget_usage_session",
            "CREATE INDEX IF NOT EXISTS idx_widget_usage_session ON widget_usage (session_id)",
        ),
        # Current active dashboard state per user. One row per (user, widget).
        # Observability reads this for "how many users currently have X on their
        # dashboard". widget_usage stays for historical trend analytics.
        (
            "user_widgets",
            """
        CREATE TABLE IF NOT EXISTS user_widgets (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            widget_key TEXT NOT NULL,
            session_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (user_id, widget_key)
        )
        """,
        ),
        (
            "idx_user_widgets_key",
            "CREATE INDEX IF NOT EXISTS idx_user_widgets_key ON user_widgets (widget_key)",
        ),
        (
            "self_service_usage",
            """
        CREATE TABLE IF NOT EXISTS self_service_usage (
            service_key      VARCHAR(255) PRIMARY KEY,
            service_name     VARCHAR(255) NOT NULL,
            execution_count  BIGINT NOT NULL DEFAULT 0,
            last_executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """,
        ),
        (
            "azure_projects",
            """
        CREATE TABLE IF NOT EXISTS azure_projects (
            id            SERIAL PRIMARY KEY,
            job_id        VARCHAR(128) UNIQUE NOT NULL,
            project_name  VARCHAR(255) NOT NULL,
            created_by    VARCHAR(255) NOT NULL,
            process_type  VARCHAR(64) NOT NULL,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            completed_at  TIMESTAMP WITH TIME ZONE
        )
        """,
        ),
        (
            "servicenow_tickets",
            """
        CREATE TABLE IF NOT EXISTS servicenow_tickets (
            id                 SERIAL PRIMARY KEY,
            ticket_id          VARCHAR(128) NOT NULL,
            created_by         VARCHAR(255) NOT NULL,
            severity           VARCHAR(32) NOT NULL,
            short_description  VARCHAR(512) DEFAULT '',
            created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """,
        ),
    ]
    for label, sql in stmts:
        try:
            execute(sql)
        except Exception as exc:
            log.warning("ensure_observability_tables: %s failed: %s", label, exc)
            raise
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_servicenow_tickets_created ON servicenow_tickets (created_at DESC)",
        # Avoid NULLS LAST — some PostgreSQL versions reject it and abort startup DDL.
        "CREATE INDEX IF NOT EXISTS idx_azure_projects_completed ON azure_projects (completed_at DESC)",
    ):
        try:
            execute(sql)
        except Exception as exc:
            log.warning("ensure_observability_tables: index failed (non-fatal): %s", exc)


def query_all_obs(sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """
    SELECT for observability tables. Never raises: returns [] on any failure so the UI stays 200.
    Attempts CREATE when the relation is missing; clears the degraded flag on success.
    """
    import logging

    global _observability_ddl_unavailable

    log = logging.getLogger(__name__)
    try:
        return _query_all_obs_try_schema(sql, params)
    except Exception as exc:
        log.warning("query_all_obs: returning no rows after error: %s", exc)
        _observability_ddl_unavailable = True
        return []


def _query_all_obs_try_schema(sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    import logging

    global _obs_tables_ensured, _observability_ddl_unavailable

    log = logging.getLogger(__name__)
    try:
        rows = query_all(sql, params)
        _observability_ddl_unavailable = False
        return rows
    except Exception as exc:
        if not _is_undefined_table(exc):
            raise
        if _observability_ddl_unavailable:
            return []
        with _obs_tables_lock:
            if _observability_ddl_unavailable:
                return []
            try:
                ensure_observability_tables()
            except Exception as ddl_exc:
                log.warning(
                    "query_all_obs: observability DDL failed (grant CREATE or apply 06_observability.sql): %s",
                    ddl_exc,
                )
                _observability_ddl_unavailable = True
                return []
            _obs_tables_ensured = True
        try:
            rows = query_all(sql, params)
            _observability_ddl_unavailable = False
            return rows
        except Exception as exc2:
            if _is_undefined_table(exc2):
                return []
            raise


def ensure_tables() -> None:
    """Create application tables if they do not already exist."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_tickets (
            id          SERIAL PRIMARY KEY,
            user_email  VARCHAR(255) NOT NULL,
            sys_id      VARCHAR(64)  NOT NULL,
            ticket_number VARCHAR(32),
            created_at  TIMESTAMP DEFAULT NOW(),
            UNIQUE (user_email, sys_id)
        )
        """
    )
    ensure_item_tables()
    ensure_observability_tables()
    ensure_approval_workflow_tables()
    ensure_approval_request_types()
    ensure_user_preference_columns()
    ensure_suggestions_table()
    ensure_favorites_table()
    ensure_inbox_dismissals_table()
    ensure_announcements_tables()
    ensure_activity_log_table()
    ensure_auth_tables()
    ensure_sso_config_table()
    ensure_user_integrations_tables()
    ensure_quick_links_table()
    ensure_user_quick_links_table()
    ensure_catalog_submissions_table()
    ensure_artifactory_cleaners_table()
    # Imported here, not at module scope: release_notes and usage_tracking read
    # through this module, so importing them back at the top is a cycle. The table's DDL lives with the code
    # that owns it rather than being a second copy here.
    from release_notes import ensure_release_notes_table
    from streaks import ensure_tables as ensure_streak_tables
    from usage_tracking import ensure_usage_tables
    from devbot.store import ensure_tables as ensure_devbot_tables

    ensure_release_notes_table()
    ensure_usage_tables()
    ensure_streak_tables()
    ensure_devbot_tables()


def ensure_approval_request_types() -> None:
    """Teach the approval_request_type ENUM every request type the app can produce.

    request_type is a Postgres ENUM, not text. A type the application considers
    supported but the enum has never heard of is accepted by every layer above the
    database and then rejected at the INSERT -- which surfaces as a 500 with
    "invalid input value for enum approval_request_type" and nothing pointing at the
    schema.

    The values come from request_types.py, the same module api/approvals.py gates on,
    so the two cannot drift.

    Read back afterwards. ALTER TYPE is wrapped in its own try because a minimal dev
    database may have the column as text (nothing to do) -- and an ALTER that quietly
    did nothing is exactly the failure this function exists to prevent, so what is
    still missing is logged rather than assumed absent.
    """
    import logging

    from request_types import ALL_REQUEST_TYPES

    log = logging.getLogger(__name__)
    enum_name = None
    try:
        row = query_one(
            """
            SELECT t.typname AS enum_name
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_type t ON t.oid = a.atttypid
             WHERE c.relname = 'approval_requests'
               AND a.attname = 'request_type'
               AND t.typtype = 'e'
            """
        )
        enum_name = (row or {}).get("enum_name")
    except Exception as exc:
        log.warning("approval request-type enum lookup failed: %s", exc)
        return

    if not enum_name:
        # The column is not an enum on this database; nothing to extend.
        return

    for value in sorted(ALL_REQUEST_TYPES):
        try:
            # The value is from a module-level frozenset, never from a request.
            execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")
        except Exception as exc:
            log.warning("could not add '%s' to %s: %s", value, enum_name, exc)

    try:
        present = {
            r["enumlabel"]
            for r in (query_all(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = %s",
                [enum_name],
            ) or [])
        }
        missing = sorted(ALL_REQUEST_TYPES - present)
        if missing:
            log.error(
                "%s is still missing %s -- requests of those types will fail at INSERT",
                enum_name, ", ".join(missing),
            )
        else:
            # WARNING, not INFO: the root logger runs at WARNING, so an INFO line
            # here is written nowhere and an operator asked to confirm the sync ran
            # finds an empty log and cannot tell that from a silent failure.
            log.warning("%s holds every request type the portal can produce", enum_name)
    except Exception as exc:
        log.warning("could not verify %s: %s", enum_name, exc)


def ensure_catalog_submissions_table() -> None:
    """
    Catalog submissions - the structured requests the portal collects on its own.

    Pipeline Characterization is the first: a questionnaire whose answers are the
    deliverable, not a side effect. It goes to ServiceNow as a requested item so the
    team works it beside everything else, and it is ALSO kept here, because a RITM
    stores the answers as a wall of variables on a ticket and cannot answer "show me
    every CI pipeline we were asked for this quarter".

    The answers live in one JSONB column on purpose. A form whose fields change every
    time somebody adds a question is not a schema, and a column per question turns
    each new question into a migration.

    snow_number is nullable and stays that way when ServiceNow is unreachable: the
    submission is still recorded, still visible to the requester, and still shows that
    it never reached the queue - which is the one thing a silent failure hides.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_submissions (
            id            SERIAL PRIMARY KEY,
            kind          VARCHAR(64)  NOT NULL,
            requester_id  VARCHAR(64)  NOT NULL,
            requester_email VARCHAR(255),
            title         VARCHAR(255) NOT NULL,
            answers       JSONB        NOT NULL DEFAULT '{}'::jsonb,
            snow_number   VARCHAR(32),
            snow_sys_id   VARCHAR(64),
            snow_error    TEXT,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_submissions_requester "
        "ON catalog_submissions (requester_id, created_at DESC)"
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_submissions_kind "
        "ON catalog_submissions (kind, created_at DESC)"
    )


def ensure_artifactory_cleaners_table() -> None:
    """
    Artifactory cleaners the portal has created.

    The record is what makes a cleaner editable. Changing one means re-rendering the
    same three files with new rules, and re-rendering needs the rules -- which
    otherwise exist only inside a merged pull request and inside the ConfigMap the
    job mounts. Reading them back out of YAML in a repository to change them would be
    a parser nobody wants to own.

    It is also the only place a requester can see what they asked for: the pull
    request is in Azure DevOps, the CronJob is in OpenShift, and neither is somewhere
    they can look.

    rules is JSONB for the same reason the submissions table uses it: the set of ways
    to select an artifact grows, and a column per rule turns each new one into a
    migration.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS artifactory_cleaners (
            id            SERIAL PRIMARY KEY,
            slug          VARCHAR(64)  NOT NULL UNIQUE,
            cleaner_name  VARCHAR(255) NOT NULL,
            repository    VARCHAR(128) NOT NULL,
            rules         JSONB        NOT NULL DEFAULT '{}'::jsonb,
            summary       TEXT,
            owner_id      VARCHAR(64)  NOT NULL,
            owner_email   VARCHAR(255),
            request_id    VARCHAR(64),
            pull_request_url TEXT,
            snow_number   VARCHAR(32),
            status        VARCHAR(32)  NOT NULL DEFAULT 'PR_OPEN',
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # The pull request's own lifecycle, added after the table shipped.
    #
    # A cleaner does nothing until somebody merges its pull request, and it does
    # nothing ever if they abandon it -- which is exactly what happened the first time
    # one was reviewed. Without these columns the portal said "completed" for both
    # outcomes, because opening the pull request was all it had ever recorded.
    #
    # pull_request_id is what Azure DevOps is asked about; status is the portal's own
    # word for it (PR_OPEN / RUNNING / ABANDONED / FAILED); pr_checked_at is when that
    # word was last confirmed, so a stale answer can say it is stale rather than
    # passing itself off as current.
    for ddl in (
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pull_request_id INTEGER",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pr_state VARCHAR(32)",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pr_closed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pr_checked_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS last_error TEXT",
        # A removal is a pull request like any other change, so the record has to
        # survive until it is reviewed -- and the outcome is the opposite of every
        # other merge: merging THIS one means the cleaner is gone.
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pending_delete BOOLEAN NOT NULL DEFAULT FALSE",
        # The Azure DevOps pipeline created when the pull request is merged. Merging
        # puts pipeline.yaml in the repository; a YAML file with no build definition
        # pointing at it never runs, so the cleaner was only ever half-created until
        # an admin made one by hand.
        #
        # pipeline_error is stored alongside on purpose. This step happens after a
        # merge that cannot be undone and is not allowed to fail it, so its failure is
        # invisible -- the files are on main and everything looks finished. The column
        # is what "Merged, not scheduled" is read from, and it carries the name and the
        # path an admin needs to create the pipeline by hand.
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pipeline_id INTEGER",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pipeline_name VARCHAR(255)",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pipeline_url TEXT",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS pipeline_error TEXT",
        # The cluster side of a removal, which this portal cannot perform: the CronJob
        # and the ConfigMap live on a different cluster, behind a firewall, and merging
        # the removal deletes only the files that describe them. '' means the objects
        # are meant to be there, PENDING means a removal was approved and they are
        # still running, CLEARED means somebody confirmed they are gone. Without this
        # the portal would report a cleaner as removed while it still deletes
        # artifacts every night.
        # The first build of a merged cleaner's pipeline. Creating the definition
        # applies nothing: pipeline.yaml is a set of instructions, and until a build
        # executes it the CronJob and ConfigMap do not exist at all. So the merge
        # queues one, and this is what became of it.
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS first_run_id INTEGER",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS first_run_url TEXT",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS first_run_state VARCHAR(16) NOT NULL DEFAULT ''",
        # Whether anything of this cleaner is on the cluster. DEFAULT TRUE, and the
        # direction is the whole point: every cleaner that existed before this column
        # did was applied by a build somebody ran by hand, so assuming FALSE would
        # have the portal declare "nothing left on the cluster" over a live CronJob
        # still deleting artifacts every night. New rows are written FALSE explicitly
        # by cleaner_store.record and become TRUE when a build succeeds.
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS cluster_applied BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS cluster_state VARCHAR(16) NOT NULL DEFAULT ''",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS cluster_cleared_by VARCHAR(255)",
        "ALTER TABLE artifactory_cleaners ADD COLUMN IF NOT EXISTS cluster_cleared_at TIMESTAMP WITH TIME ZONE",
    ):
        execute(ddl)
    execute(
        "CREATE INDEX IF NOT EXISTS idx_artifactory_cleaners_owner "
        "ON artifactory_cleaners (owner_id, created_at DESC)"
    )
    # The my-requests and approvals pages look a cleaner up by the request that
    # created it, which is a different question from "whose is it".
    execute(
        "CREATE INDEX IF NOT EXISTS idx_artifactory_cleaners_request "
        "ON artifactory_cleaners (request_id)"
    )


def ensure_quick_links_table() -> None:
    """Create admin-managed Quick Links shown to every dashboard user."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS quick_links (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            url         TEXT NOT NULL,
            icon_url    TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   BOOLEAN NOT NULL DEFAULT true,
            created_by  VARCHAR(255),
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_quick_links_active_order ON quick_links (is_active, sort_order, id)"
    )
    # Multi-links: one circle that opens a small panel of named links instead of
    # navigating somewhere itself. Added as nullable columns on the existing table
    # rather than a child table — the children are a short ordered list edited as a
    # single unit, so JSONB keeps it to one row, one write, and no join.
    # `kind` is 'single' (a plain link, `url` used) or 'multi' (`children` used).
    #
    # `admin_only` restricts a link to admins. It defaults to FALSE, which is what
    # every existing row means: they were created when the only possible answer was
    # "everyone", so that is the answer they keep. A visibility column must never
    # default to the restrictive value — that would silently hide links people are
    # already using, on the deploy that adds the feature.
    for sql in (
        "ALTER TABLE quick_links ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'single'",
        "ALTER TABLE quick_links ADD COLUMN IF NOT EXISTS children JSONB",
        "ALTER TABLE quick_links ADD COLUMN IF NOT EXISTS admin_only BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE quick_links ALTER COLUMN url DROP NOT NULL",
    ):
        try:
            execute(sql)
        except Exception:
            pass


def ensure_user_quick_links_table() -> None:
    """
    Per-user Quick Links — the ones somebody adds for themselves.

    Deliberately a separate table from ``quick_links`` rather than a nullable
    ``owner_id`` on it. The two are different things with different rules: the
    admin list is platform configuration, managed in Platform Managing, audited,
    and visible to everyone (or to admins). This one is a personal bookmark list
    nobody else can see or manage, and it must be impossible for a bug in one to
    expose or destroy the other. One column separating them would be one WHERE
    clause away from doing exactly that.

    Rows are soft-deleted so the toast's Undo is a flag flip rather than a
    re-insert that would lose the tile's place in the order.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_quick_links (
            id          SERIAL PRIMARY KEY,
            user_id     VARCHAR(255) NOT NULL,
            name        VARCHAR(255) NOT NULL,
            url         TEXT,
            icon_url    TEXT,
            kind        VARCHAR(16)  NOT NULL DEFAULT 'single',
            children    JSONB,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   BOOLEAN NOT NULL DEFAULT true,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_user_quick_links_owner "
        "ON user_quick_links (user_id, is_active, sort_order, id)"
    )


def ensure_auth_tables() -> None:
    """Add local bootstrap-login columns to users without changing SSO users."""
    for sql in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bootstrap_admin BOOLEAN NOT NULL DEFAULT FALSE",
        # The second local account: same mechanism, no admin rights. It exists so an
        # admin can see the portal exactly as an ordinary user does without signing out
        # of their own session on another machine or borrowing someone's SSO login.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bootstrap_user BOOLEAN NOT NULL DEFAULT FALSE",
    ):
        try:
            execute(sql)
        except Exception:
            pass


def ensure_sso_config_table() -> None:
    """Single-row OIDC provider configuration, managed by Platform Admins."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS sso_config (
            id                    INTEGER PRIMARY KEY DEFAULT 1,
            issuer_uri            TEXT NOT NULL,
            client_id             TEXT NOT NULL,
            client_secret_encrypted TEXT NOT NULL,
            enabled               BOOLEAN NOT NULL DEFAULT FALSE,
            created_at            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT sso_config_singleton CHECK (id = 1)
        )
        """
    )


def ensure_user_integrations_tables() -> None:
    """Per-user external-system tokens and pinned integration items."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_integrations (
            id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_id         TEXT NOT NULL,
            system          VARCHAR(32) NOT NULL,
            token_encrypted TEXT NOT NULL,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, system)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_pins (
            id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_id     TEXT NOT NULL,
            system      VARCHAR(32) NOT NULL,
            item_id     VARCHAR(255) NOT NULL,
            item_name   VARCHAR(512) NOT NULL,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, system, item_id)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_user_pins_user_system ON user_pins (user_id, system, created_at DESC)"
    )


def ensure_user_preference_columns() -> None:
    """
    Extend ``users`` with per-user UI preferences that the portal persists:
      * ``avatar_url``         — data URL (base64) or URL of the sidebar avatar.
      * ``preferred_theme``    — "light" or "night" (DaisyUI theme name).
      * ``preferred_density``  — "comfortable" or "compact" (dashboard spacing).

    Added via ALTER TABLE ... ADD COLUMN IF NOT EXISTS so the baseline schema
    in deployment/charts/.../00_schema.sql can stay untouched and dev DBs keep
    working across deploys.
    """
    for sql in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_theme VARCHAR(32)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_density VARCHAR(32)",
        # The name the identity provider gives this person, refreshed at every SSO
        # sign-in and never editable in the portal. `full_name` is the DISPLAY name,
        # which the person may change to anything; a ticket raised in their name has
        # to carry who they actually are (identity.trusted_name).
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS sso_name VARCHAR(255)",
    ):
        try:
            execute(sql)
        except Exception:
            # Silent: users table may not exist yet on a minimal dev DB.
            pass


def ensure_suggestions_table() -> None:
    """
    Suggestions — the feedback board.

    This started as a fire-and-forget box on the Settings page: you typed an idea, it
    vanished into a table, and nothing ever came back. Nobody could see anybody else's
    idea, nobody could say "yes, this, I need it too", and nobody ever heard whether it
    was going to happen. That is not a feedback feature, it is a suggestions BIN.

    It is a board now (the fider model):

      * VOTES tell you which ideas people actually want, instead of leaving an admin to
        guess from a list sorted by date. That is the whole point — a suggestion with
        thirty votes and one with none are not the same suggestion.
      * A STATUS is a promise or a refusal, publicly. 'planned', 'in_progress',
        'completed', 'declined'.
      * An ADMIN RESPONSE says WHY. A declined idea with a reason is a conversation; a
        declined idea in silence is why people stop suggesting things.

    The columns arrive as ALTERs so an existing table upgrades in place — every row
    already there keeps its title, its author and its date, and simply starts with no
    votes and no response.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
            id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_email  VARCHAR(255) NOT NULL,
            title       VARCHAR(255) NOT NULL,
            description TEXT,
            status      VARCHAR(32)  NOT NULL DEFAULT 'new',
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for ddl in (
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS admin_response TEXT",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS responded_by VARCHAR(255)",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS responded_at TIMESTAMP WITH TIME ZONE",
    ):
        execute(ddl)

    execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestions_created "
        "ON suggestions (created_at DESC)"
    )

    # One vote per person per suggestion — enforced by the primary key rather than by
    # the application remembering to check, because the application will eventually
    # forget and a feedback board whose votes can be stuffed is worth nothing.
    execute(
        """
        CREATE TABLE IF NOT EXISTS suggestion_votes (
            suggestion_id UUID NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
            user_email    VARCHAR(255) NOT NULL,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (suggestion_id, user_email)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestion_votes_suggestion "
        "ON suggestion_votes (suggestion_id)"
    )

    # Comments turn a suggestion from a poll into a conversation. is_admin is stamped at
    # write time so a reply from the platform team reads as official in the thread no
    # matter what role the author has later. Comments cascade with the suggestion.
    execute(
        """
        CREATE TABLE IF NOT EXISTS suggestion_comments (
            id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            suggestion_id UUID NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
            user_email    VARCHAR(255) NOT NULL,
            is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
            body          TEXT NOT NULL,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_suggestion_comments_suggestion "
        "ON suggestion_comments (suggestion_id, created_at)"
    )


def ensure_inbox_dismissals_table() -> None:
    """
    "I have dealt with this" — per user, per item, for the Needs You strip.

    The strip surfaces things waiting on you from five systems, and some of them are
    not yours to close: a work item can sit in Pending Review for four months for a
    reason the portal cannot see. Without a way to say so, the oldest and least
    actionable rows permanently occupy the list that exists to show what to do next.

    ``item_stamp`` is what makes this a dismissal rather than a mute. It records how
    fresh the item was when it was waved off — its changed-date. The item reappears if
    it is touched again afterwards, because that is new information; it stays gone if
    nothing has happened. So "done" does not silence an item forever, and nobody has to
    remember to un-dismiss anything.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS inbox_dismissals (
            user_id      VARCHAR(255) NOT NULL,
            item_key     VARCHAR(512) NOT NULL,
            item_stamp   VARCHAR(64)  NOT NULL DEFAULT '',
            reason       VARCHAR(32)  NOT NULL DEFAULT 'done',
            dismissed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, item_key)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_dismissals_user "
        "ON inbox_dismissals (user_id, dismissed_at DESC)"
    )


def ensure_announcements_tables() -> None:
    """
    Admin announcements, and who has read them.

    ``updated_at`` is the load-bearing column, not ``created_at``: a dismissal
    records the announcement's updated_at at the moment it was waved away, so an
    admin who EDITS a live announcement (a corrected date, a new link) has it
    reappear for everyone who had already dismissed the earlier wording. Without
    that stamp the second version is invisible to exactly the people who read the
    first one — the same mute-forever failure the inbox dismissals avoid.

    Windows are optional and open-ended on both sides: ``starts_at`` NULL means
    "now", ``ends_at`` NULL means "until an admin retires it". They are stored as
    timestamptz so a window means the same thing in every browser.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id          SERIAL PRIMARY KEY,
            title       VARCHAR(200) NOT NULL,
            body        TEXT NOT NULL DEFAULT '',
            kind        VARCHAR(16)  NOT NULL DEFAULT 'info',
            link_url    TEXT,
            link_label  VARCHAR(80),
            starts_at   TIMESTAMP WITH TIME ZONE,
            ends_at     TIMESTAMP WITH TIME ZONE,
            is_active   BOOLEAN NOT NULL DEFAULT true,
            created_by  VARCHAR(255),
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_announcements_live "
        "ON announcements (is_active, starts_at, ends_at)"
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS announcement_dismissals (
            user_id         VARCHAR(255) NOT NULL,
            announcement_id INTEGER NOT NULL,
            seen_stamp      VARCHAR(64) NOT NULL DEFAULT '',
            dismissed_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, announcement_id)
        )
        """
    )


def ensure_favorites_table() -> None:
    """
    Per-user favorites — the "⭐ Favorites" section on the dashboard.

    Deliberately separate from ``user_item_pins``: that table is a
    widget-local sort hint (a pinned ServiceNow row floats to the top of
    the SN widget) and only stores ``(user_id, source, item_id)``. The
    favorites concept here is a *global* bookmark list the dashboard reads
    without re-hitting Azure DevOps / ServiceNow, so we denormalize the
    display name alongside the pointer.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_email  VARCHAR(255) NOT NULL,
            item_type   VARCHAR(32)  NOT NULL,
            item_id     VARCHAR(255) NOT NULL,
            item_name   VARCHAR(512) NOT NULL,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_email, item_type, item_id)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_favorites_user "
        "ON favorites (user_email, created_at DESC)"
    )


def ensure_activity_log_table() -> None:
    """
    Per-user activity feed — powers the "Recent Activity" dashboard widget.

    Separate from ``audit_logs`` / ``audit_events`` on purpose: those are
    append-only admin-facing records with free-form action strings and
    detailed metadata. ``activity_log`` is a small, user-facing stream of
    the three actions that have a sensible "display card" (create project,
    open ticket, self-service submit) with pre-formatted display fields so
    the widget renders without joins.
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id          BIGSERIAL PRIMARY KEY,
            user_email  VARCHAR(255) NOT NULL,
            action_type VARCHAR(64)  NOT NULL,
            item_name   VARCHAR(512) NOT NULL,
            metadata    JSONB,
            created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_log_user_created "
        "ON activity_log (user_email, created_at DESC)"
    )


def ensure_backup_runs_table() -> None:
    """
    One row per database backup or restore-verification attempt.

    Written by the two CronJobs in the infrastructure chart (``psql`` straight
    into this table), read by ``/api/backups`` and the Platform Managing page.
    The backend never writes here — it is a status log the cluster reports INTO,
    which is the only reason an operator can tell a working backup from one that
    has been 401ing against Artifactory every night since the token expired.

    The row is inserted as ``running`` BEFORE pg_dump starts and updated on the
    way out, so a job that is OOM-killed or evicted leaves a ``running`` row that
    simply ages — "started and never finished" and "never started" are different
    failures and must not look identical.

    ``jwt_fingerprint`` is a salted hash of ``JWT_SECRET``, never the secret. It
    exists so a restore can PROVE the dump matches the secret it is about to be
    restored beside (RUNBOOK §4: a dump restored next to a different
    environment's secret is a table of unreadable strings, and nothing reports
    it until a user opens the Connections page).
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS backup_runs (
            id              BIGSERIAL PRIMARY KEY,
            kind            VARCHAR(16)  NOT NULL,
            environment     VARCHAR(64)  NOT NULL DEFAULT 'prod',
            status          VARCHAR(16)  NOT NULL,
            started_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at     TIMESTAMP WITH TIME ZONE,
            artifact        TEXT,
            size_bytes      BIGINT,
            sha256          VARCHAR(64),
            jwt_fingerprint VARCHAR(64),
            message         TEXT
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_runs_kind_started "
        "ON backup_runs (kind, started_at DESC)"
    )


def backup_runs_schema_unavailable() -> bool:
    """True when ``backup_runs`` does not exist yet.

    Distinguishes "the table was never created" from "the table is empty", which
    the API has to tell apart: the first is a deploy that has not finished, the
    second is a backup that has never run — and only the second is an alarm.
    """
    try:
        query_one("SELECT 1 FROM backup_runs LIMIT 1")
        return False
    except Exception as exc:
        if _is_undefined_table(exc):
            return True
        raise


def cleanup_old_audit_logs(retention_days: int = 7) -> int:
    """
    Delete audit_logs rows older than ``retention_days`` days.

    Audit logs grow unbounded as users exercise the portal. Keeping only the
    last week is enough for the dashboards + recent-activity views that read
    from this table, and it keeps the table small enough that admin queries
    stay snappy without a dedicated archive job.

    Returns the number of rows deleted (0 on failure — best-effort).
    """
    try:
        row = query_one(
            f"""
            WITH deleted AS (
                DELETE FROM audit_logs
                WHERE created_at < NOW() - INTERVAL '{int(retention_days)} days'
                RETURNING 1
            )
            SELECT COUNT(*) AS n FROM deleted
            """
        )
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


_item_tables_lock = threading.Lock()
_item_tables_ensured = False


def ensure_item_tables_once() -> None:
    """Run item-tracking DDL at most once per process (thread-safe)."""
    global _item_tables_ensured
    if _item_tables_ensured:
        return
    with _item_tables_lock:
        if _item_tables_ensured:
            return
        try:
            ensure_item_tables()
            _item_tables_ensured = True
        except Exception:
            # Silent: endpoints that depend on these tables degrade gracefully.
            pass


def ensure_item_tables() -> None:
    """
    Tables backing the interactive dashboard widgets:
      * user_item_pins — items the user has pinned to the top of a widget
        (e.g. an Azure DevOps work item or ServiceNow incident).
      * user_item_seen — last time the user opened/acknowledged an item,
        so the UI can show a red dot when an item updates after that.

    Both are scoped by (user_id, source, item_id). `source` is a small enum
    string ("azure_devops" or "servicenow"); item_id is whatever the source
    uses (ADO work-item id, SN sys_id).
    """
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_item_pins (
            id         SERIAL PRIMARY KEY,
            user_id    VARCHAR(255) NOT NULL,
            source     VARCHAR(32)  NOT NULL,
            item_id    VARCHAR(128) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (user_id, source, item_id)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_pins_user "
        "ON user_item_pins (user_id, source)"
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_item_seen (
            id         SERIAL PRIMARY KEY,
            user_id    VARCHAR(255) NOT NULL,
            source     VARCHAR(32)  NOT NULL,
            item_id    VARCHAR(128) NOT NULL,
            seen_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (user_id, source, item_id)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_user_item_seen_user "
        "ON user_item_seen (user_id, source)"
    )


def ensure_approval_workflow_tables() -> None:
    """
    Bring the approval-request workflow up to the schema the new self-service
    approval flow expects. Idempotent — safe to run on every startup.

    Additions vs. the base schema (deployment/charts/.../00_schema.sql):
      * approval_status enum gains IN_PROGRESS and COMPLETED values (legacy
        rows using EXECUTED remain valid; the API treats EXECUTED = COMPLETED).
      * approval_requests gains a dedicated rejection_reason column.
      * notifications table for in-app notification bell + dropdown.
    """
    # Add new approval_status values. ALTER TYPE ... ADD VALUE is idempotent
    # via IF NOT EXISTS on PG 12+ but must run outside a multi-value block.
    for value in ("IN_PROGRESS", "COMPLETED"):
        try:
            execute(f"ALTER TYPE approval_status ADD VALUE IF NOT EXISTS '{value}'")
        except Exception:
            # Enum may not exist yet on a minimal dev DB; approvals table
            # creation below is sufficient for fresh installs.
            pass

    # Optional dedicated rejection reason (approver_comments is still the
    # canonical free-text field; rejection_reason just makes filtering easier).
    try:
        execute(
            "ALTER TABLE approval_requests "
            "ADD COLUMN IF NOT EXISTS rejection_reason TEXT"
        )
    except Exception:
        pass

    # How the requester rated a completed request (1-5, optional comment). One per
    # request; deleted with it.
    try:
        execute(
            """
            CREATE TABLE IF NOT EXISTS request_ratings (
                request_id  UUID PRIMARY KEY REFERENCES approval_requests(id) ON DELETE CASCADE,
                user_email  VARCHAR(255) NOT NULL,
                rating      SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment     TEXT,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    except Exception as exc:
        # approval_requests may not exist yet on a minimal dev DB.
        logging.getLogger(__name__).warning("request_ratings not created: %s: %s", type(exc).__name__, exc)

    # In-app notifications (bell + dropdown). Email-keyed so the UI can
    # resolve them directly from the SSO token without an extra users join.
    execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_email     VARCHAR(255) NOT NULL,
            message        TEXT         NOT NULL,
            notif_type     VARCHAR(64),
            related_id     UUID,
            is_read        BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread "
        "ON notifications (user_email, is_read, created_at DESC)"
    )
    # Polish columns: click-to-navigate link + grouping key. Added via ALTER
    # so existing installs keep working without a full migration.
    for stmt in (
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS link TEXT",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS group_key VARCHAR(128)",
    ):
        try:
            execute(stmt)
        except Exception:
            pass

    # When each person last OPENED My Requests, Support and Suggestions. The sidebar's
    # "something changed" dot is every notification pointing into that page since this
    # stamp -- separate from is_read, because clearing the bell is not the same as
    # having looked at the page, and the dot must not vanish with it.
    execute(
        """
        CREATE TABLE IF NOT EXISTS user_section_seen (
            user_email VARCHAR(255) NOT NULL,
            section    VARCHAR(32)  NOT NULL,
            seen_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_email, section)
        )
        """
    )


# Env-provided bootstrap admin identity. Empty means "not configured".
BOOTSTRAP_ADMIN_EMAIL = (os.getenv("HUB_ADMIN_USERNAME") or "").strip().lower()


def sync_bootstrap_admin_role_for_email(email: str) -> None:
    """
    If ``email`` is the bootstrap admin identity, set DB role to Platform Admin.
    Uses case-insensitive match so SSO email casing cannot strand the account
    on a non-admin role. Idempotent.
    """
    e = (email or "").strip().lower()
    if e != BOOTSTRAP_ADMIN_EMAIL.lower():
        return
    try:
        role = query_one(
            "SELECT id FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
            ["platform admin"],
        )
        if not role:
            return
        execute(
            """
            UPDATE users
            SET role_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE LOWER(TRIM(email)) = %s
            """,
            [role["id"], e],
        )
    except Exception:
        pass


# Local service-account credentials are long-lived, live in a pipeline variable group,
# and are the only password the portal itself accepts — so a weak one is not a small
# problem. This does not silently "fix" anything (that would lock the operator out of
# their own portal); it refuses to be quiet about it.
#
# The floor is deliberately about LENGTH and REUSE, not character classes: a 20-character
# passphrase beats "P@ssw0rd!" and forcing symbols mostly produces the latter.
_MIN_LOCAL_PASSWORD_LEN = 14
_OBVIOUS_PASSWORDS = {
    "password", "changeme", "admin", "admin123", "devops", "devopshub",
    "letmein", "welcome", "secret", "p@ssw0rd", "passw0rd", "123456", "test",
}


def audit_local_password(label: str, username: str, password: str) -> list:
    """Return a list of human-readable weaknesses in a local account password.

    Logged at ERROR by the caller. The password itself is never logged — only what is
    wrong with it — because the whole point of the check is that this value is secret.
    """
    problems = []
    if len(password) < _MIN_LOCAL_PASSWORD_LEN:
        problems.append(
            f"shorter than {_MIN_LOCAL_PASSWORD_LEN} characters (it is {len(password)})"
        )
    if password.strip().lower() in _OBVIOUS_PASSWORDS:
        problems.append("is a well-known default password")
    if username and password.strip().lower() == username.strip().lower():
        problems.append("is the same as the username")
    if password != password.strip():
        problems.append("has leading or trailing whitespace (usually a copy-paste slip)")
    if problems:
        import logging as _lg

        _lg.getLogger(__name__).error(
            "%s: the configured password is weak — %s. Rotate it in the pipeline "
            "variable group; this credential is accepted by the portal's own sign-in form.",
            label, "; ".join(problems),
        )
    return problems


def ensure_bootstrap_platform_admin() -> None:
    """
    Ensure the env bootstrap Platform Admin is usable.

    Always upsert the HUB_ADMIN_USERNAME row so rotating GitHub Secrets updates
    the local admin login on the next deploy, even if the database already had
    a Platform Admin before the bootstrap flag existed.
    """
    import logging
    from security import hash_password

    log = logging.getLogger(__name__)
    try:
        username = (os.getenv("HUB_ADMIN_USERNAME") or "").strip()
        password = os.getenv("HUB_ADMIN_PASSWORD") or ""
        if not username or not password:
            log.warning("ensure_bootstrap_platform_admin: HUB_ADMIN_USERNAME/HUB_ADMIN_PASSWORD are not fully configured")
            return
        audit_local_password("HUB_ADMIN_PASSWORD", username, password)

        role = query_one(
            "SELECT id FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
            ["platform admin"],
        )
        if not role:
            log.warning("ensure_bootstrap_platform_admin: Platform Admin role not found")
            return
        rid = role["id"]
        password_hash = hash_password(password)

        bootstrap = query_one(
            """
            SELECT id
            FROM users
            WHERE is_bootstrap_admin = true
               OR LOWER(TRIM(username)) = %s
               OR LOWER(TRIM(email)) = %s
            LIMIT 1
            """,
            [username.lower(), username.lower()],
        )
        if bootstrap:
            execute(
                """
                UPDATE users
                SET username = %s,
                    email = %s,
                    full_name = %s,
                    role_id = %s,
                    password_hash = %s,
                    is_bootstrap_admin = true,
                    is_active = true,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                [username, username.lower(), username, rid, password_hash, bootstrap["id"]],
            )
            log.info("Admin bootstrap updated")
            return

        execute(
            """
            INSERT INTO users (username, email, full_name, role_id, password_hash, is_bootstrap_admin)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (email) DO UPDATE SET
              username = EXCLUDED.username,
              full_name = EXCLUDED.full_name,
              role_id = EXCLUDED.role_id,
              password_hash = EXCLUDED.password_hash,
              is_bootstrap_admin = TRUE,
              is_active = true,
              updated_at = CURRENT_TIMESTAMP
            """,
            [
                username,
                username.lower(),
                username,
                rid,
                password_hash,
            ],
        )
        log.info("Admin bootstrap created")
    except Exception as exc:
        log.warning("ensure_bootstrap_platform_admin failed: %s", exc)


def ensure_bootstrap_service_user() -> None:
    """Ensure the env bootstrap REGULAR user is usable.

    The mirror of ensure_bootstrap_platform_admin, with one difference that is the whole
    reason it exists: it is given the LOWEST role, and never Platform Admin. An admin
    checking "what does this actually look like to a normal person?" otherwise has to
    borrow a colleague's SSO account, which is a bad habit to build into a portal.

    Entirely optional — with HUB_USER_USERNAME / HUB_USER_PASSWORD unset, nothing is
    created and no second credential exists to attack.
    """
    import logging
    from security import hash_password

    log = logging.getLogger(__name__)
    try:
        username = (os.getenv("HUB_USER_USERNAME") or "").strip()
        password = os.getenv("HUB_USER_PASSWORD") or ""
        # An Azure Pipelines macro for a variable that was never defined arrives as the
        # literal "$(HUB_USER_USERNAME)". Creating an account under that name — with a
        # password of "$(HUB_USER_PASSWORD)" — is exactly the sort of default credential
        # this feature must not introduce. Treat unexpanded macros as "not configured".
        if "$(" in username or "$(" in password:
            log.warning(
                "ensure_bootstrap_service_user: HUB_USER_* looks like an unexpanded "
                "pipeline macro — the account was NOT created. Define the variables "
                "(empty is fine) in the variable group."
            )
            return
        if not username or not password:
            log.info("ensure_bootstrap_service_user: not configured, skipping")
            return

        admin_username = (os.getenv("HUB_ADMIN_USERNAME") or "").strip().lower()
        if admin_username and username.lower() == admin_username:
            # Same name would mean one row wearing both flags, and whichever bootstrap
            # ran last would decide whether it is an admin. Refuse rather than gamble.
            log.error(
                "ensure_bootstrap_service_user: HUB_USER_USERNAME must differ from "
                "HUB_ADMIN_USERNAME — the demo account was NOT created."
            )
            return

        audit_local_password("HUB_USER_PASSWORD", username, password)
        if password == (os.getenv("HUB_ADMIN_PASSWORD") or "__unset__"):
            log.error(
                "ensure_bootstrap_service_user: HUB_USER_PASSWORD is identical to "
                "HUB_ADMIN_PASSWORD — one leak now compromises both accounts."
            )

        # Lowest privilege available. hierarchy_level 7 is the seed's regular user;
        # falling back to the highest number keeps this correct if the seed differs.
        role = query_one(
            "SELECT id FROM roles WHERE hierarchy_level = 7 LIMIT 1"
        ) or query_one(
            "SELECT id FROM roles ORDER BY hierarchy_level DESC LIMIT 1"
        )
        if not role:
            log.warning("ensure_bootstrap_service_user: no role found, skipping")
            return

        password_hash = hash_password(password)
        existing = query_one(
            """
            SELECT id
            FROM users
            WHERE is_bootstrap_user = true
               OR LOWER(TRIM(username)) = %s
               OR LOWER(TRIM(email)) = %s
            LIMIT 1
            """,
            [username.lower(), username.lower()],
        )
        if existing:
            execute(
                """
                UPDATE users
                SET username = %s,
                    email = %s,
                    full_name = %s,
                    role_id = %s,
                    password_hash = %s,
                    is_bootstrap_user = true,
                    is_bootstrap_admin = false,
                    is_active = true,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                [username, username.lower(), username, role["id"], password_hash, existing["id"]],
            )
            log.info("Service user bootstrap updated")
            return

        execute(
            """
            INSERT INTO users (username, email, full_name, role_id, password_hash,
                               is_bootstrap_user, is_bootstrap_admin)
            VALUES (%s, %s, %s, %s, %s, TRUE, FALSE)
            ON CONFLICT (email) DO UPDATE SET
              username = EXCLUDED.username,
              full_name = EXCLUDED.full_name,
              role_id = EXCLUDED.role_id,
              password_hash = EXCLUDED.password_hash,
              is_bootstrap_user = TRUE,
              is_bootstrap_admin = FALSE,
              is_active = true,
              updated_at = CURRENT_TIMESTAMP
            """,
            [username, username.lower(), username, role["id"], password_hash],
        )
        log.info("Service user bootstrap created")
    except Exception as exc:
        log.warning("ensure_bootstrap_service_user failed: %s", exc)




def collapse_non_admin_roles() -> None:
    """Move every account that is not a Platform Admin onto the lowest role.

    The roles table shipped seven levels, but only level 1 was ever a real
    privilege boundary: the intermediate roles differed from one another solely
    in a ``permissions`` array that no authorization decision read. Approval was
    the single exception — it tested ``hierarchy_level <= 5`` — and that is now
    Platform-Admin-only too, which leaves levels 2 to 6 granting exactly nothing.

    Leaving accounts sitting on them would keep the misleading label without the
    access, so this puts the data where the model already is. Idempotent: it
    reports how many rows it touched and does nothing at all on the next start.

    Deliberately NOT a DROP or a DELETE. The intermediate role rows stay in the
    table, because deleting rows that ``users.role_id`` may still reference is
    the kind of startup-path destruction this codebase has been bitten by before.
    """
    try:
        target = query_one(
            "SELECT id, hierarchy_level FROM roles WHERE hierarchy_level = %s LIMIT 1",
            [REGULAR_USER_LEVEL],
        ) or query_one(
            "SELECT id, hierarchy_level FROM roles ORDER BY hierarchy_level DESC LIMIT 1"
        )
        if not target:
            log.warning("collapse_non_admin_roles: no role to demote to, skipping")
            return

        moved = execute_returning(
            """
            UPDATE users u
               SET role_id = %s, updated_at = CURRENT_TIMESTAMP
              FROM roles r
             WHERE u.role_id = r.id
               AND r.hierarchy_level <> %s
               AND u.role_id <> %s
            RETURNING u.email, r.name AS previous_role
            """,
            [target["id"], PLATFORM_ADMIN_LEVEL, target["id"]],
        )
        if moved:
            # WARNING, not INFO: the root logger sits at WARNING, and a silent
            # privilege change is exactly the event an operator must be able to
            # find afterwards on the Logs page.
            log.warning(
                "collapse_non_admin_roles: moved %d account(s) to the lowest role: %s",
                len(moved),
                ", ".join(
                    f"{r.get('email')} (was {r.get('previous_role')})" for r in moved[:20]
                ),
            )
    except Exception as exc:
        log.warning("collapse_non_admin_roles failed: %s", exc)
