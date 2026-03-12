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

    # OAuth / OIDC (Google SSO)
    oauth_client_id: Optional[str]
    oauth_client_secret: Optional[str]
    oauth_redirect_uri: Optional[str]
    oauth_issuer: str
    oauth_auth_url: str
    oauth_token_url: str
    oauth_jwks_url: str
    oauth_scopes: str

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

        # OAuth / OIDC (Google accounts)
        self.oauth_client_id = os.getenv("OAUTH_CLIENT_ID")
        self.oauth_client_secret = os.getenv("OAUTH_CLIENT_SECRET")
        self.oauth_redirect_uri = os.getenv("OAUTH_REDIRECT_URI")
        self.oauth_issuer = os.getenv("OAUTH_ISSUER", "https://accounts.google.com")
        self.oauth_auth_url = os.getenv("OAUTH_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
        self.oauth_token_url = os.getenv("OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token")
        self.oauth_jwks_url = os.getenv("OAUTH_JWKS_URL", "https://www.googleapis.com/oauth2/v3/certs")
        self.oauth_scopes = os.getenv("OAUTH_SCOPES", "openid email profile")

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


