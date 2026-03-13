import datetime as dt
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import get_settings


class AuthUser(Dict[str, Any]):
    """Simple dict-based representation of authenticated user payload."""


security_scheme = HTTPBearer(auto_error=False)


def _parse_expiry(expiry: str) -> dt.timedelta:
    """
    Parse expiry string like '8h', '15m', '1d' into a timedelta.
    Defaults to hours if no unit is provided.
    """
    try:
        if expiry.endswith("h"):
            hours = int(expiry[:-1])
            return dt.timedelta(hours=hours)
        if expiry.endswith("m"):
            minutes = int(expiry[:-1])
            return dt.timedelta(minutes=minutes)
        if expiry.endswith("d"):
            days = int(expiry[:-1])
            return dt.timedelta(days=days)
        # Fallback: treat as hours
        hours = int(expiry)
        return dt.timedelta(hours=hours)
    except Exception:
        # Safe default
        return dt.timedelta(hours=8)


def create_access_token(payload: Dict[str, Any]) -> str:
    """Create a JWT compatible with the Node auth-service contract."""
    settings = get_settings()
    expire_delta = _parse_expiry(settings.jwt_expiry)
    now = dt.datetime.utcnow()
    to_encode = payload.copy()
    to_encode["iat"] = int(now.timestamp())
    to_encode["exp"] = int((now + expire_delta).timestamp())
    return jwt.encode(to_encode, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> Dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> AuthUser:
    """FastAPI dependency that returns the authenticated user from JWT."""
    if credentials is None or not credentials.scheme.lower() == "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
        )
    token = credentials.credentials
    payload = decode_access_token(token)
    # Minimal validation
    required_keys = {"id", "username", "email", "role", "hierarchy_level", "permissions"}
    if not required_keys.issubset(payload.keys()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid auth payload",
        )
    # Normalize permissions to list[str]
    perms = payload.get("permissions") or []
    if isinstance(perms, str):
        perms = [perms]
    payload["permissions"] = perms
    return AuthUser(payload)


# ================================
# RBAC helpers (Python port)
# ================================


def has_role_level(user: AuthUser, required_level: int) -> bool:
    return int(user.get("hierarchy_level", 99)) <= required_level


def has_permission(user: AuthUser, permission: str) -> bool:
    permissions: List[str] = list(user.get("permissions") or [])
    if "*" in permissions:
        return True
    if permission in permissions:
        return True
    # wildcard like "view:*"
    for p in permissions:
        if p.endswith(":*"):
            prefix = p[:-1]
            if permission.startswith(prefix):
                return True
    return False


def can_view_observability(user: AuthUser) -> bool:
    """
    Observability and monitoring are restricted to Admins only.

    Frontend exposes three simplified roles:
      - Admin
      - TeamLead
      - User

    The underlying database roles are mapped to these effective roles in the auth payload.
    """
    return str(user.get("role")) == "Admin"


def can_view_aggregated_metrics(user: AuthUser) -> bool:
    # Platform Admin, Unit Commander, Branch Head (<= 3)
    return int(user.get("hierarchy_level", 99)) <= 3


def can_manage_users(user: AuthUser) -> bool:
    # Platform Admin only
    return int(user.get("hierarchy_level", 99)) == 1


