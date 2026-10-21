"""A ticket opened from the Hub must be FOR the person who opened it, not the service
account that submits it.

The fake ServiceNow behaves like this instance: the record producer's caller question
is a Catalog Builder "Choice -> Record reference" (a Lookup Select Box, type 18, with
lookup_table sys_user) inside a container, and the producer's script falls back to the
submitting account when that question arrives empty.

Run with:  cd backend && python -m pytest tests -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

httpx = pytest.importorskip("httpx", reason="backend deps not installed")

import snow_catalog  # noqa: E402
from api import servicenow  # noqa: E402

SERVICE_ACCOUNT = "svc000000000000000000000000000000"
REQUESTER = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
USERS = [
    {"sys_id": SERVICE_ACCOUNT, "email": "monitor@corp.example", "user_name": "monitor", "active": "true"},
    {"sys_id": REQUESTER, "email": "Dana.Levi@corp.example", "user_name": "dlevi", "active": "true"},
]
CALLER_QUESTION = {"name": "caller_id", "label": "Caller", "type": "18", "mandatory": "false",
                   "lookup_table": "sys_user", "choices": []}


def fake_servicenow(caller_question=CALLER_QUESTION, users=USERS):
    state = {"submitted": None, "caller": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/now/table/sys_user":
            query = request.url.params.get("sysparm_query", "")
            wanted = {part.split("=", 1)[1].lower() for part in query.split("^OR") if "=" in part}
            rows = [u for u in users if u["email"].lower() in wanted or u["user_name"].lower() in wanted]
            return httpx.Response(200, json={"result": rows})
        if path.startswith("/api/sn_sc/servicecatalog/items/") and request.method == "GET":
            container = {"name": "about_you", "type": "19", "children": [caller_question]}
            return httpx.Response(200, json={"result": {"variables": [container]}})
        if path.endswith("/submit_producer"):
            variables = json.loads(request.content)["variables"]
            state["submitted"] = variables
            # The producer's script: the caller question, else whoever submitted it.
            state["caller"] = variables.get("caller_id") or SERVICE_ACCOUNT
            return httpx.Response(200, json={"result": {"sys_id": "inc0001", "table": "incident"}})
        if path == "/api/now/table/incident/inc0001":
            name = "Dana Levi" if state["caller"] == REQUESTER else "monitor user"
            return httpx.Response(200, json={"result": {
                "number": {"value": "INC0001", "display_value": "INC0001"},
                "state": {"value": "1", "display_value": "Open"},
                "caller_id": {"value": state["caller"], "display_value": name},
            }})
        if path == "/api/now/table/sys_user_group":
            return httpx.Response(200, json={"result": [{"sys_id": "grp1"}]})
        return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {path}"}})

    return state, handler


def open_ticket(monkeypatch, handler, user):
    monkeypatch.setattr(servicenow, "_snow_ticket_flow_client",
                        lambda: httpx.Client(base_url="https://snow.example", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(servicenow.identity, "trusted_name", lambda **_: "Dana Levi")
    monkeypatch.setattr(servicenow.activity, "log_activity", lambda **_: None)
    monkeypatch.setattr(servicenow.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(servicenow, "_ensure_user_tickets_table", lambda: None)
    answers = dict(full_name="", phone_number="0501234567", branch="b", team="t", section="s", role="r",
                   network="n", devops_services="artifactory", azure_devops_support_type=None,
                   azure_devops_collection=None, azure_devops_project=None, pipeline_url=None,
                   reason="other", reason_other=None, title="Build is red", urgency="low",
                   description="d", work_impact="w", help_text="h", support_group=None, attachments=None)
    return servicenow.create_ticket_flow(**answers, current_user=user)


def test_the_ticket_is_for_the_person_who_opened_it(monkeypatch):
    state, handler = fake_servicenow()
    result = open_ticket(monkeypatch, handler, {"id": "u1", "email": "dana.levi@corp.example", "username": "dana.levi@corp.example"})
    assert state["submitted"]["caller_id"] == REQUESTER
    assert result["data"]["caller_set"] == "insert"
    assert result["data"]["caller"] == "Dana Levi"


def test_a_domain_login_resolves_by_its_bare_account(monkeypatch):
    state, handler = fake_servicenow()
    open_ticket(monkeypatch, handler, {"id": "u1", "email": "", "username": "CORP\\dlevi"})
    assert state["submitted"]["caller_id"] == REQUESTER


def test_an_unknown_person_is_reported_not_guessed(monkeypatch):
    state, handler = fake_servicenow(users=[USERS[0]])
    result = open_ticket(monkeypatch, handler, {"id": "u1", "email": "nobody@corp.example", "username": ""})
    assert "caller_id" not in state["submitted"]
    assert result["data"]["caller_set"] == "unresolved"
    assert result["data"]["caller"] == ""


def test_a_lookup_that_submits_user_names_gets_the_user_name(monkeypatch):
    question = dict(CALLER_QUESTION, choices=[{"value": "monitor", "label": "monitor user"},
                                              {"value": "dlevi", "label": "Dana Levi"}])
    state, handler = fake_servicenow(caller_question=question)
    open_ticket(monkeypatch, handler, {"id": "u1", "email": "dana.levi@corp.example", "username": "dlevi"})
    assert state["submitted"]["caller_id"] == "dlevi"


# ── the pieces ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", [
    {"name": "caller_id", "type": "18", "lookup_table": "sys_user"},          # Lookup Select Box
    {"name": "caller_id", "type": "8", "reference": "sys_user"},              # Reference
    {"name": "who", "label": "Requested for", "friendly_type": "reference", "reference": "sys_user"},
])
def test_a_question_about_a_person_is_the_caller_question(spec):
    other = {"name": "choose_a_support_group", "type": "8", "reference": "sys_user_group"}
    assert servicenow._caller_variable([other, spec]) == spec["name"]


def test_free_text_named_caller_is_never_the_caller_question():
    assert servicenow._caller_variable([{"name": "caller", "type": "6"}]) == ""


def test_a_forced_placeholder_never_picks_somebody_off_a_list():
    lookup = {"name": "caller_id", "type": "18", "lookup_table": "sys_user",
              "choices": [{"value": SERVICE_ACCOUNT}, {"value": REQUESTER}]}
    assert servicenow._first_choice_for_spec(lookup, caller_sys_id="") is None
    assert servicenow._first_choice_for_spec(lookup, caller_sys_id=REQUESTER) == REQUESTER


def test_account_forms_cover_every_spelling():
    assert snow_catalog.account_forms("Dana.Levi@corp.example", "CORP\\dlevi") == [
        "Dana.Levi@corp.example", "Dana.Levi", "CORP\\dlevi", "dlevi"]


def test_find_user_prefers_the_exact_form_and_an_active_account():
    rows = [
        {"sys_id": "old", "email": "", "user_name": "dlevi", "active": "false"},
        {"sys_id": "new", "email": "", "user_name": "dlevi", "active": "true"},
    ]
    client = httpx.Client(base_url="https://snow.example",
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"result": rows})))
    assert snow_catalog.find_user(client, "CORP\\dlevi") == "new"
