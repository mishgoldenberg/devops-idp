"""
DevOps Control Center API - FastAPI Application Factory

This module initializes the main FastAPI application with middleware, health checks,
and routing configuration.

ARCHITECTURE:
- Framework: FastAPI (async HTTP framework)
- Server: Uvicorn (ASGI server)
- Port: 8000 (configurable via PORT env var)

KEY COMPONENTS:
1. CORS Middleware - Allows frontend requests from any origin in dev mode
2. Health Checks - Kubernetes liveness & readiness probes
3. API Router - All business logic endpoints (azure_devops.py, auth.py, etc.)

HEALTH ENDPOINTS (FOR KUBERNETES):
- GET /api/health/live   - Liveness probe (pod alive, restart decision)
- GET /api/health/ready  - Readiness probe (pod ready for traffic)

ENVIRONMENT VARIABLES:
- PORT: Server port (default: 8000)
- DATABASE_URL: PostgreSQL connection string
- REDIS_HOST, REDIS_PORT, REDIS_PASSWORD: Cache configuration
- AZURE_DEVOPS_BASE_URL, AZURE_DEVOPS_ADMIN_PAT: Azure DevOps integration

STARTUP:
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

# Load .env files when present (local dev).  Earlier files take precedence;
# dotenv's load_dotenv() does NOT overwrite already-set env vars by default.
try:
    from pathlib import Path
    from dotenv import load_dotenv
    _repo_root = Path(__file__).resolve().parent.parent.parent  # backend/app -> repo root
    _backend_dir = _repo_root / "backend"
    # Highest priority first: dedicated backend .env, then the shared secrets .env
    load_dotenv(_backend_dir / ".env")
    load_dotenv(_backend_dir / "app" / ".env")
    load_dotenv(_repo_root / "infrastructure" / "k8s" / "base" / "secrets" / ".env")
except ImportError:
    pass

import logging
import threading
import time as _time_mod
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import api_router
from config import get_settings
import audit
import db
import catalog_forms
import changelog
import release_notes
import request_audit
import retention
import safe_mode
import streaks
import usage_tracking
import sso_config
from devbot import config as devbot_config
from devbot import store as devbot_store
from ui import ui_router


_log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
    )

    # Static and template directories (used by the HTMX-powered frontend)
    # Support both local layout (repo/backend/app/main.py) and container layout (/app/main.py).
    app_dir = Path(__file__).resolve().parent
    frontend_candidates = [
        app_dir / "frontend",
        app_dir.parent.parent / "frontend",
        app_dir.parent.parent.parent / "frontend",
    ]
    base_dir = next((p for p in frontend_candidates if p.exists()), frontend_candidates[0])
    # Templates & static assets used by the HTMX-based UI.
    # Access templates from request.app.state.templates in route handlers.
    app.state.templates = Jinja2Templates(directory=base_dir / "templates")
    # The version this image calls itself, as a Jinja GLOBAL rather than something
    # every route has to remember to put in its context. The sidebar reads it on every
    # page to decide whether to mark What's New as unread, and a page that forgot to
    # pass it would simply never show the mark -- a bug with no symptom, in the one
    # feature whose entire job is to be noticed.
    app.state.templates.env.globals["portal_version"] = changelog.version()
    # Everything the "+" button offers, derived from the catalogue rather than
    # written out in the banner. A global for the same reason the version is one:
    # the banner is on every page, and a route that forgot to pass it would show
    # an empty menu with nothing anywhere saying why.
    app.state.templates.env.globals["portal_quick_actions"] = catalog_forms.quick_actions()
    # Where people create their own AI model key, for the token guide on every page.
    app.state.templates.env.globals["devbot_key_help_url"] = devbot_config.key_help_url()
    static_dir = base_dir / "static"

    # Make static assets available at /static
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # CORS Middleware
    # Use configured CORS_ORIGINS (comma-separated) or "*" as a fallback.
    #
    # Credentials are only allowed when the origins are an explicit list. "*" plus
    # allow_credentials is a configuration that browsers reject anyway, and asking for
    # it invites someone to "fix" it by echoing the caller's origin back — which is how
    # a wildcard CORS hole gets created.
    allowed_origins = settings.cors_origins_list
    wildcard = allowed_origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=not wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── CSRF ────────────────────────────────────────────────────────────────────
    # Auth is an httpOnly `auth_token` COOKIE, which the browser attaches to any
    # request it makes to this origin — including one triggered by a page on a
    # different site. Without this guard, a page a signed-in user merely visits could
    # make their browser approve a request, delete a record or overwrite a stored
    # token, and the portal would honour it because the cookie rode along.
    #
    # The cookie is SameSite=Lax, which already blocks cross-site FORM posts. This
    # closes the rest: for every state-changing method, the request's Origin (or
    # Referer, for older clients) must match the host it is addressed to. Same-origin
    # requests — the whole portal — always carry a matching Origin, so nothing in the
    # app has to change. It also cannot be bypassed by a cross-site fetch: a browser
    # will not let script forge the Origin header.
    UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def _host_of(url: str) -> str:
        try:
            parsed = urlparse(url)
            return (parsed.netloc or "").lower()
        except Exception:
            return ""

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)

        # Only cookie-authenticated requests are forgeable this way. A caller using an
        # Authorization header (scripts, CI) has to supply the credential explicitly, so
        # a hostile page cannot make the browser do it for them.
        if not request.cookies.get("auth_token"):
            return await call_next(request)

        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        source = _host_of(origin) or _host_of(referer)

        # The host the request was actually addressed to, honouring the proxy header the
        # ingress sets — otherwise every request behind nginx looks like it was sent to
        # the pod's internal name and nothing would ever match.
        target = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or ""
        ).lower()

        allowed = {target} | {
            _host_of(o) for o in allowed_origins if o and o != "*"
        }
        allowed.discard("")

        if source and source not in allowed:
            _log.warning(
                "CSRF: blocked %s %s from origin %r (expected one of %s)",
                request.method, request.url.path, source, sorted(allowed),
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site request blocked."},
            )

        # A missing Origin AND Referer on a cookie-authed write is not a normal browser
        # request. Reject it rather than guess.
        if not source:
            _log.warning(
                "CSRF: blocked %s %s — no Origin or Referer",
                request.method, request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site request blocked."},
            )

        return await call_next(request)

    @app.middleware("http")
    async def portal_admin_nav_context(request: Request, call_next):
        """Expose shared nav context to Jinja templates without per-route boilerplate.

        Sets on ``request.state``:
          * ``portal_is_admin``    — boolean, used by sidebar to show Admin menu.
          * ``portal_system_urls`` — dict of external console URLs so every
            system page can render the top-right "Open" button without each
            route explicitly plumbing the config.
        """
        request.state.portal_is_admin = False
        token = request.cookies.get("auth_token")
        if token:
            try:
                from security import decode_access_token, AuthUser, has_effective_admin_access_live

                payload = decode_access_token(token)
                request.state.portal_is_admin = has_effective_admin_access_live(AuthUser(payload))
            except Exception:
                pass
        try:
            from api.system_urls import get_system_urls_for_template

            request.state.portal_system_urls = get_system_urls_for_template()
        except Exception:
            request.state.portal_system_urls = {}
        return await call_next(request)

    # ── Compression ─────────────────────────────────────────────────────────────
    # Nothing compressed anything: every page sent its stylesheet (180 KB) and the
    # shell's inline scripts raw, and the SonarQube snapshot is hundreds of KB of
    # JSON for four widgets. Text shrinks to a fifth or less. Below 1 KB it is not
    # worth the bytes of the gzip header. Outside everything but the audit log, which
    # reads status codes, never bodies.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    # ── Logging ─────────────────────────────────────────────────────────────────
    # Installed LAST, which makes it the OUTERMOST middleware — so it sees requests
    # the CSRF guard rejects and requests that fail before they reach a route. A log
    # that only sees what got through is missing precisely the events you go to a log
    # to find. It also mirrors the app's own WARNING+ log records into the same table,
    # which is what puts integration failures on the Logs page instead of in a pod's
    # stdout where nobody can reach them.
    request_audit.install(app)

    # Root endpoint (non-API) - Serves a small HTML landing page for HTMX-based UI.
    @app.get("/", include_in_schema=False)
    def root(request: Request):
        return RedirectResponse(url="/ui/auth")

    # Kubernetes probe endpoints live on the `health` router mounted at
    # /api/health (see api/health.py). The /live and /ready paths are kept
    # compatible there and must not be duplicated here — duplicate registrations
    # cause FastAPI's OpenAPI docs to become inconsistent.

    # ─── Global error boundary ────────────────────────────────────────────
    # Any unhandled exception is logged in full and surfaced as a generic,
    # user-friendly 500 so the frontend never sees raw stack traces or
    # internal tracebacks. Explicit HTTPExceptions and 422 validation errors
    # keep their original messages so clients still get actionable detail.

    # The audit middleware records the STATUS of a failed request but cannot see
    # WHY it failed — the reason lives in the exception, which is turned into a
    # response in here, below the middleware. So each handler leaves the reason on
    # request.state and the middleware picks it up. Without this a Logs page row
    # reads "Viewed: azure-devops projects — Failed · 502" and stops there, which
    # tells an operator that something is broken and nothing about what.
    def _remember(request: Request, detail) -> None:
        try:
            request.state.audit_detail = detail
        except Exception:
            pass

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 400:
            _remember(request, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        _remember(request, exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"success": False, "detail": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        _log.exception(
            "Unhandled exception during %s %s: %s",
            request.method, request.url.path, exc,
        )
        # The caller gets the generic message; the log gets the real one.
        _remember(request, f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "detail": "Something went wrong. Please try again.",
            },
        )

    @app.on_event("startup")
    def on_startup():
        # ── Schema bootstrap, OFF the startup path ───────────────────────
        # Everything below talks to Postgres. A startup hook blocks uvicorn from
        # serving *anything*, including /api/health/live, so running it inline
        # means a slow or unreachable database stops the pod from answering its
        # own liveness probe and the kubelet kills it — CrashLoopBackOff whose
        # logs show a database problem at best and nothing at all at worst,
        # because the very first connect is still hanging when SIGKILL lands.
        #
        # So: bind and serve immediately, do the DDL in the background, and
        # retry it. Readiness still gates real traffic on the DB (see
        # api/health.py), which is the correct place for that decision — the
        # pod stays up and NotReady, and says why, instead of restarting
        # forever.
        app.state.schema_ready = False
        app.state.schema_error = None

        def _check_credential_key() -> None:
            """Say so, loudly, if this pod cannot read the credentials in its own DB.

            JWT_SECRET is also the root of the key that encrypts every stored Azure
            DevOps PAT. When it does not match what the rows were written with, the
            portal behaves *almost* normally — people sign in, ServiceNow works, and
            only the Azure DevOps widgets are mysteriously empty for everybody at
            once. This turns that into one line at startup instead of a morning of
            tickets, and it is the check that tells you a restored database was
            restored next to the wrong secret.
            """
            app.state.credential_key_ok = None
            try:
                result = sso_config.credential_key_check()
            except Exception as exc:
                _log.warning("credential key self-check could not run: %s", exc)
                return

            app.state.credential_key_ok = result.get("ok")
            if result.get("ok") is False:
                _log.critical(
                    "JWT_SECRET DOES NOT MATCH THE STORED CREDENTIALS: %d of %d sampled "
                    "Azure DevOps tokens could not be decrypted. Every affected user's "
                    "widgets will be empty until they reconnect. This is what a rotated "
                    "JWT_SECRET, or a database restored beside a different environment's "
                    "secret, looks like. See docs/RUNBOOK.md.",
                    result.get("unreadable", 0), result.get("checked", 0),
                )
                audit.log_event(
                    "startup.credential_key_mismatch",
                    level=audit.Level.CRITICAL,
                    source=audit.Source.SYSTEM,
                    metadata={
                        "what": "Stored credentials cannot be decrypted with this JWT_SECRET",
                        "checked": result.get("checked", 0),
                        "unreadable": result.get("unreadable", 0),
                    },
                )
            elif result.get("checked"):
                _log.info(
                    "credential key self-check: %d stored credential(s) readable",
                    result.get("readable", 0),
                )

        def _bootstrap_schema() -> None:
            steps = (
                ("ensure_tables", db.ensure_tables),
                ("ensure_bootstrap_platform_admin", db.ensure_bootstrap_platform_admin),
                # Optional second local account, always non-admin. Re-applied on
                # every start like the admin one, so rotating the variable group
                # is a redeploy.
                ("ensure_bootstrap_service_user", db.ensure_bootstrap_service_user),
                # Runs after both bootstrap accounts exist, so the admin is already
                # on level 1 and is never caught by the demotion.
                ("collapse_non_admin_roles", db.collapse_non_admin_roles),
                # The backup CronJobs write their status rows here. Created by the
                # backend rather than by the jobs so the DDL lives in exactly one
                # place; the jobs treat a missing table as a loud warning and still
                # complete the dump, because a status row is not worth failing a
                # backup over.
                ("ensure_backup_runs_table", db.ensure_backup_runs_table),
                ("audit.ensure_table", audit.ensure_table),
                ("safe_mode.ensure_table", safe_mode.ensure_table),
                # "What's New". Runs LAST and inside the bootstrap, because it needs
                # its own table and it needs the database: the image knows which
                # version it is, and only the database knows whether THIS environment
                # has seen it before. A pod restart, a scale-up and a rollback all
                # correctly write nothing.
                ("release_notes.record", release_notes.record_current_deploy),
            )
            attempt = 0
            while True:
                attempt += 1
                failed = []
                for name, fn in steps:
                    try:
                        fn()
                    except Exception as exc:
                        failed.append(name)
                        # WARNING, not INFO: the root logger sits at WARNING, so
                        # anything quieter is invisible in the cluster exactly when
                        # it is needed.
                        _log.warning(
                            "startup step %s failed (attempt %d): %s", name, attempt, exc
                        )
                if not failed:
                    app.state.schema_ready = True
                    app.state.schema_error = None
                    _log.info("schema bootstrap complete (attempt %d)", attempt)
                    _check_credential_key()
                    return
                app.state.schema_error = ", ".join(failed)
                # Never give up. Readiness is gated on this, so a thread that
                # stopped retrying would leave the pod permanently NotReady even
                # after the database came back — the outage would outlive its own
                # cause. Back off to once a minute and keep going; log the first
                # few attempts, then every tenth, so a long outage does not bury
                # everything else in the log.
                if attempt <= 5 or attempt % 10 == 0:
                    _log.warning(
                        "schema bootstrap still failing after %d attempt(s) (%s); "
                        "pod stays up and NotReady - check DATABASE_URL and that "
                        "Postgres is reachable from this namespace",
                        attempt,
                        app.state.schema_error,
                    )
                _time_mod.sleep(min(60, 2 * attempt))

        try:
            threading.Thread(
                target=_bootstrap_schema, name="schema-bootstrap", daemon=True
            ).start()
        except Exception as exc:  # pragma: no cover - thread creation cannot realistically fail
            _log.warning("could not start schema bootstrap thread: %s", exc)
            _bootstrap_schema()

        # ── Audit log retention ──────────────────────────────────────────
        # Purge rows older than AUDIT_RETENTION_DAYS (default 7). We run one
        # pass immediately so freshly-started pods don't display stale rows
        # from a previous incarnation, then loop every 6 hours. Using a
        # daemon thread keeps it simple and safe: the interpreter exit
        # reaps it, and we never block request handling.
        #
        # `threading` / `time` are imported at module scope: a function-local
        # `import threading` here would make the name local to the WHOLE function,
        # so the schema-bootstrap thread above — which runs earlier in the same
        # function — would die on UnboundLocalError.

        def _audit_retention_loop() -> None:
            interval_seconds = 6 * 60 * 60
            while True:
                try:
                    audit.delete_older_than(audit.AUDIT_RETENTION_DAYS)
                except Exception as exc:
                    _log.warning("audit retention sweep failed: %s", exc)
                try:
                    # Same loop, one extra cheap query: the Postgres volume cannot be
                    # enlarged by redeploying, so filling it has to be seen coming.
                    audit.check_database_size()
                except Exception as exc:
                    _log.warning("database size check failed: %s", exc)
                try:
                    # Finished requests, after a year. Same loop for the same reason:
                    # approval_requests and catalog_submissions only ever grow, on a
                    # volume that cannot be resized. Cleaners are excluded -- their
                    # request row is what the portal reads to change or remove a
                    # CronJob that is still running every night.
                    retention.delete_old_requests()
                except Exception as exc:
                    _log.warning("request retention sweep failed: %s", exc)
                try:
                    # Usage sessions and per-day activity, which grow with every minute
                    # anybody has the portal open.
                    usage_tracking.prune()
                except Exception as exc:
                    _log.warning("usage retention sweep failed: %s", exc)
                try:
                    # Streak days older than any streak can reach back.
                    streaks.prune()
                except Exception as exc:
                    _log.warning("streak retention sweep failed: %s", exc)
                try:
                    # DevBot conversations nobody has opened for DEVBOT_HISTORY_DAYS,
                    # and the usage rows older than the Usage view reads.
                    devbot_store.purge(devbot_config.history_days())
                except Exception as exc:
                    _log.warning("DevBot retention sweep failed: %s", exc)
                _time_mod.sleep(interval_seconds)

        try:
            t = threading.Thread(
                target=_audit_retention_loop,
                name="audit-retention",
                daemon=True,
            )
            t.start()
            _log.info(
                "audit retention sweeper started (every 6h, %d day cutoff)",
                audit.AUDIT_RETENTION_DAYS,
            )
        except Exception as exc:
            _log.warning("could not start audit retention thread: %s", exc)

    # Include all API routers (azure_devops, auth, approvals, etc.)
    app.include_router(api_router)

    # UI router (HTMX-powered HTML endpoints)
    app.include_router(ui_router)
    return app


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# Application instance (entry point for uvicorn)
app = create_app()
