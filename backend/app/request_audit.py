"""
Automatic logging: HTTP calls, and the application's own log records.

The Logs page used to show about ten kinds of event, because those were the ten
places somebody had remembered to call ``audit.log()``. Everything else the portal
did — every token saved, every widget preference, every failed call to Artifactory,
every 500 — happened silently. The page was a list of the events we had thought to
name, which is not what a log is for: you go to a log to find out about the thing
you did NOT anticipate.

Two mechanisms here, and between them they catch the rest:

  1. ``AuditMiddleware`` records every state-changing HTTP request.
  2. ``DatabaseLogHandler`` mirrors WARNING-and-above from the application's Python
     loggers into the same table.

(2) is the one that changes the character of the page. The integrations already log
their failures — "Artifactory AQL failed", "SNow producer returned 403" — but those
went to the pod's stdout, where nobody could reach them without a kubectl session.
Now they land in the same stream as the user action that triggered them, one row
apart, and the question "why did that fail for them and not for me" is answerable
from a browser.

WHAT IS DELIBERATELY NOT LOGGED
-------------------------------
GET requests. Every widget on the dashboard polls; with ten widgets and a room full
of users that is millions of rows a week saying nothing happened, and the writes we
DO care about would be buried under them. Reads are not events. Failed reads are —
a GET that 4xx/5xx's is recorded, because that is not a read, it is a problem.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

import audit


_log = logging.getLogger(__name__)

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 428 is how the integrations say "not connected — add your token". On a read that is an
# expected state the user resolves by connecting, not a failure worth a WARNING row — the
# SonarQube/Artifactory widgets poll and 428 until a token exists, which otherwise fills
# the log with identical "failed" rows. Mutating requests that 428 are still recorded.
BENIGN_READ_STATUSES = {428}

# Paths whose noise outweighs their value.
#
#   * Health probes fire every few seconds from kubelet and would otherwise be the
#     single most common row in the table by an order of magnitude.
#   * The two observability beacons are POSTs, but they are not ACTIONS — the browser
#     fires them automatically on every dashboard mount, one per widget. Ten rows
#     every time anyone presses F5, saying nothing except "the dashboard rendered".
#     They are telemetry about the portal, and they already have their own tables.
#
# The rule this encodes: the log records what a PERSON did, not what their browser
# did on their behalf. Everything that fails still gets through — the 4xx/5xx path
# below is checked before this list is consulted for failures.
SKIP_PREFIXES = (
    "/static",
    "/favicon.ico",
    "/api/health",
    "/api/observability/widget",
    "/api/dashboard/widgets/sync",
)

# Bodies are never stored. Some of these routes carry a password or a PAT, and a log
# that quietly accumulates credentials is a breach waiting for someone to read it.
# We store the route, the outcome and who — never what they typed.


# What each route actually MEANS, in words.
#
# The middleware knows the route, not the intent — so on its own the log reads as a
# list of URLs, and "POST /api/support/tickets" is not what happened, it is how what
# happened was transported. A log nobody can read at a glance is a log nobody reads.
#
# Keyed by (METHOD, route template). Anything not listed falls back to a generic
# phrasing derived from the verb, so a new endpoint is never invisible — it just gets
# a duller sentence until someone adds it here.
FRIENDLY: Dict[tuple, str] = {
    ("POST", "/api/support/tickets"): "Opened a support ticket",
    ("POST", "/api/support/tickets/create-flow"): "Opened a support ticket",
    ("POST", "/api/support/tickets/reply"): "Replied to a support ticket",
    ("POST", "/api/approvals/requests"): "Requested a self-service automation",
    ("DELETE", "/api/approvals/requests/{request_id}"): "Withdrew a self-service request",
    ("POST", "/api/approvals/requests/{request_id}/approve"): "Approved a self-service request",
    ("POST", "/api/approvals/requests/{request_id}/reject"): "Rejected a self-service request",
    ("POST", "/api/approvals/requests/{request_id}/force-fail"): "Force-failed a self-service run",
    ("POST", "/api/azure-devops/projects/create"): "Created an Azure DevOps project",
    ("POST", "/api/azure-devops/projects"): "Created an Azure DevOps project",
    ("POST", "/api/azure-devops/pat"): "Connected Azure DevOps",
    ("DELETE", "/api/azure-devops/pat"): "Disconnected Azure DevOps",
    ("POST", "/api/integrations/{system}/token"): "Connected an integration",
    ("DELETE", "/api/integrations/{system}/token"): "Disconnected an integration",
    ("POST", "/api/admin/grant-role"): "Changed a user's role",
    ("POST", "/api/admin/quick-links"): "Added a quick link",
    ("PATCH", "/api/admin/quick-links/{quick_link_id}"): "Edited a quick link",
    ("DELETE", "/api/admin/quick-links/{quick_link_id}"): "Removed a quick link",
    ("POST", "/api/announcements"): "Posted an announcement",
    ("PATCH", "/api/announcements/{announcement_id}"): "Edited an announcement",
    ("DELETE", "/api/announcements/{announcement_id}"): "Deleted an announcement",
    ("DELETE", "/api/audit-logs"): "Cleared log entries",
    ("POST", "/api/admin/sso/config"): "Changed the SSO configuration",
    ("POST", "/api/admin/sso/test"): "Tested the SSO configuration",
    ("POST", "/api/safe-mode/toggle"): "Toggled Safe Mode",
    ("POST", "/api/suggestions"): "Posted a suggestion",
    ("PATCH", "/api/suggestions/{suggestion_id}"): "Responded to a suggestion",
    ("DELETE", "/api/suggestions/{suggestion_id}"): "Deleted a suggestion",
    ("POST", "/api/suggestions/{suggestion_id}/vote"): "Voted on a suggestion",
    ("DELETE", "/api/suggestions/{suggestion_id}/vote"): "Removed a vote",
    ("POST", "/api/suggestions/{suggestion_id}/comments"): "Commented on a suggestion",
    ("DELETE", "/api/suggestions/{suggestion_id}/comments/{comment_id}"): "Deleted a comment",
    ("POST", "/api/me/avatar"): "Changed their avatar",
    ("DELETE", "/api/me/avatar"): "Removed their avatar",
    ("PUT", "/api/me/display-name"): "Changed their display name",
    ("POST", "/api/favorites"): "Added a favorite",
    ("DELETE", "/api/favorites"): "Removed a favorite",
    ("POST", "/api/integrations/{system}/pins"): "Pinned an item",
    ("DELETE", "/api/integrations/{system}/pins"): "Unpinned an item",
    ("POST", "/ui/azure-devops/pat"): "Connected Azure DevOps",
    ("DELETE", "/ui/azure-devops/pat"): "Disconnected Azure DevOps",
    ("POST", "/ui/profile"): "Updated their profile",
    ("POST", "/ui/dashboard/preferences"): "Changed their dashboard layout",
}

# Preference writes fire constantly (a theme toggle, a density switch) and say nothing
# an operator needs. They are recorded — but at DEBUG, so they are out of the way
# unless somebody deliberately goes looking.
LOW_VALUE_ROUTES = {
    ("PUT", "/api/me/theme"),
    ("PUT", "/api/me/density"),
    ("POST", "/api/notifications/read-all"),
    # Reading an announcement is not an event, it is a person closing a banner.
    ("POST", "/api/announcements/{announcement_id}/dismiss"),
    ("POST", "/api/notifications/{notification_id}/read"),
    # A vote is a click, not an event an operator ever audits — and on a busy board it
    # is the single most frequent write there is. Recorded, but out of the way at DEBUG.
    ("POST", "/api/suggestions/{suggestion_id}/vote"),
    ("DELETE", "/api/suggestions/{suggestion_id}/vote"),
}

# GET is here so a FAILED read (the only kind that gets logged) reads as a sentence
# instead of a raw "GET /api/...". Successful reads are never recorded at all.
_VERBS = {"GET": "Viewed", "POST": "Created", "PUT": "Updated", "PATCH": "Updated", "DELETE": "Removed"}


def _describe(method: str, route: str) -> str:
    """A human sentence for a route, or a serviceable guess."""
    known = FRIENDLY.get((method, route))
    if known:
        return known
    verb = _VERBS.get(method)
    if not verb:
        return f"{method} {route}"
    # "/api/admin/sso/config" -> "sso config"
    parts = [p for p in route.split("/") if p and p != "api" and not p.startswith("{")]
    return f"{verb}: {' '.join(parts[-2:]) or route}"


def _level_for_status(status_code: int) -> str:
    if status_code >= 500:
        return audit.Level.ERROR
    if status_code >= 400:
        # A refusal IS worth seeing. 401/403 in particular are the shape of both an
        # expired token and someone probing, and you cannot tell which without a log.
        return audit.Level.WARNING
    return audit.Level.INFO


def _route_of(request: Request) -> str:
    """The route TEMPLATE, not the concrete path.

    "/api/admin/quick-links/{quick_link_id}" rather than ".../37". Otherwise every id
    becomes its own action and the filter dropdown is useless within a day.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)


