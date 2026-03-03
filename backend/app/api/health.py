from fastapi import APIRouter

from ..db import health_check as db_health_check
from ..redis_client import health_check as redis_health_check


router = APIRouter()


@router.get("/", summary="Basic health check")
def health_root():
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    status = "healthy" if db_healthy and redis_healthy else "unhealthy"
    status_code = 200 if status == "healthy" else 503
    return {
        "status": status,
        "timestamp": _now_iso(),
        "services": {
            "database": "up" if db_healthy else "down",
            "redis": "up" if redis_healthy else "down",
        },
    }, status_code


@router.get("/ready", summary="Readiness probe")
def readiness():
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    if db_healthy and redis_healthy:
        return {"ready": True}
    return {"ready": False}


@router.get("/live", summary="Liveness probe")
def liveness():
    return {"alive": True}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


