import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from db import execute_returning, query_all, query_one
from security import AuthUser, get_current_user, has_effective_admin_access_live

router = APIRouter()

# ── The shape rules, in one place ──────────────────────────────────────────
# There are two writers now — Platform Managing (api/admin.py) for the shared
# list and the dashboard widget for a personal one — and they must agree about
# what a quick link IS. These lived in admin.py, which the personal endpoints
# cannot import without a cycle, so they live here and admin.py imports them.
# A second copy of "a multi-link needs at least one child" is a second copy that
# eventually disagrees with the first.

_QUICK_LINK_KINDS = ("single", "multi")
# A quick-link panel is a shortcut list, not a directory. The cap keeps one
# oversized group from turning the popover into its own scrolling page.
_QUICK_LINK_MAX_CHILDREN = 30
# Per person, not per portal: a personal list long enough to need paging is a
# bookmark bar, and this widget is not one.
_USER_QUICK_LINK_MAX = 24


class QuickLinkChild(BaseModel):
    """One row of a multi-link's table: a display name and where it goes."""

    name: str
    url: str

    @field_validator("name", "url")
    @classmethod
    def required_string(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Required")
        return s


def normalize_kind(kind: Optional[str]) -> str:
    k = (kind or "single").strip().lower()
    return k if k in _QUICK_LINK_KINDS else "single"


def validate_quick_link_shape(
    kind: str,
    url: str,
    children: Optional[List[QuickLinkChild]],
) -> tuple:
    """Enforce the one rule the two kinds differ by, and return storable values.

    A 'single' needs a URL and stores no children; a 'multi' needs at least one
    child and stores no URL of its own.
    """
    if kind == "multi":
        rows = [{"name": c.name, "url": c.url} for c in (children or [])]
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A multi-link needs at least one link in its table",
            )
        if len(rows) > _QUICK_LINK_MAX_CHILDREN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A multi-link can hold at most {_QUICK_LINK_MAX_CHILDREN} links",
            )
        return "", rows
    if not (url or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL is required",
        )
    return url.strip(), None


def normalize_children(raw: Any) -> List[Dict[str, str]]:
    """Normalise the JSONB `children` column into [{name, url}, …].

    psycopg2 hands JSONB back already decoded, but a row written before the
    column existed (or by hand) can still be a string or NULL — so parse
    defensively and drop anything that is not a usable name+url pair, rather
    than letting one malformed row break the whole widget.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if name and url:
            out.append({"name": name, "url": url})
    return out


def _row_to_quick_link(row: Dict[str, Any], scope: str = "shared") -> Dict[str, Any]:
    kind = str(row.get("kind") or "single").strip().lower()
    if kind not in ("single", "multi"):
        kind = "single"
    children = normalize_children(row.get("children")) if kind == "multi" else []
    # A multi-link with no usable children is a circle that does nothing at all.
    # Fall back to treating it as a plain link so the failure is visible rather
    # than a dead tile.
    if kind == "multi" and not children:
        kind = "single"
    # Personal ids are prefixed. The widget keys its saved drag order, its lookups
    # and its DOM nodes on this id, and the two tables are separate SERIALs — so
    # without a namespace, personal link 3 and shared link 3 are the same tile as
    # far as the browser is concerned.
    raw_id = str(row.get("id"))
    return {
        "id": ("u" + raw_id) if scope == "personal" else raw_id,
        "name": row.get("name") or "",
        "url": row.get("url") or "",
        "icon_url": row.get("icon_url") or "",
        "sort_order": int(row.get("sort_order") or 0),
        "kind": kind,
        "children": children,
        "admin_only": bool(row.get("admin_only")),
        # "shared" — an admin put it there for other people. "personal" — you did,
        # and only you can see or change it. The widget needs this to know which
        # tiles it is allowed to offer an edit button on.
        "scope": scope,
    }


@router.get("")
def list_quick_links(
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the active Quick Links this caller is allowed to see.

    De-duplicates at read time on (lower(name), lower(url)) so historical
    duplicates created before the create endpoint became idempotent don't
    show up twice on the dashboard. The first occurrence (lowest sort_order
    / id) wins.

    VISIBILITY IS DECIDED HERE, not in the template. An admin-only link that is
    filtered out in the browser has still been sent to the browser, and this
    endpoint is one `curl` away — so a row the caller may not see must never
    leave the server. The dashboard widget renders whatever it is given and asks
    no questions, which is exactly the point: there is one filter, in the one
    place that cannot be bypassed.

    It also now requires a signed-in caller. It never did before, because a list
    of shortcut tiles was public information; the moment one of them can be
    restricted, an unauthenticated reader is a hole rather than a convenience.
    """
    rows = query_all(
        """
        SELECT id, name, url, icon_url, sort_order, kind, children, admin_only
        FROM quick_links
        WHERE is_active = true
        ORDER BY sort_order ASC, id ASC
        """
    )
    # The live privilege lookup was deliberately removed from this path once, because
    # it cost a query on every dashboard load for every user. So only ask when the
    # answer can change the result: if nothing is restricted, there is nothing to
    # hide, and the common case stays exactly as cheap as it was.
    restricted = any(bool(r.get("admin_only")) for r in rows)
    is_admin = has_effective_admin_access_live(current_user) if restricted else False

    seen: set[tuple[str, str]] = set()
    deduped: list[Dict[str, Any]] = []
    for row in rows:
        link = _row_to_quick_link(row)
        if link["admin_only"] and not is_admin:
            continue
        # Multi-links carry no URL of their own, so keying on (name, url) would
        # collapse every one of them into a single entry. Key those on the name
        # alone, in a namespace a real URL can never collide with.
        key = (
            link["name"].strip().lower(),
            "\x00multi" if link["kind"] == "multi" else link["url"].strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)

    # Your own links come last, after the shared ones — they are additions to the
    # portal's list, not a replacement for it, and a new joiner should see the
    # platform's links first. The widget's saved drag order overrides this anyway
    # for anyone who cares where their tiles sit.
    #
    # NOT de-duplicated against the shared list: if you bookmarked something an
    # admin later added centrally, that is two tiles because it is two decisions,
    # and silently deleting somebody's own tile is worse than a duplicate.
    for row in query_all(
        """
        SELECT id, name, url, icon_url, sort_order, kind, children
        FROM user_quick_links
        WHERE user_id = %s AND is_active = true
        ORDER BY sort_order ASC, id ASC
        """,
        [_owner(current_user)],
    ):
        deduped.append(_row_to_quick_link(row, scope="personal"))

    return {
        "success": True,
        "data": deduped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Personal quick links ───────────────────────────────────────────────────
# Mounted under the same /api/quick-links prefix, so these paths are relative to
# it: the full routes are /api/quick-links/mine[/{id}].


def _owner(current_user: AuthUser) -> str:
    """The key a personal link belongs to.

    The user's stable id, never their email: an address can change, and a link
    list that quietly empties itself because somebody's surname changed is a bug
    nobody would think to look for.
    """
    owner = str(current_user.get("id") or "").strip()
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in again to manage your own quick links.",
        )
    return owner