def _user_email_of(request: Request) -> Optional[str]:
    """Best-effort identity from the auth cookie.

    Deliberately tolerant: an unauthenticated or malformed request still deserves a
    row — an anonymous one — because "somebody hit this endpoint without a token" is
    exactly the kind of thing you want the log to have kept.
    """
    token = request.cookies.get("auth_token")
    if not token:
        return None
    try:
        from security import decode_access_token

        payload = decode_access_token(token) or {}
        return payload.get("email") or payload.get("sub") or payload.get("username")
    except Exception:
        return None


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = request.client
    return client.host[:64] if client else None


# ── Collapsing repeated failures ──────────────────────────────────────────────
# A widget polls. When whatever it polls is broken, it fails on every poll, and the
# Logs page fills with hundreds of identical rows — same user, same route, same
# status — which is worse than useless: the one row that says something new is
# buried under a thousand that say the same thing.
#
# So an identical FAILING READ is written once per window per user, and the next
# row for that combination carries how many times it happened in between. Nothing
# is hidden — the count is on the row — and the log stays readable.
#
# Reads only. A failing WRITE is a distinct thing a person did and every one of
# them is recorded, however many there are.
_REPEAT_WINDOW_S = 300.0
_repeat_lock = threading.Lock()
_repeats: Dict[tuple, list] = {}   # key -> [first_logged_at, suppressed_count]
_REPEATS_MAX = 2000


