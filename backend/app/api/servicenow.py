import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

import db
import cache
from security import AuthUser, get_current_user

router = APIRouter()
_log = logging.getLogger(__name__)


def _ensure_user_tickets_table() -> None:
    # Keep ownership mapping available even when startup table creation was skipped.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_tickets (
            id          SERIAL PRIMARY KEY,
            user_email  VARCHAR(255) NOT NULL,
            sys_id      VARCHAR(64)  NOT NULL,
            ticket_number VARCHAR(32),
            created_at  TIMESTAMP DEFAULT NOW(),
            UNIQUE (user_email, sys_id)
        )
        """
    )

def _use_mock() -> bool:
    return os.getenv("USE_MOCK_SERVICENOW", "true").lower() in ("true", "1")

# Accept either SERVICENOW_URL (full URL already in .env) or construct from SERVICENOW_INSTANCE.
# Also tolerates URLs stored without the https:// scheme.
def _resolve_instance() -> str:
    url = os.getenv("SERVICENOW_URL", "").strip().rstrip("/")
    if url:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        # Stored without scheme (e.g. "mycompany.service-now.com")
        return f"https://{url}"
    instance = os.getenv("SERVICENOW_INSTANCE", "").strip().rstrip("/")
    if instance.startswith("http://") or instance.startswith("https://"):
        return instance
    if instance:
        return f"https://{instance}.service-now.com"
    return ""


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
    user = os.getenv("SERVICENOW_USERNAME") or os.getenv("SERVICENOW_USER", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")
    return httpx.Client(
        base_url=_resolve_instance(),
        auth=(user, password),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15.0,
        follow_redirects=True,
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
        # Widgets use sys_updated_on to show the "new updates" indicator; fall
        # back to opened_at if the field was not requested.
        "updated_at": raw.get("sys_updated_on", "") or raw.get("opened_at", ""),
    }


def _map_message(raw: dict) -> dict:
    return {
        "sys_id": raw.get("sys_id", ""),
        "sys_created_by": raw.get("sys_created_by", ""),
        "sys_created_on": raw.get("sys_created_on", ""),
        "value": raw.get("value", ""),
    }


def _raise_snow_error(exc: Exception, context: str) -> None:
    """
    Emit a ServiceNow failure as a 502 with a **plain-string** detail.

    Every branch returns a single user-readable line — never a dict — so the
    frontend can safely do ``errorEl.textContent = json.detail`` without
    producing the dreaded ``[object Object]``. Full upstream body (JSON and
    headers) is still logged server-side so admins can debug the root cause.
    """
    instance = _resolve_instance()
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "ServiceNow is not configured: SERVICENOW_URL env var is missing or empty. "
                "Set it to your instance URL (e.g. https://mycompany.service-now.com)."
            ),
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        # Best-effort extraction of the ServiceNow error.message — the common
        # shape is ``{"error": {"message": "...", "detail": "..."}}``. Anything
        # that doesn't match is logged server-side and surfaced generically.
        snow_message = ""
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    snow_message = str(err.get("message") or err.get("detail") or "").strip()
                if not snow_message:
                    snow_message = str(body.get("message") or "").strip()
        except Exception:
            snow_message = exc.response.text[:200].strip()

        _log.warning(
            "ServiceNow %s failed (context=%s, instance=%s): status=%s body=%s",
            context, context, instance, status_code, exc.response.text[:400],
        )

        user_msg = f"ServiceNow returned HTTP {status_code} while {context}."
        if snow_message:
            user_msg += f" {snow_message}"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=user_msg)

    _log.warning(
        "ServiceNow %s unreachable (instance=%s): %s: %s",
        context, instance, type(exc).__name__, exc,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"Could not reach ServiceNow while {context}. "
            "Check that SERVICENOW_URL and credentials are configured and the "
            "instance is reachable from the portal."
        ),
    )


# ── GET /status (config health-check, no auth required) ──────────────────────

@router.get("/status")
def get_status():
    instance = _resolve_instance()
    user = os.getenv("SERVICENOW_USERNAME") or os.getenv("SERVICENOW_USER", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")

    result = {
        "mock_mode": _use_mock(),
        "instance_url": instance or None,
        "username": user or None,
        "password_length": len(password),
        "ready": _use_mock() or (bool(instance) and bool(user)),
    }

    if not _use_mock() and instance and user:
        try:
            with httpx.Client(
                base_url=instance,
                auth=(user, password),
                headers={"Accept": "application/json"},
                timeout=10.0,
                follow_redirects=True,
            ) as client:
                resp = client.get("/api/now/table/incident", params={"sysparm_limit": "1"})
                result["connection_test"] = {"status_code": resp.status_code, "ok": resp.status_code == 200}
                if resp.status_code != 200:
                    try:
                        result["connection_test"]["snow_response"] = resp.json()
                    except Exception:
                        result["connection_test"]["snow_response"] = resp.text[:300]
        except Exception as exc:
            result["connection_test"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return result


# ── GET /tickets ─────────────────────────────────────────────────────────────

@router.get("/tickets")
def get_tickets(current_user: AuthUser = Depends(get_current_user)):
    user_email = current_user.get("username", "")

    if not _use_mock() and not _resolve_instance():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "ServiceNow is not configured: SERVICENOW_URL env var is missing or empty. "
                "Set it to your instance URL (e.g. https://mycompany.service-now.com)."
            ),
        )

    if _use_mock():
        tickets = [t for t in _MOCK_TICKETS if t["assigned_to"] == user_email]
        return {"success": True, "data": tickets, "timestamp": _now_iso()}

    # Primary source of truth: ServiceNow itself, scoped to the configured
    # portal account (secret variable) or the active user email fallback.
    snow_user = (os.getenv("SERVICENOW_USERNAME") or os.getenv("SERVICENOW_USER") or "").strip()
    if not snow_user:
        snow_user = str(user_email)

    query = (
        f"opened_by={snow_user}"
        f"^ORcaller_id.user_name={snow_user}"
        "^ORDERBYDESCsys_created_on"
    )

    def _fetch_live():
        try:
            with _snow_client() as client:
                resp = client.get(
                    "/api/now/table/incident",
                    params={
                        "sysparm_query": query,
                        "sysparm_fields": "sys_id,number,short_description,state,priority,assigned_to,opened_at,sys_updated_on",
                        "sysparm_limit": 50,
                        "sysparm_display_value": "true",
                    },
                )
                resp.raise_for_status()
                return [_map_ticket(r) for r in resp.json().get("result", [])]
        except Exception as exc:
            _raise_snow_error(exc, "fetching tickets")

    # 60s cache keyed on the effective ServiceNow identity; the list of my
    # tickets is hit by the support page header, badges, and the dashboard
    # widget simultaneously.
    try:
        from integrations_cache import cached_external
        records = cached_external(
            "snow",
            (user_email or "anon").lower(),
            f"tickets:{snow_user}",
            _fetch_live,
            ttl=60,
        )
    except HTTPException:
        raise
    except Exception:
        records = _fetch_live()

    return {"success": True, "data": records, "timestamp": _now_iso()}


# ── GET /tickets/{sys_id} ─────────────────────────────────────────────────────

@router.get("/tickets/{sys_id}")
def get_ticket_detail(sys_id: str, current_user: AuthUser = Depends(get_current_user)):
    if _use_mock():
        ticket = next((t for t in _MOCK_TICKETS if t["sys_id"] == sys_id), None)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        conversation = _MOCK_CONVERSATIONS.get(sys_id, [])
        return {
            "success": True,
            "data": {**ticket, "description": ticket["short_description"], "conversation": conversation},
            "timestamp": _now_iso(),
        }

    def _fetch():
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

            ticket = _map_ticket(raw)
            ticket["description"] = raw.get("description", raw.get("short_description", ""))
            ticket["conversation"] = [_map_message(m) for m in messages]
            return ticket

    try:
        ticket_data = cache.get_cached(f"snow:detail:{sys_id}", ttl=10, producer=_fetch)
    except Exception as exc:
        _raise_snow_error(exc, "fetching ticket details")

    return {"success": True, "data": ticket_data, "timestamp": _now_iso()}


# ── POST /tickets/{sys_id}/reply ─────────────────────────────────────────────

@router.post("/tickets/{sys_id}/reply")
def reply_to_ticket(
    sys_id: str,
    body: ReplyRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if _use_mock():
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

    # Invalidate conversation cache so the next poll sees the new reply immediately
    cache.invalidate(f"snow:detail:{sys_id}")

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
    user_email = current_user.get("username", "")

    # Hardening: reject empty title/description so we never post meaningless
    # tickets to ServiceNow. Mirrors the frontend guard for defense-in-depth.
    title_clean = (body.title or "").strip()
    description_clean = (body.description or "").strip()
    if not title_clean:
        raise HTTPException(status_code=400, detail="Ticket title is required.")
    if not description_clean:
        raise HTTPException(status_code=400, detail="Ticket description is required.")
    if len(title_clean) > 160:
        raise HTTPException(
            status_code=400,
            detail="Ticket title is too long (max 160 characters).",
        )

    try:
        _ensure_user_tickets_table()
    except Exception:
        pass

    # Safe Mode: simulate success without hitting ServiceNow.
    try:
        import safe_mode as _safe_mode
        import audit as _audit
        if _safe_mode.is_enabled():
            ts = int(datetime.now(timezone.utc).timestamp())
            simulated = {
                "sys_id": f"safe_{ts}",
                "number": f"INC-SAFE-{ts % 10_000_000:07d}",
                "short_description": title_clean,
                "state": "New",
                "priority": str(body.priority or "3"),
                "assigned_to": user_email,
                "opened_at": _now_iso(),
                "description": description_clean,
                "safe_mode": True,
            }
            try:
                _audit.log(
                    _audit.Action.TICKET_CREATED,
                    user_email=user_email,
                    metadata={"safe_mode": True, "title": title_clean[:160]},
                )
            except Exception:
                pass
            return {"success": True, "data": simulated, "timestamp": _now_iso()}
    except Exception:
        pass

    _PRIORITY_LABELS = {
        "1": "1 - Critical",
        "2": "2 - High",
        "3": "3 - Moderate",
        "4": "4 - Low",
        "5": "5 - Planning",
    }
    priority_label = _PRIORITY_LABELS.get(str(body.priority), "3 - Moderate")

    if _use_mock():
        new_sys_id = f"new_{int(datetime.now(timezone.utc).timestamp())}"
        ticket_number = f"INC{int(datetime.now(timezone.utc).timestamp()) % 10_000_000:07d}"
        new_ticket = {
            "sys_id": new_sys_id,
            "number": ticket_number,
            "short_description": body.title,
            "state": "New",
            "priority": priority_label,
            "assigned_to": user_email,
            "opened_at": _now_iso(),
            "description": body.description,
        }
        _MOCK_TICKETS.append(new_ticket)
        _MOCK_CONVERSATIONS[new_sys_id] = [
            {
                "sys_id": f"msg_init_{new_sys_id}",
                "sys_created_by": user_email,
                "sys_created_on": _now_iso(),
                "value": body.description,
            }
        ]
        try:
            from observability_tracking import (
                priority_to_severity_band,
                record_servicenow_portal_ticket,
            )

            record_servicenow_portal_ticket(
                ticket_number or new_sys_id,
                user_email,
                priority_to_severity_band(str(body.priority)),
                body.title or "",
            )
        except Exception:
            pass
        try:
            import audit as _audit
            _audit.log(
                _audit.Action.TICKET_CREATED,
                user_email=user_email,
                metadata={
                    "ticket_number": ticket_number,
                    "sys_id": new_sys_id,
                    "priority": str(body.priority or ""),
                    "title": title_clean[:160],
                    "mock": True,
                },
            )
        except Exception:
            pass
        return {"success": True, "data": new_ticket, "timestamp": _now_iso()}

    # ServiceNow derives 'priority' from impact × urgency via a lookup matrix —
    # setting 'priority' directly is ignored.  Map portal priorities to the
    # impact/urgency pair that produces the matching calculated priority.
    _PRIORITY_TO_IMPACT_URGENCY = {
        "1": ("1", "1"),  # Critical  → impact 1 (High),    urgency 1 (High)
        "2": ("1", "2"),  # High      → impact 1 (High),    urgency 2 (Medium)
        "3": ("2", "2"),  # Moderate  → impact 2 (Medium),  urgency 2 (Medium)
        "4": ("3", "2"),  # Low       → impact 3 (Low),     urgency 2 (Medium)
        "5": ("3", "3"),  # Planning  → impact 3 (Low),     urgency 3 (Low)
    }
    impact, urgency = _PRIORITY_TO_IMPACT_URGENCY.get(str(body.priority), ("2", "2"))

    snow_user = os.getenv("SERVICENOW_USERNAME") or os.getenv("SERVICENOW_USER", "")
    try:
        with _snow_client() as client:
            resp = client.post(
                "/api/now/table/incident",
                json={
                    "short_description": body.title,
                    "description": body.description,
                    "impact": impact,
                    "urgency": urgency,
                    # Use the service account as caller so the field is never empty
                    "caller_id": snow_user,
                    # Record the portal user in work notes for visibility in ServiceNow
                    "work_notes": f"Submitted via DevOps Control Center by: {user_email}",
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

    # Record the ticket ownership in the local DB so "My Tickets" can find it
    try:
        db.execute(
            """
            INSERT INTO user_tickets (user_email, sys_id, ticket_number)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_email, sys_id) DO NOTHING
            """,
            [user_email, ticket["sys_id"], ticket["number"]],
        )
    except Exception:
        pass  # Non-fatal: the ticket was created in ServiceNow successfully

    # Bust the ticket-list cache so the new ticket appears immediately
    cache.invalidate(f"snow:tickets:{user_email}")
    try:
        from integrations_cache import invalidate_owner
        invalidate_owner("snow", user_email)
    except Exception:
        pass

    try:
        from observability_tracking import (
            priority_to_severity_band,
            record_servicenow_portal_ticket,
        )

        record_servicenow_portal_ticket(
            str(ticket.get("number") or ticket.get("sys_id") or ""),
            user_email,
            priority_to_severity_band(str(body.priority)),
            body.title or "",
        )
    except Exception:
        pass

    try:
        import audit as _audit
        _audit.log(
            _audit.Action.TICKET_CREATED,
            user_email=user_email,
            metadata={
                "ticket_number": ticket.get("number"),
                "sys_id": ticket.get("sys_id"),
                "priority": str(body.priority or ""),
                "title": title_clean[:160],
            },
        )
    except Exception:
        pass

    return {"success": True, "data": ticket, "timestamp": _now_iso()}


# ── GET /stats (existing widget endpoint, kept for compatibility) ─────────────

@router.get("/stats")
def get_stats(current_user: AuthUser = Depends(get_current_user)):
    if _use_mock():
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
