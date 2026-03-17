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

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import api_router
from config import get_settings
import db


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
    )

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

    # Root endpoint (non-API, for basic connectivity check)
    @app.get("/")
    def root():
        return {
            "name": "DevOps Control Center API (Python)",
            "version": "1.0.0",
            "status": "running",
            "timestamp": _now_iso(),
        }

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

    # Include all API routers (azure_devops, auth, approvals, etc.)
    app.include_router(api_router)

    return app


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# Application instance (entry point for uvicorn)
app = create_app()
