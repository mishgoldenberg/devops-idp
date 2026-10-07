"""Security checks, each made through the app the way a request reaches it:

  * an icon or avatar upload must be a real picture
  * a request that fails validation is answered in one sentence, without internals
  * the API map is not published
  * dependency pins stay at or above their fixed versions
  * the assistants treat fetched text as data, and the action routers accept only
    ids in the shape of ids
  * no outbound address can climb out of its path, and no query value can add a
    condition, whichever module builds it
  * a file attached to a ticket or a request cannot be a program, and one served
    back cannot run as the Hub
  * the signing secret cannot be the example's, and no message carries an
    exception's text or another system's raw page

Run with:  cd backend && python -m pytest tests -q
"""
import base64
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("fastapi", reason="backend deps not installed")

import common  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 40


def data_url(kind: str, payload: bytes) -> str:
    return f"data:{kind};base64," + base64.b64encode(payload).decode()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from main import create_app
    import security

    c = TestClient(create_app())
    c.cookies.set("auth_token", security.create_access_token(
        {"id": "u1", "email": "user@corp.example", "username": "user", "role": "User", "hierarchy_level": 7}))
    c.headers["Origin"] = "http://testserver"
    return c


# ── uploads and links ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    data_url("application/x-msdownload", EXE),   # the testers' own upload
    data_url("image/png", EXE),                   # an .exe calling itself a PNG
    data_url("image/svg+xml", b"<svg onload='alert(1)'/>"),
    "data:image/png;base64,not base64!",
    "javascript:alert(1)",
])
def test_a_picture_that_is_not_one_is_refused(value):
    with pytest.raises(ValueError):
        common.safe_image(value, 256 * 1024)


def test_real_pictures_and_web_addresses_are_accepted():
    assert common.safe_image(data_url("image/png", PNG), 256 * 1024)
    assert common.safe_image(data_url("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 20), 256 * 1024)
    assert common.safe_image(data_url("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "), 256 * 1024)
    assert common.safe_image("https://icons.example/logo.png", 256 * 1024)
    assert common.safe_image("", 256 * 1024) == ""


def test_an_oversized_picture_is_refused():
    with pytest.raises(ValueError, match="at most"):
        common.safe_image(data_url("image/png", PNG + b"\x00" * 300_000), 256 * 1024)


@pytest.mark.parametrize("url", ["javascript:alert(1)", " JaVaScRiPt:alert(1)", "java\tscript:alert(1)",
                                 "vbscript:msgbox", "data:text/html,<script>"])
def test_a_link_that_would_run_code_is_refused(url):
    with pytest.raises(ValueError):
        common.safe_link(url)


@pytest.mark.parametrize("url", ["https://grafana.example", "/ui/support", "file://share/docs", ""])
def test_ordinary_links_pass(url):
    assert common.safe_link(url) == url


def test_the_quick_link_endpoint_refuses_the_upload_the_testers_made(client):
    res = client.post("/api/quick-links/mine", json={
        "name": "hawhaw", "url": "https://x.example", "icon_url": data_url("application/x-msdownload", EXE)})
    assert res.status_code == 422
    assert "PNG, JPEG, GIF or WebP" in res.json()["detail"]


def test_the_avatar_endpoint_checks_the_bytes(client):
    res = client.post("/api/me/avatar", json={"avatar_url": data_url("image/png", EXE)})
    assert res.status_code == 400
    assert "not the picture it claims" in res.json()["detail"]


# ── error answers ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/activity?limit=0", None),
    ("POST", "/api/integrations/artifactory/pins", {"item_id": 1, "item_name": "aaaaa"}),
    ("POST", "/api/approvals/requests", {"request_type": "bruh"}),
])
def test_a_rejected_request_says_what_is_wrong_and_nothing_more(client, method, path, body):
    res = client.request(method, path, json=body)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert isinstance(detail, str) and detail.startswith("The request was not valid")
    for internal in ("greater_than_equal", "string_type", "ctx", "'input'", "loc", "bruh", "aaaaa"):
        assert internal not in detail


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_map_is_not_published(client, path):
    assert client.get(path).status_code == 404


