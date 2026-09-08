"""
What's New: the changelog, and which of its releases this environment is running.

Read-only, and readable by anyone signed in -- a changelog is for the people using
the portal, not for the people who deploy it.

Two sources, deliberately. The TEXT comes from changelog.py, which ships in the image
and is written by hand; it is the same on every environment and never needs a
database. The DATES come from the release_notes table, which is the only thing that
knows when a version actually reached THIS environment. If the database is
unreachable the page still renders in full, just without "arrived here on" beside
each version -- a changelog with no deployment dates is still a changelog, and a
changelog behind a 500 is nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

import changelog
import release_notes as notes
from security import AuthUser, get_current_user

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
def list_releases(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """The whole changelog, newest first, with what this environment runs."""
    del current_user
    releases = changelog.releases()
    try:
        arrived = notes.deployed_map()
        environment = notes.environment()
    except Exception as exc:
        log.warning("deployment dates unavailable: %s: %s", type(exc).__name__, exc)
        arrived, environment = {}, ""

    for entry in releases:
        entry["arrived_at"] = arrived.get(entry["version"], "")

    return {
        "success": True,
        "data": releases,
        "running": changelog.version(),
        "environment": environment,
        "build": notes.build_info().get("build_id") or "",
    }
