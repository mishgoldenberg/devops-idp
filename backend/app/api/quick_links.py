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
    """Return active admin-managed Quick Links; template falls back if empty."""
    rows = query_all(
        """
        SELECT id, name, url, icon_url, sort_order
        FROM quick_links
        WHERE is_active = true
        ORDER BY sort_order ASC, id ASC
        """
    )
    return {
        "success": True,
        "data": [_row_to_quick_link(row) for row in rows],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
