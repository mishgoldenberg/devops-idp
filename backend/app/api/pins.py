"""
User-scoped item tracking shared by the interactive dashboard widgets.

Currently used by the Azure DevOps ("azure_devops") and ServiceNow
("servicenow") widgets so each user can:
  * pin important items to the top of the list
  * mark an item as "seen" after they open it, so the red update indicator
    clears until the remote item is updated again

The endpoints are intentionally small and source-agnostic. New widgets can
reuse them by picking a fresh `source` string.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import db
from db import execute, query_all
from security import AuthUser, get_current_user


log = logging.getLogger(__name__)
router = APIRouter()

# Keep this narrow; any unknown source is rejected so widgets can't scribble
# into shared tracking tables with arbitrary keys.
_ALLOWED_SOURCES = {"azure_devops", "servicenow"}


class PinPayload(BaseModel):
    source: str = Field(..., min_length=1, max_length=32)
    item_id: str = Field(..., min_length=1, max_length=128)


class SeenPayload(BaseModel):
    source: str = Field(..., min_length=1, max_length=32)
    item_id: str = Field(..., min_length=1, max_length=128)


def _user_id(current_user: AuthUser) -> str:
    uid = str(
        current_user.get("email")
        or current_user.get("username")
        or current_user.get("id")
        or ""
    ).strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uid[:255]


def _validate_source(source: str) -> str:
    s = (source or "").strip().lower()
    if s not in _ALLOWED_SOURCES:
        raise HTTPException(status_code=400, detail=f"Unsupported source: {source!r}")
    return s


@router.get("/pins")
def list_pins(
    source: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    """Return the current user's pins, optionally filtered by source."""
    db.ensure_item_tables_once()
    uid = _user_id(current_user)
    try:
        if source:
            rows = query_all(
                "SELECT source, item_id, created_at "
                "FROM user_item_pins WHERE user_id = %s AND source = %s "
                "ORDER BY created_at DESC",
                [uid, _validate_source(source)],
            )
        else:
            rows = query_all(
                "SELECT source, item_id, created_at "
                "FROM user_item_pins WHERE user_id = %s "
                "ORDER BY created_at DESC",
                [uid],
            )
    except Exception as exc:
        log.warning("list_pins failed: %s", exc)
        rows = []
    return {"success": True, "data": rows}


@router.post("/pins")
def add_pin(
    body: PinPayload = Body(...),
    current_user: AuthUser = Depends(get_current_user),
):
    db.ensure_item_tables_once()
    uid = _user_id(current_user)
    src = _validate_source(body.source)
    try:
        execute(
            """
            INSERT INTO user_item_pins (user_id, source, item_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, source, item_id) DO NOTHING
            """,
            [uid, src, body.item_id[:128]],
        )
    except Exception as exc:
        log.warning("add_pin failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to pin item")
    return {"success": True}


@router.delete("/pins")
def remove_pin(
    body: PinPayload = Body(...),
    current_user: AuthUser = Depends(get_current_user),
):
    db.ensure_item_tables_once()
    uid = _user_id(current_user)
    src = _validate_source(body.source)
    try:
        execute(
            "DELETE FROM user_item_pins WHERE user_id = %s AND source = %s AND item_id = %s",
            [uid, src, body.item_id[:128]],
        )
    except Exception as exc:
        log.warning("remove_pin failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to unpin item")
    return {"success": True}


@router.post("/items/mark-seen")
def mark_seen(
    body: SeenPayload = Body(...),
    current_user: AuthUser = Depends(get_current_user),
):
    """Record that the user has now seen/opened this item."""
    db.ensure_item_tables_once()
    uid = _user_id(current_user)
    src = _validate_source(body.source)
    try:
        execute(
            """
            INSERT INTO user_item_seen (user_id, source, item_id, seen_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id, source, item_id)
            DO UPDATE SET seen_at = NOW()
            """,
            [uid, src, body.item_id[:128]],
        )
    except Exception as exc:
        log.warning("mark_seen failed: %s", exc)
        # Seen-tracking is best-effort; never break the UI over it.
        return {"success": False}
    return {"success": True}


def load_pin_index(user_id: str, source: str) -> set[str]:
    """Return the set of item_ids the user has pinned for a given source."""
    db.ensure_item_tables_once()
    try:
        rows = query_all(
            "SELECT item_id FROM user_item_pins WHERE user_id = %s AND source = %s",
            [user_id, source],
        )
    except Exception as exc:
        log.warning("load_pin_index failed: %s", exc)
        return set()
    return {str(r["item_id"]) for r in rows}


def load_seen_index(user_id: str, source: str) -> dict[str, str]:
    """Return a map of item_id -> ISO seen_at timestamp for the given source."""
    db.ensure_item_tables_once()
    try:
        rows = query_all(
            "SELECT item_id, seen_at FROM user_item_seen WHERE user_id = %s AND source = %s",
            [user_id, source],
        )
    except Exception as exc:
        log.warning("load_seen_index failed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for r in rows:
        iid = str(r["item_id"])
        seen_at = r["seen_at"]
        out[iid] = seen_at.isoformat() if hasattr(seen_at, "isoformat") else str(seen_at or "")
    return out
