from typing import Any, Dict, Optional

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from config import get_settings
from db import query_one
from security import create_access_token, decode_access_token


router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _map_role_to_effective(role_name: str) -> str:
    """
    Map detailed platform roles into simplified app roles:
      - Admin
      - TeamLead
      - User
    """
    normalized = (role_name or "").strip().lower()
    if normalized in {
        "platform admin",
        "head of section",
        "branch head",
        "unit commander",
    }:
        return "Admin"
    if normalized in {
        "team lead",
        "project manager",
    }:
        return "TeamLead"
    return "User"


def _get_or_create_user_by_email(email: str, full_name: Optional[str]) -> Dict[str, Any]:
    """
    Look up a user by email; if not found, create a regular user with lowest role.
    """
    user_row = query_one(
        """
        SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.email = %s AND u.is_active = true
        """,
        [email],
    )
    if not user_row:
        role_row = query_one(
            "SELECT id, name, hierarchy_level, permissions FROM roles WHERE hierarchy_level = 7 LIMIT 1"
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
            RETURNING id, username, email, full_name, role_id
            """,
            [email, email, full_name or email, role_row["id"]],
        )
        user_row = {
            **created,
            "role_name": role_row["name"],
            "hierarchy_level": int(role_row["hierarchy_level"]),
            "permissions": role_row.get("permissions") or [],
        }

    query_one("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id", [user_row["id"]])
    return user_row


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

    email: Optional[str] = payload.get("email")
    full_name: Optional[str] = payload.get("name")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email not present in ID token",
        )

    user_row = _get_or_create_user_by_email(email, full_name)
    auth_user: Dict[str, Any] = {
        "id": str(user_row["id"]),
        "username": user_row["username"],
        "email": user_row["email"],
        # Expose simplified role for frontend & RBAC helpers
        "role": _map_role_to_effective(str(user_row["role_name"])),
        "hierarchy_level": int(user_row["hierarchy_level"]),
        "permissions": user_row.get("permissions") or [],
    }
    token = create_access_token(auth_user)

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Authentication successful</title>
  </head>
  <body>
    <script>
      (function() {{
        var payload = {{"token": "{token}", "user": {auth_user!r} }};
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage({{ type: "auth:success", data: payload }}, "*");
        }}
        window.close();
      }})();
    </script>
    <p>Authentication successful. You can close this window.</p>
  </body>
  </html>
    """
    return Response(content=html, media_type="text/html")


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


