import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from security import AuthUser, get_current_user

router = APIRouter()

USE_MOCK = os.getenv("USE_MOCK_SERVICENOW", "true").lower() in ("true", "1")

# Accept either SERVICENOW_URL (full URL already in .env) or construct from SERVICENOW_INSTANCE
def _resolve_instance() -> str:
    url = os.getenv("SERVICENOW_URL", "").rstrip("/")
    if url and url.startswith("http"):
        return url
    instance = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
    if instance.startswith("http"):
        return instance
    if instance:
        return f"https://{instance}.service-now.com"
    return ""

_INSTANCE = _resolve_instance()
# SERVICENOW_USERNAME is the primary env var name used in the project .env files
_USER = os.getenv("SERVICENOW_USERNAME") or os.getenv("SERVICENOW_USER", "")
_PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")


# ── Pydantic models ──────────────────────────────────────────────────────────

class CreateTicketRequest(BaseModel):
    title: str
    description: str
    priority: str = "3"  # ServiceNow priority code: 1=Critical 2=High 3=Moderate 4=Low 5=Planning


class ReplyRequest(BaseModel):
    message: str


# ── Mock data ────────────────────────────────────────────────────────────────

_MOCK_TICKETS = [
    {
        "sys_id": "abc001",
        "number": "INC0012345",
        "short_description": "Unable to access Azure DevOps pipelines",
        "state": "In Progress",
        "priority": "2 - High",
        "assigned_to": "admin",
        "opened_at": "2024-01-14T10:00:00Z",
    },
    {
        "sys_id": "abc002",
        "number": "INC0012346",
        "short_description": "Dashboard widget not loading data",
        "state": "New",
        "priority": "3 - Moderate",
        "assigned_to": "admin",
        "opened_at": "2024-01-15T08:15:00Z",
    },
    {
        "sys_id": "abc003",
        "number": "INC0012347",
        "short_description": "SonarQube scan failing on main branch",
        "state": "Resolved",
        "priority": "2 - High",
        "assigned_to": "admin",
        "opened_at": "2024-01-13T14:30:00Z",
    },
]

_MOCK_CONVERSATIONS: dict[str, list[dict]] = {
    "abc001": [
        {
            "sys_id": "msg001",
            "sys_created_by": "admin",
            "sys_created_on": "2024-01-14T10:00:00Z",
            "value": "I am unable to access Azure DevOps pipelines. The page shows a 403 error.",
        },
        {
            "sys_id": "msg002",
            "sys_created_by": "support.agent",
            "sys_created_on": "2024-01-14T11:30:00Z",
            "value": "Thank you for reporting this. We are investigating the permissions issue. Can you confirm your username?",
        },
        {
            "sys_id": "msg003",
            "sys_created_by": "admin",
            "sys_created_on": "2024-01-14T12:00:00Z",
            "value": "My username is admin@company.com. This started happening after yesterday's SSO configuration change.",
        },
    ],
    "abc002": [
        {
            "sys_id": "msg010",
            "sys_created_by": "admin",
            "sys_created_on": "2024-01-15T08:15:00Z",
            "value": "The Artifactory widget on my dashboard is not loading. It shows a blank card with no data.",
        },
    ],
    "abc003": [
        {
            "sys_id": "msg020",
            "sys_created_by": "admin",
            "sys_created_on": "2024-01-13T14:30:00Z",
            "value": "SonarQube scans are failing on our main branch with a connection timeout error.",
        },
        {
            "sys_id": "msg021",
            "sys_created_by": "support.agent",
            "sys_created_on": "2024-01-13T16:00:00Z",
            "value": "This has been resolved — the SonarQube server was temporarily unreachable due to maintenance. Please re-run your scans.",
        },
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snow_client() -> httpx.Client:
    return httpx.Client(
        base_url=_INSTANCE,
        auth=(_USER, _PASSWORD),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15.0,
    )


_STATE_MAP = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Cancelled",
}


def _map_state(code: str) -> str:
    return _STATE_MAP.get(str(code), code)


def _extract_display(field) -> str:
    if isinstance(field, dict):
        return field.get("display_value") or field.get("value") or ""
    return str(field) if field else ""


def _map_ticket(raw: dict) -> dict:
    return {
        "sys_id": raw.get("sys_id", ""),
        "number": raw.get("number", ""),
        "short_description": raw.get("short_description", ""),
        "state": _map_state(raw.get("state", "")),
        "priority": _extract_display(raw.get("priority", "")),
        "assigned_to": _extract_display(raw.get("assigned_to", "")),
        "opened_at": raw.get("opened_at", ""),
    }


def _map_message(raw: dict) -> dict:
    return {
        "sys_id": raw.get("sys_id", ""),
        "sys_created_by": raw.get("sys_created_by", ""),
        "sys_created_on": raw.get("sys_created_on", ""),
        "value": raw.get("value", ""),
    }


def _raise_snow_error(exc: Exception, context: str) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ServiceNow returned an error while {context}: {exc.response.status_code}",
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"ServiceNow is unreachable while {context}.",
    )


