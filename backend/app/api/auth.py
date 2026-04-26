from typing import Any, Dict, Optional

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from db import query_one
from security import create_access_token, decode_access_token
from sso_config import (
    decrypt_client_secret,
    fetch_openid_configuration,
    get_enabled_sso_config,
)


router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _normalize_permissions(perms: Any) -> list:
    """JSONB / JWT-safe list for the token payload."""
    if perms is None:
        return []
    if isinstance(perms, list):
        return perms
    if isinstance(perms, str):
        return [perms]
    return []


def _map_role_to_effective(
    role_name: str,
    _email: Optional[str] = None,
    hierarchy_level: Optional[int] = None,
) -> str:
    """
    Map detailed platform roles into simplified app roles:
      - Admin
      - TeamLead
      - User

    For now we only expose three roles in the app. TeamLead and User share
    the same permissions; Admin has access to admin-only features.

    ``hierarchy_level == 1`` always maps to Admin (Platform Admin in seed data)
    so production DB role *names* can differ slightly without breaking RBAC.
    """
    if hierarchy_level is not None:
        try:
            if int(hierarchy_level) == 1:
                return "Admin"
        except (TypeError, ValueError):
            pass
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


def _set_auth_cookie(response: RedirectResponse, request: Request, token: str) -> RedirectResponse:
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


def _auth_user_from_db_row(user_row: Dict[str, Any]) -> Dict[str, Any]:
    hl = int(user_row["hierarchy_level"])
    perms = _normalize_permissions(user_row.get("permissions"))
    role_eff = _map_role_to_effective(str(user_row["role_name"]), None, hl)
    return {
        "id": str(user_row["id"]),
        "username": user_row["username"],
        "email": user_row["email"],
        "role": role_eff,
        "hierarchy_level": hl,
        "permissions": perms,
    }


@router.get("/login")
def oidc_login(request: Request):
    """Initiate the admin-configured OIDC login flow."""
    sso = get_enabled_sso_config()
    if not sso:
        return RedirectResponse(url="/ui/auth?error=sso_not_configured", status_code=status.HTTP_303_SEE_OTHER)
    try:
        discovery = fetch_openid_configuration(str(sso["issuer_uri"]))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configured SSO provider is unavailable",
        )
    state = secrets.token_urlsafe(32)
    redirect_uri = str(request.url_for("sso_callback"))
    params = {
        "client_id": sso["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    response = RedirectResponse(
        f"{discovery['authorization_endpoint']}?{urlencode(params)}",
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        "sso_state",
        state,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=600,
        path="/",
    )
    return response


@router.get("/sso/callback")
async def sso_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None):
    """OIDC authorization-code callback for admin-managed SSO configuration."""
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code")
    expected_state = request.cookies.get("sso_state")
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid SSO state")

    sso = get_enabled_sso_config()
    if not sso:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SSO is not enabled")

    discovery = fetch_openid_configuration(str(sso["issuer_uri"]))
    redirect_uri = str(request.url_for("sso_callback"))
    try:
        client_secret = decrypt_client_secret(str(sso["client_secret_encrypted"]))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SSO secret is unavailable")

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": sso["client_id"],
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )

    if token_resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Failed to exchange authorization code")

    token_data = token_resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ID token not returned by provider")

    try:
        import jwt as pyjwt

        signing_key = pyjwt.PyJWKClient(discovery["jwks_uri"]).get_signing_key_from_jwt(id_token)
        payload = pyjwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=sso["client_id"],
            issuer=discovery.get("issuer") or str(sso["issuer_uri"]).rstrip("/"),
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ID token")

    email = str(payload.get("email") or "").strip().lower()
    username = str(payload.get("preferred_username") or email).strip()
    full_name = payload.get("name") or username or email
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email not present in ID token")

    user_row = _get_or_create_user_by_email(email, str(full_name))
    if username and username != user_row.get("username"):
        # Best-effort username refresh from the provider. Do not fail login for it.
        try:
            query_one(
                "UPDATE users SET username = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
                [username, user_row["id"]],
            )
            user_row["username"] = username
        except Exception:
            pass
    token = create_access_token(_auth_user_from_db_row(user_row))
    response = RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("sso_state", path="/")
    return _set_auth_cookie(response, request, token)


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


