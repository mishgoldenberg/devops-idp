import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import db
from security import AuthUser, get_current_user
from widget_registry import ALL_KEYS as _ALLOWED_WIDGET_KEYS
from .notifications import notify_once
from .pins import load_pin_index, load_seen_index
from common import now_iso

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/widgets/sync")
def sync_user_widgets(
    payload: Dict[str, Any] = Body(default={}),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Replace the authenticated user's active-widget list.
    Observability reads from user_widgets so removals decrement counts immediately.
    Silent-fails on DB errors so dashboard rendering is never blocked.
    """
    widgets = (payload or {}).get("widgets") or []
    session_id = (payload or {}).get("session_id")
    session_id = str(session_id).strip()[:128] if session_id else None

    if not isinstance(widgets, list):
        raise HTTPException(status_code=400, detail="'widgets' must be a list")

    keys = [
        str(k).strip()[:255]
        for k in widgets
        if isinstance(k, str) and str(k).strip() in _ALLOWED_WIDGET_KEYS
    ]

    user_id = str(
        current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
    ).strip()[:255]
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        db.ensure_observability_tables_once()
        # One transaction, two statements. It was a DELETE and then an INSERT per widget,
        # each its own round trip and commit (600 ms for sixteen widgets), and a reader
        # between them saw this person with no widgets at all.
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_widgets WHERE user_id = %s", [user_id])
                if keys:
                    cur.execute(
                        """
                        INSERT INTO user_widgets (user_id, widget_key, session_id, created_at)
                        SELECT %s, k, %s, NOW() FROM unnest(%s::text[]) AS k
                        ON CONFLICT (user_id, widget_key) DO NOTHING
                        """,
                        [user_id, session_id, list(dict.fromkeys(keys))],
                    )
    except Exception:
        log.warning("dashboard: widget list not synced for %s", user_id, exc_info=True)
    return {"success": True, "data": {"widgets": keys}}


# ─────────────────────────────────────────────────────────────────────────────
# Interactive dashboard feeds
#
# Two widgets (Azure DevOps work items, ServiceNow incidents) share the same
# normalized contract so the frontend can render them with a single component:
#
#   {
#     "data": [
#       { id, title, status, updated_at, url, source, pinned, has_update, meta }
#     ],
#     "error": "Service unavailable" | "",
#     "pins_available": true,
#     "timestamp": "..."
#   }
#
# Failures are swallowed into `error` so the widget UX stays stable (HTTP 200,
# empty list, inline warning) instead of throwing 502s at the browser.
# ─────────────────────────────────────────────────────────────────────────────


def _user_id(current_user: AuthUser) -> str:
    return str(
        current_user.get("email")
        or current_user.get("username")
        or current_user.get("id")
        or ""
    ).strip()[:255]


def _parse_ts(raw: Any) -> float:
    """Best-effort parse of an ISO timestamp to epoch seconds (0.0 on failure)."""
    if not raw:
        return 0.0
    if isinstance(raw, datetime):
        return raw.timestamp()
    try:
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        # ServiceNow returns "YYYY-MM-DD HH:MM:SS" in display-value mode.
        try:
            return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0.0


def _apply_user_flags(
    items: List[Dict[str, Any]],
    *,
    user_id: str,
    source: str,
) -> List[Dict[str, Any]]:
    """Annotate items with pinned/has_update and sort pinned → recent first."""
    pins = load_pin_index(user_id, source) if user_id else set()
    seen = load_seen_index(user_id, source) if user_id else {}
    for item in items:
        iid = str(item.get("id", ""))
        item["pinned"] = iid in pins
        updated_ts = _parse_ts(item.get("updated_at"))
        seen_ts = _parse_ts(seen.get(iid)) if iid in seen else 0.0
        # The dot means "there is an update you have not seen" — not "you have never
        # clicked this". Treating never-opened as unread put a red dot on EVERY row,
        # including tickets the user raised seconds ago that nobody has touched, which
        # made the indicator meaningless.
        #
        # Never opened  -> unread only if the item has actually CHANGED since it was
        #                  created (i.e. someone updated it after you filed it).
        # Opened before -> unread if it changed after you last looked. The 1s slack
        #                  absorbs identical stamps.
        created_ts = _parse_ts(item.get("created_at") or (item.get("meta") or {}).get("created_at"))
        if seen_ts == 0.0:
            item["has_update"] = bool(created_ts) and updated_ts > created_ts + 1.0
        else:
            item["has_update"] = updated_ts > seen_ts + 1.0
    items.sort(
        key=lambda it: (
            0 if it.get("pinned") else 1,
            -_parse_ts(it.get("updated_at")),
        )
    )
    return items


@router.get("/ado-items")
def get_ado_items(
    project: Optional[str] = Query(None),
    work_item_type: Optional[str] = Query(None),
    iteration: Optional[str] = Query(None),
    area_path: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Latest Azure DevOps work items assigned to the current user, normalized. The
    type, iteration and area filters are passed straight through."""
    from api.azure_devops import get_work_items as _ado_work_items

    uid = _user_id(current_user)
    error = ""
    items: List[Dict[str, Any]] = []
    try:
        result = _ado_work_items(
            project=project,
            work_item_type=work_item_type,
            iteration=iteration,
            area_path=area_path,
            current_user=current_user,
        )
        rows = result.get("data", []) if isinstance(result, dict) else []
        for wi in rows:
            assigned = wi.get("assigned_to")
            if isinstance(assigned, dict):
                assigned_name = assigned.get("displayName") or assigned.get("uniqueName") or ""
            else:
                assigned_name = str(assigned or "")
            items.append(
                {
                    "id": str(wi.get("id", "")),
                    "title": wi.get("title") or f"Work item #{wi.get('id', '')}",
                    "status": wi.get("state") or "",
                    "updated_at": wi.get("changed_date") or wi.get("created_date") or "",
                    "url": wi.get("url") or "",
                    "source": "azure_devops",
                    "meta": {
                        "id_display": f"#{wi.get('id', '')}" if wi.get("id") else "",
                        "type": wi.get("type") or "",
                        "project": wi.get("project") or "",
                        "collection": wi.get("collection") or "",
                        "iteration": wi.get("iteration") or "",
                        "area_path": wi.get("area_path") or "",
                        "state_category": wi.get("state_category") or "",
                        "assigned_to": assigned_name,
                    },
                }
            )
    except HTTPException as exc:
        log.info("get_ado_items: ADO unavailable: %s", exc.detail)
        detail = exc.detail if isinstance(exc.detail, str) else "Azure DevOps unavailable"
        error = detail or "Azure DevOps unavailable"
    except Exception as exc:
        log.warning("get_ado_items: unexpected error: %s", exc)
        error = "Service unavailable"

    # The widget is a double-width sprint board now, so it has room for more than
    # the 20 rows the old narrow list showed.
    items = _apply_user_flags(items[:50], user_id=uid, source="azure_devops")
    return {
        "success": True,
        "data": items,
        "error": error,
        "source": "azure_devops",
        "timestamp": now_iso(),
    }


@router.get("/snow-items")
def get_snow_items(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Latest ServiceNow incidents for the current user, normalized."""
    from api.servicenow import get_tickets as _snow_get_tickets

    uid = _user_id(current_user)
    error = ""
    items: List[Dict[str, Any]] = []
    try:
        result = _snow_get_tickets(current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        for t in rows:
            sys_id = t.get("sys_id") or ""
            items.append(
                {
                    "id": sys_id,
                    "title": t.get("short_description") or t.get("number") or "Incident",
                    "status": t.get("state") or "",
                    "updated_at": t.get("updated_at") or t.get("opened_at") or "",
                    # Needed by _apply_user_flags: an unopened ticket only counts as
                    # unread if it changed AFTER it was raised.
                    "created_at": t.get("opened_at") or "",
                    # Clicking navigates inside the portal; the support page
                    # picks up ?ticket=<sys_id> and auto-opens the conversation.
                    "url": f"/ui/support?ticket={sys_id}" if sys_id else "/ui/support",
                    "source": "servicenow",
                    # Shown as a severity word; the derived priority number is carried,
                    # not shown.
                    "urgency": t.get("urgency") or "",
                    # Whether THIS user has an update they haven't opened — computed in
                    # servicenow.py against their read receipts.
                    "unread": bool(t.get("unread")),
                    "meta": {
                        "id_display": t.get("number") or "",
                        "urgency": t.get("urgency") or "",
                        "priority": t.get("priority") or "",
                        "assigned_to": t.get("assigned_to") or "",
                    },
                }
            )
    except HTTPException as exc:
        # Upstream raised a structured error — don't leak JSON decode traces.
        log.info("get_snow_items: SN unavailable: %s", exc.detail)
        error = "Service unavailable"
    except Exception as exc:
        log.warning("get_snow_items: unexpected error: %s", exc)
        error = "Service unavailable"

    items = _apply_user_flags(items[:20], user_id=uid, source="servicenow")

    # A ticket with an unseen update is exactly what deserves a notification, and we have
    # just worked out which ones those are — so no background poller is needed. notify_once
    # keys on the specific update, so a given change notifies once and not again on every
    # 60s widget refresh.
    for item in items:
        if not item.get("has_update"):
            continue
        number = (item.get("meta") or {}).get("id_display") or item.get("id")
        notify_once(
            user_email=uid,
            message=f"{number} has a new update: {item.get('title') or 'ticket'}",
            group_key=f"snow-update:{item.get('id')}:{item.get('updated_at')}",
            notif_type="ticket_update",
            related_id=str(item.get("id") or ""),
            link=item.get("url") or "/ui/support",
        )

    return {
        "success": True,
        "data": items,
        "error": error,
        "source": "servicenow",
        "timestamp": now_iso(),
    }

