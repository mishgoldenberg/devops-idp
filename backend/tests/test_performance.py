"""What keeps the Hub fast under load and on every page switch: connections that are
kept and waited for, files the browser may keep, and one request where there were many.

The database parts run against a real Postgres (skipped where none is reachable).

Run with:  cd backend && DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("psycopg2", reason="backend deps not installed")
pytest.importorskip("httpx", reason="backend deps not installed")

import db  # noqa: E402


@pytest.fixture(scope="module")
def database():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    try:
        db.query_one("SELECT 1 AS ok")
        db.ensure_observability_tables_once()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no database: {exc}")
    return db


# ── connections ──────────────────────────────────────────────────────────────

def test_returned_connections_are_kept_not_closed(database):
    pool = db._KeepingPool(1, 4, db._dsn_with_timeouts(os.environ["DATABASE_URL"]))
    try:
        held = [pool.getconn() for _ in range(3)]
        for conn in held:
            pool.putconn(conn)
        # psycopg2's own pool would have closed two of the three (it keeps minconn=1).
        assert len(pool._pool) == 3 and not any(c.closed for c in held)
        again = pool.getconn()
        assert again in held  # reused, not a new connection
        pool.putconn(again)
    finally:
        pool.closeall()


def test_a_busy_pool_makes_a_request_wait_not_fail(database, monkeypatch):
    monkeypatch.setattr(db, "_pool_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(db, "_POOL_WAIT", 5.0)
    released = threading.Event()

    def hold():
        with db.get_connection():
            time.sleep(0.4)
        released.set()

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.05)
    started = time.monotonic()
    assert db.query_one("SELECT 1 AS ok")["ok"] == 1  # waited for the one slot
    assert time.monotonic() - started >= 0.25 and released.is_set()
    holder.join()

    monkeypatch.setattr(db, "_POOL_WAIT", 0.1)
    with db.get_connection():
        with pytest.raises(db.DatabaseBusy):
            db.query_one("SELECT 1 AS ok")


# ── files the browser keeps ──────────────────────────────────────────────────

def test_static_files_say_how_long_they_may_be_kept(tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from main import CachedStaticFiles, static_fingerprint

    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "output.css").write_text("body{}")
    app = Starlette(routes=[Mount("/static", CachedStaticFiles(directory=tmp_path))])
    client = TestClient(app)
    assert "immutable" in client.get("/static/css/output.css?v=abc").headers["cache-control"]
    assert client.get("/static/css/output.css").headers["cache-control"].startswith("public, max-age=3600")
    before = static_fingerprint(tmp_path)
    (tmp_path / "css" / "output.css").write_text("body{color:red}")
    assert static_fingerprint(tmp_path) != before  # a changed file is a new link


# ── one request where there were many ───────────────────────────────────────

def test_widget_views_arrive_as_one_request(database):
    from api.observability import record_widget_event

    who = f"perf-{uuid.uuid4().hex[:8]}@example.com"
    out = record_widget_event({"widget_keys": ["quick_links", "snow_my_tickets", "quick_links", "nope"],
                               "session_id": "s1"}, current_user={"email": who})
    assert out["success"]
    rows = db.query_all("SELECT widget_key FROM widget_usage WHERE user_id = %s ORDER BY widget_key", [who])
    assert [r["widget_key"] for r in rows] == ["quick_links", "snow_my_tickets"]
    # The single-key form the old page sends still works.
    record_widget_event({"widget_key": "quick_links"}, current_user={"email": who})
    assert len(db.query_all("SELECT 1 FROM widget_usage WHERE user_id = %s", [who])) == 3


def test_the_widget_list_is_replaced_in_one_transaction(database):
    from api.dashboards import sync_user_widgets

    who = f"perf-{uuid.uuid4().hex[:8]}@example.com"
    sync_user_widgets({"widgets": ["quick_links", "snow_my_tickets"]}, current_user={"email": who})
    sync_user_widgets({"widgets": ["snow_my_tickets"]}, current_user={"email": who})
    rows = db.query_all("SELECT widget_key FROM user_widgets WHERE user_id = %s", [who])
    assert [r["widget_key"] for r in rows] == ["snow_my_tickets"]
