"""
Database backup status — read-only, Platform Admin only.

The backups themselves are taken by two CronJobs in the infrastructure chart
(``postgres-backup-cronjob.yaml`` / ``postgres-restore-verify-cronjob.yaml``),
not by this process. Those jobs write a row into ``backup_runs`` describing what
they did; everything here reads that table back.

Why the portal shows this at all: a nightly job that started failing is silent.
The dump stops being written, nothing on screen changes, and the first person to
find out is whoever needs the dump. Until this page existed the answer to "are we
backed up?" was `oc get cronjob`, which nobody runs on a good day. A backup whose
failure is invisible is not a backup, it is a belief.

There is deliberately no endpoint here that STARTS a backup or a restore. A
restore is destructive, and putting a button on it in a web page is how a
mis-click becomes an outage; the procedure lives in docs/RUNBOOK.md §6 where it
belongs, behind a human reading it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status

import db
from security import AuthUser, get_current_user, has_effective_admin_access_live


log = logging.getLogger(__name__)
router = APIRouter()


# A nightly backup is late once it has missed a night, not the moment it is a
# minute overdue: the job runs at 01:15, so anything under 48h has had a full
# extra chance to succeed and does not need waking anybody up.
BACKUP_STALE_HOURS = 48
# The verify job is weekly, so two missed weeks is the signal.
VERIFY_STALE_DAYS = 15
# A job that says "running" for longer than this did not finish — it was evicted,
# OOM-killed, or the node went away. Two hours is far beyond a real dump here.
RUN_STUCK_HOURS = 2


def _keep_last() -> int:
    """How many dumps the backup job keeps in Artifactory (global.backupKeepLast).

    Read here so the portal can say which recorded backups still EXIST. Without it
    the run history looks like a list of restore points, and most of the entries
    are dumps that were pruned days ago — the worst possible thing to hand someone
    who is mid-incident and picking one to restore.
    """
    try:
        return max(1, int(os.environ.get("BACKUP_KEEP_LAST", "3")))
    except (TypeError, ValueError):
        return 3


def _restore_context() -> Dict[str, Any]:
    """The names the restore commands need, from this pod's own environment.

    Every value is what the chart rendered for THIS environment, so the
    instructions say postgres-test in test and postgres in production rather than
    a guess that is right half the time. Database name and user come out of
    DATABASE_URL, which is the same string the app connects with — there is no
    second copy to disagree with it.
    """
    parsed = urlparse(os.environ.get("DATABASE_URL", ""))
    return {
        "namespace": os.environ.get("HUB_NAMESPACE") or "<namespace>",
        "statefulset": os.environ.get("BACKUP_PG_STATEFULSET") or "postgres",
        "deployment": os.environ.get("BACKUP_BACKEND_DEPLOYMENT") or "backend",
        "replicas": os.environ.get("BACKUP_BACKEND_REPLICAS") or "2",
        "db_user": (parsed.username or "devops"),
        "db_name": (parsed.path or "/devops_control_center").lstrip("/"),
        # The label the fingerprint is salted with, so the UI can print the exact
        # one-liner that recomputes it rather than describing the algorithm.
        "fingerprint_label": "devops-hub-backup-fingerprint-v1:",
    }


def _require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not has_effective_admin_access_live(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to view backup status.",
        )
    return user


def _age(ts: Optional[datetime]) -> Optional[timedelta]:
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts


def _humanise(delta: Optional[timedelta]) -> Optional[str]:
    if delta is None:
        return None
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _effective_status(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """A ``running`` row past the stuck threshold reports as ``stuck``.

    Without this a job killed mid-dump shows "running" forever and the page reads
    as healthy — the worst of the three possible wrong answers.
    """
    if not row:
        return None
    raw = str(row.get("status") or "").lower()
    if raw == "running" and (_age(row.get("started_at")) or timedelta()) > timedelta(
        hours=RUN_STUCK_HOURS
    ):
        return "stuck"
    return raw


def _latest(rows: List[Dict[str, Any]], kind: str, only_succeeded: bool = False):
    for row in rows:
        if str(row.get("kind")) != kind:
            continue
        if only_succeeded and str(row.get("status")).lower() != "succeeded":
            continue
        return row
    return None


def _derive_health(rows: List[Dict[str, Any]], table_missing: bool) -> Dict[str, Any]:
    """Turn the run history into one sentence an operator can act on.

    Ordered worst-first, and every branch names the next step rather than just the
    state — "check the Artifactory credential" is actionable, "failed" is not.
    """
    if table_missing:
        return {
            "level": "unknown",
            "headline": "Backup status is unavailable",
            "detail": (
                "The backup_runs table does not exist yet. It is created at backend "
                "startup — if this persists, the schema bootstrap is failing; check "
                "/api/health/ready."
            ),
        }

    last_backup = _latest(rows, "backup")
    last_good_backup = _latest(rows, "backup", only_succeeded=True)
    last_verify = _latest(rows, "verify")
    last_good_verify = _latest(rows, "verify", only_succeeded=True)

    if not last_backup:
        return {
            "level": "critical",
            "headline": "No backup has ever run",
            "detail": (
                "Nothing has ever written to backup_runs. Either the backup CronJob "
                "is not deployed (it renders only in production, and only when the "
                "Artifactory backup credentials are set) or it has never fired. "
                "Check: oc get cronjob -n <namespace>."
            ),
        }

    state = _effective_status(last_backup)
    backup_age = _age(last_good_backup.get("started_at")) if last_good_backup else None

    if state == "failed":
        return {
            "level": "critical",
            "headline": "The last backup failed",
            "detail": (
                str(last_backup.get("message") or "No reason was recorded.")
                + " Last good backup: "
                + (_humanise(backup_age) or "never")
                + "."
            ),
        }
    if state == "stuck":
        return {
            "level": "critical",
            "headline": "The last backup started and never finished",
            "detail": (
                "The job was most likely evicted or OOM-killed. Check: oc get jobs "
                "-n <namespace> and the pod's events. Last good backup: "
                + (_humanise(backup_age) or "never")
                + "."
            ),
        }
    if backup_age is None:
        return {
            "level": "critical",
            "headline": "No backup has ever succeeded",
            "detail": "Every recorded attempt failed. See the run history below.",
        }
    if backup_age > timedelta(hours=BACKUP_STALE_HOURS):
        return {
            "level": "critical",
            "headline": f"The newest backup is {_humanise(backup_age)}",
            "detail": (
                f"Backups run nightly, so anything older than {BACKUP_STALE_HOURS}h "
                "means the job has stopped firing. Check that the CronJob is not "
                "suspended: oc get cronjob -n <namespace>."
            ),
        }

    # Backups are fine — now the restore side, which is the half that is usually
    # untrue rather than merely late.
    verify_state = _effective_status(last_verify)
    verify_age = _age(last_good_verify.get("started_at")) if last_good_verify else None

    if verify_state in ("failed", "stuck"):
        return {
            "level": "warning",
            "headline": "Backups are running, but the last restore test failed",
            "detail": (
                str(last_verify.get("message") or "No reason was recorded.")
                + " The dumps exist; whether they restore is currently unproven."
            ),
        }
    if verify_age is None:
        return {
            "level": "warning",
            "headline": "Backups are running, but none has ever been restored",
            "detail": (
                "A restore that has never been tested is not a backup. The weekly "
                "verify CronJob proves it; check that it is deployed."
            ),
        }
    if verify_age > timedelta(days=VERIFY_STALE_DAYS):
        return {
            "level": "warning",
            "headline": f"The last successful restore test was {_humanise(verify_age)}",
            "detail": "The weekly verify job has stopped proving the dumps restore.",
        }

    return {
        "level": "ok",
        "headline": f"Backed up {_humanise(backup_age)}, restore verified "
        f"{_humanise(verify_age)}",
        "detail": "The newest dump has been restored into a scratch database and read back.",
    }


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    started = row.get("started_at")
    finished = row.get("finished_at")
    duration = None
    if started and finished:
        duration = int((finished - started).total_seconds())
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "environment": row.get("environment"),
        "status": _effective_status(row),
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "age": _humanise(_age(started)),
        "duration_seconds": duration,
        "artifact": row.get("artifact"),
        "size_bytes": row.get("size_bytes"),
        "sha256": row.get("sha256"),
        # Deliberately truncated. It is already only a hash of the secret, but a
        # prefix is enough to compare two environments by eye and there is no
        # reason to publish more of it than that.
        "jwt_fingerprint": (str(row.get("jwt_fingerprint"))[:12] or None)
        if row.get("jwt_fingerprint")
        else None,
        "message": row.get("message"),
    }


def _restore_points(rows: List[Dict[str, Any]], keep_last: int) -> List[Dict[str, Any]]:
    """The succeeded backups, newest first, marked with whether they still exist.

    Built from ``backup_runs`` rather than by listing Artifactory, and that is a
    security decision, not a shortcut: listing would mean giving this process the
    backup repository's credential, which can DELETE. A portal that can be
    compromised should not be able to erase the backups taken to survive it.

    The cost is that this is a record of what WAS written, not a live directory —
    so anything past ``keep_last`` is marked ``retained: false``. That is the one
    thing an operator picking a restore point must not get wrong.
    """
    points = []
    succeeded = [
        row for row in rows
        if str(row.get("kind")) == "backup"
        and str(row.get("status")).lower() == "succeeded"
        and row.get("artifact")
    ]
    for index, row in enumerate(succeeded):
        artifact = str(row.get("artifact"))
        points.append({
            "id": row.get("id"),
            "filename": artifact.rsplit("/", 1)[-1],
            "artifact": artifact,
            "taken_at": row["started_at"].isoformat() if row.get("started_at") else None,
            "age": _humanise(_age(row.get("started_at"))),
            "size_bytes": row.get("size_bytes"),
            "sha256": row.get("sha256"),
            # Twelve characters is enough to compare against the live secret by eye
            # and there is no reason to publish more of a hash than that.
            "jwt_fingerprint": str(row.get("jwt_fingerprint") or "")[:12] or None,
            "retained": index < keep_last,
        })
    return points


@router.get("")
def backup_status(
    limit: int = Query(20, ge=1, le=100),
    _admin: AuthUser = Depends(_require_admin),
) -> Dict[str, Any]:
    """Recent backup and restore-verification runs, plus a derived verdict."""
    table_missing = False
    rows: List[Dict[str, Any]] = []
    try:
        rows = db.query_all(
            """
            SELECT id, kind, environment, status, started_at, finished_at,
                   artifact, size_bytes, sha256, jwt_fingerprint, message
            FROM backup_runs
            ORDER BY started_at DESC
            LIMIT %s
            """,
            [limit],
        )
    except Exception as exc:
        if db._is_undefined_table(exc):
            table_missing = True
        else:
            # Do not turn a database blip into a 500 on an admin page: report it
            # as unknown, which is what it is, and say so in the detail line.
            log.warning("backup status query failed: %s", exc)
            return {
                "success": True,
                "health": {
                    "level": "unknown",
                    "headline": "Backup status could not be read",
                    "detail": f"The query against backup_runs failed: {exc}",
                },
                "runs": [],
                "last_backup": None,
                "last_verify": None,
                "restore_points": [],
                "restore_context": _restore_context(),
            }

    health = _derive_health(rows, table_missing)
    last_backup = _latest(rows, "backup")
    last_verify = _latest(rows, "verify")
    keep_last = _keep_last()
    return {
        "success": True,
        "health": health,
        "runs": [_shape(r) for r in rows],
        "last_backup": _shape(last_backup) if last_backup else None,
        "last_verify": _shape(last_verify) if last_verify else None,
        # What an operator can actually restore, and the names the commands need.
        # The portal never performs the restore — see the module docstring — it just
        # removes the guesswork from doing it by hand.
        "restore_points": _restore_points(rows, keep_last),
        "restore_context": _restore_context(),
        "thresholds": {
            "backup_stale_hours": BACKUP_STALE_HOURS,
            "verify_stale_days": VERIFY_STALE_DAYS,
            "keep_last": keep_last,
        },
    }
