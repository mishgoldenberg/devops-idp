import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db import health_check as db_health_check
from redis_client import health_check as redis_health_check


router = APIRouter()

# The image tag this pod was built from, injected by the chart from
# `.Values.image.tag`. "unknown" when running outside Kubernetes (docker compose,
# a local uvicorn) — it identifies a release, so there is nothing to report when
# there is no release.
APP_BUILD = os.getenv("APP_BUILD", "unknown")


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
def readiness(request: Request):
    """
    Kubernetes marks the pod Ready only on HTTP 2xx.

    We require **PostgreSQL** here — without the DB the API cannot serve
    meaningful traffic. **Redis** is best-effort (cache); a down Redis
    degrades performance but should not block rollout, otherwise clusters
    where Redis is still starting or temporarily unreachable wedge every
    backend pod in NotReady forever.

    The response names what is wrong. `oc get pods` can only say NotReady;
    this is where an operator finds out whether that means "Postgres is
    unreachable" or "the schema bootstrap has not finished yet", without
    having to read pod logs to tell two very different problems apart.

    For a strict DB+Redis check (e.g. synthetic monitors), use GET /api/health
    which returns 503 if either dependency is down.
    """
    db_healthy = db_health_check()
    redis_healthy = redis_health_check()
    schema_ready = bool(getattr(request.app.state, "schema_ready", True))
    schema_error = getattr(request.app.state, "schema_error", None)

    services = {
        "database": "up" if db_healthy else "down",
        "redis": "up" if redis_healthy else "down",
        "schema": "ready" if schema_ready else "pending",
    }

    # Informational, and deliberately NOT part of the ready/not-ready decision. A
    # JWT_SECRET that cannot decrypt the stored PATs is a serious problem, but it
    # is not one a rollout can fix — refusing readiness would take the portal down
    # and remove the very screen people need to reconnect. It belongs where an
    # operator looks when something is odd, which is this payload.
    credential_key_ok = getattr(request.app.state, "credential_key_ok", None)
    if credential_key_ok is False:
        services["credentials"] = "unreadable"
    elif credential_key_ok is True:
        services["credentials"] = "ok"
    if not db_healthy or not schema_ready:
        payload = {"ready": False, "services": services}
        if schema_error:
            payload["schema_error"] = schema_error
        if not db_healthy:
            payload["detail"] = (
                "PostgreSQL is not reachable from this pod. Check DATABASE_URL "
                "and that the Postgres service accepts connections from this "
                "namespace."
            )
        elif not schema_ready:
            payload["detail"] = (
                "Startup schema bootstrap has not completed yet; it retries in "
                "the background."
            )
        return JSONResponse(status_code=503, content=payload)
    return {"ready": True, "services": services}


@router.get("/live", summary="Liveness probe")
def liveness():
    """
    Deliberately dependency-free.

    Liveness answers one question — should the kubelet RESTART this container —
    and a restart cannot fix an unreachable database. Touching the DB here is
    what turns an outage into a crash loop. Readiness above is where dependency
    state belongs.

    It also reports which build is answering. That single field is what makes
    "did my change actually deploy?" a question with an answer: the pipeline asks
    the public hostname for it after every deploy and fails if it is not the tag
    it just built. Production ran an older image than test for weeks behind a
    pipeline that reported success every time, because `oc apply` returns 0 when
    it changes nothing and nobody was checking the other end.

    Unauthenticated on purpose — it is a probe endpoint, and a build number is
    not a secret. It reveals nothing an attacker cannot infer from the pages.
    """
    return {"alive": True, "build": APP_BUILD}


