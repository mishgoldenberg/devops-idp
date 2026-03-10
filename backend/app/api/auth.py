from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..db import query_one, query_all
from security import AuthUser, create_access_token, decode_access_token


router = APIRouter()


class LoginRequest(BaseModel):
    username: str


class TokenRequest(BaseModel):
    token: str


class MapRoleRequest(BaseModel):
    domainGroups: List[str]


@router.post("/login")
def login(request: LoginRequest):
    """Mock SSO login, ported from Node auth-service."""
    user_row = query_one(
        """
        SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.username = %s AND u.is_active = true
        """,
        [request.username],
    )
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Update last_login_at
    query_one("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id", [user_row["id"]])

    auth_user: Dict[str, Any] = {
        "id": str(user_row["id"]),
        "username": user_row["username"],
        "email": user_row["email"],
        "role": user_row["role_name"],
        "hierarchy_level": int(user_row["hierarchy_level"]),
        "permissions": user_row.get("permissions") or [],
    }
    token = create_access_token(auth_user)

    return {
        "success": True,
        "data": {
            "token": token,
            "user": auth_user,
        },
        "timestamp": _now_iso(),
    }


@router.post("/verify")
def verify(request: TokenRequest):
    payload = decode_access_token(request.token)
    return {
        "success": True,
        "data": payload,
        "timestamp": _now_iso(),
    }


@router.get("/callback")
def sso_callback():
    # Placeholder matching Node auth-service behavior
    return {
        "success": True,
        "message": "SSO callback - implement with your SSO provider",
    }


@router.post("/map-role")
def map_role(request: MapRoleRequest):
    if not request.domainGroups:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Domain groups required",
        )

    role_mapping: Dict[str, int] = {
        "DOMAIN\\DevOps-Admins": 1,
        "DOMAIN\\Unit-Commanders": 2,
        "DOMAIN\\Branch-Heads": 3,
        "DOMAIN\\Section-Heads": 4,
        "DOMAIN\\Project-Managers": 5,
        "DOMAIN\\Team-Leads": 6,
        "DOMAIN\\Engineers": 7,
    }

    role_id = 7
    for group in request.domainGroups:
        mapped = role_mapping.get(group)
        if mapped and mapped < role_id:
            role_id = mapped

    role = query_one("SELECT * FROM roles WHERE id = %s", [role_id])
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )

    return {
        "success": True,
        "data": role,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


