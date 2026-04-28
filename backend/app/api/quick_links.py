from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter

from db import query_all

router = APIRouter()


def _row_to_quick_link(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "name": row.get("name") or "",
        "url": row.get("url") or "",
        "icon_url": row.get("icon_url") or "",
        "sort_order": int(row.get("sort_order") or 0),
    }


@router.get("")
def list_quick_links() -> Dict[str, Any]:
    """Return active admin-managed Quick Links; template falls back if empty.

    De-duplicates at read time on (lower(name), lower(url)) so historical
    duplicates created before the create endpoint became idempotent don't
    show up twice on the dashboard. The first occurrence (lowest sort_order
    / id) wins.
    """
    rows = query_all(
        """
        SELECT id, name, url, icon_url, sort_order
        FROM quick_links
        WHERE is_active = true
        ORDER BY sort_order ASC, id ASC
        """
    )
    seen: set[tuple[str, str]] = set()
    deduped: list[Dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("name") or "").strip().lower(),
            str(row.get("url") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(_row_to_quick_link(row))
    return {
        "success": True,
        "data": deduped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