def _repeat_decision(key: tuple, now: float) -> Optional[int]:
    """None to suppress this row; otherwise how many were suppressed before it.

    Bounded on purpose: the key includes the user, so an unbounded dict is a slow
    leak on a portal with hundreds of accounts. When it fills, the oldest half
    goes — losing a suppression count is a cosmetic loss, a growing dict is not.
    """
    with _repeat_lock:
        entry = _repeats.get(key)
        if entry is None or (now - entry[0]) >= _REPEAT_WINDOW_S:
            if len(_repeats) >= _REPEATS_MAX:
                for stale in sorted(_repeats, key=lambda k: _repeats[k][0])[: _REPEATS_MAX // 2]:
                    _repeats.pop(stale, None)
            suppressed = entry[1] if entry else 0
            _repeats[key] = [now, 0]
            return suppressed
        entry[1] += 1
        return None


class AuditMiddleware(BaseHTTPMiddleware):
    """Record every state-changing request, and every request that failed."""

    async def dispatch(self, request: Request, call_next):
        # `quiet` paths are exempt from routine logging, but NOT from failure logging.
        # A health probe returning 200 forever is noise; a health probe returning 500
        # is the most important row in the table that day.
        quiet = request.url.path.startswith(SKIP_PREFIXES)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The global handler in main.py turns this into a 500 for the caller, but
            # the exception is re-raised past us — so record it here, on every path,
            # or a crash is the one thing the log never sees.
            duration_ms = int((time.perf_counter() - started) * 1000)
            await self._record(request, 500, duration_ms)
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        status_code = response.status_code

        if quiet:
            interesting = status_code >= 500
        elif request.method not in MUTATING_METHODS and status_code in BENIGN_READ_STATUSES:
            interesting = False
        else:
            interesting = request.method in MUTATING_METHODS or status_code >= 400

        if interesting:
            await self._record(request, status_code, duration_ms)
        return response

    async def _record(self, request: Request, status_code: int, duration_ms: int) -> None:
        route = _route_of(request)
        method = request.method
        user = _user_email_of(request)

        suppressed = 0
        if status_code >= 400 and method not in MUTATING_METHODS:
            decision = _repeat_decision((user or "-", method, route, status_code), time.monotonic())
            if decision is None:
                return
            suppressed = decision

        metadata: Dict[str, Any] = {
            # The sentence the Logs page shows. The action stays the route, because that
            # is what you filter by; this is what you READ.
            "what": _describe(method, route),
            "method": method,
            "path": request.url.path,
            "status": status_code,
            "duration_ms": duration_ms,
        }
        ip = _client_ip(request)
        if ip:
            metadata["ip"] = ip

        # WHY it failed, not just that it did. Set by the exception handlers in
        # main.py, which are the only place the reason exists.
        if status_code >= 400:
            detail = getattr(getattr(request, "state", None), "audit_detail", None)
            if detail is not None:
                metadata["detail"] = str(detail)[:1000]
        if suppressed:
            metadata["repeated"] = suppressed + 1

        level = _level_for_status(status_code)
        # A successful preference write is noise; a FAILED one is not, so the downgrade
        # only applies while things are working.
        if level == audit.Level.INFO and (method, route) in LOW_VALUE_ROUTES:
            level = audit.Level.DEBUG

        # psycopg2 is synchronous. Called straight from this coroutine it would stall
        # the event loop — every write in the portal, once per request — so the insert
        # goes to the threadpool, the same place FastAPI already runs the `def` route
        # handlers that do the real work.
        await run_in_threadpool(
            audit.log_event,
            f"{method} {route}",
            level=level,
            source=audit.Source.HTTP,
            user_email=user,
            metadata=metadata,
        )


# ── Mirroring the application's own log records ────────────────────────────────
# A logging.Handler that writes WARNING+ into the same table. The obvious hazard is
# recursion: the handler writes to the DB, the DB write fails, db.py logs a warning
# about it, which reaches the handler, which writes to the DB… A thread-local
# re-entrancy guard is the whole defence, and audit.log_event swallowing its own
# failures silently is the other half of it.

_guard = threading.local()

# Loggers whose output is either already covered or is pure noise.
IGNORED_LOGGERS = (
    "uvicorn.access",  # one line per request; the middleware already has these
    "audit",           # would recurse
    "request_audit",   # would recurse
)


class DatabaseLogHandler(logging.Handler):
    """Mirror WARNING-and-above application log records into audit_events."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "busy", False):
            return
        if record.levelno < logging.WARNING:
            return
        if record.name.startswith(IGNORED_LOGGERS):
            return

        _guard.busy = True
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)

            metadata: Dict[str, Any] = {
                "logger": record.name,
                "message": message[:4000],
                "module": record.module,
                "line": record.lineno,
            }
            # An exception is the most useful thing in the row; keep the type and the
            # message, but not the traceback — a stack trace per row would make the
            # table unreadable and the JSONB column enormous.
            if record.exc_info and record.exc_info[0]:
                metadata["exception"] = record.exc_info[0].__name__
                metadata["exception_detail"] = str(record.exc_info[1])[:1000]

            audit.log_event(
                record.name,
                level=record.levelname,
                source=audit.Source.SYSTEM,
                metadata=metadata,
            )
        except Exception:
            # A logging handler that raises breaks the thing it was watching.
            pass
        finally:
            _guard.busy = False


def install(app: ASGIApp) -> None:
    """Wire both mechanisms into the application."""
    app.add_middleware(AuditMiddleware)

    handler = DatabaseLogHandler()
    handler.setLevel(logging.WARNING)

    root = logging.getLogger()
    # Idempotent: uvicorn's reloader can import this module more than once, and two
    # handlers would mean every warning appearing in the table twice.
    if not any(isinstance(h, DatabaseLogHandler) for h in root.handlers):
        root.addHandler(handler)
    _log.info("audit: request middleware and log mirror installed")
