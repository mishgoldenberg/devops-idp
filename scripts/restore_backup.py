#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Restore a DevOps Hub Postgres backup, end to end, from one command.

    python scripts/restore_backup.py --namespace devops-hub

Why this exists rather than a list of commands in the runbook: the manual procedure
had four defects and every one of them was a difference from what the CronJob does
(curl without -f, no checksum comparison, no source for the backup credentials, no
PGPASSWORD). A written procedure is only tested when somebody follows it, which is
during an incident. This script performs the same steps the weekly verify job
performs, so there is one description of a restore, not two.

It also removes the shell problem: the runbook is written in bash and gets run in
cmd, where a trailing backslash is not a line continuation and $VAR does not expand.
Nothing here depends on the shell.

WHAT IT DOES, in order:

  1. reads the backup credentials out of the cluster Secret
  2. lists the dumps in Artifactory and picks the newest (or --file)
  3. reads the dump's manifest and the RUNNING JWT_SECRET fingerprint, and refuses
     to continue if they disagree - a mismatch means the restored credentials will
     be undecryptable, which is worse than not restoring
  4. downloads the dump and verifies its sha256 against the manifest
  5. scales the backend to zero
  6. pg_restore --clean --if-exists into the live database
  7. scales the backend back up and polls /api/health/ready for credentials: ok

Steps 5-7 are destructive and only run with --yes, or after typing RESTORE.

Read-only rehearsal, safe to run any time, does everything except 5-7:

    python scripts/restore_backup.py --namespace devops-hub --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

FP_LABEL = "devops-hub-backup-fingerprint-v1:"
REPO = "devops-hub-backups"
PATH_ = "postgres"


# ── plumbing ────────────────────────────────────────────────────────────────

def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print("\nFAILED: " + msg, file=sys.stderr)
    sys.exit(1)


def step(n: str, msg: str) -> None:
    print("\n[%s] %s" % (n, msg))


