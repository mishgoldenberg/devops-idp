"""What DevBot and AdminBot may put in front of a person to confirm, and what happens
when they do: the drafts, the caps, and the endpoints that perform them.

No network and no database: the far systems are fakes, and every write is checked for
what it would have sent.

Run with:  cd backend && python -m pytest tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from devbot import orchestrator, tools  # noqa: E402
from devbot.tools import hub, selfservice, support  # noqa: E402

ME = {"id": "u1", "email": "me@example.com", "username": "me"}
ALL = {"azure": True, "sonarqube": True, "artifactory": True, "confluence": True, "servicenow": True, "requests": True}


def _names(specs):
    return {s["function"]["name"] for s in specs}


# ── drafts of the Hub's own forms ────────────────────────────────────────────

def test_a_draft_fills_only_real_questions_with_real_choices():
    values, dropped = selfservice.prefill("support_ticket", {
        "title": "Build agent out of disk", "urgency": "HIGH", "devops_services": "azure devops",
        "phone_number": "050", "full_name": "Somebody Else",  # the requester's own details: never
        "attachments": "x.png",                              # a file: never
        "reason": "because I said so",                       # not one of its choices
        "made_up": "x",
    })
    assert values == {"title": "Build agent out of disk", "urgency": "high", "devops_services": "azure devops"}
    assert set(dropped) == {"phone_number", "full_name", "attachments", "reason", "made_up"}


def test_a_request_draft_names_its_form_and_what_submitting_does():
    out = tools.run(tools.ToolContext(ME, ALL), "propose_request",
                    {"form": "artifactory_quota", "values": {"justification": "Nightly builds grew", "increase_by": "50 GB"}})
    assert out["ok"], out
    action = out["actions"][0]
    assert action["kind"] == "catalog-request" and action["system"] == "requests"
    assert action["target"]["form"] == "artifactory_quota"
    assert action["target"]["values"] == {"justification": "Nightly builds grew", "increase_by": "50 GB"}
    assert any("approv" in line for line in action["target"]["lines"])
    refused = tools.run(tools.ToolContext(ME, ALL), "propose_request", {"form": "support_ticket"})
    assert not refused["ok"]


def test_a_ticket_draft_is_a_support_card():
    out = tools.run(tools.ToolContext(ME, ALL), "propose_support_ticket",
                    {"title": "Agent-04 out of disk", "description": "No space left on device", "urgency": "urgent"})
    action = out["actions"][0]
    assert action["kind"] == "support-ticket" and action["system"] == "servicenow"
    assert action["target"]["values"]["urgency"] == "urgent"


def test_a_reply_is_drafted_only_on_the_persons_own_open_ticket(monkeypatch):
    monkeypatch.setattr(support, "_mine", lambda ctx: [
        {"number": "INC001", "sys_id": "a1", "short_description": "VPN", "state": "In Progress"},
        {"number": "INC002", "sys_id": "a2", "short_description": "Old", "state": "Closed"}])
    ctx = tools.ToolContext(ME, ALL)
    out = tools.run(ctx, "propose_ticket_reply", {"number": "inc001", "text": "It works now, thanks."})
    assert out["ok"] and out["actions"][0]["target"]["sys_id"] == "a1"
    assert not tools.run(ctx, "propose_ticket_reply", {"number": "INC999", "text": "x"})["ok"]
    assert not tools.run(ctx, "propose_ticket_reply", {"number": "INC002", "text": "x"})["ok"]


def test_asking_for_a_change_offers_the_drafts():
    assert "propose_request" in _names(tools.specs_for("I need more Artifactory quota for my project", ALL))
    assert "propose_support_ticket" in _names(tools.specs_for("open a support ticket about this", ALL))
    assert "propose_pipeline_cancel" in _names(tools.specs_for("cancel my running azure devops pipeline", ALL))
    # A plain question is not a request for a change.
    assert not [n for n in _names(tools.specs_for("what is the status of my support tickets?", ALL))
                if n.startswith("propose_")]


# ── the caps ─────────────────────────────────────────────────────────────────

def test_proposals_are_capped_per_answer_and_per_conversation():
    turn = orchestrator._Turn()
    assert orchestrator.room_for_proposals(turn) == orchestrator.MAX_PROPOSALS_PER_ANSWER
    turn.actions = [{"suggested": True}] * 3  # an investigation's own offers do not count
    assert orchestrator.room_for_proposals(turn) == orchestrator.MAX_PROPOSALS_PER_ANSWER
    turn.actions += [{}] * orchestrator.MAX_PROPOSALS_PER_ANSWER
    assert orchestrator.room_for_proposals(turn) == 0
    rows = [{"role": "assistant", "meta": {"actions": [{}, {"suggested": True}]}}] * 29
    turn = orchestrator._Turn()
    turn.proposed_before = orchestrator.proposals_so_far(rows)
    assert turn.proposed_before == 29 and orchestrator.room_for_proposals(turn) == 1


# ── AdminBot ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(monkeypatch):
    import security

    monkeypatch.setattr(security, "has_effective_admin_access_live", lambda user: True)
    return {"id": "a1", "email": "admin@example.com", "username": "admin"}


def test_a_bulk_decision_decides_each_request_on_its_own(admin, monkeypatch):
    from api import approvals

    decided = []

    def approve(request_id, body, current_user=None):
        if request_id == "r2":
            raise HTTPException(status_code=409, detail="It is no longer pending.")
        decided.append((request_id, body.comments))

    monkeypatch.setattr(approvals, "approve_request", approve)
    action = {"kind": "requests-approve", "target": {"items": [
        {"request_id": "r1", "title": "Quota A", "requester": "x", "type": "Q"},
        {"request_id": "r2", "title": "Quota B", "requester": "y", "type": "Q"},
        {"request_id": "r3", "title": "Quota C", "requester": "z", "type": "Q"}], "comment": ""}}
    out = hub.run_action(admin, action, "Approved in the weekly review")
    assert decided == [("r1", "Approved in the weekly review"), ("r3", "Approved in the weekly review")]
    assert out["result"].startswith("Approved 2 of 3.") and "Quota B: It is no longer pending." in out["result"]

    monkeypatch.setattr(approvals, "approve_request", lambda *a, **k: (_ for _ in ()).throw(HTTPException(409, "no")))
    with pytest.raises(HTTPException) as err:
        hub.run_action(admin, action)
    assert err.value.status_code == 409 and "None was done" in err.value.detail


def test_the_index_build_is_started_and_refused_through_the_index(admin, monkeypatch):
    from devbot import knowledge

    monkeypatch.setattr(knowledge, "start", lambda trigger: {"started": trigger == "adminbot"})
    assert "started" in hub.run_action(admin, {"kind": "index-build", "target": {}})["result"]
    monkeypatch.setattr(knowledge, "start", lambda trigger: {"started": False, "why": "A build is already running."})
    with pytest.raises(HTTPException) as err:
        hub.run_action(admin, {"kind": "index-build", "target": {}})
    assert err.value.detail == "A build is already running."
    monkeypatch.setattr(knowledge, "request_stop", lambda: "none")
    with pytest.raises(HTTPException):
        hub.run_action(admin, {"kind": "index-stop", "target": {}})


def test_a_past_fix_is_hidden_in_the_admins_name(admin, monkeypatch):
    from devbot import knowledge

    seen = []
    monkeypatch.setattr(knowledge, "set_hidden", lambda fid, hidden, by: seen.append((fid, hidden, by)) or True)
    out = hub.run_action(admin, {"kind": "fix-hide", "target": {"id": "ado:x:1", "title": "T", "ref": "#1"}})
    assert seen == [("ado:x:1", True, "admin")] and out["result"] == "Hidden: #1 T"


def test_adminbot_refuses_a_non_admin_and_an_unknown_kind(monkeypatch):
    import security

    monkeypatch.setattr(security, "has_effective_admin_access_live", lambda user: False)
    with pytest.raises(HTTPException) as err:
        hub.run_action({"id": "u"}, {"kind": "index-build", "target": {}})
    assert err.value.status_code == 403
    monkeypatch.setattr(security, "has_effective_admin_access_live", lambda user: True)
    with pytest.raises(HTTPException) as err:
        hub.run_action({"id": "u"}, {"kind": "drop-everything", "target": {}})
    assert err.value.status_code == 400


# ── the endpoints that perform them ──────────────────────────────────────────

BASE = "https://ado.example/Coll"


@pytest.fixture
def ado(monkeypatch):
    """Azure DevOps as a fake: every request is recorded, answered from ``state``."""
    from api import ado_actions

    sent = []
    state = {"status": "inProgress", "branches": ["refs/heads/main", "refs/heads/release"]}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, request.read().decode() or ""))
        path = request.url.path
        if path.endswith("/_apis/build/definitions/7"):
            return httpx.Response(200, json={"id": 7, "name": "Deploy", "queueStatus": "enabled",
                                             "repository": {"id": "r1", "name": "app", "type": "TfsGit",
                                                            "defaultBranch": "refs/heads/main"}})
        if path.endswith("/refs"):
            wanted = "refs/" + request.url.params.get("filter", "")
            return httpx.Response(200, json={"value": [{"name": b} for b in state["branches"] if b.startswith(wanted)]})
        if path.endswith("/_apis/build/builds") and request.method == "POST":
            return httpx.Response(200, json={"id": 99, "buildNumber": "20260929.1"})
        if "/_apis/build/builds/" in path:
            if request.method == "PATCH":
                state["status"] = "cancelling"
            return httpx.Response(200, json={"id": 5, "buildNumber": "5.1", "status": state["status"],
                                             "definition": {"name": "Deploy"}, "sourceBranch": "refs/heads/main"})
        return httpx.Response(404, json={"message": "nope"})

    monkeypatch.setattr(ado_actions, "_client", lambda pat: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ado_actions, "_get_pat_for_user", lambda user: "pat")
    monkeypatch.setattr(ado_actions, "_discover_ado_bases", lambda client: [BASE])
    monkeypatch.setattr(ado_actions, "_collection_name", lambda base: "Coll")
    monkeypatch.setattr(ado_actions.safe_mode, "is_enabled", lambda: False)
    monkeypatch.setattr(ado_actions, "_done", lambda user, kind: None)
    return ado_actions, sent, state


def test_a_run_is_checked_then_queued_on_the_branch_with_its_parameters(ado):
    ado_actions, sent, _ = ado
    body = ado_actions.PipelineRunBody(collection="Coll", project="P", definition_id=7, branch="release",
                                       parameters={"env": "test"}, dry_run=True)
    dry = ado_actions.pipeline_run(body, current_user=ME)
    assert dry["dry_run"] and any("release exists" in c for c in dry["checks"])
    assert not [s for s in sent if s[0] == "POST"]

    body.dry_run = False
    done = ado_actions.pipeline_run(body, current_user=ME)
    posted = [s for s in sent if s[0] == "POST"][0][2]
    assert '"sourceBranch": "refs/heads/release"' in posted.replace('":"', '": "')
    assert '"env"' in posted and done["result"] == "Queued as run 20260929.1."

    with pytest.raises(HTTPException) as err:
        ado_actions.pipeline_run(ado_actions.PipelineRunBody(project="P", definition_id=7, branch="nope",
                                                             collection="Coll", dry_run=True), current_user=ME)
    assert err.value.status_code == 409 and "no branch nope" in err.value.detail


def test_a_cancel_is_read_back_and_a_finished_run_is_refused(ado):
    ado_actions, sent, state = ado
    body = ado_actions.CancelBody(collection="Coll", project="P", build_id=5)
    out = ado_actions.pipeline_cancel(body, current_user=ME)
    assert ("PATCH", "/Coll/P/_apis/build/builds/5", '{"status":"cancelling"}') in \
           [(m, p, b.replace(" ", "")) for m, p, b in sent]
    assert out["result"].startswith("Cancelling")
    state["status"] = "completed"
    with pytest.raises(HTTPException) as err:
        ado_actions.pipeline_cancel(body, current_user=ME)
    assert err.value.status_code == 409


def test_a_reply_check_sends_nothing(monkeypatch):
    from api import servicenow

    monkeypatch.setattr(servicenow, "_require_own_ticket", lambda sys_id, user: {"number": "INC001"})
    monkeypatch.setattr(servicenow, "_snow_client", lambda: pytest.fail("nothing may be sent on a check"))
    out = servicenow.reply_to_ticket(sys_id="a1", message="Thanks", attachments=None, dry_run=True, current_user=ME)
    assert out["dry_run"] and out["summary"] == "Reply on INC001"


# ── the ticket form asks what its endpoint requires ──────────────────────────

def test_the_ticket_form_requires_what_the_endpoint_requires_and_no_more():
    import catalog_forms

    spec = catalog_forms.form("support_ticket")
    base = {"full_name": "x", "phone_number": "0501111111", "branch": "דיגיטל", "team": "t", "section": "s",
            "role": "r", "network": "סודי", "reason": "bug", "title": "t", "urgency": "low", "description": "d",
            "work_impact": "w", "help_text": "h"}
    azure = catalog_forms.validate_answers(spec, {**base, "devops_services": "azure devops",
                                                  "azure_devops_support_type": "pipelines"})
    assert {"azure_devops_collection", "azure_devops_project", "pipeline_url"} <= set(azure)
    # Switched to another service: what was answered for Azure DevOps asks for nothing.
    other = catalog_forms.validate_answers(spec, {**base, "devops_services": "artifactory",
                                                  "azure_devops_support_type": "pipelines"})
    assert other == {}
    assert "phone_number" in catalog_forms.validate_answers(spec, {**base, "devops_services": "artifactory",
                                                                   "phone_number": "050-111"})
