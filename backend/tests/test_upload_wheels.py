"""scripts/upload_wheels.py against a fake Artifactory PyPI repository.

Run with:  cd backend && python -m pytest tests -q
"""
import base64
import hashlib
import importlib.util
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "upload_wheels.py")
spec = importlib.util.spec_from_file_location("upload_wheels", SCRIPT)
upload_wheels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload_wheels)

BASIC = "Basic " + base64.b64encode(b"admin:tok").decode()


class FakeArtifactory(BaseHTTPRequestHandler):
    files = {}       # repo path -> bytes
    refuse = False
    seen_auth = []

    def log_message(self, *args):
        pass

    def _authorised(self):
        auth = self.headers.get("Authorization", "")
        FakeArtifactory.seen_auth.append(auth.split(" ")[0])
        return auth == BASIC  # this server takes the token as a Basic password only

    def do_GET(self):
        if not self._authorised():
            return self._send(401, b"")
        project = self.path.rstrip("/").split("/")[-1]
        names = [p.split("/")[-1] for p in self.files if p.split("/")[0] == project]
        if not names:
            return self._send(404, b"")
        self._send(200, "".join(f'<a href="#">{n}</a>' for n in names).encode())

    def do_PUT(self):
        if not self._authorised():
            return self._send(401, b"")
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.refuse:
            return self._send(403, b'{"errors":[{"status":403,"message":"Not enough permissions to deploy"}]}')
        assert self.headers["X-Checksum-Sha256"] == hashlib.sha256(body).hexdigest()
        self.files[self.path.split("/artifactory/repo/")[1]] = body
        self._send(201, b"{}")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch, tmp_path):
    FakeArtifactory.files = {"fastapi/0.142.2/fastapi-0.142.2-py3-none-any.whl": b"old"}
    FakeArtifactory.refuse = False
    FakeArtifactory.seen_auth = []
    srv = HTTPServer(("127.0.0.1", 0), FakeArtifactory)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for name in ("fastapi-0.142.2-py3-none-any.whl", "starlette-1.7.0-py3-none-any.whl"):
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in tmp_path.glob("*.whl")))
    monkeypatch.setattr(upload_wheels, "WHEELS", tmp_path)
    monkeypatch.setattr(upload_wheels.time, "sleep", lambda s: None)
    monkeypatch.setenv("PIP_INDEX_URL", f"http://127.0.0.1:{srv.server_port}/artifactory/api/pypi/repo/simple")
    monkeypatch.setenv("ARTIFACTORY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ARTIFACTORY_ADMIN_TOKEN", "tok")
    monkeypatch.delenv("PIP_TRUSTED_HOST", raising=False)
    yield tmp_path
    srv.shutdown()


def test_uploads_only_what_the_index_lacks(server, capsys):
    assert upload_wheels.main() == 0
    out = capsys.readouterr().out
    assert "present   fastapi-0.142.2" in out
    assert "uploaded  starlette-1.7.0" in out
    assert FakeArtifactory.files["starlette/1.7.0/starlette-1.7.0-py3-none-any.whl"] == b"starlette-1.7.0-py3-none-any.whl"
    assert FakeArtifactory.files["fastapi/0.142.2/fastapi-0.142.2-py3-none-any.whl"] == b"old"   # never overwritten
    assert FakeArtifactory.seen_auth[:2] == ["Bearer", "Basic"]


def test_a_refusal_is_a_warning_not_a_failed_build(server, capsys):
    FakeArtifactory.refuse = True
    assert upload_wheels.main() == 0
    out = capsys.readouterr().out
    assert "##vso[task.logissue type=warning]starlette-1.7.0-py3-none-any.whl: Artifactory refused the upload (HTTP 403)" in out
    assert "Not enough permissions" in out


def test_a_wheel_that_does_not_match_its_checksum_is_not_uploaded(server, capsys):
    (server / "starlette-1.7.0-py3-none-any.whl").write_bytes(b"tampered")
    upload_wheels.main()
    assert "does not match SHA256SUMS" in capsys.readouterr().out
    assert not any(k.startswith("starlette/") for k in FakeArtifactory.files)


def test_no_token_means_no_upload(server, monkeypatch, capsys):
    monkeypatch.setenv("ARTIFACTORY_ADMIN_TOKEN", "$(ARTIFACTORY_ADMIN_TOKEN)")
    assert upload_wheels.main() == 0
    assert "were not uploaded" in capsys.readouterr().out


def test_the_bundled_wheels_match_their_checksums():
    folder = upload_wheels.WHEELS
    sums = upload_wheels.expected_sums(folder)
    for wheel in folder.glob("*.whl"):
        assert sums.get(wheel.name) == hashlib.sha256(wheel.read_bytes()).hexdigest(), wheel.name
