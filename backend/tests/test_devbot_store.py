"""DevBot's SQL, run against a real Postgres.

Every query in devbot/store.py runs here, because the pure tests cannot catch a query
Postgres refuses -- an ORDER BY naming an output alias inside an expression parses in
Python and fails in the database. Skipped where no database is reachable.

Run with:  cd backend && DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("psycopg2", reason="backend deps not installed")


@pytest.fixture(scope="module")
def store():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    from devbot import store as devbot_store
    try:
        devbot_store.ensure_tables()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no database: {exc}")
    return devbot_store


def test_a_conversation_round_trip(store):
    owner = "test-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(owner, "Why did it fail?", "m1")
    cid = conv["id"]
    store.add_message(cid, "user", "Why did it fail?", {})
    answer = store.add_message(cid, "assistant", "Because.", {"actions": [{"id": "a1", "status": "proposed", "title": "Re-run"}]})
    assert [m["role"] for m in store.messages(owner, cid)] == ["user", "assistant"]
    assert store.messages("someone-else", cid) == []
    assert store.get_conversation("someone-else", cid) is None

    updated = store.update_action(owner, cid, answer, "a1", {"status": "done", "result": "Queued."})
    assert updated["status"] == "done"
    assert store.update_action("someone-else", cid, answer, "a1", {"status": "declined"}) is None
    assert store.messages(owner, cid)[-1]["meta"]["actions"][0]["result"] == "Queued."

    assert store.rename_conversation(owner, cid, "Renamed")
    assert store.list_conversations(owner)[0]["title"] == "Renamed"
    store.touch_conversation(owner, cid, "m2")
    assert store.get_conversation(owner, cid)["model"] == "m2"
    assert store.delete_conversation(owner, cid)
    assert store.get_conversation(owner, cid) is None


def test_usage_adds_up_per_model(store):
    owner = "test-" + uuid.uuid4().hex[:8]
    store.record_usage(owner, "m1", 1000, 200, 2)
    store.record_usage(owner, "m1", 500, 100, 1)
    store.record_usage(owner, "m2", 10, 5, 1)
    rows = {r["model"]: r for r in store.usage_summary(owner)["models"]}
    assert rows["m1"]["today_requests"] == 3 and rows["m1"]["month_in"] == 1500 and rows["m1"]["questions"] == 2
    assert list(rows)[0] == "m1"


def test_purge_runs(store):
    store.purge(30)
