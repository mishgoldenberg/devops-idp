import os
import logging
from functools import lru_cache
from typing import List, Optional

log = logging.getLogger(__name__)


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

    # External integrations
    azure_devops_base_url: Optional[str]
    azure_devops_admin_pat: Optional[str]
    artifactory_base_url: Optional[str]
    sonarqube_base_url: Optional[str]

    # ServiceNow
    snow_base_url: Optional[str]
    snow_api_username: Optional[str]
    snow_api_password: Optional[str]

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

        self.jwt_secret = os.getenv("JWT_SECRET", "")
        if not self.jwt_secret:
            log.error("Missing critical environment variable: JWT_SECRET")
        # Keep same semantics as Node auth-service default
        self.jwt_expiry = os.getenv("JWT_EXPIRY", "8h")

        # External integrations
        self.azure_devops_base_url = os.getenv("AZURE_DEVOPS_BASE_URL")
        self.azure_devops_admin_pat = os.getenv("AZURE_DEVOPS_ADMIN_PAT")
        self.artifactory_base_url = os.getenv("ARTIFACTORY_BASE_URL")
        self.sonarqube_base_url = os.getenv("SONARQUBE_BASE_URL")

        # ServiceNow
        self.snow_base_url = os.getenv("SNOW_BASE_URL")
        self.snow_api_username = os.getenv("SNOW_API_USERNAME")
        self.snow_api_password = os.getenv("SNOW_API_PASSWORD")

        for name, value in {
            "HUB_ADMIN_USERNAME": os.getenv("HUB_ADMIN_USERNAME"),
            "HUB_ADMIN_PASSWORD": os.getenv("HUB_ADMIN_PASSWORD"),
            "AZURE_DEVOPS_BASE_URL": self.azure_devops_base_url,
            "AZURE_DEVOPS_ADMIN_PAT": self.azure_devops_admin_pat,
            "SNOW_BASE_URL": self.snow_base_url,
            "SNOW_API_USERNAME": self.snow_api_username,
            "SNOW_API_PASSWORD": self.snow_api_password,
            "ARTIFACTORY_BASE_URL": self.artifactory_base_url,
            "SONARQUBE_BASE_URL": self.sonarqube_base_url,
        }.items():
            if not value:
                log.error("Missing critical environment variable: %s", name)

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


