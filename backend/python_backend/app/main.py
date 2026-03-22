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

from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import api_router
from .config import get_settings
from .ui import ui_router


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
    )

    # Static and template directories (used by the HTMX-powered frontend)
    base_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    # Templates & static assets used by the HTMX-based UI.
    # Access templates from request.app.state.templates in route handlers.
    app.state.templates = Jinja2Templates(directory=base_dir / "templates")
    static_dir = base_dir / "static"

    # Make static assets available at /static
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # CORS Middleware
    # In docker-compose, frontend and backend run on the same network, so we're permissive in dev.
    # For production, configure specific allowed origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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