# ── dependencies ──────────────────────────────────────────────────────────────────────

def test_requirements_pins_are_at_or_above_the_fixed_versions():
    text = open(os.path.join(os.path.dirname(__file__), "..", "app", "requirements.txt"), encoding="utf-8").read()
    pins = dict(re.findall(r"^([A-Za-z0-9_.\[\]-]+)==([\d.]+)", text, re.M))

    def version(name):
        return tuple(int(p) for p in pins[name].split("."))

    assert version("starlette") >= (1, 3, 1)
    assert version("PyJWT") >= (2, 15, 0)
    assert version("cryptography") >= (50, 0, 0)
    for unused in ("google-cloud-storage", "kubernetes", "aiofiles"):
        assert unused not in pins
    unbounded = [line for line in text.splitlines()
                 if line.strip() and not line.startswith("#") and not re.search(r"[=>]=", line)]
    assert not unbounded, f"every dependency needs a version: {unbounded}"


# ── the assistants and the action routers ────────────────────────────────────

def test_both_assistants_are_told_fetched_text_is_data():
    from datetime import date
    from devbot import prompts

    dev = prompts.system_prompt("Dana", {"azure": True}, ["ado_work_items", "propose_rerun"], date.today())
    admin = prompts.admin_prompt("Dana", ["hub_find_users"], date.today())
    for prompt in (dev, admin):
        assert prompts.UNTRUSTED in prompt
    assert "asked for in their own words" in admin


@pytest.mark.parametrize("body", [
    {"page_id": "../../space/ADMIN", "content": "x"},
    {"page_id": "123/../456", "content": "x"},
])
def test_a_confluence_page_id_must_be_a_number(client, body):
    assert client.post("/api/confluence/actions/page-append", json={**body, "dry_run": True}).status_code == 422


def test_a_confluence_space_key_cannot_carry_a_path(client):
    res = client.post("/api/confluence/actions/page-create",
                      json={"space": "DEV/../../user", "title": "t", "content": "c", "dry_run": True})
    assert res.status_code == 422


def test_a_proposal_outcome_cannot_store_a_script_link(monkeypatch):
    from api import devbot

    seen = {}
    monkeypatch.setattr(devbot.store, "update_action",
                        lambda uid, cid, mid, aid, patch, *a: seen.update(patch) or {"id": aid, **patch})
    body = devbot.OutcomeBody(message_id=1, status="done", url="javascript:alert(document.cookie)")

    class Req:
        class state:
            pass

    devbot.action_outcome("c1", "a1", body, Req(), current_user={"id": "u1", "email": "u@x"})
    assert seen["url"] == ""


def test_adminbot_looks_requests_up_by_id_only(monkeypatch):
    import security
    from devbot.tools import hub
    from devbot.tools.base import ToolFailure

    monkeypatch.setattr(security, "has_effective_admin_access_live", lambda user: True)
    monkeypatch.setattr(hub, "query_one", lambda *a, **k: pytest.fail("no query for a malformed id"))

    class Ctx:
        admin = True
        user = {"id": "u1"}

    for rid in ("%", "1' OR '1'='1", "abc%def"):
        with pytest.raises(ToolFailure):
            hub.request(Ctx(), {"id": rid})


def test_failure_text_never_names_the_exception():
    import httpx

    words = common.failure_text("Confluence", httpx.ConnectError("boom", request=httpx.Request("GET", "https://c.example/x")))
    assert "ConnectError" not in words and "boom" not in words and "c.example" in words
    assert "KeyError" not in common.failure_text("Confluence", KeyError("secret"))


# ── outbound addresses: no value can walk a path to another endpoint ─────────────────────────

