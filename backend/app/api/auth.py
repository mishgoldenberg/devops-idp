import logging
from typing import Any, Dict, Optional

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from config import get_settings
from db import query_one, sync_bootstrap_admin_role_for_email
from security import create_access_token, decode_access_token


router = APIRouter()
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _map_role_to_effective(role_name: str, _email: Optional[str] = None) -> str:
    """
    Map detailed platform roles into simplified app roles:
      - Admin
      - TeamLead
      - User

    For now we only expose three roles in the app. TeamLead and User share
    the same permissions; Admin has access to admin-only features.

    Platform Admin in the database maps to Admin; the primary bootstrap admin
    is ensured at startup via ``db.ensure_bootstrap_platform_admin``.
    """
    normalized = (role_name or "").strip().lower()
    if normalized == "platform admin":
        return "Admin"
    if normalized == "team lead":
        return "TeamLead"
    # All other backend roles are treated as regular users in the app
    return "User"


def _get_or_create_user_by_email(email: str, full_name: Optional[str]) -> Dict[str, Any]:
    """
    Look up a user by email; if not found, create a regular user with lowest role.

    Email is normalized to lowercase for lookup and storage so SSO casing matches
    the bootstrap admin row and RBAC stays consistent.
    """
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is required",
        )

    user_row = query_one(
        """
        SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE LOWER(TRIM(u.email)) = %s AND u.is_active = true
        """,
        [email],
    )
    if not user_row:
        role_row = query_one(
            "SELECT id, name, hierarchy_level, permissions FROM roles WHERE hierarchy_level = 7 LIMIT 1"
        )
        # Fallback for environments where hierarchy values differ from seed defaults.
        if not role_row:
            role_row = query_one(
                "SELECT id, name, hierarchy_level, permissions FROM roles ORDER BY hierarchy_level DESC LIMIT 1"
            )
        if not role_row:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Default user role not configured",
            )
        created = query_one(
            """
            INSERT INTO users (username, email, full_name, role_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE
              SET username = EXCLUDED.username,
                  full_name = EXCLUDED.full_name,
                  role_id = users.role_id,
                  is_active = true
            RETURNING id, username, email, full_name, role_id
            """,
            [email, email, full_name or email, role_row["id"]],
        )
        if not created:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create or update user",
            )
        # Re-query with role join to guarantee full shape.
        user_row = query_one(
            """
            SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
            FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE u.id = %s
            """,
            [created["id"]],
        )
        if not user_row:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Created user could not be loaded",
            )

    query_one("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id", [user_row["id"]])
    sync_bootstrap_admin_role_for_email(email)
    refreshed = query_one(
        """
        SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = %s AND u.is_active = true
        """,
        [user_row["id"]],
    )
    return refreshed or user_row


@router.get("/login")
def oauth_login():
    """
    Initiate Google OAuth2 login by redirecting to Google consent screen.
    """
    settings = get_settings()
    if not settings.oauth_client_id or not settings.oauth_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth configuration is missing",
        )

    state = secrets.token_urlsafe(32)

    params = {
        "client_id": settings.oauth_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "response_type": "code",
        "scope": settings.oauth_scopes,
        "access_type": "online",
        "include_granted_scopes": "true",
        "state": state,
        "prompt": "consent",
    }

    auth_url = f"{settings.oauth_auth_url}?{urlencode(params)}"
    return RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def oauth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None):
    """
    OAuth2 callback endpoint for Google. Exchanges code for tokens and issues app JWT.
    """
    settings = get_settings()
    if not settings.oauth_client_id or not settings.oauth_client_secret or not settings.oauth_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth configuration is missing",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing authorization code",
        )

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            settings.oauth_token_url,
            data={
                "code": code,
                "client_id": settings.oauth_client_id,
                "client_secret": settings.oauth_client_secret,
                "redirect_uri": settings.oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to exchange authorization code",
        )

    token_data = token_resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ID token not returned by provider",
        )

    try:
        import jwt as pyjwt

        payload = pyjwt.decode(id_token, options={"verify_signature": False})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid ID token",
        )

    email_raw: Optional[str] = payload.get("email")
    email = (email_raw or "").strip().lower()
    full_name: Optional[str] = payload.get("name")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email not present in ID token",
        )

    try:
        user_row = _get_or_create_user_by_email(email, full_name)
        auth_user: Dict[str, Any] = {
            "id": str(user_row["id"]),
            "username": user_row["username"],
            "email": user_row["email"],
            # Expose simplified role for frontend & RBAC helpers
            "role": _map_role_to_effective(str(user_row["role_name"]), email),
            "hierarchy_level": int(user_row["hierarchy_level"]),
            "permissions": user_row.get("permissions") or [],
        }
    except Exception as exc:
        # Temporary resilience for schema drift during integration:
        # allow login even if DB provisioning fails.
        logger.exception("SSO user provisioning failed for %s: %s", email, exc)
        auth_user = {
            "id": email,
            "username": email,
            "email": email,
            "role": "User",
            "hierarchy_level": 7,
            "permissions": [],
        }
    token = create_access_token(auth_user)
    response = RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/verify")
def verify_token(token: str):
    """
    Verify an application JWT and return its payload.
    """
    payload = decode_access_token(token)
    return {
        "success": True,
        "data": payload,
        "timestamp": _now_iso(),
    }


