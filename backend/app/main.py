from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import api_router
from .config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
    )

    # CORS – mirror existing gateway behavior of allowing configured origins.
    # In docker-compose, the frontend and backend run on the same network,
    # so we can be permissive in dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Root endpoint (non-API)
    @app.get("/")
    def root():
        return {
            "name": "DevOps Control Center API (Python)",
            "version": "1.0.0",
            "status": "running",
            "timestamp": _now_iso(),
        }

    app.include_router(api_router)
    return app


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


app = create_app()