def test_httpx_alone_would_have_sent_a_climbing_path_somewhere_else():
    import httpx

    request = httpx.Request("GET", "https://snow.example/api/now/table/incident/../../sys_user")
    assert request.url.path == "/api/now/sys_user"   # collapsed without a word


@pytest.mark.parametrize("path", [
    "/api/now/table/incident/../../sys_user",
    "/api/now/table/incident/%2e%2e/%2e%2e/sys_user",
    "/api/now/table/incident/..%2F..%2Fsys_user",
    "/api/now/table/incident/%252e%252e/sys_user",
])
def test_the_guarded_client_refuses_a_climbing_path_before_sending(path):
    import httpx
    import resilient_http

    sent = []
    client = resilient_http.Client(base_url="https://snow.example",
                                   transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    with pytest.raises(resilient_http.UnsafeURL):
        client.get(path)
    assert not sent


def test_the_guarded_client_sends_ordinary_addresses():
    import httpx
    import resilient_http

    sent = []
    client = resilient_http.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    client.get("https://gitlab.example/api/v4/projects/group%2Fproject")
    client.get("https://ado.example/tfs/Main/_apis/git/repositories/My%20Repo/items", params={"path": "/a/../b"})
    assert len(sent) == 2


def _rules():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "check_security_rules.py")
    spec = importlib.util.spec_from_file_location("check_security_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_rule_holds_across_the_whole_backend():
    assert _rules().main() == 0


def test_the_rules_catch_each_kind_of_regression():
    import ast
    from pathlib import Path

    rules = _rules()
    where = Path(rules.APP) / "example.py"
    code = ast.parse(
        "import httpx\n"
        "def f(resp):\n"
        "    httpx.Client()\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        detail = f'failed: {exc}'\n"
        "        log.warning(f'failed: {exc}')\n"
        "    message = f'{resp.text[:200]}'\n"
        "    params = {'sysparm_query': f'name={value}'}\n"
        "    password = 'Devops4ever'\n")
    assert len(rules.direct_clients(where, code)) == 1
    assert len(rules.leaked_errors(where, code, set())) == 2          # the log line is allowed
    assert len(rules.unescaped_snow_queries(where, code)) == 1
    assert len(rules.secret_literals(where, code)) == 1


def test_the_rules_catch_a_password_written_into_a_script(tmp_path, monkeypatch):
    rules = _rules()
    (tmp_path / "setup.sh").write_text('export DB_PASS="Devops4ever"\nexport DB_USER="${DB_USER}"\n')
    monkeypatch.setattr(rules, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rules, "CONFIG_FILES", ["setup.sh"])
    assert rules.config_secrets() == ["setup.sh:1: DB_PASS is set to a literal value"]


# ── query languages: a value cannot add a condition of its own ───────────────────────────────

def test_a_servicenow_query_value_cannot_add_conditions():
    import snow_catalog

    assert snow_catalog.query_value("Dana Levi") == "Dana Levi"
    for value in ("x^ORactive=true", "x\nname=y"):
        with pytest.raises(ValueError):
            snow_catalog.query_value(value)
    assert snow_catalog.account_forms("me^ORactive=true", "CORP\\me") == ["CORP\\me", "me"]


def test_cql_and_aql_literals_cannot_be_closed_early():
    assert common.quoted_text('x\\" OR space = "ADMIN') == '"x\\\\\\" OR space = \\"ADMIN"'
    assert common.wiql_text("x' OR [System.Id] > 0") == "'x'' OR [System.Id] > 0'"


# ── files people attach to tickets and requests ──────────────────────────────────────────────

@pytest.mark.parametrize("name, data", [
    ("report.png", EXE),                                      # a program calling itself a picture
    ("notes.txt", b"\x7fELF\x02\x01"),
    ("diagram.svg", b"<svg onload='alert(1)'/>"),
    ("readme.txt", b"  <!DOCTYPE html><script>alert(1)</script>"),
    ("run.sh", b"echo hi"),
    ("invoice.pdf.exe", b"%PDF-1.7"),
])
def test_an_attachment_that_would_run_is_refused(name, data):
    with pytest.raises(ValueError):
        common.safe_attachment(name, data, 1024 * 1024)


def test_an_attachment_keeps_only_its_name_and_takes_its_type_from_its_bytes():
    assert common.safe_attachment("C:\\Users\\me\\shot.png", PNG, 1024) == ("shot.png", "image/png")
    assert common.safe_attachment("../../etc/log.txt", b"line one\n", 1024) == ("log.txt", "text/plain")
    assert common.safe_attachment("spec.pdf", b"%PDF-1.7 ...", 1024)[1] == "application/pdf"
    with pytest.raises(ValueError):
        common.safe_attachment("big.txt", b"x" * 2048, 1024)


def test_a_ticket_reply_with_a_program_attached_is_refused_before_anything_is_sent(client, monkeypatch):
    from api import servicenow

    monkeypatch.setattr(servicenow, "_require_own_ticket", lambda sys_id, user: {"number": "INC0001"})
    monkeypatch.setattr(servicenow, "_snow_client", lambda: pytest.fail("ServiceNow was called"))
    resp = client.post("/api/support/tickets/reply", data={"sys_id": "a" * 32, "message": "see attached"},
                       files={"attachments": ("screenshot.png", EXE, "image/png")})
    assert resp.status_code == 400 and "program" in resp.json()["detail"]


def test_a_catalog_attachment_is_checked_by_its_bytes():
    from fastapi import HTTPException
    from api import catalog

    with pytest.raises(HTTPException) as refused:
        catalog._decode(catalog.Attachment(field="diagram", filename="diagram.png",
                                           data_base64=base64.b64encode(EXE).decode()))
    assert refused.value.status_code == 400
    name, kind, blob = catalog._decode(catalog.Attachment(field="diagram", filename="d.png",
                                                          data_base64=data_url("image/png", PNG)))
    assert (name, kind, blob) == ("d.png", "image/png", PNG)


@pytest.mark.parametrize("payload, disposition, media_type", [
    (b"<html><script>alert(document.cookie)</script></html>", "attachment", "text/plain"),
    (PNG, "inline", "image/png"),
])
def test_a_ticket_attachment_is_served_so_it_cannot_run_as_the_hub(client, monkeypatch, payload, disposition,
                                                                  media_type):
    import httpx
    import resilient_http
    from api import servicenow

    ticket, attachment = "a" * 32, "b" * 32

    def handler(request):
        if request.url.path.endswith("/file"):
            return httpx.Response(200, content=payload, headers={"Content-Type": "text/html"})
        return httpx.Response(200, json={"result": {"table_sys_id": ticket, "file_name": "x.html"}})

    monkeypatch.setattr(servicenow, "_require_own_ticket", lambda sys_id, user: {"number": "INC0001"})
    monkeypatch.setattr(servicenow, "_snow_client", lambda: resilient_http.Client(
        base_url="https://snow.example", transport=httpx.MockTransport(handler)))
    resp = client.get(f"/api/support/tickets/{ticket}/attachments/{attachment}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith(disposition)
    assert resp.headers["content-type"].startswith(media_type)
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]


# ── secrets and error wording ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "replace_with_a_random_32_character_string", "change_me_in_production"])
def test_the_app_refuses_a_signing_secret_anyone_could_know(monkeypatch, value):
    import config

    monkeypatch.setenv("JWT_SECRET", value)
    with pytest.raises(RuntimeError):
        config.Settings()


def test_far_message_reads_the_other_systems_message_never_its_page():
    import httpx
    from resilient_http import far_message

    assert far_message(httpx.Response(400, json={"error": {"message": "Invalid table"}})) == "Invalid table"
    assert far_message(httpx.Response(400, json={"message": "TF401019: no such repository"})).startswith("TF401019")
    assert far_message(httpx.Response(500, text="<html><body>Stack trace ...</body></html>")) == ""
