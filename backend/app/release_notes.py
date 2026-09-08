"""
When each release actually arrived, per environment.

WHAT CHANGED HERE AND WHY
-------------------------
This module used to BE the changelog: it read the commits baked into the image,
worked out which were new, and derived both the version and the text from them. The
text that came out was a list of commit subjects, which is a document written for
whoever reviews the diff and not for anybody using the portal.

So the writing moved to `changelog.py`, where a person writes it and chooses the
version. What is left here is the half a person cannot write: WHEN a version reached
THIS environment. The image knows what it is (`changelog.version()`); only the
database knows that test got 1.3.0 on Monday and production got it on Thursday.

That is why the row is still written at start-up and still keyed on the environment.
It is a deployment record, not a changelog -- one row the first time a version is
seen here, and nothing at all on a restart, a scale-up or a rollback.

TEST AND PRODUCTION
-------------------
Versioned separately, keyed on the environment name, because they are deployed from
different branches at different times. A single counter would have production
announcing a version it does not have.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from db import execute, query_all, query_one

log = logging.getLogger(__name__)

# Written into the image by scripts/gen_build_info.py. Absent in local development,
# which is not an error -- there is simply no build to record.
BUILD_FILE = pathlib.Path(__file__).resolve().parent / "build_info.json"


def environment() -> str:
    """Which deployment this pod is. Test and production keep separate version lines.

    Derived from the BRANCH the image was built from when nothing says otherwise,
    because that is what already distinguishes the two here: the pipeline deploys dev
    to test and main to production, and it deploys only from a real branch ref. That
    means this needs no chart change to be right on both.

    PORTAL_ENVIRONMENT overrides it, for the day the two stop lining up.
    """
    explicit = (os.getenv("PORTAL_ENVIRONMENT") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if explicit:
        return explicit
    branch = str(build_info().get("branch") or "").strip().lower()
    return "production" if branch in ("main", "master") else "test"


def build_info() -> Dict[str, Any]:
    """The branch, build number and commit this image was made from. Possibly empty."""
    try:
        if not BUILD_FILE.exists():
            return {}
        return json.loads(BUILD_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("build info unreadable: %s: %s", type(exc).__name__, exc)
        return {}


def ensure_release_notes_table() -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS release_notes (
            id           SERIAL PRIMARY KEY,
            environment  VARCHAR(32)  NOT NULL,
            version      VARCHAR(32)  NOT NULL,
            build_id     VARCHAR(64),
            branch       VARCHAR(128),
            commit_sha   VARCHAR(64),
            released_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (environment, version)
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_release_notes_env "
        "ON release_notes (environment, released_at DESC)"
    )


def record_current_deploy() -> Optional[Dict[str, Any]]:
    """Record that this environment now runs the version in the image. Never raises.

    Returns the row it wrote, or None when this version was already recorded here --
    which is the normal case for a pod restart, a scale-up, or a rollback. A portal
    that will not start because it could not write its own deployment record is a bad
    trade, so every failure here is a warning and nothing more.
    """
    try:
        import changelog

        ensure_release_notes_table()
        version = changelog.version()
        env = environment()
        info = build_info()

        if query_one(
            "SELECT id FROM release_notes WHERE environment = %s AND version = %s",
            [env, version],
        ):
            log.warning("release %s is already recorded for %s", version, env)
            return None

        execute(
            """
            INSERT INTO release_notes (environment, version, build_id, branch, commit_sha)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (environment, version) DO NOTHING
            """,
            [env, version, str(info.get("build_id") or ""),
             str(info.get("branch") or ""), str(info.get("commit_sha") or "")],
        )
        log.warning("release %s recorded for %s (build %s, branch %s)",
                    version, env, info.get("build_id") or "?", info.get("branch") or "?")
        return latest(env)
    except Exception as exc:
        log.warning("release not recorded: %s: %s", type(exc).__name__, exc)
        return None


def latest(env: str = "") -> Optional[Dict[str, Any]]:
    row = query_one(
        "SELECT * FROM release_notes WHERE environment = %s "
        "ORDER BY released_at DESC, id DESC LIMIT 1",
        [env or environment()],
    )
    return _shape(row) if row else None


def history(env: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    rows = query_all(
        "SELECT * FROM release_notes WHERE environment = %s "
        "ORDER BY released_at DESC, id DESC LIMIT %s",
        [env or environment(), max(1, min(int(limit or 50), 200))],
    ) or []
    return [_shape(r) for r in rows]


def deployed_map(env: str = "") -> Dict[str, str]:
    """version -> the date it arrived here. Empty when the table cannot be read.

    Read straight onto the changelog page, so an entry can say when it landed on THIS
    environment rather than when it was written. Failure is silent by design: a
    changelog with no dates beside it is still a changelog.
    """
    try:
        return {row["version"]: row["released_at"] for row in history(env, limit=200)}
    except Exception as exc:
        log.warning("deployment dates unavailable: %s: %s", type(exc).__name__, exc)
        return {}


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": str(row.get("version") or ""),
        "environment": str(row.get("environment") or ""),
        "branch": str(row.get("branch") or ""),
        "build_id": str(row.get("build_id") or ""),
        "released_at": (
            row["released_at"].isoformat() if hasattr(row.get("released_at"), "isoformat")
            else str(row.get("released_at") or "")
        ),
    }
