import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException, status
import httpx
from pydantic import BaseModel, field_validator

from db import execute_returning, query_all, query_one
from security import AuthUser, get_current_user, has_effective_admin_access_live
from sso_config import (
    fetch_openid_configuration,
    get_sso_config_redacted,
    save_sso_config,
)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


class GrantRoleRequest(BaseModel):
    email: str
    role: Literal["Admin"]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        s = (v or "").strip()
        if not s or not _EMAIL_RE.match(s):
            raise ValueError("Invalid email format")
        return s.lower()


class SsoConfigRequest(BaseModel):
    issuer_uri: str
    client_id: str
    client_secret: str
    enabled: bool = True

    @field_validator("issuer_uri", "client_id", "client_secret")
    @classmethod
    def required_string(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Required")
        return s


def _require_app_admin(current_user: AuthUser) -> None:
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )


def _platform_admin_role() -> Dict[str, Any]:
    row = query_one(
        "SELECT id, name, hierarchy_level, permissions FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
        ["platform admin"],
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform Admin role is not configured",
        )
    return row


def _is_platform_admin_role_name(role_name: str) -> bool:
    return (role_name or "").strip().lower() == "platform admin"


@router.get("/sso/config")
def get_sso_config(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return SSO config metadata without the client secret."""
    _require_app_admin(current_user)
    return {
        "success": True,
        "data": get_sso_config_redacted(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/sso/test")
def test_sso_connection(
    body: SsoConfigRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Validate issuer discovery only; never stores or returns credentials."""
    _require_app_admin(current_user)
    try:
        fetch_openid_configuration(body.issuer_uri)
        return {
            "success": True,
            "message": "OpenID configuration is reachable and valid.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "success": False,
            "message": f"Connection failed: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


@router.post("/sso/config")
def save_admin_sso_config(
    body: SsoConfigRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Encrypt and save the active SSO configuration."""
    _require_app_admin(current_user)
    try:
        # Validate before saving so a typo cannot lock users into a bad provider.
        fetch_openid_configuration(body.issuer_uri)
        save_sso_config(
            issuer_uri=body.issuer_uri,
            client_id=body.client_id,
            client_secret=body.client_secret,
            enabled=body.enabled,
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SSO configuration is invalid: {exc}",
        )
    return {
        "success": True,
        "message": "SSO configuration saved.",
        "data": get_sso_config_redacted(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/users")
def list_users(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Return all active users who have logged in at least once.
    Excludes the current admin from the list. Admin-only.
    """
    _require_app_admin(current_user)

    rows = query_all(
        """
        SELECT u.email, COALESCE(u.full_name, u.email) AS display_name, r.name AS role_name
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.is_active = true
          AND LOWER(u.email) != %s
        ORDER BY u.email ASC
        """,
        [str(current_user.get("email", "")).strip().lower()],
    )

    users: List[Dict[str, str]] = [
        {
            "email": str(row["email"]),
            "display_name": str(row["display_name"]),
            "role_name": str(row["role_name"]),
        }
        for row in rows
    ]

    return {
        "success": True,
        "data": users,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/grant-role")
def grant_role(
    body: GrantRoleRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Promote an existing user to Admin (database role: Platform Admin).
    Caller must be an Admin. Unknown emails return 404 (users are created on first SSO login).
    """
    _require_app_admin(current_user)

    admin_role = _platform_admin_role()
    target = query_one(
        """
        SELECT u.id, u.email, r.name AS role_name
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE LOWER(u.email) = %s AND u.is_active = true
        """,
        [body.email],
    )
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found for this email",
        )

    if _is_platform_admin_role_name(str(target.get("role_name", ""))):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has Admin access",
        )

    updated = execute_returning(
        """
        UPDATE users
        SET role_id = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        RETURNING id
        """,
        [admin_role["id"], str(target["id"])],
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Role update failed — please try again",
        )

    return {
        "success": True,
        "message": f"Admin access granted to {target['email']}. They must sign out and sign back in for the change to take effect.",
        "data": {"email": target["email"], "role": body.role},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
