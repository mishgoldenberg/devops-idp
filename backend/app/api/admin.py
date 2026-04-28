import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
import httpx
from pydantic import BaseModel, field_validator

from db import execute, execute_returning, query_all, query_one
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


class QuickLinkRequest(BaseModel):
    name: str
    url: str
    icon_url: Optional[str] = ""

    @field_validator("name", "url")
    @classmethod
    def required_string(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Required")
        return s

    @field_validator("icon_url")
    @classmethod
    def optional_string(cls, v: Optional[str]) -> str:
        return (v or "").strip()


class QuickLinkPatchRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    icon_url: Optional[str] = None

    @field_validator("name", "url", "icon_url")
    @classmethod
    def normalize_optional_string(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip()


def _require_app_admin(current_user: AuthUser) -> None:
    if not has_effective_admin_access_live(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )


def _quick_link_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "name": row.get("name") or "",
        "url": row.get("url") or "",
        "icon_url": row.get("icon_url") or "",
        "sort_order": int(row.get("sort_order") or 0),
        "is_active": bool(row.get("is_active")),
        "created_by": row.get("created_by") or "",
    }


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


@router.post("/quick-links")
def create_quick_link(
    body: QuickLinkRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Create a globally visible Quick Link. Admin-only on the backend.

    Idempotent on (name, url): a second submit with the same values — e.g.
    when the modal is double-clicked before the first POST returns — quietly
    returns the existing row instead of inserting a duplicate. The frontend
    also has an in-flight lock; this is the belt-and-suspenders backend half.
    """
    _require_app_admin(current_user)

    name = body.name.strip()
    url = body.url.strip()
    icon = (body.icon_url or "").strip() or None

    # Re-activate any previously soft-deleted row with the same identity, or
    # return the existing active row, before inserting a new one.
    existing = query_one(
        """
        SELECT id, name, url, icon_url, sort_order, is_active, created_by
        FROM quick_links
        WHERE LOWER(name) = LOWER(%s) AND LOWER(url) = LOWER(%s)
        ORDER BY is_active DESC, id ASC
        LIMIT 1
        """,
        [name, url],
    )
    if existing:
        if not existing.get("is_active") or (icon and existing.get("icon_url") != icon):
            existing = query_one(
                """
                UPDATE quick_links
                SET is_active = true,
                    icon_url = COALESCE(%s, icon_url)
                WHERE id = %s
                RETURNING id, name, url, icon_url, sort_order, is_active, created_by
                """,
                [icon, existing["id"]],
            )
        return {
            "success": True,
            "data": _quick_link_row(existing),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    max_order = query_one("SELECT COALESCE(MAX(sort_order), 0) AS n FROM quick_links")
    next_order = int((max_order or {}).get("n") or 0) + 10
    rows = execute_returning(
        """
        INSERT INTO quick_links (name, url, icon_url, sort_order, is_active, created_by)
        VALUES (%s, %s, %s, %s, true, %s)
        RETURNING id, name, url, icon_url, sort_order, is_active, created_by
        """,
        [
            name,
            url,
            icon,
            next_order,
            str(current_user.get("email") or current_user.get("username") or ""),
        ],
    )
    return {
        "success": True,
        "data": _quick_link_row(rows[0]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.patch("/quick-links/{quick_link_id}")
def update_quick_link(
    quick_link_id: int,
    body: QuickLinkPatchRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Update a Quick Link without changing visibility. Admin-only."""
    _require_app_admin(current_user)
    existing = query_one(
        "SELECT id FROM quick_links WHERE id = %s AND is_active = true",
        [quick_link_id],
    )
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quick link not found")
    current = query_one(
        "SELECT name, url, icon_url FROM quick_links WHERE id = %s",
        [quick_link_id],
    ) or {}
    name = body.name if body.name is not None else str(current.get("name") or "")
    url = body.url if body.url is not None else str(current.get("url") or "")
    icon_url = body.icon_url if body.icon_url is not None else str(current.get("icon_url") or "")
    if not name or not url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name and URL are required")
    rows = execute_returning(
        """
        UPDATE quick_links
        SET name = %s, url = %s, icon_url = %s
        WHERE id = %s
        RETURNING id, name, url, icon_url, sort_order, is_active, created_by
        """,
        [name, url, icon_url or None, quick_link_id],
    )
    return {
        "success": True,
        "data": _quick_link_row(rows[0]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.delete("/quick-links/{quick_link_id}")
def delete_quick_link(
    quick_link_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Soft-delete a Quick Link so existing IDs do not need to be reused."""
    _require_app_admin(current_user)
    execute(
        "UPDATE quick_links SET is_active = false WHERE id = %s",
        [quick_link_id],
    )
    return {
        "success": True,
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
