"""DevBot monitoring and past fixes: the pure parts, then every monitoring query against a
real Postgres (skipped where none is reachable).

Run with:  cd backend && DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")

import httpx  # noqa: E402

from devbot import knowledge  # noqa: E402


# ── past fixes: finding the field a fix is written in ─────────────────────────

FIELDS = [
    {"referenceName": "System.Title", "name": "Title", "type": "string"},
    {"referenceName": "Microsoft.VSTS.Common.ResolvedReason", "name": "Resolved Reason", "type": "string"},
    {"referenceName": "Custom.RootCause", "name": "Root Cause", "type": "plainText"},
    {"referenceName": "Custom.Resolution", "name": "Resolution", "type": "html"},
    {"referenceName": "Custom.ResolutionDate", "name": "Resolution date", "type": "dateTime"},
    {"referenceName": "Custom.Fix", "name": "פתרון", "type": "html"},
]


def test_the_fix_field_is_found_by_what_it_is_about():
    found = knowledge.resolution_fields(FIELDS)
    assert found[0] == "Custom.Resolution"
    assert set(found) == {"Custom.Resolution", "Custom.RootCause", "Custom.Fix"}
    assert knowledge.resolution_fields(FIELDS, "Custom.Whatever") == ["Custom.Whatever"]


def test_types_and_projects_may_have_spaces():
    assert knowledge.parse_list("Bug, Grafana Alert\nProduct Backlog Item, Bug") == ["Bug", "Grafana Alert", "Product Backlog Item"]


def test_ado_lists_only_items_with_a_fix_and_says_how_many_had_none(monkeypatch):
    from api import azure_devops as ado_api

    items = {
        1: {"System.Title": "TLS to Artifactory", "System.WorkItemType": "Bug", "System.TeamProject": "Hub Project",
            "System.Rev": 4, "System.Description": "<p>x509 unknown authority</p>", "Custom.Resolution": "<p>Installed the CA</p>"},
        2: {"System.Title": "No fix written", "System.WorkItemType": "Bug", "System.TeamProject": "Hub Project", "System.Rev": 1},
    }
    seen_queries = []

    def handler(request):
        path = request.url.path
        if path.endswith("/_apis/wit/fields"):
            return httpx.Response(200, json={"value": FIELDS})
        if path.endswith("/_apis/wit/wiql"):
            seen_queries.append(request.read().decode())
            return httpx.Response(200, json={"workItems": [{"id": 1}, {"id": 2}]})
        if path.endswith("/_apis/wit/workitems"):
            return httpx.Response(200, json={"value": [{"id": i, "fields": f} for i, f in items.items()]})
        return httpx.Response(404, json={})

    monkeypatch.setattr(ado_api, "_admin_ado_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://ado.test"))
    monkeypatch.setattr(ado_api, "_discover_ado_bases", lambda client: ["https://ado.test/tfs/Main"])
    s = {"ado": {"types": ["Bug", "Grafana'Alert"], "projects": ["Hub Project"], "days": 365, "field": ""}}
    detail = {}
    out = knowledge.list_ado(s, detail)
    assert [o["id"] for o in out] == ["ado:Main:1"]
    # Found by the problem; the fix goes only to the review (knowledge.review_fix).
    assert out[0]["ref"] == "Bug #1" and out[0]["text"] == "x509 unknown authority"
    assert "Installed the CA" in out[0]["fix_raw"] and "Installed" not in out[0]["text"]
    assert out[0]["url"] == "https://ado.test/tfs/Main/Hub%20Project/_workitems/edit/1"
    assert detail["ado"]["without_fix"] == 1
    assert detail["ado"]["collections"]["Main"]["fields"][0] == "Custom.Resolution"
    # A quote in a type name cannot end the WIQL string early.
    assert "'Grafana''Alert'" in seen_queries[0] and "'Hub Project'" in seen_queries[0]
    # Only the collections named; one that does not exist is said, not silently empty.
    assert knowledge.list_ado({"ado": {**s["ado"], "collections": ["Main", "Gone"]}}, {}) != []
    with pytest.raises(RuntimeError, match="no collection called gone"):
        knowledge.list_ado({"ado": {**s["ado"], "collections": ["Gone"]}}, {})


def test_a_work_item_the_person_cannot_open_is_not_visible(monkeypatch):
    def handler(request):
        return httpx.Response(200 if request.url.path.endswith("/1") else 404, json={"id": 1})

    real = httpx.Client
    monkeypatch.setattr(knowledge.httpx, "Client", lambda *a, **kw: real(transport=httpx.MockTransport(handler)))
    check = knowledge.ado_check("pat")
    assert check({"id": "ado:Main:1", "origin": "https://ado.test/tfs/Main"}) is True
    assert check({"id": "ado:Main:2", "origin": "https://ado.test/tfs/Main"}) is False


# ── the save-the-fix offer ────────────────────────────────────────────────────

def test_save_the_fix_is_offered_only_for_a_real_answer_with_confluence():
    from devbot import orchestrator

    state = orchestrator._Turn()
    state.answer = "Install the CA on the agent."
    state.save_fix = {"title": "Fix: x509 unknown authority", "about": "x509 unknown authority"}
    offer = orchestrator.save_fix_action(state, {"confluence": True})
    assert offer and offer["kind"] == "conf-create" and "Install the CA" in offer["target"]["content"]
    assert offer["target"]["title"] == "Fix: x509 unknown authority"
    assert orchestrator.save_fix_action(state, {"confluence": False}) is None
    state.found_only = True
    assert orchestrator.save_fix_action(state, {"confluence": True}) is None


# ── monitoring, against a real Postgres ───────────────────────────────────────

@pytest.fixture(scope="module")
def monitor():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    from devbot import monitor as devbot_monitor
    try:
        devbot_monitor.ensure_tables()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no database: {exc}")
    from db import query_one

    # The people view joins the Hub's users; a bare test database has none.
    # The people view joins the Hub's users and connections, and the Hub key's figures
    # read the index's runs; a bare test database has none of them.
    for table in ("users", "user_integrations"):
        if not (query_one("SELECT to_regclass(%s) IS NOT NULL AS ok", [table]) or {}).get("ok"):
            pytest.skip(f"this database has no {table} table")
    from devbot import knowledge
    knowledge.ensure_tables()
    return devbot_monitor


def test_every_monitoring_query_runs(monitor):
    from db import execute

    uid = "test-" + uuid.uuid4().hex[:8]
    steps = [{"tool": "conf_search", "system": "confluence", "status": "done", "ms": 120},
             {"tool": "ado_work_item", "system": "azure", "status": "error", "ms": 40, "detail": "401 from Azure DevOps"}]
    monitor.record(uid, conversation_id="c1", message_id=None, model="m/one", outcome="answered", error_kind="",
                   requests=2, prompt_tokens=1000, completion_tokens=100, duration_ms=2500, limit_waits=1, steps=steps)
    monitor.record(uid, conversation_id="c1", message_id=None, model="m/one", outcome="failed", error_kind="limit",
                   requests=1, prompt_tokens=10, completion_tokens=0, duration_ms=300, limit_waits=0, steps=[])
    monitor.set_feedback(uid, "c1", 987654321, "up", "", "Why?", "Because.",
                         {"model": "m/one", "steps": steps, "sources": [{"title": "Page", "url": "https://conf/p/1"}]})
    try:
        ov = monitor.overview(30)
        assert ov["totals"]["questions"] >= 2 and ov["totals"]["failed"] >= 1 and ov["totals"]["up"] >= 1
        assert any(t["tool"] == "ado_work_item" and t["failed"] >= 1 for t in ov["tools"])
        assert any(e["error"] == "401 from Azure DevOps" for e in ov["tool_errors"])
        assert any(e["error_kind"] == "limit" for e in ov["errors"])
        assert len(ov["daily"]) == 30
        me = [p for p in monitor.people(30) if p["user_id"] == uid]
        assert me and me[0]["questions"] == 2 and me[0]["failed"] == 1 and me[0]["up"] == 1 and me[0]["limit_waits"] == 1
        assert any(f["question"] == "Why?" for f in monitor.feedback("up", 30))
        monitor._votes["at"] = 0
        assert monitor.source_votes().get("https://conf/p/1", 0) >= 1
        monitor.set_feedback(uid, "c1", 987654321, "", "", "", "", {})
        assert not any(f["question"] == "Why?" and f.get("username") is None and f["rating"] == "up"
                       for f in monitor.feedback("up", 30) if f["note"] == "" and f["answer"] == "Because.")
        # AdminBot's questions are not DevBot's: they count only in the Hub key's spend.
        monitor.record(uid, conversation_id="c2", message_id=None, model="m/one", outcome="answered", error_kind="",
                       requests=3, prompt_tokens=500, completion_tokens=50, duration_ms=900, limit_waits=0, steps=[],
                       bot="admin")
        again = monitor.overview(30)
        assert again["totals"]["questions"] == ov["totals"]["questions"]
        assert again["hub_key"]["adminbot_questions"] >= 1 and again["hub_key"]["adminbot_tokens"] >= 550
        # All time: from the first question kept, drawn in days or weeks.
        forever = monitor.overview(0)
        assert forever["all_time"] and forever["days"] >= 1 and forever["daily"]
        assert monitor.people(0) and isinstance(monitor.feedback("", 0), list)
        monitor.purge()
    finally:
        execute("DELETE FROM devbot_events WHERE user_id = %s", [uid])
        execute("DELETE FROM devbot_feedback WHERE user_id = %s", [uid])
