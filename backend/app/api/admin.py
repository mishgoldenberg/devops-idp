import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
import httpx
from pydantic import BaseModel, field_validator

import widget_registry
from api.quick_links import (
    QuickLinkChild,
    normalize_children,
    normalize_kind as _normalize_kind,
    validate_quick_link_shape as _validate_quick_link_shape,
)
from db import execute, execute_returning, query_all, query_one
from security import (
    PLATFORM_ADMIN_LEVEL,
    REGULAR_USER_LEVEL,
    AuthUser,
    get_current_user,
    has_effective_admin_access_live,
    is_platform_admin_level,
)
from sso_config import (
    fetch_openid_configuration,
    get_sso_config_redacted,
    save_sso_config,
)

router = APIRouter()
log = logging.getLogger(__name__)

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


# The kinds, the child model and the shape rules now live in api/quick_links.py,
# imported above: the dashboard widget writes personal quick links through that
# module and cannot import this one without a cycle. One definition, two writers.


class QuickLinkRequest(BaseModel):
    name: str
    # Optional at the schema level because a multi-link has no URL of its own;
    # which of url/children is actually required is decided by `kind`.
    url: Optional[str] = ""
    icon_url: Optional[str] = ""
    kind: str = "single"
    children: Optional[List[QuickLinkChild]] = None
    # Restrict the link to admins. Defaults to False so a client that predates the
    # field — or simply omits it — creates a link everyone can see, which is the
    # answer that surprises nobody.
    admin_only: bool = False

    @field_validator("name")
    @classmethod
    def required_string(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Required")
        return s

    @field_validator("url", "icon_url")
    @classmethod
    def optional_string(cls, v: Optional[str]) -> str:
        return (v or "").strip()


class QuickLinkPatchRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    icon_url: Optional[str] = None
    kind: Optional[str] = None
    children: Optional[List[QuickLinkChild]] = None
    # None means "leave it as it is" — every other field on this model works that
    # way, and a PATCH that silently republished a restricted link because it did
    # not mention visibility would be the worst possible default.
    admin_only: Optional[bool] = None

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
    kind = _normalize_kind(row.get("kind"))
    return {
        "id": str(row.get("id")),
        "name": row.get("name") or "",
        "url": row.get("url") or "",
        "icon_url": row.get("icon_url") or "",
        "sort_order": int(row.get("sort_order") or 0),
        "is_active": bool(row.get("is_active")),
        "created_by": row.get("created_by") or "",
        "kind": kind,
        "children": normalize_children(row.get("children")) if kind == "multi" else [],
        "admin_only": bool(row.get("admin_only")),
    }


# Every RETURNING/SELECT of a quick link needs the same column list; naming it once
# stops a new column being added to one query and silently missing from another.
_QL_COLUMNS = (
    "id, name, url, icon_url, sort_order, is_active, created_by, kind, children, admin_only"
)


def _platform_admin_role() -> Dict[str, Any]:
    row = query_one(
        "SELECT id, name, hierarchy_level FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
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
    icon = (body.icon_url or "").strip() or None
    kind = _normalize_kind(body.kind)
    url, children = _validate_quick_link_shape(kind, body.url or "", body.children)
    children_json = json.dumps(children) if children is not None else None

    # Re-activate any previously soft-deleted row with the same identity, or
    # return the existing active row, before inserting a new one. `kind` is part
    # of the identity: a group and a plain link can share a name without one
    # silently overwriting the other.
    existing = query_one(
        f"""
        SELECT {_QL_COLUMNS}
        FROM quick_links
        WHERE LOWER(name) = LOWER(%s) AND LOWER(COALESCE(url, '')) = LOWER(%s) AND kind = %s
        ORDER BY is_active DESC, id ASC
        LIMIT 1
        """,
        [name, url, kind],
    )
    if existing:
        # Re-submitting an existing group is how an admin edits its table, so the
        # children are always refreshed — not just the icon.
        # admin_only is set outright, not COALESCEd: the form always sends it, so
        # "not ticked" is a decision to publish, not an absent value. COALESCE here
        # would make un-ticking the box do nothing at all.
        existing = query_one(
            f"""
            UPDATE quick_links
            SET is_active = true,
                icon_url = COALESCE(%s, icon_url),
                children = COALESCE(%s::jsonb, children),
                admin_only = %s
            WHERE id = %s
            RETURNING {_QL_COLUMNS}
            """,
            [icon, children_json, bool(body.admin_only), existing["id"]],
        )
        return {
            "success": True,
            "data": _quick_link_row(existing),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    max_order = query_one("SELECT COALESCE(MAX(sort_order), 0) AS n FROM quick_links")
    next_order = int((max_order or {}).get("n") or 0) + 10
    rows = execute_returning(
        f"""
        INSERT INTO quick_links (name, url, icon_url, sort_order, is_active, created_by, kind, children, admin_only)
        VALUES (%s, %s, %s, %s, true, %s, %s, %s::jsonb, %s)
        RETURNING {_QL_COLUMNS}
        """,
        [
            name,
            url,
            icon,
            next_order,
            str(current_user.get("email") or current_user.get("username") or ""),
            kind,
            children_json,
            bool(body.admin_only),
        ],
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create quick link")
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
        "SELECT name, url, icon_url, kind, children, admin_only FROM quick_links WHERE id = %s",
        [quick_link_id],
    ) or {}
    name = body.name if body.name is not None else str(current.get("name") or "")
    url = body.url if body.url is not None else str(current.get("url") or "")
    icon_url = body.icon_url if body.icon_url is not None else str(current.get("icon_url") or "")
    kind = _normalize_kind(body.kind if body.kind is not None else current.get("kind"))
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")

    # Children come from the body when the form sent a table, otherwise from what
    # is already stored — so editing only the name of a group cannot wipe its links.
    if body.children is not None:
        children_in: Optional[List[QuickLinkChild]] = body.children
    else:
        children_in = [
            QuickLinkChild(name=c["name"], url=c["url"])
            for c in normalize_children(current.get("children"))
        ]
    url, children = _validate_quick_link_shape(kind, url, children_in)
    children_json = json.dumps(children) if children is not None else None
    admin_only = (
        bool(body.admin_only)
        if body.admin_only is not None
        else bool(current.get("admin_only"))
    )

    rows = execute_returning(
        f"""
        UPDATE quick_links
        SET name = %s, url = %s, icon_url = %s, kind = %s, children = %s::jsonb,
            admin_only = %s
        WHERE id = %s
        RETURNING {_QL_COLUMNS}
        """,
        [name, url, icon_url or None, kind, children_json, admin_only, quick_link_id],
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update quick link")
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
        SELECT u.email, COALESCE(u.full_name, u.email) AS display_name,
               r.name AS role_name, u.is_active, r.hierarchy_level
        FROM users u
        JOIN roles r ON u.role_id = r.id
        -- Inactive accounts are INCLUDED. They used to be filtered out, which was
        -- correct while this list only fed a "grant admin" picker; now that the same
        -- list is the management table, hiding them meant deactivating someone made
        -- them unreachable and there was no way to ever reactivate them.
        WHERE LOWER(u.email) != %s
        ORDER BY u.is_active DESC, u.email ASC
        """,
        [str(current_user.get("email", "")).strip().lower()],
    )

    users: List[Dict[str, str]] = [
        {
            "email": str(row["email"]),
            "display_name": str(row["display_name"]),
            "role_name": str(row["role_name"]),
            "is_active": bool(row["is_active"]),
            "is_admin": is_platform_admin_level(row["hierarchy_level"]),
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


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard widget visibility (Platform Admins)
#
# Which widgets exist at all for users. Distinct from the Customize drawer, which
# is each user's own choice among the widgets policy already permits. Enforcement
# lives in the render path in ui.py — these endpoints only read and write policy.
# ─────────────────────────────────────────────────────────────────────────────


class WidgetVisibilityRequest(BaseModel):
    key: str
    hidden: bool

    @field_validator("key")
    @classmethod
    def key_must_be_present(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("key is required")
        return s


@router.get("/dashboard-widgets")
def list_dashboard_widgets(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """The full widget catalogue with each one's current show/hide state."""
    _require_app_admin(current_user)
    return {
        "success": True,
        "data": {"widgets": widget_registry.catalogue(is_admin=True, include_hidden=True)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.put("/dashboard-widgets")
def set_dashboard_widget_visibility(
    body: WidgetVisibilityRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Show or hide one widget for every user."""
    _require_app_admin(current_user)

    try:
        widget_registry.set_hidden(
            body.key, body.hidden, actor=getattr(current_user, "email", "") or "admin"
        )
    except KeyError:
        # An unknown key is the caller's mistake, not a server fault — and saying so
        # is what stops a typo becoming a silently ignored write.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown widget key: {body.key}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("dashboard widget visibility write failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save widget visibility — please try again",
        )

    return {
        "success": True,
        "data": {"widgets": widget_registry.catalogue(is_admin=True, include_hidden=True)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# User management
#
# What existed before was one direction only: promote somebody to Admin. There was
# no way to take it back, and no way to set any of the other five roles the RBAC
# seed defines — so the screen called "User Role Management" could perform exactly
# one of the operations it appeared to offer.
#
# THE GUARD THAT MATTERS: a portal whose last administrator can be demoted is a
# portal that locks everybody out of its own settings, permanently, with no way
# back through the UI. Every write below counts the remaining admins first.
# ─────────────────────────────────────────────────────────────────────────────


class SetRoleRequest(BaseModel):
    email: str
    role_name: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not s:
            raise ValueError("email is required")
        return s

    @field_validator("role_name")
    @classmethod
    def required_role(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("role_name is required")
        return s


class SetActiveRequest(BaseModel):
    email: str
    is_active: bool

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not s:
            raise ValueError("email is required")
        return s


def _admin_count() -> int:
    """How many ACTIVE Platform Admins remain."""
    row = query_one(
        """
        SELECT COUNT(*) AS n
        FROM users u JOIN roles r ON u.role_id = r.id
        WHERE u.is_active = true AND r.hierarchy_level = %s
        """,
        [PLATFORM_ADMIN_LEVEL],
    )
    return int((row or {}).get("n") or 0)


def _user_by_email(email: str) -> Dict[str, Any]:
    row = query_one(
        """
        SELECT u.id, u.email, u.is_active, r.name AS role_name, r.hierarchy_level
        FROM users u JOIN roles r ON u.role_id = r.id
        WHERE LOWER(TRIM(u.email)) = %s
        """,
        [email],
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such user. Accounts are created on first sign-in.",
        )
    return row


def _guard_last_admin(target: Dict[str, Any], *, becoming_admin: bool) -> None:
    """
    Refuse any change that would remove the final administrator.

    Checked here, in the function that performs the write, rather than only in the
    UI: the UI can be bypassed, and this is the one mistake in this file that cannot
    be undone from inside the portal afterwards.
    """
    was_admin = int(target.get("hierarchy_level") or 99) == 1
    if was_admin and not becoming_admin and _admin_count() <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This is the only remaining administrator. Give somebody else Admin "
                "first — otherwise nobody could reach these settings again."
            ),
        )


@router.get("/roles")
def list_roles(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Every assignable role, most privileged first.

    Only the two roles that mean something are offered. Rows for the retired
    intermediate levels are still in the table (they are referenced by history and
    are not deleted from a startup path), but handing one out would label an
    account "Branch Head" while granting it exactly what Regular User gets.
    """
    _require_app_admin(current_user)
    rows = query_all(
        """
        SELECT name, hierarchy_level, description
          FROM roles
         WHERE hierarchy_level IN (%s, %s)
         ORDER BY hierarchy_level ASC
        """,
        [PLATFORM_ADMIN_LEVEL, REGULAR_USER_LEVEL],
    )
    return {
        "success": True,
        "data": [
            {
                "name": str(r["name"]),
                "hierarchy_level": int(r["hierarchy_level"]),
                "description": str(r.get("description") or ""),
                "is_admin": is_platform_admin_level(r["hierarchy_level"]),
            }
            for r in (rows or [])
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.put("/users/role")
def set_user_role(
    body: SetRoleRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set any user's role to any seeded role - promotion and demotion alike."""
    _require_app_admin(current_user)

    role = query_one(
        "SELECT id, name, hierarchy_level FROM roles WHERE LOWER(TRIM(name)) = %s LIMIT 1",
        [body.role_name.strip().lower()],
    )
    if not role:
        raise HTTPException(status_code=400, detail=f"Unknown role: {body.role_name}")

    target = _user_by_email(body.email)
    _guard_last_admin(target, becoming_admin=is_platform_admin_level(role["hierarchy_level"]))

    updated = execute_returning(
        "UPDATE users SET role_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
        [role["id"], str(target["id"])],
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Role update failed - please try again")

    demoted = int(target.get("hierarchy_level") or 99) == 1 and int(role["hierarchy_level"]) != 1
    return {
        "success": True,
        "message": f"{target['email']} is now {role['name']}.",
        "data": {
            "email": target["email"],
            "role_name": role["name"],
            # Sessions carry the role in a signed token, so a change does not reach an
            # already-signed-in user until their token expires. Said plainly rather than
            # left for the admin to discover when someone still has access afterwards.
            "takes_effect": "immediately on their next sign-in",
            "warning": (
                "They keep their current access until their session expires (up to 8 hours). "
                "Ask them to sign out and back in to apply it now."
                if demoted else None
            ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.put("/users/active")
def set_user_active(
    body: SetActiveRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Deactivate or reactivate an account.

    Deactivation rather than deletion: the audit trail, approvals and activity rows
    reference the user, and deleting the row would either break those references or
    silently take history with it. A deactivated user cannot sign in and does not
    appear in pickers, which is what "remove them" actually means here.
    """
    _require_app_admin(current_user)

    if body.email == str(current_user.get("email", "")).strip().lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )

    target = _user_by_email(body.email)
    if not body.is_active:
        _guard_last_admin(target, becoming_admin=False)

    updated = execute_returning(
        "UPDATE users SET is_active = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
        [bool(body.is_active), str(target["id"])],
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Update failed - please try again")

    # Every request now re-checks is_active behind a short cache. Clearing it here makes
    # the change instant on this pod; other replicas pick it up within the TTL.
    try:
        from security import invalidate_active_cache

        invalidate_active_cache(str(target["id"]))
    except Exception as exc:  # pragma: no cover - a cache miss is not a failed update
        log.warning("could not clear active-cache for %s: %s", target["email"], exc)

    return {
        "success": True,
        "message": (
            f"{target['email']} reactivated."
            if body.is_active
            else f"{target['email']} deactivated — signed out of the portal within a minute."
        ),
        "data": {"email": target["email"], "is_active": bool(body.is_active)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
