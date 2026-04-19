import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2 import errorcodes, pool
from psycopg2.extensions import register_adapter
from psycopg2.extras import Json, RealDictCursor
from config import get_settings

# Globally teach psycopg2 how to serialize Python dicts into Postgres JSON /
# JSONB columns. Without this, any `execute(sql, [some_dict])` raises
# `ProgrammingError: can't adapt type 'dict'`, which bites endpoints that
# store JSONB (approval_requests.request_payload, audit_logs.details, etc.).
# We intentionally don't adapt `list` — psycopg2's default list→ARRAY
# adapter is still correct for any Postgres text/uuid array columns.
register_adapter(dict, Json)

# Lazily initialised so that the module can be imported without a live DB
# (avoids CrashLoopBackOff when the pool creation fails at import time).
_db_pool: "psycopg2.pool.SimpleConnectionPool | None" = None


def _get_pool() -> "psycopg2.pool.SimpleConnectionPool":
    global _db_pool
    if _db_pool is None:
        settings = get_settings()
        _db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, settings.database_url)
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


def health_check() -> bool:
    """Simple database health check."""
    try:
        query_one("SELECT 1")
        return True
    except Exception:
        return False


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
        # Legacy tables from earlier iterations — dropped so the new
        # event-based widget_usage schema can take over cleanly.
        (
            "drop_legacy_widget_usage",
            "DROP TABLE IF EXISTS widget_usage",
        ),
        (
            "drop_legacy_widget_user_views",
            "DROP TABLE IF EXISTS widget_user_views",
        ),
        (
            "drop_legacy_home_widget_prefs",
            "DROP TABLE IF EXISTS home_widget_prefs",
        ),
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


# Primary bootstrap admin (DB role Platform Admin). Single allowed hard-coded identity.
BOOTSTRAP_ADMIN_EMAIL = "golden.mihel@gmail.com"


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


def ensure_bootstrap_platform_admin() -> None:
    """
    Ensure the bootstrap user exists and has Platform Admin (app role Admin).
    Safe to run on every startup; no-ops if core tables or role seed are missing.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        role = query_one(
            "SELECT id FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
            ["platform admin"],
        )
        if not role:
            log.warning("ensure_bootstrap_platform_admin: Platform Admin role not found")
            return
        rid = role["id"]
        execute(
            """
            INSERT INTO users (username, email, full_name, role_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET
              role_id = EXCLUDED.role_id,
              is_active = true,
              updated_at = CURRENT_TIMESTAMP
            """,
            [
                BOOTSTRAP_ADMIN_EMAIL,
                BOOTSTRAP_ADMIN_EMAIL,
                "Golden Mihel",
                rid,
            ],
        )
    except Exception as exc:
        log.warning("ensure_bootstrap_platform_admin failed: %s", exc)


