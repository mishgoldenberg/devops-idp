from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor
from config import get_settings

# Initialize a connection pool globally (create once, reuse many times)
settings = get_settings()
db_pool = psycopg2.pool.SimpleConnectionPool(
    1, 20, settings.database_url
)

@contextmanager
def get_connection():
    """Context manager yielding a PostgreSQL connection from the pool."""
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()  # Automatically commit if no errors occur
    except Exception as e:
        conn.rollback()  # Roll back if an error occurs
        raise e
    finally:
        db_pool.putconn(conn) # Return connection to the pool

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


