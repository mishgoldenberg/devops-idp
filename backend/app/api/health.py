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
    """
    Kubernetes marks the pod Ready only on HTTP 2xx.

    We require **PostgreSQL** here — without the DB the API cannot serve
    meaningful traffic. **Redis** is best-effort (cache); a down Redis
    degrades performance but should not block rollout, otherwise clusters
    where Redis is still starting or temporarily unreachable wedge every
    backend pod in NotReady forever.

    For a strict DB+Redis check (e.g. synthetic monitors), use GET /api/health
    which returns 503 if either dependency is down.
    """
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    if not db_healthy:
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "services": {
                    "database": "down",
                    "redis": "up" if redis_healthy else "down",
                },
            },
        )
    return {
        "ready": True,
        "services": {
            "database": "up",
            "redis": "up" if redis_healthy else "down",
        },
    }


@router.get("/live", summary="Liveness probe")
def liveness():
    return {"alive": True}


