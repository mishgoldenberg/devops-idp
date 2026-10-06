"""Security checks, each made through the app the way a request reaches it:

  * an icon or avatar upload must be a real picture
  * a request that fails validation is answered in one sentence, without internals
  * the API map is not published
  * dependency pins stay at or above their fixed versions
  * the assistants treat fetched text as data, and the action routers accept only
    ids in the shape of ids

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
