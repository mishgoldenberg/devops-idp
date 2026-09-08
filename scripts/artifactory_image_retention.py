#!/usr/bin/env python3
"""Keep only the newest N image tags per Docker image in Artifactory.

Every pipeline run pushes a new ``backend`` and ``frontend`` image tagged with the
build id, so the Docker repo grows without bound. This prunes each image down to the
most recent N tags (default 5), always keeping the moving tags (``latest``).

Pure standard library on purpose: it runs in the docker-build-and-push job, which does
NOT ``pip install`` anything. No requests/httpx, no extra agent setup.

It is HOUSEKEEPING — it must never fail a deploy. On any missing configuration or API
error it logs and exits 0; the pipeline step also runs with continueOnError.

Configuration (environment variables):
  ARTIFACTORY_BASE_URL          e.g. https://artifactory.corp/artifactory   (required)
  ARTIFACTORY_DOCKER_REPO_KEY   the Docker repository KEY, e.g. docker-local (required)
  ARTIFACTORY_ACCESS_TOKEN      access token (preferred), OR
  ARTIFACTORY_DOCKER_USERNAME / ARTIFACTORY_DOCKER_PASSWORD   basic-auth fallback
  ARTIFACTORY_IMAGE_RETENTION   how many tags to keep per image   (default 5)
  ARTIFACTORY_RETENTION_IMAGES  comma list of image names         (default backend,frontend)
  ARTIFACTORY_RETENTION_ALWAYS_KEEP  tags never deleted           (default latest)
  ARTIFACTORY_TLS_VERIFY        verify TLS (closed net is self-signed)  (default false)
"""
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request


def env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


BASE = env("ARTIFACTORY_BASE_URL").rstrip("/")
REPO_KEY = env("ARTIFACTORY_DOCKER_REPO_KEY")
TOKEN = env("ARTIFACTORY_ACCESS_TOKEN")
USER = env("ARTIFACTORY_DOCKER_USERNAME")
PASSWORD = env("ARTIFACTORY_DOCKER_PASSWORD")
IMAGES = [s.strip() for s in env("ARTIFACTORY_RETENTION_IMAGES", "backend,frontend").split(",") if s.strip()]
ALWAYS_KEEP = {t.strip() for t in env("ARTIFACTORY_RETENTION_ALWAYS_KEEP", "latest").split(",") if t.strip()}

# 1.3.0-test / 1.3.0-prod, written by the build from backend/app/changelog.py.
# Anchored at both ends so a build tag that merely starts with digits is not
# mistaken for a release and quietly exempted from retention forever.
_RELEASE_TAG = re.compile(r"^\d+\.\d+\.\d+-[a-z]+$")
VERIFY_TLS = env("ARTIFACTORY_TLS_VERIFY", "false").lower() in ("1", "true", "yes")

try:
    KEEP = max(1, int(env("ARTIFACTORY_IMAGE_RETENTION", "5")))
except ValueError:
    KEEP = 5


def _ssl_context():
    ctx = ssl.create_default_context()
    if not VERIFY_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _auth_header():
    if TOKEN:
        return "Bearer " + TOKEN
    if USER:
        raw = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return "Basic " + raw
    return ""


def _request(method, url, data=None, content_type=None):
    req = urllib.request.Request(url, data=data, method=method)
    auth = _auth_header()
    if auth:
        req.add_header("Authorization", auth)
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as resp:
        return resp.status, resp.read()


def _tags_newest_first(image):
    """Every tag of `image`, newest first, from a single AQL query.

    A Docker tag in Artifactory is the folder ``{repo}/{image}/{tag}/`` holding a
    ``manifest.json``; that manifest's ``created`` time is the tag's push time. One AQL
    call returns them all, already sorted, so we never guess order from the tag string.
    """
    aql = (
        'items.find({'
        f'"repo":"{REPO_KEY}",'
        f'"path":{{"$match":"{image}/*"}},'
        '"name":"manifest.json"'
        '}).include("path","created").sort({"$desc":["created"]})'
    )
    status, body = _request(
        "POST", f"{BASE}/api/search/aql", data=aql.encode("utf-8"), content_type="text/plain"
    )
    results = json.loads(body or b"{}").get("results", [])
    tags = []
    for item in results:
        # path is "IMAGE/TAG"; take the last segment as the tag.
        path = str(item.get("path") or "")
        tag = path.rsplit("/", 1)[-1] if "/" in path else ""
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _delete_tag(image, tag):
    _request("DELETE", f"{BASE}/{REPO_KEY}/{image}/{tag}")


def prune_image(image):
    try:
        tags = _tags_newest_first(image)
    except urllib.error.HTTPError as exc:
        print(f"  {image}: could not list tags (HTTP {exc.code}); skipping.")
        return
    except Exception as exc:  # noqa: BLE001 - housekeeping must not crash the build
        print(f"  {image}: could not list tags ({exc}); skipping.")
        return

    if not tags:
        print(f"  {image}: no tags found; nothing to do.")
        return

    # Keep the newest KEEP build tags plus the always-keep moving tags (latest) plus
    # every RELEASE tag. Everything older is deleted. Counting only build tags means
    # neither 'latest' nor a release tag eats into the five real builds we promised to
    # retain.
    #
    # Release tags (1.3.0-test, 1.3.0-prod) are never pruned. They are how anyone
    # answers "which release is production actually on" without reading a build
    # number, and there is one per release rather than one per build, so they do not
    # grow the way build tags do. Deleting one would silently break a rollback to a
    # named version -- the failure of a retention sweep is always invisible until the
    # day you need the thing it deleted.
    protected = [t for t in tags if _RELEASE_TAG.match(t)]
    build_tags = [t for t in tags if t not in ALWAYS_KEEP and t not in protected]
    keep = set(ALWAYS_KEEP) | set(protected) | set(build_tags[:KEEP])

    to_delete = [t for t in tags if t not in keep]
    if not to_delete:
        print(f"  {image}: {len(tags)} tag(s), within retention ({KEEP}); nothing to delete.")
        return

    print(f"  {image}: {len(tags)} tag(s); keeping {sorted(keep)}; deleting {len(to_delete)}.")
    for tag in to_delete:
        try:
            _delete_tag(image, tag)
            print(f"      deleted {image}:{tag}")
        except urllib.error.HTTPError as exc:
            print(f"      could NOT delete {image}:{tag} (HTTP {exc.code})")
        except Exception as exc:  # noqa: BLE001
            print(f"      could NOT delete {image}:{tag} ({exc})")


def main():
    if not BASE or not REPO_KEY:
        print(
            "artifactory_image_retention: ARTIFACTORY_BASE_URL and "
            "ARTIFACTORY_DOCKER_REPO_KEY must be set — skipping image pruning."
        )
        return 0
    if not (TOKEN or USER):
        print("artifactory_image_retention: no credentials provided — skipping image pruning.")
        return 0

    print(
        f"Pruning Artifactory images in '{REPO_KEY}' to the newest {KEEP} tag(s) "
        f"(always keeping {sorted(ALWAYS_KEEP)}): {', '.join(IMAGES)}"
    )
    for image in IMAGES:
        prune_image(image)
    # Note: deleting tags frees the manifests immediately; unreferenced blob layers are
    # reclaimed when Artifactory's scheduled Garbage Collection runs (an admin setting).
    print("Done. (Blob storage is reclaimed by Artifactory's next Garbage Collection.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
