"""The FastAPI application: middleware, error handlers, background jobs and routers.

Run locally with ``uvicorn main:app --reload --port 8000`` from backend/app.
"""

# Local development reads .env files; nothing overrides a variable already set.
try:
    from pathlib import Path
    from dotenv import load_dotenv

    _backend_dir = Path(__file__).resolve().parent.parent
    load_dotenv(_backend_dir / ".env")
    load_dotenv(_backend_dir / "app" / ".env")
except ImportError:
    pass

import hashlib
import logging
import os
import threading
import time as _time_mod
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
from devbot import knowledge as devbot_knowledge
from devbot import monitor as devbot_monitor
from devbot import store as devbot_store
from ui import ui_router


_log = logging.getLogger(__name__)

# The files every page links, fingerprinted together (asset_v).
_FINGERPRINTED = ("css/output.css", "css/theme.css", "js/htmx.min.js")


def static_fingerprint(static_dir) -> str:
    digest = hashlib.sha256()
    for name in _FINGERPRINTED:
        try:
            digest.update((static_dir / name).read_bytes())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:12]


class CachedStaticFiles(StaticFiles):
    """Static files the browser may keep: a fingerprinted link (?v=) for a year,
    anything else for an hour and then revalidated in the background."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            versioned = b"v=" in (scope.get("query_string") or b"")
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if versioned
                else "public, max-age=3600, stale-while-revalidate=604800"
            )
        return response


# Where a validation error is located, not which field: never shown to anyone.
_LOCATIONS = {"body", "query", "path", "header", "cookie"}


def validation_message(errors) -> str:
    """One sentence for a request that failed validation.

    Names the field and says what is wrong with it -- never the type, the rule's
    parameters or the value that was sent. A ValueError's message is our own
    sentence (common.safe_image), so it is passed through."""
    parts = []
    for error in errors or []:
        field = next((str(p) for p in reversed(error.get("loc") or ()) if str(p) not in _LOCATIONS
                      and not isinstance(p, int)), "")
        kind = str(error.get("type") or "")
        if kind == "value_error":
            reason = str(error.get("msg") or "").removeprefix("Value error, ")
            parts.append(f"{field}: {reason}" if field else reason)
        elif kind == "missing":
            parts.append(f"{field} is required" if field else "A required value is missing")
        else:
            parts.append(f"{field} is not valid" if field else "A value is not valid")
    unique = list(dict.fromkeys(p.rstrip(".") for p in parts))
    return ("The request was not valid: " + "; ".join(unique) + ".") if unique else "The request was not valid."


def _widen_threadpool() -> None:
    """Most routes are plain `def`s that wait on another system; with the default 40
    threads, forty slow calls queue every page render behind them. Waiting threads
    are cheap, and the database is protected by its own pool."""
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = int(os.getenv("WORKER_THREADS", "100"))


def _start_background_work(app: FastAPI) -> None:
    # Database work runs in a background thread and retries forever: a startup hook
    # blocks uvicorn from answering even its liveness probe, so a slow database
    # would become a CrashLoopBackOff. Readiness reports the pending state instead.
    app.state.schema_ready = False
    app.state.schema_error = None

    def _check_credential_key() -> None:
        """Say loudly if JWT_SECRET cannot decrypt the stored credentials -- what a
        rotated secret, or a database restored beside another environment's, looks
        like. Otherwise only the Azure DevOps widgets go quietly empty."""
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
            _log.info("credential key self-check: %d stored credential(s) readable", result.get("readable", 0))

    def _bootstrap_schema() -> None:
        steps = (
            ("ensure_tables", db.ensure_tables),
            ("ensure_bootstrap_platform_admin", db.ensure_bootstrap_platform_admin),
            ("ensure_bootstrap_service_user", db.ensure_bootstrap_service_user),
            # After both bootstrap accounts, so the admin is never demoted.
            ("collapse_non_admin_roles", db.collapse_non_admin_roles),
            ("ensure_backup_runs_table", db.ensure_backup_runs_table),
            ("audit.ensure_table", audit.ensure_table),
            ("safe_mode.ensure_table", safe_mode.ensure_table),
            # Last: records the version this environment is now running (What's New).
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
                    _log.warning("startup step %s failed (attempt %d): %s", name, attempt, exc)
            if not failed:
                app.state.schema_ready = True
                app.state.schema_error = None
                _log.info("schema bootstrap complete (attempt %d)", attempt)
                _check_credential_key()
                return
            app.state.schema_error = ", ".join(failed)
            # Never give up: readiness is gated on this, so stopping would leave the
            # pod NotReady after the database came back.
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
        threading.Thread(target=_bootstrap_schema, name="schema-bootstrap", daemon=True).start()
    except Exception as exc:  # pragma: no cover - thread creation cannot realistically fail
        _log.warning("could not start schema bootstrap thread: %s", exc)
        _bootstrap_schema()

    # Retention, every 6 hours, starting now. `threading` and `time` are imported at
    # module scope: a local import here would make them local to the whole function
    # and break the bootstrap thread above.
    def _audit_retention_loop() -> None:
        sweeps = (
            ("audit retention", lambda: audit.delete_older_than(audit.AUDIT_RETENTION_DAYS)),
            # The Postgres volume cannot grow by redeploying; filling it must be seen coming.
            ("database size check", audit.check_database_size),
            # Finished requests after a year (never a live cleaner's only record).
            ("request retention", retention.delete_old_requests),
            ("usage retention", usage_tracking.prune),
            ("streak retention", streaks.prune),
            ("DevBot retention", lambda: (devbot_store.purge(devbot_config.history_days()),
                                          devbot_monitor.purge())),
        )
        while True:
            for name, sweep in sweeps:
                try:
                    sweep()
                except Exception as exc:
                    _log.warning("%s sweep failed: %s", name, exc)
            _time_mod.sleep(6 * 60 * 60)

    try:
        threading.Thread(target=_audit_retention_loop, name="audit-retention", daemon=True).start()
        _log.info("audit retention sweeper started (every 6h, %d day cutoff)", audit.AUDIT_RETENTION_DAYS)
    except Exception as exc:
        _log.warning("could not start audit retention thread: %s", exc)

    # DevBot's page index catches up with edits by itself; a Redis lock lets one pod
    # build at a time, and nothing happens until an admin has set the index up.
    if not devbot_config.switched_on():
        _log.warning("HUB_AI_ENABLED is false: DevBot and AdminBot are off in this deployment")
        return
    try:
        threading.Thread(target=devbot_knowledge.schedule_loop, name="devbot-index-schedule", daemon=True).start()
    except Exception as exc:
        _log.warning("could not start the DevBot index schedule: %s", exc)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _widen_threadpool()
    _start_background_work(app)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    # No /docs, /redoc or /openapi.json: a public map of every endpoint helps nobody
    # who uses the portal. docs/API_REFERENCE.md is generated from app.openapi().
    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=_lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # The frontend sits beside backend/ locally and inside /app in the image.
    app_dir = Path(__file__).resolve().parent
    frontend_candidates = [
        app_dir / "frontend",
        app_dir.parent.parent / "frontend",
        app_dir.parent.parent.parent / "frontend",
    ]
    base_dir = next((p for p in frontend_candidates if p.exists()), frontend_candidates[0])
    app.state.templates = Jinja2Templates(directory=base_dir / "templates")
    templates_env = app.state.templates.env
    # Templates ship in the image and never change while it runs; TEMPLATES_AUTO_RELOAD=1
    # makes Jinja re-read them when editing locally.
    templates_env.auto_reload = os.getenv("TEMPLATES_AUTO_RELOAD", "").strip().lower() in ("1", "true", "yes")
    # Globals rather than per-route context: the banner and sidebar are on every page,
    # and a route that forgot to pass one of these would fail silently.
    templates_env.globals["portal_version"] = changelog.version()
    templates_env.globals["portal_quick_actions"] = catalog_forms.quick_actions()
    templates_env.globals["devbot_key_help_url"] = devbot_config.key_help_url()
    templates_env.globals["devbot_enabled"] = devbot_config.enabled()
    templates_env.globals["ai_enabled"] = devbot_config.switched_on()
    static_dir = base_dir / "static"
    templates_env.globals["asset_v"] = static_fingerprint(static_dir)
    # The dotted sidebar pages by path, so a page that came from a prefetch can say it
    # was visited (portal-chrome.html).
    from api.notifications import SECTIONS as _dotted_sections

    templates_env.globals["seen_sections"] = {path: key for key, path in _dotted_sections.items()}
    templates_env.globals["credits_image"] = (static_dir / "images" / "credits.png").exists()

    app.mount("/static", CachedStaticFiles(directory=static_dir), name="static")

    # Credentials only with an explicit origin list: "*" with credentials is refused by
    # browsers, and "fixing" it by echoing the caller's origin is a CORS hole.
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
    # The session is a cookie, which the browser attaches to requests a page on another
    # site triggers. SameSite=Lax stops cross-site form posts; this stops the rest: a
    # cookie-authenticated write must carry an Origin (or Referer) naming this host.
    UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def _host_of(url: str) -> str:
        try:
            return (urlparse(url).netloc or "").lower()
        except Exception:
            return ""

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in UNSAFE_METHODS or not request.cookies.get("auth_token"):
            return await call_next(request)

        source = _host_of(request.headers.get("origin") or "") or _host_of(request.headers.get("referer") or "")
        # The host the browser addressed, as the ingress forwards it.
        target = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
        allowed = {target} | {_host_of(o) for o in allowed_origins if o and o != "*"}
        allowed.discard("")

        if not source or source not in allowed:
            _log.warning("CSRF: blocked %s %s from origin %r (expected one of %s)",
                         request.method, request.url.path, source or None, sorted(allowed))
            return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked."})
        return await call_next(request)

    @app.middleware("http")
    async def portal_admin_nav_context(request: Request, call_next):
        """Put portal_is_admin and portal_system_urls on request.state for the page
        templates. Pages only: static files and /api calls render no sidebar."""
        request.state.portal_is_admin = False
        path = request.url.path
        if path.startswith("/static/") or path.startswith("/api/"):
            request.state.portal_system_urls = {}
            return await call_next(request)
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

    # Level 2: on a pod's CPU level 5 cost three times the time for a tenth fewer bytes.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=2)

    # Installed last, so it is the OUTERMOST middleware and also records requests the
    # CSRF guard rejects and ones that fail before reaching a route.
    request_audit.install(app)

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        return RedirectResponse(url="/ui/auth")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static_dir / "images" / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=86400"})

    # ── Errors ────────────────────────────────────────────────────────────────────
    # The audit middleware sees a failed request's status but not its reason, which
    # lives in the exception; each handler leaves the reason on request.state for it.
    def _remember(request: Request, detail) -> None:
        try:
            request.state.audit_detail = detail
        except Exception:
            pass

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 400:
            _remember(request, exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"success": False, "detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        # The full errors go to the Logs page; the caller gets one plain sentence.
        _remember(request, [{k: e.get(k) for k in ("type", "loc", "msg")} for e in exc.errors()])
        return JSONResponse(
            status_code=422,
            content={"success": False, "detail": validation_message(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        _log.exception("Unhandled exception during %s %s: %s", request.method, request.url.path, exc)
        _remember(request, f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "detail": "Something went wrong. Please try again."},
        )

    app.include_router(api_router)
    app.include_router(ui_router)
    return app


app = create_app()
