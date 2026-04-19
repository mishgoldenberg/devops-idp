from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import health_check as db_health_check
from redis_client import health_check as redis_health_check


router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _render_health() -> JSONResponse:
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    overall = "healthy" if db_healthy and redis_healthy else "unhealthy"
    status_code = 200 if overall == "healthy" else 503
    payload = {
        "status": overall,
        "timestamp": _now_iso(),
        "services": {
            "database": "up" if db_healthy else "down",
            "redis": "up" if redis_healthy else "down",
        },
    }
    return JSONResponse(content=payload, status_code=status_code)


@router.get("", summary="Basic health check")
def health_root_noslash():
    """GET /api/health — combined DB + Redis liveness for external monitors."""
    return _render_health()


@router.get("/", summary="Basic health check")
def health_root():
    return _render_health()


@router.get("/ready", summary="Readiness probe")
def readiness():
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    if db_healthy and redis_healthy:
        return {"ready": True}
    return JSONResponse(content={"ready": False}, status_code=503)


@router.get("/live", summary="Liveness probe")
def liveness():
    return {"alive": True}


