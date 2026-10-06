"""GitLab, end to end against a fake GitLab (API v4) on a local port: the token check,
merge requests and their approvals, pipelines mine and all, every action's check /
confirm / read-back, Safe Mode, the refusals, Needs You, search -- and that a GitLab
401 never reaches the browser as a 401 (which would sign the person out of the Hub).

Run with:  cd backend && python -m pytest tests/test_gitlab.py -q
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")
pytest.importorskip("fastapi", reason="backend deps not installed")

GOOD = "glpat-good"
ME = {"id": 7, "username": "dana", "name": "Dana Levi", "email": "dana@corp.test"}


def _fresh_state():
    return {
        "groups": [{"id": 1, "full_path": "platform", "path": "platform", "name": "Platform", "parent_id": None},
                   {"id": 2, "full_path": "platform/tools", "path": "tools", "name": "Tools", "parent_id": 1}],
        "projects": {
            11: {"id": 11, "path_with_namespace": "platform/api", "name": "api", "web_url": "http://gl/platform/api",
                 "jobs_enabled": True, "branches": ["main", "feature/x"], "tags": ["v1"]},
            12: {"id": 12, "path_with_namespace": "platform/tools/cli", "name": "cli", "web_url": "http://gl/platform/tools/cli",
                 "jobs_enabled": True, "branches": ["main"], "tags": []},
        },
        "mrs": {
            (11, 5): {"iid": 5, "project_id": 11, "title": "Add retries", "state": "opened", "author": {"id": 7, "name": "Dana Levi", "username": "dana"},
                      "reviewers": [], "source_branch": "feature/x", "target_branch": "main", "created_at": "2026-10-01T08:00:00Z",
                      "web_url": "http://gl/platform/api/-/merge_requests/5", "references": {"full": "platform/api!5"}, "draft": False},
            (12, 9): {"iid": 9, "project_id": 12, "title": "Fix the CLI flag", "state": "opened", "author": {"id": 3, "name": "Ron", "username": "ron"},
                      "reviewers": [7], "source_branch": "fix/flag", "target_branch": "main", "created_at": "2026-10-02T08:00:00Z",
                      "web_url": "http://gl/platform/tools/cli/-/merge_requests/9", "references": {"full": "platform/tools/cli!9"}, "draft": True},
            (11, 6): {"iid": 6, "project_id": 11, "title": "Already approved", "state": "opened", "author": {"id": 3, "name": "Ron", "username": "ron"},
                      "reviewers": [7], "source_branch": "a", "target_branch": "main", "created_at": "2026-10-03T08:00:00Z",
                      "web_url": "http://gl/platform/api/-/merge_requests/6", "references": {"full": "platform/api!6"}},
        },
        "approvals": {(11, 5): [], (12, 9): [], (11, 6): [7]},
        "required": {(12, 9): 2},
        "notes": {},
        "pipelines": {
            11: [{"id": 101, "iid": 3, "status": "failed", "ref": "main", "source": "push", "user": "dana",
                  "created_at": "2026-10-03T09:00:00Z", "updated_at": "2026-10-03T09:10:00Z", "web_url": "http://gl/platform/api/-/pipelines/101",
                  "variables": {"DEPLOY": "no"}},
                 {"id": 100, "iid": 2, "status": "success", "ref": "main", "source": "schedule", "user": "ron",
                  "created_at": "2026-10-02T09:00:00Z", "updated_at": "2026-10-02T09:10:00Z", "web_url": "http://gl/platform/api/-/pipelines/100",
                  "variables": {}}],
            12: [{"id": 200, "iid": 1, "status": "running", "ref": "main", "source": "web", "user": "dana",
                  "created_at": "2026-10-03T10:00:00Z", "updated_at": "2026-10-03T10:01:00Z", "web_url": "http://gl/platform/tools/cli/-/pipelines/200",
                  "variables": {}}],
        },
        "events": [{"action_name": "opened", "target_type": "MergeRequest", "created_at": "2026-10-03T08:00:00Z"},
                   {"action_name": "commented on", "target_type": "DiffNote", "note": {"noteable_type": "MergeRequest"},
                    "created_at": "2026-10-02T08:00:00Z"}],
        "calls": [],
        "reviewer_filter": True,
    }


STATE = _fresh_state()


class FakeGitLab(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=None, headers=None):
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        if self.headers.get("PRIVATE-TOKEN") != GOOD:
            self._send(401, {"message": "401 Unauthorized"})
            return False
        return True

    def _route(self, method):
        if not self._authed():
            return
        parts = urlsplit(self.path)
        # GitLab takes a project's URL-encoded path wherever it takes its id.
        raw = re.sub(r"^/api/v4/projects/([^/]+)",
                     lambda m: "/api/v4/projects/" + str(next((p["id"] for p in STATE["projects"].values()
                                                               if p["path_with_namespace"] == unquote(m.group(1))),
                                                              unquote(m.group(1)))), parts.path)
        path = unquote(raw)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        STATE["calls"].append((method, path, q))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        assert path.startswith("/api/v4/"), path
        path = path[len("/api/v4"):]
        S = STATE

        if path == "/user":
            return self._send(200, ME)
        if path == "/groups":
            return self._send(200, S["groups"])
        m = re.fullmatch(r"/groups/(.+)/projects", path)
        if m:
            return self._send(200, [p for p in S["projects"].values() if p["path_with_namespace"].startswith(m.group(1) + "/")])
        if path == "/projects":
            rows = list(S["projects"].values())
            if q.get("search"):
                rows = [p for p in rows if q["search"].lower() in p["name"].lower()]
            return self._send(200, rows)
        m = re.fullmatch(r"/projects/(\d+)", path)
        if m:
            p = S["projects"].get(int(m.group(1)))
            return self._send(200, p) if p else self._send(404, {"message": "404 Project Not Found"})
        if path == "/merge_requests":
            rows = [mr for mr in S["mrs"].values() if mr["state"] == q.get("state", "opened")]
            if q.get("scope") == "created_by_me":
                rows = [mr for mr in rows if mr["author"]["id"] == ME["id"]]
            elif q.get("reviewer_id"):
                if not S["reviewer_filter"]:
                    return self._send(400, {"error": "reviewer_id does not have a valid value"})
                rows = [mr for mr in rows if int(q["reviewer_id"]) in mr["reviewers"]]
            return self._send(200, rows)
        m = re.fullmatch(r"/projects/(\d+)/merge_requests/(\d+)(/.*)?", path)
        if m:
            key = (int(m.group(1)), int(m.group(2)))
            mr = S["mrs"].get(key)
            if not mr:
                return self._send(404, {"message": "404 Not found"})
            rest = m.group(3) or ""
            if rest == "" and method == "GET":
                return self._send(200, mr)
            if rest == "/approvals":
                ids = S["approvals"][key]
                req = S["required"].get(key)
                out = {"approved_by": [{"user": {"id": i, "name": "Dana Levi" if i == 7 else f"user{i}"}} for i in ids],
                       "user_has_approved": ME["id"] in ids, "user_can_approve": mr["author"]["id"] != ME["id"]}
                if req:
                    out.update(approvals_required=req, approvals_left=max(0, req - len(ids)))
                return self._send(200, out)
            if rest == "/approve" and method == "POST":
                if ME["id"] in S["approvals"][key]:
                    return self._send(401, {"message": "401 Unauthorized"})
                S["approvals"][key].append(ME["id"])
                return self._send(201, {})
            if rest == "/unapprove" and method == "POST":
                if ME["id"] not in S["approvals"][key]:
                    return self._send(404, {"message": "404 Not Found"})
                S["approvals"][key].remove(ME["id"])
                return self._send(201, {})
            if rest == "/notes" and method == "POST":
                nid = len(S["notes"]) + 1
                S["notes"][nid] = {"id": nid, "body": body.get("body"), "mr": key}
                return self._send(201, S["notes"][nid])
            m2 = re.fullmatch(r"/notes/(\d+)", rest)
            if m2:
                n = S["notes"].get(int(m2.group(1)))
                return self._send(200, n) if n else self._send(404, {})
        m = re.fullmatch(r"/projects/(\d+)/pipelines", path)
        if m:
            rows = S["pipelines"].get(int(m.group(1)), [])
            if q.get("username"):
                rows = [p for p in rows if p["user"] == q["username"]]
            if q.get("status"):
                rows = [p for p in rows if p["status"] == q["status"]]
            return self._send(200, [{k: v for k, v in p.items() if k not in ("user", "variables")} for p in rows])
        m = re.fullmatch(r"/projects/(\d+)/pipelines/(\d+)(/.*)?", path)
        if m:
            rows = S["pipelines"].get(int(m.group(1)), [])
            p = next((x for x in rows if x["id"] == int(m.group(2))), None)
            if not p:
                return self._send(404, {"message": "404 Not found"})
            rest = m.group(3) or ""
            if rest == "/retry" and method == "POST":
                p["status"] = "running"
                return self._send(201, p)
            if rest == "/variables":
                return self._send(200, [{"key": k, "value": v} for k, v in p["variables"].items()])
            return self._send(200, {k: v for k, v in p.items() if k not in ("user", "variables")})
        m = re.fullmatch(r"/projects/(\d+)/pipeline", path)
        if m and method == "POST":
            pid = int(m.group(1))
            new = {"id": 900 + len(S["pipelines"][pid]), "iid": 50, "status": "created", "ref": body["ref"], "source": "api",
                   "user": "dana", "created_at": "2026-10-04T09:00:00Z", "updated_at": "2026-10-04T09:00:00Z",
                   "web_url": f"http://gl/p/{pid}/-/pipelines/new",
                   "variables": {v["key"]: v["value"] for v in body.get("variables") or []}}
            S["pipelines"][pid].insert(0, new)
            return self._send(201, {k: v for k, v in new.items() if k not in ("user", "variables")})
        m = re.fullmatch(r"/projects/(\d+)/repository/(branches|tags)/(.+)", path)
        if m:
            p = S["projects"][int(m.group(1))]
            names = p["branches"] if m.group(2) == "branches" else p["tags"]
            return self._send(200, {"name": m.group(3)}) if m.group(3) in names else self._send(404, {"message": "404 Branch Not Found"})
        if path == "/events":
            return self._send(200, S["events"])
        return self._send(404, {"message": f"no route {path}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitLab)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def env(server, monkeypatch):
    STATE.clear()
    STATE.update(_fresh_state())
    monkeypatch.setenv("GITLAB_BASE_URL", server + "/api/v4/")
    import gitlab_client
    import integrations_cache
    import safe_mode

    tokens = {"u1": GOOD}
    monkeypatch.setattr(gitlab_client, "user_token", lambda user: tokens.get(str(user.get("id")), ""))
    # No Redis here: every read is fresh.
    monkeypatch.setattr(integrations_cache, "get_cached", lambda key, ttl, producer: producer())
    import api.gitlab as gl_api
    monkeypatch.setattr(gl_api, "cached_external", lambda system, owner, suffix, producer, ttl=0: producer())
    monkeypatch.setattr(safe_mode, "is_enabled", lambda: False)
    import streaks
    recorded = []
    monkeypatch.setattr(streaks, "record", lambda email, kind, **k: recorded.append((email, kind)))
    import api.gitlab_actions as acts
    monkeypatch.setattr(acts, "invalidate_owner", lambda *a: None)
    return {"tokens": tokens, "recorded": recorded}


USER = {"id": "u1", "email": "dana@corp.test", "username": "dana", "role": "User", "hierarchy_level": 4}


@pytest.fixture()
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import security
    from api import gitlab, gitlab_actions

    app = FastAPI()
    app.include_router(gitlab.router, prefix="/api/gitlab")
    app.include_router(gitlab_actions.router, prefix="/api/gitlab/actions")
    who = {"u": USER}
    app.dependency_overrides[security.get_current_user] = lambda: who["u"]
    cl = TestClient(app)
    cl.who = who
    return cl


def test_base_url_forms(monkeypatch):
    import gitlab_client
    for raw, want in [("gitlab.corp", "https://gitlab.corp"), ("https://gitlab.corp/api/v4/", "https://gitlab.corp"),
                      ("$(GITLAB_BASE_URL)", ""), ("", ""), ("http://g:8080/", "http://g:8080")]:
        monkeypatch.setenv("GITLAB_BASE_URL", raw)
        assert gitlab_client.base_url() == want


def test_token_check(env):
    import gitlab_client
    from fastapi import HTTPException
    assert gitlab_client.test_token(GOOD)["username"] == "dana"
    with pytest.raises(HTTPException) as e:
        gitlab_client.test_token("glpat-bad")
    assert e.value.status_code == 401 and "read_api" in e.value.detail


def test_merge_requests_mine_and_review(client):
    rows = client.get("/api/gitlab/merge-requests").json()["data"]
    by = {(r["project_id"], r["iid"]): r for r in rows}
    assert set(by) == {(11, 5), (12, 9), (11, 6)}
    assert by[(11, 5)]["is_creator"] and not by[(11, 5)]["is_reviewer"]
    assert by[(12, 9)]["project"] == "platform" and by[(12, 9)]["repository"] == "cli" and by[(12, 9)]["draft"]
    assert by[(12, 9)]["approvals"]["required"] == 2 and by[(12, 9)]["approvals"]["left"] == 2
    assert by[(11, 6)]["approved_by_me"] is True
    review = client.get("/api/gitlab/merge-requests", params={"role": "review"}).json()["data"]
    assert [(r["project_id"], r["iid"]) for r in review] == [(12, 9)], "approved by me is not waiting on me"
    created = client.get("/api/gitlab/merge-requests", params={"role": "created"}).json()["data"]
    assert [r["iid"] for r in created] == [5]
    assert client.get("/api/gitlab/merge-requests", params={"project": "platform/api"}).json()["data"][0]["project_path"] == "platform/api"
    assert client.get("/api/gitlab/merge-requests", params={"group": "nope"}).json()["data"] == []


def test_older_gitlab_without_reviewers(client):
    STATE["reviewer_filter"] = False
    out = client.get("/api/gitlab/merge-requests").json()
    assert "assigned to you" in out["note"]


def test_groups_and_projects(client):
    assert [g["name"] for g in client.get("/api/gitlab/groups").json()["data"]] == ["platform"], "top level only"
    names = [p["name"] for p in client.get("/api/gitlab/projects", params={"group": "platform/tools"}).json()["data"]]
    assert names == ["platform/tools/cli"]


def test_pipelines_mine_and_all(client):
    mine = client.get("/api/gitlab/pipelines").json()
    assert mine["counts"] == {"mine": 2, "all": 3}
    assert {r["id"] for r in mine["data"]} == {101, 200}
    allr = client.get("/api/gitlab/pipelines", params={"scope": "all"}).json()
    status = {r["id"]: r["status"] for r in allr["data"]}
    assert status == {101: "failed", 100: "succeeded", 200: "running"}
    one = client.get("/api/gitlab/pipelines", params={"project": "platform/tools/cli", "scope": "all"}).json()
    assert [r["id"] for r in one["data"]] == [200]


def test_a_gitlab_401_is_never_a_hub_401(client, env):
    env["tokens"]["u1"] = "glpat-expired"
    r = client.get("/api/gitlab/merge-requests")
    assert r.status_code == 424 and "Connections" in r.json()["detail"]
    r = client.post("/api/gitlab/actions/mr-comment", json={"project_id": 11, "iid": 5, "text": "hi"})
    assert r.status_code == 424


def test_not_connected(client, env):
    env["tokens"].clear()
    r = client.get("/api/gitlab/merge-requests")
    assert r.status_code == 428 and "not connected" in r.json()["detail"]


def test_approve_checks_then_approves_and_reads_back(client, env):
    body = {"project_id": 12, "iid": 9, "approve": True, "comment": "LGTM", "dry_run": True}
    dry = client.post("/api/gitlab/actions/mr-approve", json=body).json()
    assert dry["dry_run"] and "Approve merge request !9" in dry["summary"]
    assert any("0 of 2 required" in c for c in dry["checks"]) and any("Draft" in c for c in dry["checks"])
    assert STATE["approvals"][(12, 9)] == [], "a check changes nothing"
    done = client.post("/api/gitlab/actions/mr-approve", json={**body, "dry_run": False}).json()
    assert STATE["approvals"][(12, 9)] == [7] and "1 of 2 required" in done["result"]
    assert any(n["body"] == "LGTM" for n in STATE["notes"].values())
    assert env["recorded"] == [("dana@corp.test", "pr")]
    again = client.post("/api/gitlab/actions/mr-approve", json=body)
    assert again.status_code == 409 and "already approved" in again.json()["detail"]


def test_unapprove_and_own_merge_request(client):
    r = client.post("/api/gitlab/actions/mr-approve", json={"project_id": 11, "iid": 6, "approve": False})
    assert r.status_code == 200 and STATE["approvals"][(11, 6)] == []
    r = client.post("/api/gitlab/actions/mr-approve", json={"project_id": 11, "iid": 5, "approve": True, "dry_run": True})
    assert r.status_code == 409 and "may not approve" in r.json()["detail"]


def test_closed_merge_request_is_refused(client):
    STATE["mrs"][(11, 5)]["state"] = "merged"
    r = client.post("/api/gitlab/actions/mr-comment", json={"project_id": 11, "iid": 5, "text": "x", "dry_run": True})
    assert r.status_code == 409 and "merged" in r.json()["detail"]


def test_comment(client):
    r = client.post("/api/gitlab/actions/mr-comment", json={"project_id": 11, "iid": 5, "text": "Nice"}).json()
    assert r["result"].startswith("Your comment") and list(STATE["notes"].values())[0]["body"] == "Nice"


def test_safe_mode_sends_nothing(client, monkeypatch):
    import safe_mode
    monkeypatch.setattr(safe_mode, "is_enabled", lambda: True)
    r = client.post("/api/gitlab/actions/mr-approve", json={"project_id": 12, "iid": 9}).json()
    assert r["simulated"] and STATE["approvals"][(12, 9)] == []


def test_retry_only_what_failed(client):
    r = client.post("/api/gitlab/actions/pipeline-retry", json={"project_id": 11, "pipeline_id": 100, "dry_run": True})
    assert r.status_code == 409 and "Run again" in r.json()["detail"]
    r = client.post("/api/gitlab/actions/pipeline-retry", json={"project_id": 11, "pipeline_id": 101}).json()
    assert "running" in r["result"]


def test_run_again_with_variables(client):
    assert client.get("/api/gitlab/actions/pipeline-variables", params={"project_id": 11, "pipeline_id": 101}).json()["data"] == {"DEPLOY": "no"}
    body = {"project_id": 11, "ref": "main", "variables": {"DEPLOY": "yes"}, "from_pipeline_id": 101}
    dry = client.post("/api/gitlab/actions/pipeline-run", json={**body, "dry_run": True}).json()
    assert "branch main" in dry["summary"] and "DEPLOY=yes" in dry["summary"]
    done = client.post("/api/gitlab/actions/pipeline-run", json=body).json()
    assert "Created pipeline" in done["result"] and STATE["pipelines"][11][0]["variables"] == {"DEPLOY": "yes"}
    r = client.post("/api/gitlab/actions/pipeline-run", json={**body, "ref": "nope", "dry_run": True})
    assert r.status_code == 409 and "no branch or tag nope" in r.json()["detail"]


def test_needs_you_lists_what_waits_for_my_approval(env):
    from api import inbox
    items = inbox._merge_requests_awaiting_me(USER)
    assert [i["key"] for i in items] == ["mr:12:9"]
    assert items[0]["review"]["kind"] == "gl-approve" and items[0]["system"] == "GitLab"
    env["tokens"].clear()
    assert inbox._merge_requests_awaiting_me(USER) == [], "not connected is not unavailable"


def test_search(env):
    from api import search
    assert [h["title"] for h in search._gitlab_merge_requests("flag", USER)] == ["Fix the CLI flag"]
    assert [h["subtitle"] for h in search._gitlab_projects("cli", USER)] == ["platform/tools/cli"]
