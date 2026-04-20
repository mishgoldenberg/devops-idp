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

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api import api_router
from config import get_settings
import db
from ui import ui_router


# How often the audit-log retention sweep runs in the background. Hourly is
# frequent enough that we never hold more than an hour's worth of rows older
# than the retention window, but rare enough that we don't pressure the DB.
AUDIT_CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
AUDIT_RETENTION_DAYS = 7


def _start_audit_retention_worker() -> None:
    """Start a daemon thread that periodically prunes old audit_logs rows.

    We intentionally use threading.Timer (not APScheduler / asyncio) to avoid
    pulling in a new dependency and to keep the worker self-contained — it
    dies with the process and doesn't interact with the FastAPI event loop.
    """
    import logging
    import threading

    log = logging.getLogger(__name__)

    def _run() -> None:
        try:
            deleted = db.cleanup_old_audit_logs(AUDIT_RETENTION_DAYS)
            if deleted:
                log.info("audit_logs retention: deleted %d rows older than %dd",
                         deleted, AUDIT_RETENTION_DAYS)
        except Exception as exc:
            log.warning("audit_logs retention sweep failed: %s", exc)
        finally:
            t = threading.Timer(AUDIT_CLEANUP_INTERVAL_SECONDS, _run)
            t.daemon = True
            t.start()

    # Run once on startup (so a pod that was down during the daily window
    # still catches up), then reschedule itself.
    _run()


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

    # Kubernetes PROBE ENDPOINTS
    # These are essential for Kubernetes orchestration:
    # - Liveness: Determines if pod should be restarted
    # - Readiness: Determines if pod should receive traffic
    @app.get("/api/health/live", status_code=status.HTTP_200_OK)
    def liveness_probe():
        """
        Kubernetes Liveness Probe
        
        Returns 200 if the pod is alive and functioning.
        If this endpoint dies/hangs, Kubernetes will restart the pod.
        Should be lightweight - just check that the process is running.
        """
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "alive", "timestamp": _now_iso()}
        )

    @app.get("/api/health/ready", status_code=status.HTTP_200_OK)
    def readiness_probe():
        """
        Kubernetes Readiness Probe
        
        Returns 200 if the pod is ready to serve traffic.
        Failed readiness checks remove the pod from load balancers temporarily.
        
        ENHANCEMENT TODO: Add checks for:
        - Database connectivity (psycopg2 connection test)
        - Redis connectivity (redis-py ping test)
        - External system availability (Azure DevOps API)
        """
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready", "timestamp": _now_iso()}
        )

    @app.on_event("startup")
    def on_startup():
        try:
            db.ensure_tables()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("ensure_tables() failed: %s", exc)
        try:
            db.ensure_bootstrap_platform_admin()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("ensure_bootstrap_platform_admin() failed: %s", exc)
        # Kick off the audit-log retention sweep. Must run AFTER ensure_tables
        # because the audit_logs table is created there on a fresh install.
        try:
            _start_audit_retention_worker()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("audit retention worker failed to start: %s", exc)

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
