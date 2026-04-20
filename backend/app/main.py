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
- AZURE_DEVOPS_PAT, AZURE_DEVOPS_API_URL: Azure DevOps integration
- USE_MOCK_AZURE_DEVOPS: Enable mock implementations for dev

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

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import api_router
from config import get_settings
import audit
import db
import safe_mode
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
    static_dir = base_dir / "static"

    # Make static assets available at /static
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # CORS Middleware
    # Use configured CORS_ORIGINS (comma-separated) or "*" as a fallback.
    allowed_origins = settings.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
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
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "detail": "Something went wrong. Please try again.",
            },
        )

    @app.on_event("startup")
    def on_startup():
        try:
            db.ensure_tables()
        except Exception as exc:
            _log.warning("ensure_tables() failed: %s", exc)
        try:
            db.ensure_bootstrap_platform_admin()
        except Exception as exc:
            _log.warning("ensure_bootstrap_platform_admin() failed: %s", exc)
        try:
            audit.ensure_table()
        except Exception as exc:
            _log.warning("audit.ensure_table() failed: %s", exc)
        try:
            safe_mode.ensure_table()
        except Exception as exc:
            _log.warning("safe_mode.ensure_table() failed: %s", exc)

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
