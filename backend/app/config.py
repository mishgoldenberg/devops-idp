import os
from functools import lru_cache
from typing import List, Optional


class Settings:
    """Application settings loaded from environment variables."""

    # Core
    app_name: str = "DevOps Control Center API (Python)"
    host: str
    port: int
    environment: str

    # Database
    database_url: str

    # Redis
    redis_host: str
    redis_port: int
    redis_password: Optional[str]

    # Auth / JWT
    jwt_secret: str
    jwt_expiry: str

    # ServiceNow
    servicenow_instance: Optional[str]
    servicenow_user: Optional[str]
    servicenow_password: Optional[str]

    # CORS
    cors_origins: str

    def __init__(self) -> None:
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "8000"))
        self.environment = os.getenv("NODE_ENV", os.getenv("ENVIRONMENT", "development"))

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        self.database_url = db_url

        self.redis_host = os.getenv("REDIS_HOST", "redis")
        self.redis_port = int(os.getenv("REDIS_PORT", "6379"))
        self.redis_password = os.getenv("REDIS_PASSWORD")

        self.jwt_secret = os.getenv("JWT_SECRET", "change_this_in_production")
        # Keep same semantics as Node auth-service default
        self.jwt_expiry = os.getenv("JWT_EXPIRY", "8h")

        # ServiceNow
        self.servicenow_instance = os.getenv("SERVICENOW_INSTANCE")
        self.servicenow_user = os.getenv("SERVICENOW_USER")
        self.servicenow_password = os.getenv("SERVICENOW_PASSWORD")

        # CORS
        # Comma-separated list of origins, e.g. "http://localhost:3000,https://devops.internal.company"
        self.cors_origins = os.getenv("CORS_ORIGINS", "*")

    @property
    def cors_origins_list(self) -> List[str]:
        """
        Return CORS origins as a list suitable for FastAPI CORSMiddleware.
        "*" means allow all origins.
        """
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()


