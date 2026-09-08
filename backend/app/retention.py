"""
How long the portal keeps a finished request.

Approval requests and catalog submissions only ever accumulate. Every quota increase,
every rejected form, every characterization stays in Postgres for as long as the
database lives, and the database is a fixed PersistentVolume on a cluster that
cannot be enlarged by redeploying. Nothing here is read after the first fortnight.

WHAT IS NEVER DELETED, AND WHY
------------------------------
A cleaner's request row is not history. It is the link between the portal's record of
a cleaner and the pull request that created it -- cleaner_store.for_requests() reads
requests by id to give each one its state, and api/approvals reads the payload to
re-render the files when somebody changes it. A CronJob deleting artifacts every night
can outlive a year comfortably, so deleting its request would leave a live scheduled
job with no record of who asked for it or what it was meant to delete.

So every ARTIFACTORY_CLEANER_* type is excluded, by reading the same set the rest of
the application gates on rather than a second list written here.

Only TERMINAL requests go. A request still PENDING after a year is not clutter, it is
a request nobody ever decided -- deleting it would hide the fact rather than fix it,
so those are counted and logged instead.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from db import query_one
from request_types import CLEANER_REQUEST_TYPES

log = logging.getLogger(__name__)

# A year. Long enough that no audit conversation is ever cut short by it, short enough
# that the table does not grow without bound.
REQUEST_RETENTION_DAYS = int(os.getenv("REQUEST_RETENTION_DAYS") or "365")

# The states a request never leaves. Anything else is still in flight.
_TERMINAL = ("COMPLETED", "EXECUTED", "FAILED", "REJECTED")


def delete_old_requests(days: int = REQUEST_RETENTION_DAYS) -> Dict[str, Any]:
    """Purge finished requests and submissions older than `days`. Never raises.

    Returns what it did, so the caller can log one line rather than three.

    The positive-day guard is the same one the audit sweeper carries, for the same
    reason: a misread environment variable turning a retention window into 0 would
    make this a table truncation that runs every six hours.
    """
    out: Dict[str, Any] = {"requests": 0, "submissions": 0, "stuck": 0, "days": days}
    try:
        days = int(days)
    except (TypeError, ValueError):
        log.warning("retention: REQUEST_RETENTION_DAYS is not a number (%r) -- skipped", days)
        return out
    if days <= 0:
        log.warning("retention: refusing days=%s (must be > 0)", days)
        return out
    out["days"] = days

    excluded = sorted(CLEANER_REQUEST_TYPES)
    try:
        row = query_one(
            """
            WITH deleted AS (
                DELETE FROM approval_requests
                 WHERE created_at < NOW() - (%s || ' days')::interval
                   AND status = ANY(%s)
                   AND NOT (request_type::text = ANY(%s))
                 RETURNING 1
            )
            SELECT COUNT(*)::bigint AS n FROM deleted
            """,
            [days, list(_TERMINAL), excluded],
        )
        out["requests"] = int((row or {}).get("n") or 0)
    except Exception as exc:
        log.warning("retention: approval_requests sweep failed: %s: %s", type(exc).__name__, exc)

    try:
        row = query_one(
            """
            WITH deleted AS (
                DELETE FROM catalog_submissions
                 WHERE created_at < NOW() - (%s || ' days')::interval
                 RETURNING 1
            )
            SELECT COUNT(*)::bigint AS n FROM deleted
            """,
            [days],
        )
        out["submissions"] = int((row or {}).get("n") or 0)
    except Exception as exc:
        log.warning("retention: catalog_submissions sweep failed: %s: %s", type(exc).__name__, exc)

    # Counted, not deleted, and said out loud. A request that has been PENDING for a
    # year is somebody waiting for an answer that never came.
    try:
        row = query_one(
            """
            SELECT COUNT(*)::bigint AS n
              FROM approval_requests
             WHERE created_at < NOW() - (%s || ' days')::interval
               AND NOT (status = ANY(%s))
            """,
            [days, list(_TERMINAL)],
        )
        out["stuck"] = int((row or {}).get("n") or 0)
    except Exception as exc:
        log.warning("retention: undecided-request count failed: %s: %s", type(exc).__name__, exc)

    if out["requests"] or out["submissions"] or out["stuck"]:
        log.warning(
            "retention (%s days): purged %s request(s) and %s submission(s); "
            "%s request(s) older than that are still undecided and were kept; "
            "cleaners are never purged",
            out["days"], out["requests"], out["submissions"], out["stuck"],
        )
    return out