# ── GET /tickets ─────────────────────────────────────────────────────────────

@router.get("/tickets")
def get_tickets(current_user: AuthUser = Depends(get_current_user)):
    assigned_user = current_user.get("username", "admin")

    if USE_MOCK:
        tickets = [t for t in _MOCK_TICKETS if t["assigned_to"] == assigned_user]
        return {"success": True, "data": tickets, "timestamp": _now_iso()}

    try:
        with _snow_client() as client:
            resp = client.get(
                "/api/now/table/incident",
                params={
                    "sysparm_query": f"assigned_to.user_name={assigned_user}^active=true",
                    "sysparm_fields": "sys_id,number,short_description,state,priority,assigned_to,opened_at",
                    "sysparm_limit": 50,
                    "sysparm_display_value": "true",
                },
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
    except Exception as exc:
        _raise_snow_error(exc, "fetching tickets")

    return {
        "success": True,
        "data": [_map_ticket(r) for r in records],
        "timestamp": _now_iso(),
    }


# ── GET /tickets/{sys_id} ─────────────────────────────────────────────────────

@router.get("/tickets/{sys_id}")
def get_ticket_detail(sys_id: str, current_user: AuthUser = Depends(get_current_user)):
    if USE_MOCK:
        ticket = next((t for t in _MOCK_TICKETS if t["sys_id"] == sys_id), None)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        conversation = _MOCK_CONVERSATIONS.get(sys_id, [])
        return {
            "success": True,
            "data": {**ticket, "description": ticket["short_description"], "conversation": conversation},
            "timestamp": _now_iso(),
        }

    try:
        with _snow_client() as client:
            ticket_resp = client.get(
                f"/api/now/table/incident/{sys_id}",
                params={"sysparm_display_value": "true"},
            )
            ticket_resp.raise_for_status()
            raw = ticket_resp.json().get("result", {})

            journal_resp = client.get(
                "/api/now/table/sys_journal_field",
                params={
                    "sysparm_query": f"element_id={sys_id}^nameINincident^elementINcomments,work_notes",
                    "sysparm_fields": "sys_id,sys_created_by,sys_created_on,value,element",
                    "sysparm_orderby": "sys_created_on",
                    "sysparm_limit": 100,
                },
            )
            journal_resp.raise_for_status()
            messages = journal_resp.json().get("result", [])

    except Exception as exc:
        _raise_snow_error(exc, "fetching ticket details")

    ticket = _map_ticket(raw)
    ticket["description"] = raw.get("description", raw.get("short_description", ""))
    ticket["conversation"] = [_map_message(m) for m in messages]

    return {"success": True, "data": ticket, "timestamp": _now_iso()}


# ── POST /tickets/{sys_id}/reply ─────────────────────────────────────────────

@router.post("/tickets/{sys_id}/reply")
def reply_to_ticket(
    sys_id: str,
    body: ReplyRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if USE_MOCK:
        new_message = {
            "sys_id": f"msg_{int(datetime.now(timezone.utc).timestamp())}",
            "sys_created_by": current_user.get("username", "admin"),
            "sys_created_on": _now_iso(),
            "value": body.message,
        }
        _MOCK_CONVERSATIONS.setdefault(sys_id, []).append(new_message)
        return {"success": True, "data": new_message, "timestamp": _now_iso()}

    try:
        with _snow_client() as client:
            resp = client.patch(
                f"/api/now/table/incident/{sys_id}",
                json={"comments": body.message},
                params={"sysparm_display_value": "true"},
            )
            resp.raise_for_status()
    except Exception as exc:
        _raise_snow_error(exc, "sending reply")

    new_message = {
        "sys_id": "",
        "sys_created_by": current_user.get("username", "admin"),
        "sys_created_on": _now_iso(),
        "value": body.message,
    }
    return {"success": True, "data": new_message, "timestamp": _now_iso()}


# ── POST /tickets ─────────────────────────────────────────────────────────────

@router.post("/tickets")
def create_ticket(
    body: CreateTicketRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    assigned_user = current_user.get("username", "admin")

    _PRIORITY_LABELS = {
        "1": "1 - Critical",
        "2": "2 - High",
        "3": "3 - Moderate",
        "4": "4 - Low",
        "5": "5 - Planning",
    }
    priority_label = _PRIORITY_LABELS.get(str(body.priority), "3 - Moderate")

    if USE_MOCK:
        new_sys_id = f"new_{int(datetime.now(timezone.utc).timestamp())}"
        ticket_number = f"INC{int(datetime.now(timezone.utc).timestamp()) % 10_000_000:07d}"
        new_ticket = {
            "sys_id": new_sys_id,
            "number": ticket_number,
            "short_description": body.title,
            "state": "New",
            "priority": priority_label,
            "assigned_to": assigned_user,
            "opened_at": _now_iso(),
            "description": body.description,
        }
        _MOCK_TICKETS.append(new_ticket)
        _MOCK_CONVERSATIONS[new_sys_id] = [
            {
                "sys_id": f"msg_init_{new_sys_id}",
                "sys_created_by": assigned_user,
                "sys_created_on": _now_iso(),
                "value": body.description,
            }
        ]
        return {"success": True, "data": new_ticket, "timestamp": _now_iso()}

    try:
        with _snow_client() as client:
            resp = client.post(
                "/api/now/table/incident",
                json={
                    "short_description": body.title,
                    "description": body.description,
                    "assigned_to": assigned_user,
                    "caller_id": assigned_user,
                    "priority": str(body.priority),
                },
                params={"sysparm_display_value": "true"},
            )
            resp.raise_for_status()
            raw = resp.json().get("result", {})
    except Exception as exc:
        _raise_snow_error(exc, "creating ticket")

    ticket = _map_ticket(raw)
    ticket["description"] = body.description
    ticket["conversation"] = []

    return {"success": True, "data": ticket, "timestamp": _now_iso()}


# ── GET /stats (existing widget endpoint, kept for compatibility) ─────────────

@router.get("/stats")
def get_stats(current_user: AuthUser = Depends(get_current_user)):
    if USE_MOCK:
        return {
            "success": True,
            "data": {"open": 127, "in_progress": 43, "resolved": 892, "total": 1062},
            "timestamp": _now_iso(),
        }

    try:
        with _snow_client() as client:
            new_resp = client.get(
                "/api/now/table/incident",
                params={"sysparm_query": "state=1", "sysparm_count": "true", "sysparm_limit": 1},
            )
            new_resp.raise_for_status()
            new_count = int(new_resp.headers.get("X-Total-Count", 0))

            ip_resp = client.get(
                "/api/now/table/incident",
                params={"sysparm_query": "state=2", "sysparm_count": "true", "sysparm_limit": 1},
            )
            ip_resp.raise_for_status()
            ip_count = int(ip_resp.headers.get("X-Total-Count", 0))

            res_resp = client.get(
                "/api/now/table/incident",
                params={"sysparm_query": "state=6", "sysparm_count": "true", "sysparm_limit": 1},
            )
            res_resp.raise_for_status()
            res_count = int(res_resp.headers.get("X-Total-Count", 0))

    except Exception as exc:
        _raise_snow_error(exc, "fetching stats")

    return {
        "success": True,
        "data": {
            "open": new_count,
            "in_progress": ip_count,
            "resolved": res_count,
            "total": new_count + ip_count + res_count,
        },
        "timestamp": _now_iso(),
    }
