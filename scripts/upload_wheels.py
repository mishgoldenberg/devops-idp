#!/usr/bin/env python3
"""
Upload the wheels in offline-deps/wheels/ to the internal PyPI repository, once each.

    python3 scripts/upload_wheels.py

The repository is the one PIP_INDEX_URL names (".../artifactory/api/pypi/<repo>/simple");
the credentials are ARTIFACTORY_ADMIN_USERNAME and ARTIFACTORY_ADMIN_TOKEN. A wheel the
index already lists is skipped; a new one is checked against SHA256SUMS, uploaded, and
looked for in the index again. Pure standard library: it runs on the pipeline agent.

Never fails the build. The image build installs with --find-links offline-deps/wheels,
so a wheel that could not be uploaded is still installed; the outcome of every file is
printed, and a refusal is raised as a pipeline warning with Artifactory's own answer.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WHEELS = Path(__file__).resolve().parent.parent / "offline-deps" / "wheels"


def warn(message: str) -> None:
    print(f"##vso[task.logissue type=warning]{message}")


def split_index(index_url: str):
    """(artifactory root, repository key) from a PyPI simple index URL, or (None, None)."""
    m = re.match(r"^(https?://.+?)/api/pypi/([^/]+)/simple/?$", (index_url or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def project_of(wheel: str):
    """(normalised project name, version) from a wheel's file name."""
    name, version = wheel.split("-")[:2]
    return re.sub(r"[-_.]+", "-", name).lower(), version


def expected_sums(folder: Path) -> dict:
    sums = {}
    listing = folder / "SHA256SUMS"
    if listing.exists():
        for line in listing.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


class Artifactory:
    def __init__(self, root: str, repo: str, index: str, user: str, token: str, verify: bool = True):
        self.root, self.repo, self.index = root.rstrip("/"), repo, index.rstrip("/")
        self.user, self.token = user, token
        self.context = None if verify else ssl._create_unverified_context()
        self.auth = None  # the scheme that worked, once one has

    def _headers(self, scheme: str) -> dict:
        if scheme == "bearer":
            return {"Authorization": f"Bearer {self.token}"}
        pair = base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {pair}"}

    def _call(self, method: str, url: str, data: bytes = None, headers: dict = None):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.context) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def listed(self, project: str) -> str:
        """The index page for one project ("" when it has none)."""
        for scheme in ([self.auth] if self.auth else ["bearer", "basic"]):
            status, body = self._call("GET", f"{self.index}/{project}/", headers=self._headers(scheme))
            if status == 200:
                self.auth = scheme
                return body.decode("utf-8", "replace")
            if status == 404:
                return ""
        return ""

    def upload(self, project: str, version: str, path: Path):
        url = f"{self.root}/{self.repo}/{project}/{version}/{path.name}"
        data = path.read_bytes()
        last = (0, b"")
        for scheme in ([self.auth] if self.auth else ["bearer", "basic"]):
            headers = {**self._headers(scheme), "Content-Type": "application/octet-stream",
                       "X-Checksum-Sha256": hashlib.sha256(data).hexdigest()}
            last = self._call("PUT", url, data, headers)
            if last[0] in (200, 201):
                self.auth = scheme
                return last
            if last[0] not in (401, 403):
                return last
        return last


def main() -> int:
    wheels = sorted(WHEELS.glob("*.whl"))
    if not wheels:
        print("No wheels to upload.")
        return 0
    root, repo = split_index(os.environ.get("PIP_INDEX_URL", ""))
    token = os.environ.get("ARTIFACTORY_ADMIN_TOKEN", "").strip()
    if token.startswith("$("):
        token = ""
    if not root or not token:
        warn("Wheels were not uploaded: PIP_INDEX_URL is not an Artifactory PyPI URL or "
             "ARTIFACTORY_ADMIN_TOKEN is empty. The build installs them from the repository instead.")
        return 0
    verify = os.environ.get("PIP_TRUSTED_HOST", "").strip() in ("", "$(PIP_TRUSTED_HOST)")
    art = Artifactory(root, repo, os.environ["PIP_INDEX_URL"], os.environ.get("ARTIFACTORY_ADMIN_USERNAME", ""),
                      token, verify)
    sums = expected_sums(WHEELS)
    print(f"Uploading to {root}/{repo} ({len(wheels)} wheel(s))")
    problems = 0
    for path in wheels:
        project, version = project_of(path.name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if sums.get(path.name) not in (None, digest):
            warn(f"{path.name}: its SHA-256 does not match SHA256SUMS; not uploaded.")
            problems += 1
            continue
        if path.name in art.listed(project):
            print(f"    present   {path.name}")
            continue
        status, body = art.upload(project, version, path)
        if status not in (200, 201):
            warn(f"{path.name}: Artifactory refused the upload (HTTP {status}): "
                 f"{body.decode('utf-8', 'replace')[:300]}")
            problems += 1
            continue
        # Uploaded is not the same as installable: wait for the index to list it.
        for _ in range(12):
            if path.name in art.listed(project):
                print(f"    uploaded  {path.name}")
                break
            time.sleep(5)
        else:
            warn(f"{path.name}: uploaded (HTTP {status}) but the index does not list it yet.")
            problems += 1
    print("Done." if not problems else f"Done, {problems} problem(s): the build uses the wheels in the repository.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
