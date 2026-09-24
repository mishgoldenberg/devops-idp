from typing import Any, Dict, Optional

import re
import secrets
import ssl
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from db import query_one, sync_bootstrap_admin_role_for_email
import identity
from resilient_http import tls_verify
from security import (
    REGULAR_USER_LEVEL,
    SSO_NAME_CLAIM,
    create_access_token,
    decode_access_token,
    is_platform_admin_level,
)
from sso_config import (
    decrypt_client_secret,
    fetch_openid_configuration,
    get_enabled_sso_config,
)


def _jwks_ssl_context() -> Optional[ssl.SSLContext]:
    """Unverified TLS context for the JWKS fetch when INTEGRATION_TLS_VERIFY is
    off (an internal CA or a self-signed IdP). PyJWKClient verifies certs by
    default, so without this the JWKS fetch fails on internal CAs."""
    if tls_verify():
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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
    if hierarchy_level is not None and is_platform_admin_level(hierarchy_level):
        return "Admin"
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
        SELECT u.*, r.name as role_name, r.hierarchy_level
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE LOWER(TRIM(u.email)) = %s AND u.is_active = true
        """,
        [email],
    )
    if not user_row:
        role_row = query_one(
            "SELECT id, name, hierarchy_level FROM roles WHERE hierarchy_level = %s LIMIT 1",
            [REGULAR_USER_LEVEL],
        )
        # Fallback for environments where hierarchy values differ from seed defaults:
        # take the least privileged row there is. A first-time SSO sign-in must never
        # land anywhere but the bottom of the hierarchy.
        if not role_row:
            role_row = query_one(
                "SELECT id, name, hierarchy_level FROM roles ORDER BY hierarchy_level DESC LIMIT 1"
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
            SELECT u.*, r.name as role_name, r.hierarchy_level
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
        SELECT u.*, r.name as role_name, r.hierarchy_level
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = %s AND u.is_active = true
        """,
        [user_row["id"]],
    )
    return refreshed or user_row


def request_is_https(request: Request) -> bool:
    """True when the *browser-facing* connection is HTTPS.

    TLS is terminated at the nginx proxy / ingress, so the backend sees plain HTTP
    on the pod-internal hop and ``request.url.scheme`` is "http". Trusting that alone
    leaves the auth cookie without ``Secure``, so it can ride an accidental HTTP
    request in cleartext. The proxy sets ``X-Forwarded-Proto`` to the real scheme;
    honour it, and fall back to the direct scheme for local (proxy-less) dev.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded:
        return forwarded == "https"
    return request.url.scheme == "https"


def sso_redirect_uri(request: Request) -> str:
    """The OIDC callback URL, with the scheme the BROWSER used.

    ``request.url_for`` builds from the ASGI scope, which is only as good as the
    X-Forwarded-Proto the proxy chain passed in. An IdP compares redirect_uri to its
    registered value as an exact string, so one wrong scheme is a hard login failure
    rather than a downgrade. Resolve it from the same helper the cookie uses.
    """
    uri = str(request.url_for("sso_callback"))
    want = "https" if request_is_https(request) else "http"
    return re.sub(r"^https?://", want + "://", uri, count=1)


def _set_auth_cookie(response: RedirectResponse, request: Request, token: str) -> RedirectResponse:
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )
    return response


def _auth_user_from_db_row(user_row: Dict[str, Any]) -> Dict[str, Any]:
    hl = int(user_row["hierarchy_level"])
    role_eff = _map_role_to_effective(str(user_row["role_name"]), None, hl)
    return {
        "id": str(user_row["id"]),
        "username": user_row["username"],
        "email": user_row["email"],
        "role": role_eff,
        "hierarchy_level": hl,
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
    redirect_uri = sso_redirect_uri(request)
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
        secure=request_is_https(request),
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
    redirect_uri = sso_redirect_uri(request)
    try:
        client_secret = decrypt_client_secret(str(sso["client_secret_encrypted"]))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SSO secret is unavailable")

    async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as client:
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

        signing_key = pyjwt.PyJWKClient(
            discovery["jwks_uri"], ssl_context=_jwks_ssl_context()
        ).get_signing_key_from_jwt(id_token)
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
    # One rule names a new account AND is kept as the provider's name, so a ticket
    # carries exactly the name the Hub first gave the person.
    provider_name = identity.sso_name_from_claims(payload)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email not present in ID token")

    user_row = _get_or_create_user_by_email(email, provider_name)
    # The provider's name, kept apart from the editable display name, at EVERY sign-in:
    # it is what tickets carry (identity.trusted_name), and a name changed at the
    # provider has to reach them too.
    identity.record_sso_name(str(user_row["id"]), provider_name)
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
    token = create_access_token({**_auth_user_from_db_row(user_row), SSO_NAME_CLAIM: 1})
    response = RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("sso_state", path="/")
    return _set_auth_cookie(response, request, token)


# NOTE: a POST /verify endpoint that decoded an arbitrary token (passed as a query
# param) and echoed its payload used to live here. It had no caller, and it was both a
# JWT-decode oracle for anyone who could reach it and a token-in-URL leak (query strings
# land in nginx access logs and Referer headers). Removed — token validation happens in
# get_current_user for every real request; there is no need for a standalone endpoint.


