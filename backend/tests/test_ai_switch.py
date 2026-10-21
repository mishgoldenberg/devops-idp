"""HUB_AI_ENABLED=false takes DevBot and AdminBot out of a deployment while their code
stays in the image: no route, no page, no link, no index thread.

Each case starts a fresh interpreter, because the routers are mounted at import.

Run with:  cd backend && python -m pytest tests -q
"""
import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("fastapi", reason="backend deps not installed")

APP = os.path.join(os.path.dirname(__file__), "..", "app")

PROBE = r"""
import json, re, sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from main import create_app
import security

client = TestClient(create_app(), raise_server_exceptions=False)
client.cookies.set("auth_token", security.create_access_token(
    {"id": "00000000-0000-0000-0000-000000000001", "email": "admin@corp.example", "username": "admin",
     "role": "Platform Admin", "hierarchy_level": 1}))
status = {path: client.get(path, follow_redirects=False).status_code for path in (
    "/api/devbot/status", "/api/devbot/conversations", "/api/devbot/index/state", "/api/adminbot/status",
    "/ui/devbot", "/ui/adminbot", "/ui/devbot-monitor", "/ui/", "/ui/connections")}
home = client.get("/ui/").text
print(json.dumps({"status": status, "links": [h for h in ("/ui/devbot", "/ui/adminbot") if re.search(r'<a\s[^>]*href="' + h + '"', home)]}))
"""


def probe(enabled: str) -> dict:
    env = {**os.environ, "HUB_AI_ENABLED": enabled, "DEVBOT_LLM_BASE_URL": "http://127.0.0.1:9/v1"}  # refuses at once
    env.setdefault("DATABASE_URL", "postgresql://hub:hub@127.0.0.1:1/hub")
    env.setdefault("JWT_SECRET", "test-only-signing-key-0123456789abcdef")
    out = subprocess.run([sys.executable, "-c", PROBE, APP], capture_output=True, text=True, env=env, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_switched_off_the_assistants_are_gone_and_the_hub_still_works():
    seen = probe("false")
    status = seen["status"]
    for path in ("/api/devbot/status", "/api/devbot/conversations", "/api/devbot/index/state",
                 "/api/adminbot/status", "/ui/devbot", "/ui/adminbot", "/ui/devbot-monitor"):
        assert status[path] == 404, (path, status[path])
    assert status["/ui/"] < 400 and status["/ui/connections"] < 400
    assert seen["links"] == []


def test_switched_on_they_are_there():
    seen = probe("true")
    assert seen["status"]["/ui/devbot"] < 400 and seen["status"]["/api/devbot/status"] != 404
    assert "/ui/devbot" in seen["links"]