_UQL_COLUMNS = "id, name, url, icon_url, sort_order, kind, children"


class UserQuickLinkRequest(BaseModel):
    name: str
    url: Optional[str] = ""
    icon_url: Optional[str] = ""
    kind: str = "single"
    children: Optional[List[QuickLinkChild]] = None

    @field_validator("name")
    @classmethod
    def required_string(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("Required")
        return s[:255]

    @field_validator("url", "icon_url")
    @classmethod
    def optional_string(cls, v: Optional[str]) -> str:
        return (v or "").strip()


def _personal_row(owner: str, link_id: int) -> Dict[str, Any]:
    """Fetch one of THIS user's links, or 404.

    Ownership is part of the WHERE clause, not a check after the fact: a query
    that can return somebody else's row is one forgotten `if` away from editing
    it, and here the id came straight from the URL.
    """
    row = query_one(
        f"SELECT {_UQL_COLUMNS}, is_active FROM user_quick_links WHERE id = %s AND user_id = %s",
        [link_id, owner],
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Quick link not found"
        )
    return row


@router.post("/mine")
def create_my_quick_link(
    body: UserQuickLinkRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Add a link only this user will see."""
    owner = _owner(current_user)
    kind = normalize_kind(body.kind)
    url, children = validate_quick_link_shape(kind, body.url or "", body.children)

    count = query_one(
        "SELECT COUNT(*)::int AS n FROM user_quick_links WHERE user_id = %s AND is_active = true",
        [owner],
    )
    if int((count or {}).get("n") or 0) >= _USER_QUICK_LINK_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"You already have {_USER_QUICK_LINK_MAX} of your own quick links. "
                "Remove one to add another."
            ),
        )

    max_order = query_one(
        "SELECT COALESCE(MAX(sort_order), 0) AS n FROM user_quick_links WHERE user_id = %s",
        [owner],
    )
    rows = execute_returning(
        f"""
        INSERT INTO user_quick_links (user_id, name, url, icon_url, kind, children, sort_order)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING {_UQL_COLUMNS}
        """,
        [
            owner,
            body.name.strip()[:255],
            url,
            (body.icon_url or "").strip() or None,
            kind,
            json.dumps(children) if children is not None else None,
            int((max_order or {}).get("n") or 0) + 10,
        ],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create quick link",
        )
    return {"success": True, "data": _row_to_quick_link(rows[0], scope="personal")}


@router.patch("/mine/{link_id}")
def update_my_quick_link(
    link_id: int,
    body: UserQuickLinkRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    owner = _owner(current_user)
    _personal_row(owner, link_id)
    kind = normalize_kind(body.kind)
    url, children = validate_quick_link_shape(kind, body.url or "", body.children)

    rows = execute_returning(
        f"""
        UPDATE user_quick_links
           SET name = %s, url = %s, icon_url = %s, kind = %s, children = %s::jsonb,
               is_active = true, updated_at = CURRENT_TIMESTAMP
         WHERE id = %s AND user_id = %s
        RETURNING {_UQL_COLUMNS}
        """,
        [
            body.name.strip()[:255],
            url,
            (body.icon_url or "").strip() or None,
            kind,
            json.dumps(children) if children is not None else None,
            link_id,
            owner,
        ],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update quick link",
        )
    return {"success": True, "data": _row_to_quick_link(rows[0], scope="personal")}


@router.delete("/mine/{link_id}")
def delete_my_quick_link(
    link_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Soft-delete, so Undo is a flag flip that keeps the tile's place."""
    owner = _owner(current_user)
    _personal_row(owner, link_id)
    execute_returning(
        "UPDATE user_quick_links SET is_active = false, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND user_id = %s RETURNING id",
        [link_id, owner],
    )
    return {"success": True}


@router.post("/mine/{link_id}/restore")
def restore_my_quick_link(
    link_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """The other half of the delete toast's Undo — the same row, the flag flipped
    back, so it returns where it was rather than at the end of the list."""
    owner = _owner(current_user)
    _personal_row(owner, link_id)
    rows = execute_returning(
        f"UPDATE user_quick_links SET is_active = true, updated_at = CURRENT_TIMESTAMP "
        f"WHERE id = %s AND user_id = %s RETURNING {_UQL_COLUMNS}",
        [link_id, owner],
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore quick link",
        )
    return {"success": True, "data": _row_to_quick_link(rows[0], scope="personal")}
