from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from config import get_settings

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