def oc(args: List[str], *, stdin_file=None, check: bool = True) -> str:
    """Run an oc command.

    encoding is pinned rather than left to text=True: the default is the machine
    locale (cp1255 on a Hebrew Windows install), and one non-ASCII byte in a pod log
    kills the reader thread AFTER the command has already run.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        ["oc"] + args,
        stdin=stdin_file,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        die("oc " + " ".join(args) + "\n" + out.strip())
    return out.strip()


def http(url: str, user: str, token: str, *, insecure: bool, timeout: int = 120):
    """GET, raising on any non-2xx.

    urllib raises HTTPError on 4xx/5xx, which is the whole point: curl without -f
    saves a 401 body as the dump and exits 0, and pg_restore then reports a
    truncated archive. That reads as a corrupt backup and is not one.
    """
    req = urllib.request.Request(url)
    cred = base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
    req.add_header("Authorization", "Basic " + cred)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(req, context=ctx, timeout=timeout)


# ── the steps ───────────────────────────────────────────────────────────────

def read_credentials(ns: str) -> Tuple[str, str, str]:
    step("1/7", "Reading backup credentials and Artifactory URL from the cluster")
    out = oc(["get", "secret", "all-secrets", "-n", ns, "-o", "json"])
    data = json.loads(out).get("data", {})

    def dec(key: str) -> str:
        raw = data.get(key)
        if not raw:
            return ""
        return base64.b64decode(raw).decode("utf-8").strip()

    user = dec("ARTIFACTORY_BACKUP_USERNAME")
    token = dec("ARTIFACTORY_BACKUP_TOKEN")
    base = dec("ARTIFACTORY_BASE_URL") or os.getenv("ARTIFACTORY_BASE_URL", "")
    if not user or not token:
        die("ARTIFACTORY_BACKUP_USERNAME / ARTIFACTORY_BACKUP_TOKEN are empty in the "
            "all-secrets Secret. Backups are switched off for this environment.")
    if not base:
        die("No Artifactory base URL in the Secret or the environment. Pass "
            "--artifactory-url.")
    print("    account: %s   repo: %s/%s" % (user, REPO, PATH_))
    return user, token, base.rstrip("/")


def list_dumps(base: str, user: str, token: str, insecure: bool) -> List[str]:
    url = "%s/api/storage/%s/%s" % (base, REPO, PATH_)
    with http(url, user, token, insecure=insecure, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    names = [
        str(c.get("uri", "")).lstrip("/")
        for c in data.get("children", [])
        if not c.get("folder")
    ]
    # The name ends in an ISO date, one per day, so lexicographic order IS
    # chronological order here - which is not true of image tags.
    return sorted((n for n in names if n.endswith(".dump")), reverse=True)


def running_fingerprint(ns: str, deployment: str) -> str:
    expr = ("import hashlib,os;print(hashlib.sha256(('" + FP_LABEL +
            "'+os.environ['JWT_SECRET']).encode()).hexdigest())")
    return oc(["exec", "-n", ns, "deploy/" + deployment, "--", "python", "-c", expr]).strip()


def fetch(base: str, user: str, token: str, name: str, insecure: bool) -> Tuple[str, Dict]:
    step("3/7", "Downloading %s and its manifest" % name)
    dump_url = "%s/%s/%s/%s" % (base, REPO, PATH_, name)
    with http(dump_url, user, token, insecure=insecure) as resp:
        with open(name, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
    with http(dump_url + ".manifest.json", user, token, insecure=insecure) as resp:
        manifest = json.loads(resp.read().decode("utf-8"))

    digest = hashlib.sha256()
    with open(name, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    actual, expected = digest.hexdigest(), manifest.get("sha256", "")
    size = os.path.getsize(name)
    print("    %d bytes" % size)
    if actual != expected:
        die("checksum mismatch - the download is not the dump that was uploaded.\n"
            "  manifest: %s\n  file:     %s" % (expected, actual))
    print("    sha256 matches the manifest")

    with open(name, "rb") as fh:
        if fh.read(5) != b"PGDMP":
            die("the file does not begin with PGDMP, so it is not a pg_dump archive "
                "even though its checksum matched. Suspect the manifest.")
    return name, manifest


def do_restore(ns: str, deployment: str, statefulset: str, db_user: str,
               db_name: str, replicas: int, dump: str) -> None:
    step("5/7", "Scaling %s to 0 so nothing writes during the restore" % deployment)
    oc(["scale", "deploy/" + deployment, "-n", ns, "--replicas=0"])
    oc(["rollout", "status", "deploy/" + deployment, "-n", ns, "--timeout=120s"], check=False)

    step("6/7", "pg_restore --clean --if-exists into %s" % db_name)
    # PGPASSWORD, not POSTGRESQL_PASSWORD: the pod carries the image's name for it
    # and pg_restore reads the libpq one. Without this the restore fails as a
    # password error followed by a broken pipe, which looks like a network fault.
    inner = ('PGPASSWORD="$POSTGRESQL_PASSWORD" pg_restore -U %s -d %s '
             '--clean --if-exists --no-owner --no-privileges' % (db_user, db_name))
    with open(dump, "rb") as fh:
        out = oc(["exec", "-i", "-n", ns, "statefulset/" + statefulset, "--",
                  "sh", "-c", inner], stdin_file=fh, check=False)
    if out:
        print("    " + out.replace("\n", "\n    "))

    step("7/7", "Scaling %s back to %d and waiting for readiness" % (deployment, replicas))
    oc(["scale", "deploy/" + deployment, "-n", ns, "--replicas=%d" % replicas])
    oc(["rollout", "status", "deploy/" + deployment, "-n", ns, "--timeout=300s"], check=False)


def check_ready(ns: str, deployment: str) -> None:
    """The real success criterion: the backend confirming it can still DECRYPT.

    pg_restore's exit code says the rows arrived. It says nothing about whether the
    JWT_SECRET in the cluster can read the credentials that came back, which is the
    failure that costs every user their stored PAT.
    """
    for attempt in range(30):
        out = oc(["exec", "-n", ns, "deploy/" + deployment, "--",
                  "python", "-c",
                  "import urllib.request,sys;"
                  "sys.stdout.write(urllib.request.urlopen("
                  "'http://127.0.0.1:8000/api/health/ready', timeout=5).read().decode())"],
                 check=False)
        if out.startswith("{"):
            try:
                payload = json.loads(out)
            except ValueError:
                payload = {}
            creds = str(payload.get("credentials", "")).lower()
            print("    /api/health/ready -> " + json.dumps(payload)[:300])
            if creds in ("ok", "true"):
                print("\nRESTORE VERIFIED: the backend can decrypt what was restored.")
                return
            if creds:
                die("readiness reports credentials: %s. The dump and the running "
                    "JWT_SECRET do not agree - see RUNBOOK section 4." % creds)
            print("\nRestore finished. Readiness answered but did not report a "
                  "'credentials' field; check it in the portal.")
            return
        time.sleep(5)
    die("the backend did not answer readiness within 150s. Check: oc get pods -n " + ns)


# ── entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Restore a DevOps Hub Postgres backup.")
    ap.add_argument("--namespace", "-n", required=True)
    ap.add_argument("--file", help="dump filename; default is the newest")
    ap.add_argument("--artifactory-url", default="")
    ap.add_argument("--deployment", default="backend")
    ap.add_argument("--statefulset", default="postgres")
    ap.add_argument("--db-user", default="devops")
    ap.add_argument("--db-name", default="devops_control_center")
    ap.add_argument("--replicas", type=int, default=2)
    ap.add_argument("--insecure", action="store_true", default=True,
                    help="skip TLS verification (default: on, internal CA)")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify everything, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = ap.parse_args()

    ns = args.namespace
    user, token, base = read_credentials(ns)
    if args.artifactory_url:
        base = args.artifactory_url.rstrip("/")

    step("2/7", "Choosing a dump and checking it matches the running secret")
    dumps = list_dumps(base, user, token, args.insecure)
    if not dumps:
        die("no dumps in %s/%s. Has the backup job ever succeeded?" % (REPO, PATH_))
    name = args.file or dumps[0]
    if name not in dumps:
        die("%s is not in the repository. Available:\n  %s" % (name, "\n  ".join(dumps)))
    print("    available: " + ", ".join(dumps))
    print("    chosen:    " + name)

    dump, manifest = fetch(base, user, token, name, args.insecure)

    step("4/7", "Comparing the dump's fingerprint with the RUNNING JWT_SECRET")
    want = manifest.get("jwt_secret_fingerprint", "")
    have = running_fingerprint(ns, args.deployment)
    print("    dump:    " + (want or "(not recorded)"))
    print("    running: " + have)
    if want and have and want != have:
        die("fingerprint mismatch. This dump was taken under a DIFFERENT JWT_SECRET, "
            "so every stored Azure DevOps PAT and the OIDC client secret would "
            "restore unreadable. Restore the matching secret first - RUNBOOK "
            "section 4. Nothing has been changed.")
    if not want:
        print("    WARNING: this dump has no recorded fingerprint; cannot verify.")

    if args.dry_run:
        print("\nDRY RUN: verified and downloaded %s. Nothing was changed." % dump)
        return 0

    print("\n" + "=" * 70)
    print("The next steps SCALE DOWN %s and run pg_restore --clean against" % args.deployment)
    print("%s in namespace %s. --clean DROPS every object first. No undo." % (args.db_name, ns))
    print("=" * 70)
    if not args.yes:
        if input("Type RESTORE to continue: ").strip() != "RESTORE":
            print("Aborted. Nothing was changed.")
            return 1

    do_restore(ns, args.deployment, args.statefulset, args.db_user,
               args.db_name, args.replicas, dump)
    check_ready(ns, args.deployment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
