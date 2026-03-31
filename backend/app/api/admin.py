import re
from datetime import datetime, timezone
from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from db import execute, query_one
from security import AuthUser, get_current_user

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


def _require_app_admin(current_user: AuthUser) -> None:
    if str(current_user.get("role")) != "Admin":
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

    execute(
        """
        UPDATE users
        SET role_id = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        [admin_role["id"], target["id"]],
    )

    return {
        "success": True,
        "message": "User granted Admin permissions",
        "data": {"email": target["email"], "role": body.role},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
