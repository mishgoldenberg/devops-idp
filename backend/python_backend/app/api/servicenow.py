from fastapi import APIRouter, Depends, Query

from ..security import AuthUser, get_current_user


router = APIRouter()


@router.get("/tickets")
def get_tickets(
    username: str | None = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    effective_username = username or current_user.get("username")
    tickets = [
        {
            "sys_id": "INC0012345",
            "number": "INC0012345",
            "short_description": "Unable to access Azure DevOps",
            "state": "In Progress",
            "priority": "2 - High",
            "assigned_to": effective_username,
            "created": "2024-01-14T10:00:00Z",
            "updated": "2024-01-15T09:30:00Z",
            "url": "https://servicenow.internal/nav_to.do?uri=incident.do?sys_id=INC0012345",
        },
        {
            "sys_id": "INC0012346",
            "number": "INC0012346",
            "short_description": "Dashboard widget not loading",
            "state": "New",
            "priority": "3 - Moderate",
            "assigned_to": effective_username,
            "created": "2024-01-15T08:15:00Z",
            "updated": "2024-01-15T08:15:00Z",
            "url": "https://servicenow.internal/nav_to.do?uri=incident.do?sys_id=INC0012346",
        },
        {
            "sys_id": "RITM0045678",
            "number": "RITM0045678",
            "short_description": "Request access to Artifactory",
            "state": "Pending Approval",
            "priority": "4 - Low",
            "assigned_to": effective_username,
            "created": "2024-01-13T14:30:00Z",
            "updated": "2024-01-14T16:00:00Z",
            "url": "https://servicenow.internal/nav_to.do?uri=sc_req_item.do?sys_id=RITM0045678",
        },
    ]
    return {
        "success": True,
        "data": tickets,
        "timestamp": _now_iso(),
    }


@router.get("/stats")
def get_stats(current_user: AuthUser = Depends(get_current_user)):
    stats = {
        "open": 127,
        "in_progress": 43,
        "resolved": 892,
        "total": 1062,
    }
    return {
        "success": True,
        "data": stats,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


