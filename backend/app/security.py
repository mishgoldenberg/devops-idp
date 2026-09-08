"""
Authentication, token handling, and RBAC helpers.

The portal issues its own HS256 JWTs after OIDC or bootstrap-admin login and
stores them in an ``auth_token`` HttpOnly cookie.
Every protected endpoint depends on :func:`get_current_user`, which
accepts either the cookie or an ``Authorization: Bearer <token>`` header —
the Bearer path exists so internal tools and tests don't have to
round-trip cookies.

``has_effective_admin_access_live`` is intentionally uncached: an admin
grant/revoke must take effect immediately without waiting for token
expiry, so the check reads the user's current role from Postgres on every
call. The slight cost is worth the correctness.
"""

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import get_settings


log = logging.getLogger(__name__)


class AuthUser(Dict[str, Any]):
    """Simple dict-based representation of authenticated user payload."""


security_scheme = HTTPBearer(auto_error=False)


# ── Deactivation, enforced on every request ──────────────────────────────────
#
# Deactivating a user used to do almost nothing. `is_active = true` is checked when
# somebody SIGNS IN — and that is the one moment a deactivated user is not doing
# anything. Whoever was already signed in kept a valid JWT, and since nothing on the
# request path ever looked at the database again, they kept full access for the
# remaining life of the token: up to eight hours of an account an admin believed they
# had just switched off.
#
# So the check moves to where tokens are read. It is cached briefly because it now runs
# on every request including page renders, and the cost of a per-request query on the
# hot path is real; 30 seconds is short enough that "deactivate" means what it says.
_ACTIVE_TTL_S = int(os.getenv("AUTH_ACTIVE_CHECK_TTL", "30"))
_active_cache: Dict[str, Tuple[float, bool]] = {}
_active_lock = threading.Lock()


def invalidate_active_cache(user_id: Optional[str] = None) -> None:
    """Drop a cached active-flag so a deactivation takes effect on this pod at once."""
    with _active_lock:
        if user_id is None:
            _active_cache.clear()
        else:
            _active_cache.pop(str(user_id), None)


def user_is_active(user_id: str) -> bool:
    """
    Is this account still enabled?

    Fails OPEN when the database cannot be reached, and CLOSED when it answers and the
    answer is no. Those are different situations and deserve opposite defaults: a
    Postgres blip must not sign every user in the portal out, while a row that says
    `is_active = false` is a decision somebody made on purpose.
    """
    key = str(user_id)
    now = time.monotonic()
    with _active_lock:
        hit = _active_cache.get(key)
        if hit and now - hit[0] < _ACTIVE_TTL_S:
            return hit[1]

    try:
        from db import query_one

        row = query_one("SELECT is_active FROM users WHERE id = %s", [key])
    except Exception as exc:
        log.warning("active-check failed for %s, allowing: %s", key, exc)
        return True

    active = bool(row and row.get("is_active"))
    with _active_lock:
        _active_cache[key] = (time.monotonic(), active)
    return active


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


def hash_password(password: str) -> str:
    """Hash a local bootstrap-admin password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a bootstrap-admin password with bcrypt.checkpw."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception:
        return False


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Verify a portal JWT and confirm the account behind it is still enabled.

    The active check lives here rather than in ``get_current_user`` because this is the
    single point every path goes through — API dependencies, the UI page routes that
    read the cookie directly, and the admin-flag middleware. Putting it one layer up
    would have covered /api and left every rendered page open to a deactivated user.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = payload.get("id")
        if user_id and not user_is_active(str(user_id)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This account has been deactivated.",
            )
        return payload
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
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> AuthUser:
    """FastAPI dependency that returns the authenticated user from JWT."""
    token: Optional[str] = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        token = request.cookies.get("auth_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization missing")
    payload = decode_access_token(token)
    # Minimal validation. "permissions" is deliberately NOT required: the per-role
    # permission list was never consulted by any authorization decision, and a token
    # issued before it was dropped must keep working until it expires.
    required_keys = {"id", "username", "email", "role", "hierarchy_level"}
    if not required_keys.issubset(payload.keys()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid auth payload",
        )
    return AuthUser(payload)


# ================================
# RBAC helpers
# ================================
#
# There is exactly one privilege boundary in this application: Platform Admin, or
# not. Everything else is an ordinary user.
#
# It used to look richer than that. A seven-level hierarchy sat in the roles table,
# each row carrying a permissions array ("approve:ado_projects", "view:observability"),
# and helpers existed to read both. None of them were ever called — the array was
# loaded, signed into the token and ignored, while the real gates tested the
# hierarchy level directly. Four roles in the middle were therefore identical to one
# another and two at the bottom identical to each other, which is the worst way for an
# access model to be wrong: it reads as fine-grained and behaves as coarse.
#
# has_role_level(), has_permission(), can_view_aggregated_metrics() and
# can_manage_users() were that dead machinery and are gone. Do not reintroduce a
# check that a caller has to remember to make; add the gate to the handler.

PLATFORM_ADMIN_LEVEL = 1
REGULAR_USER_LEVEL = 7


def has_effective_admin_access(user: AuthUser) -> bool:
    """
    True for portal / API admin-only features.

    Accepts either JWT ``role == "Admin"``, DB top-of-hierarchy, or the single
    bootstrap admin email (matches DB bootstrap; JWT may lag after role fixes).
    """
    if str(user.get("role") or "") == "Admin":
        return True
    if is_platform_admin_level(user.get("hierarchy_level", 99)):
        return True
    try:
        from db import BOOTSTRAP_ADMIN_EMAIL

        em = str(user.get("email") or "").strip().lower()
        if em and em == BOOTSTRAP_ADMIN_EMAIL.strip().lower():
            return True
    except Exception:
        pass
    return False


def has_effective_admin_access_live(user: AuthUser) -> bool:
    """
    Like has_effective_admin_access but also checks the live DB role as a fallback.

    Use this for sidebar / page-guard decisions so that users whose DB role was
    promoted after their current JWT was issued don't have to re-login just to see
    the Admin section — they'll get full API access on their next login.
    """
    if has_effective_admin_access(user):
        return True
    user_id = user.get("id")
    if not user_id:
        return False
    try:
        from db import query_one

        row = query_one(
            """
            SELECT r.name AS role_name, r.hierarchy_level
            FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE u.id = %s AND u.is_active = true
            """,
            [str(user_id)],
        )
        if not row:
            return False
        if is_platform_admin_level(row.get("hierarchy_level", 99)):
            return True
        if (row.get("role_name") or "").strip().lower() == "platform admin":
            return True
    except Exception:
        pass
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
    return has_effective_admin_access(user)


def is_platform_admin_level(hierarchy_level) -> bool:
    """The one privilege test in the system, in one place.

    Kept as a named function rather than a bare ``== 1`` scattered across modules so
    that "what counts as an admin" has a single definition to read and to change.
    """
    try:
        return int(hierarchy_level) == PLATFORM_ADMIN_LEVEL
    except (TypeError, ValueError):
        return False


