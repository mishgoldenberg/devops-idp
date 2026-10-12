"""
Support tickets in ServiceNow: the person's own tickets, their conversation, replies
and attachments, and opening a new one through the "Open A Ticket" record producer.

Everything goes through one service account. The `user_tickets` table records which
portal user opened which ticket, and every endpoint that takes a ticket id checks it
against that person's own list (`_require_own_ticket`) before the account touches it.
Configured by SNOW_BASE_URL, SNOW_API_USERNAME and SNOW_API_PASSWORD.
"""

import logging
import os
import re
from html import unescape as _html_unescape
from urllib.parse import quote
from typing import Any, Dict, List, Optional

import httpx

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

import activity
import cache
import db
import identity
import snow_catalog
from common import attachment_type, looks_like_sys_id, now_iso, safe_attachment, truthy
from security import AuthUser, get_current_user, has_effective_admin_access_live
import resilient_http
from resilient_http import far_message

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

def _ticket_owner_key(email: str) -> str:
    """The one form of an address that `user_tickets` is keyed by.

    This is not tidiness, it is the reason a ticket raised through the wizard did
    not appear under My Tickets. The wizard endpoint lowercased the address before
    filing the row; the list endpoint did not lowercase it before looking the row
    up; and `user_email = %s` is case-sensitive in Postgres. An address with a
    capital in it was written under one key and read under another, so the OR-ed
    `sys_idIN` clause was built from an empty list and the ticket -- which exists,
    and is correct -- was invisible to the person who opened it.

    Every reader and writer of that table goes through here now.
    """
    return (email or "").strip().lower()


def _portal_ticket_sys_ids(user_email: str) -> List[str]:
    """sys_ids of the tickets this user opened THROUGH THE PORTAL.

    The producer submits as the service account, so ServiceNow's own fields may not name
    the portal user. Every ticket created here is recorded in ``user_tickets``, and
    ownership is read from there.
    """
    try:
        _ensure_user_tickets_table()
        # Matched on the normalised address rather than the stored one, so rows
        # written before the two sides agreed are still found.
        rows = db.execute_returning(
            "SELECT sys_id FROM user_tickets WHERE LOWER(user_email) = %s",
            [_ticket_owner_key(user_email)],
        )
        return [str(r[0]) for r in (rows or []) if r and r[0]]
    except Exception:
        return []


def _resolve_instance() -> str:
    url = os.getenv("SNOW_BASE_URL", "").strip().rstrip("/")
    if url:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"https://{url}"
    return ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _snow_client() -> httpx.Client:
    user = os.getenv("SNOW_API_USERNAME", "")
    password = os.getenv("SNOW_API_PASSWORD", "")
    return resilient_http.Client(
        base_url=_resolve_instance(),
        auth=(user, password),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=True,
    )


def _snow_ticket_flow_client() -> httpx.Client:
	base_url = (os.getenv("SNOW_BASE_URL") or "").strip().rstrip("/")
	username = (os.getenv("SNOW_API_USERNAME") or "").strip()
	password = os.getenv("SNOW_API_PASSWORD") or ""
	if not base_url or not username or not password:
		raise HTTPException(
			status_code=status.HTTP_502_BAD_GATEWAY,
			detail="ServiceNow ticket creation is not configured. Set SNOW_BASE_URL, SNOW_API_USERNAME, and SNOW_API_PASSWORD.",
		)
	if not base_url.startswith(("http://", "https://")):
		base_url = f"https://{base_url}"
	return resilient_http.Client(
		base_url=base_url,
		auth=(username, password),
		headers={"Accept": "application/json"},
		timeout=httpx.Timeout(30.0, connect=5.0),
		follow_redirects=True,
	)


def _safe_snow_error(exc: Exception, context: str) -> HTTPException:
	if isinstance(exc, HTTPException):
		return exc
	if isinstance(exc, httpx.HTTPStatusError):
		message = far_message(exc.response)
		_log.warning(
			"ServiceNow ticket flow failed while %s: status=%s body=%s",
			context,
			exc.response.status_code,
			exc.response.text[:400],
		)
		detail = f"ServiceNow returned HTTP {exc.response.status_code} while {context}."
		if message:
			detail += f" {message}"
		return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
	_log.warning("ServiceNow ticket flow failed while %s: %s: %s", context, type(exc).__name__, exc)
	return HTTPException(
		status_code=status.HTTP_502_BAD_GATEWAY,
		detail=f"Could not reach ServiceNow while {context}.",
	)


def _required_form_value(name: str, value: Optional[str]) -> str:
	clean = (value or "").strip()
	if not clean:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{name} is required.")
	return clean


def _format_ticket_description(fields: Dict[str, str]) -> str:
	lines = [
		fields["description"],
		"",
		"--- DevOps Hub request details ---",
	]
	for key, value in fields.items():
		if key == "description" or not value:
			continue
		lines.append(f"{key.replace('_', ' ').title()}: {value}")
	return "\n".join(lines).strip()


_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _checked_attachments(attachments: Optional[List[UploadFile]]) -> List[tuple]:
	"""(file name, media type, bytes) for each upload, checked by common.safe_attachment
	before anything is sent, so a refused file never leaves a half-made ticket."""
	files = []
	for upload in attachments or []:
		if not upload or not upload.filename:
			continue
		upload.file.seek(0)
		data = upload.file.read(_MAX_ATTACHMENT_BYTES + 1)
		try:
			files.append((*safe_attachment(upload.filename, data, _MAX_ATTACHMENT_BYTES), data))
		except ValueError as exc:
			raise HTTPException(status_code=400, detail=str(exc))
	return files


def _upload_snow_attachments(client: httpx.Client, record_sys_id: str, files: List[tuple],
							 table: str = "incident") -> int:
	count = 0
	for name, media_type, data in files:
		# The /api/now/attachment/file endpoint wants table_name, table_sys_id and
		# file_name as QUERY params and the file as the raw request body (not a
		# multipart form) — sending multipart drops file_name → HTTP 400.
		try:
			resp = client.post(
				"/api/now/attachment/file",
				params={"table_name": table or "incident", "table_sys_id": record_sys_id, "file_name": name},
				content=data,
				headers={"Content-Type": media_type},
			)
			resp.raise_for_status()
			count += 1
		except Exception as exc:
			raise _safe_snow_error(exc, f"uploading attachment {name}") from exc
	return count


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
        # The widgets show SEVERITY as a word (Urgent / High / Medium / Low), not the
        # derived priority number. With sysparm_display_value=true ServiceNow hands
        # back the choice LABEL, which includes this instance's custom "Urgent" level.
        "urgency": _extract_display(raw.get("urgency", "")),
        "assigned_to": _extract_display(raw.get("assigned_to", "")),
        "opened_at": raw.get("opened_at", ""),
        # Widgets use sys_updated_on to show the "new updates" indicator; fall
        # back to opened_at if the field was not requested.
        "updated_at": raw.get("sys_updated_on", "") or raw.get("opened_at", ""),
    }


# All ServiceNow writes use the shared service account, so a portal reply would
# otherwise be recorded as the SA ("Admin"). We tag each portal comment with an
# invisible marker carrying the real requester's name/email, then strip + parse
# it on read so the chat shows the right author and can colour user vs. team.
_PORTAL_MSG_RE = re.compile(
    r"^\[portal:(?P<name>[^|\]]*)\|(?P<email>[^\]]*)\]\r?\n?(?P<body>.*)$",
    re.DOTALL,
)


def _portal_display_name(user: AuthUser) -> str:
    for key in ("name", "full_name", "display_name", "preferred_username", "username"):
        val = str(user.get(key) or "").strip()
        if val:
            return val
    email = str(user.get("email") or "").strip()
    return email.split("@")[0] if email else "Portal user"


_SNOW_SA_IDENTITIES: Optional[set] = None


def _service_account_identities(client: httpx.Client) -> set:
    """Lowercased {user_name, display name} of the integration service account.

    The portal posts replies as this shared account with no inline author marker,
    so on read we recognise those journal entries by author and attribute them to
    the viewing requester instead of "Support". The display name is resolved from
    sys_user once (overridable via SNOW_SERVICE_ACCOUNT_DISPLAY) and cached.
    """
    global _SNOW_SA_IDENTITIES
    if _SNOW_SA_IDENTITIES is not None:
        return _SNOW_SA_IDENTITIES
    ids: set = set()
    user_name = (os.getenv("SNOW_API_USERNAME", "") or "").strip()
    if user_name:
        ids.add(user_name.lower())
    disp = (os.getenv("SNOW_SERVICE_ACCOUNT_DISPLAY", "") or "").strip()
    if not disp and user_name:
        try:
            resp = client.get(
                "/api/now/table/sys_user",
                params={"sysparm_query": f"user_name={snow_catalog.query_value(user_name)}", "sysparm_fields": "name",
                        "sysparm_limit": "1"},
            )
            if resp.status_code == 200:
                rows = resp.json().get("result") or []
                if rows:
                    disp = str(rows[0].get("name") or "").strip()
        except Exception:
            disp = ""
    if disp:
        ids.add(disp.lower())
    _SNOW_SA_IDENTITIES = ids
    return ids


def _resolve_display_names(client: httpx.Client, user_names: set) -> Dict[str, str]:
    """Map sys_user.user_name -> display name in one batch. Best-effort."""
    names = sorted(u for u in user_names if u)
    if not names:
        return {}
    try:
        resp = client.get(
            "/api/now/table/sys_user",
            params={
                "sysparm_query": "user_nameIN" + ",".join(names),
                "sysparm_fields": "user_name,name",
                "sysparm_limit": str(len(names) + 5),
            },
        )
        if resp.status_code != 200:
            return {}
        out: Dict[str, str] = {}
        for row in resp.json().get("result", []) or []:
            un = row.get("user_name")
            if un:
                out[un] = (row.get("name") or un).strip() or un
        return out
    except Exception:
        return {}


_HTML_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_HTML_BLOCK_RE = re.compile(r"(?i)</\s*(?:p|div|li|tr|h[1-6]|blockquote)\s*>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(value: str) -> str:
    """ServiceNow returns journal/comment text as HTML wrapped in its [code]...[/code]
    markup. Convert it to readable plain text: drop the [code] wrapper, turn block
    and <br> tags into newlines, strip every other tag, and unescape entities. Plain
    text passes through. Prevents the chat showing literal '[code]<div>msg</div>'."""
    if not value:
        return ""
    # Remove ServiceNow's [code]/[/code] HTML wrapper first so the inner markup
    # (if any) is stripped below; without this the chat shows the literal tags.
    v = re.sub(r"(?is)\[/?code\]", "", value)
    if "<" not in v:
        return v.strip()
    v = _HTML_BR_RE.sub("\n", v)
    v = _HTML_BLOCK_RE.sub("\n", v)
    v = _HTML_TAG_RE.sub("", v)
    v = _html_unescape(v)
    v = re.sub(r"\n{3,}", "\n\n", v)
    return v.strip()


def _map_message(
    raw: dict,
    display_names: Optional[Dict[str, str]] = None,
    portal_ids: Optional[set] = None,
    viewer_name: str = "",
) -> dict:
    display_names = display_names or {}
    portal_ids = portal_ids or set()
    created_by = raw.get("sys_created_by", "") or ""
    # Clean HTML BEFORE the portal-marker check: a marker we wrote as plain text can
    # come back wrapped in HTML, which would otherwise defeat the regex match.
    value = _html_to_text(raw.get("value", "") or "")
    marked = _PORTAL_MSG_RE.match(value)
    if marked:
        # Legacy replies that still carry the inline [portal:...] marker.
        author = (marked.group("name") or "").strip() or "Portal user"
        author_email = (marked.group("email") or "").strip()
        value = marked.group("body")
        is_portal_user = True
    elif created_by and created_by.strip().lower() in portal_ids:
        # Authored by the integration service account => it's the portal user's own
        # reply (the Hub viewer is the ticket requester), so show it on their side.
        author = viewer_name or "You"
        author_email = ""
        is_portal_user = True
    else:
        author = display_names.get(created_by) or created_by or "Support"
        author_email = ""
        is_portal_user = False
    return {
        "sys_id": raw.get("sys_id", ""),
        "sys_created_by": created_by,
        "sys_created_on": raw.get("sys_created_on", ""),
        "author": author,
        "author_email": author_email,
        "is_portal_user": is_portal_user,
        "element": raw.get("element", ""),
        "value": value,
    }


# A GET on the incident with sysparm_display_value=true returns each journal
# field (comments / work_notes) as its FULL activity text: one entry per block,
# each starting with a header line "<created_on> - <Author> (<Type>)" followed by
# the entry body, blocks separated by blank lines. We parse that text into
# structured messages because a direct sys_journal_field Table API query returns
# nothing for the service account on this instance (its ACLs filter that table).
_JOURNAL_HEADER_RE = re.compile(
    r"(?m)^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*-\s*(?P<author>.+?)\s*\([^)]*\)\s*$"
)


def _parse_journal_text(text: str, element: str = "comments") -> List[Dict[str, Any]]:
    text = text or ""
    out: List[Dict[str, Any]] = []
    headers = list(_JOURNAL_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[h.end():body_end].strip("\r\n").strip()
        out.append({
            "sys_id": "",
            "sys_created_by": (h.group("author") or "").strip(),
            "sys_created_on": h.group("ts"),
            "value": body,
            "element": element,
        })
    return out


def _map_attachment(raw: dict, ticket_sys_id: str) -> dict:
    content_type = raw.get("content_type", "") or ""
    return {
        "sys_id": raw.get("sys_id", ""),
        "file_name": raw.get("file_name", ""),
        "content_type": content_type,
        "is_image": content_type.startswith("image/"),
        "size_bytes": raw.get("size_bytes", ""),
        "created_on": raw.get("sys_created_on", ""),
        # Served back through the portal so the browser never talks to ServiceNow
        # directly and the service-account auth stays server-side.
        "url": f"/api/support/tickets/{ticket_sys_id}/attachments/{raw.get('sys_id', '')}",
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
                "ServiceNow is not configured: SNOW_BASE_URL env var is missing or empty. "
                "Set it to your instance URL (e.g. https://mycompany.service-now.com)."
            ),
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        # Best-effort extraction of the ServiceNow error.message — the common
        # shape is ``{"error": {"message": "...", "detail": "..."}}``. Anything
        # that doesn't match is logged server-side and surfaced generically.
        snow_message = far_message(exc.response)

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
            "Check that SNOW_BASE_URL and credentials are configured and the "
            "instance is reachable from the portal."
        ),
    )


# ── GET /tickets ─────────────────────────────────────────────────────────────

@router.get("/tickets")
def get_tickets(refresh: bool = False, current_user: AuthUser = Depends(get_current_user)):
    """The signed-in user's tickets. `refresh=1` goes past the 60s cache.

    Pressing Refresh has to mean something. Without this the button was answered
    from the same cached list for up to a minute -- so the one action somebody
    takes when the page looks stale was the one action that could not change it.
    """
    user_email = (current_user.get("email") or current_user.get("username") or "").strip()
    if refresh:
        try:
            from integrations_cache import invalidate_owner
            invalidate_owner("snow", (user_email or "anon").lower())
        except Exception as exc:
            _log.debug("ticket cache invalidation failed: %s", exc)

    if not _resolve_instance():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "ServiceNow is not configured: SNOW_BASE_URL env var is missing or empty. "
                "Set it to your instance URL (e.g. https://mycompany.service-now.com)."
            ),
        )

    # Auth uses the service account (admin token via _snow_client), but the
    # ticket scope is the logged-in hub user: their hub email matches their
    # ServiceNow user (same SSO identity), so we filter on that email and show
    # every incident that user ever opened in ServiceNow.
    snow_email = str(user_email).strip()
    if not snow_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No email on the current session to scope ServiceNow tickets.",
        )

    # Match the user on the email or user_name of opened_by/caller_id (instances differ
    # in which carries the SSO identity), OR on the sys_ids in our own user_tickets
    # table: a ticket opened through the portal may name only the service account.
    portal_ids = _portal_ticket_sys_ids(snow_email)
    clauses = [
        f"opened_by.email={snow_email}",
        f"caller_id.email={snow_email}",
        f"opened_by.user_name={snow_email}",
        f"caller_id.user_name={snow_email}",
    ]
    if portal_ids:
        clauses.append("sys_idIN" + ",".join(portal_ids))
    query = "^OR".join(clauses) + "^ORDERBYDESCsys_created_on"
    _log.info(
        "ServiceNow my-tickets: scoping to email=%r + %d portal ticket(s)",
        snow_email, len(portal_ids),
    )

    def _fetch_live():
        try:
            with _snow_client() as client:
                resp = client.get(
                    "/api/now/table/incident",
                    params={
                        "sysparm_query": query,
                        "sysparm_fields": "sys_id,number,short_description,state,priority,urgency,assigned_to,opened_at,sys_updated_on",
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
            f"tickets:{snow_email}",
            _fetch_live,
            ttl=60,
        )
    except HTTPException:
        raise
    except Exception:
        records = _fetch_live()

    _log.info("ServiceNow my-tickets: email=%r returned %d ticket(s)", snow_email, len(records or []))
    return {"success": True, "data": records, "timestamp": now_iso()}


# ── whose ticket is it ───────────────────────────────────────────────────────

_SYS_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def _require_own_ticket(sys_id: str, current_user: AuthUser) -> Dict[str, Any]:
    """Refuse a ticket that is not on this person's own list (get_tickets).

    Every read and write here goes through the service account, which can open ANY
    ticket: the id in the address was the only thing standing between one person and
    another's ticket, its attachments and its comment box. The list is answered from
    its 60-second cache and read again once, uncached, before refusing, so a ticket
    raised a moment ago is not turned away. Checked in each function that reads or
    writes, not in the page. Returns the ticket as the list has it."""
    if not _SYS_ID.match(str(sys_id or "")):
        raise HTTPException(status_code=404, detail="That ticket does not exist, or it is not yours.")
    for refresh in (False, True):
        mine = {str(t.get("sys_id") or ""): t for t in (get_tickets(refresh=refresh, current_user=current_user).get("data") or [])}
        if sys_id in mine:
            return mine[sys_id]
    _log.warning("ServiceNow: %s asked for ticket %s, which is not theirs",
                 current_user.get("email") or current_user.get("username"), sys_id)
    raise HTTPException(status_code=404, detail="That ticket does not exist, or it is not yours.")


# ── GET /tickets/{sys_id} ─────────────────────────────────────────────────────

@router.get("/tickets/{sys_id}")
def get_ticket_detail(sys_id: str, current_user: AuthUser = Depends(get_current_user)):
    _require_own_ticket(sys_id, current_user)

    def _fetch():
        with _snow_client() as client:
            ticket_resp = client.get(
                f"/api/now/table/incident/{sys_id}",
                params={"sysparm_display_value": "true"},
            )
            ticket_resp.raise_for_status()
            raw = ticket_resp.json().get("result", {})

            # Read the conversation from the incident's OWN journal fields. The GET
            # above (sysparm_display_value=true) already returned each journal field
            # as its full activity text, so this needs only incident read access —
            # unlike a direct sys_journal_field Table API query, which this instance's
            # ACLs return EMPTY for the service account (why the chat looked blank).
            #
            # Elements are configurable via SNOW_JOURNAL_ELEMENTS (default "comments"
            # = customer-facing only; set to "comments,work_notes" to also show the
            # support group's internal notes). No author/date filter, so the FULL
            # history is returned — including tickets opened before the Hub existed.
            elements = [
                e.strip()
                for e in (os.getenv("SNOW_JOURNAL_ELEMENTS", "comments") or "comments").split(",")
                if e.strip()
            ] or ["comments"]
            messages: List[Dict[str, Any]] = []
            for el in elements:
                messages.extend(_parse_journal_text(_extract_display(raw.get(el)), el))

            # Fallback: if the record's journal text was empty or in an unexpected
            # format, try the journal table directly (works on instances that allow it).
            if not messages:
                try:
                    jr = client.get(
                        "/api/now/table/sys_journal_field",
                        params={
                            "sysparm_query": f"element_id={snow_catalog.query_value(sys_id)}"
                                             f"^elementIN{snow_catalog.query_value(','.join(elements))}",
                            "sysparm_fields": "sys_id,sys_created_by,sys_created_on,value,element",
                            "sysparm_orderby": "sys_created_on",
                            "sysparm_limit": 500,
                        },
                    )
                    if jr.status_code == 200:
                        messages = jr.json().get("result", []) or []
                except Exception:
                    messages = []

            # Oldest-first for the chat (the record journal text is newest-first).
            messages.sort(key=lambda m: str(m.get("sys_created_on") or ""))

            # Resolve the real display name for non-portal (team/agent) authors.
            agent_users = {
                m.get("sys_created_by")
                for m in messages
                if m.get("sys_created_by") and not _PORTAL_MSG_RE.match(m.get("value") or "")
            }
            display_names = _resolve_display_names(client, agent_users)
            # Identities of the service account so its (markerless) replies are
            # attributed to the viewing requester, and the requester's own name.
            portal_ids = _service_account_identities(client)
            viewer_name = _portal_display_name(current_user)

            # Attachments on the record (e.g. the PNG submitted with the ticket).
            attachments = []
            try:
                att_resp = client.get(
                    "/api/now/attachment",
                    params={
                        "sysparm_query": f"table_name=incident^table_sys_id={snow_catalog.query_value(sys_id)}",
                        "sysparm_fields": "sys_id,file_name,content_type,size_bytes,sys_created_on",
                        "sysparm_limit": 50,
                    },
                )
                if att_resp.status_code == 200:
                    attachments = att_resp.json().get("result", []) or []
            except Exception:
                attachments = []

            ticket = _map_ticket(raw)
            ticket["description"] = raw.get("description", raw.get("short_description", ""))
            ticket["conversation"] = [
                _map_message(m, display_names, portal_ids, viewer_name) for m in messages
            ]
            ticket["attachments"] = [_map_attachment(a, sys_id) for a in attachments]
            return ticket

    try:
        ticket_data = cache.get_cached(f"snow:detail:{sys_id}", ttl=10, producer=_fetch)
    except Exception as exc:
        _raise_snow_error(exc, "fetching ticket details")

    # Opening the ticket IS seeing it. The receipt goes into the SAME store the widgets
    # read (user_item_seen — the one pins.py owns and dashboards.py reads), so the dot
    # clears everywhere at once. A private table here would have left the dashboard
    # still showing a dot for a ticket the user was looking at.
    try:
        uid = str(
            current_user.get("email") or current_user.get("username") or current_user.get("id") or ""
        ).strip()[:255]
        if uid:
            db.ensure_item_tables_once()
            db.execute(
                """
                INSERT INTO user_item_seen (user_id, source, item_id, seen_at)
                VALUES (%s, 'servicenow', %s, NOW())
                ON CONFLICT (user_id, source, item_id)
                DO UPDATE SET seen_at = NOW()
                """,
                [uid, str(sys_id)[:128]],
            )
    except Exception as exc:
        _log.info("ticket read-receipt failed for %s: %s", sys_id, exc)

    return {"success": True, "data": ticket_data, "timestamp": now_iso()}


# ── GET /tickets/{sys_id}/attachments/{attachment_id} ────────────────────────

@router.get("/tickets/{sys_id}/attachments/{attachment_id}")
def get_attachment(
    sys_id: str,
    attachment_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """Stream an attachment's bytes through the portal so the browser never hits
    ServiceNow directly and the service-account credentials stay server-side. Only an
    attachment OF that ticket, and only of one of the person's own tickets."""
    _require_own_ticket(sys_id, current_user)
    if not _SYS_ID.match(str(attachment_id or "")):
        raise HTTPException(status_code=404, detail="That attachment does not exist.")
    try:
        with _snow_client() as client:
            meta = client.get(f"/api/now/attachment/{attachment_id}")
            meta.raise_for_status()
            record = meta.json().get("result") or {}
            if str(record.get("table_sys_id") or "") != sys_id:
                raise HTTPException(status_code=404, detail="That attachment does not exist.")
            resp = client.get(
                f"/api/now/attachment/{attachment_id}/file",
                headers={"Accept": "*/*"},
            )
            resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as exc:
        _raise_snow_error(exc, "downloading attachment")
    # Served from the Hub's own address, so the type comes from the bytes (never from
    # ServiceNow's header) and only a picture opens in the browser: anything else, a
    # page or a script included, is downloaded and cannot run as the Hub.
    media_type = attachment_type(resp.content)
    disposition = "inline" if media_type.startswith("image/") else "attachment"
    return Response(content=resp.content, media_type=media_type, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(str(record.get('file_name') or 'attachment'), safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


def _comment_present(client: httpx.Client, sys_id: str, text: str) -> bool:
    """True if `text` already appears in the incident's comments journal. Used to
    confirm a reply landed when ServiceNow's business rule returns 403 after the
    journal entry was already persisted (so we don't report a false failure)."""
    needle = _html_to_text(text).strip()
    if not needle:
        return False
    try:
        resp = client.get(
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_fields": "comments", "sysparm_display_value": "true"},
        )
        if resp.status_code == 200:
            comments = _html_to_text(_extract_display((resp.json().get("result") or {}).get("comments")))
            return needle in comments
    except Exception:
        return False
    return False


# ── POST /tickets/reply ──────────────────────────────────────────────────────

@router.post("/tickets/reply")
def reply_to_ticket(
    sys_id: str = Form(...),
    message: str = Form(""),
    attachments: Optional[List[UploadFile]] = File(None),
    # True: check that the ticket is the caller's and say what would be sent; send nothing.
    # The confirmation step of a reply DevBot drafted (the shared action dialog).
    dry_run: bool = Form(False),
    current_user: AuthUser = Depends(get_current_user),
):
    # Posted as multipart/form-data to a STATIC path (sys_id in the body, not the
    # URL) so a comment and its attachments arrive as one request. The comment is a
    # plain incident.comments write (no marker — see the write block below); files
    # go through the attachment API.
    text = (message or "").strip()
    files = _checked_attachments(attachments)
    has_files = bool(files)
    if not text and not has_files:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    number = str(_require_own_ticket(sys_id, current_user).get("number") or "your ticket")
    # "is True": called as a plain function, the parameter is still its Form() default.
    if dry_run is True:
        return {
            "success": True,
            "dry_run": True,
            "summary": f"Reply on {number}",
            "checks": [f"{number} is one of your tickets.",
                       "The support team sees the message on the ticket, under your name."],
        }

    display = _portal_display_name(current_user)
    email = (current_user.get("email") or current_user.get("username") or "").strip()

    attachment_count = 0
    try:
        with _snow_client() as client:
            if text:
                # Plain comment write — NO inline marker. The instance's "Align
                # Direction comments and work notes" business rule aborts comment
                # writes that carry our old "[portal:...]" prefix (it injects LTR
                # text ahead of the RTL body and the direction helper chokes). A
                # plain message writes fine, as it did before the marker existed.
                # Author attribution is recovered on read by matching the service
                # account (see _service_account_identities) instead of polluting the
                # comment body.
                resp = client.patch(
                    f"/api/now/table/incident/{sys_id}",
                    json={"comments": text},
                    params={"sysparm_fields": "sys_id"},
                )
                # The 'Align Direction' rule does an AFTER-abort: ServiceNow returns
                # 403 "Operation Failed" but the comment journal entry IS persisted.
                # Verify it landed and, if so, treat the reply as sent; re-raise any
                # other error (real ACL/transport failure leaves nothing behind).
                if resp.status_code == 403 and _comment_present(client, sys_id, text):
                    _log.warning(
                        "SNow reply: comment persisted despite 403 business-rule abort; treating as sent."
                    )
                else:
                    resp.raise_for_status()
            if has_files:
                attachment_count = _upload_snow_attachments(client, sys_id, files, "incident")
    except Exception as exc:
        _raise_snow_error(exc, "sending reply")

    # Invalidate conversation cache so the next poll sees the new reply immediately
    cache.invalidate(f"snow:detail:{sys_id}")

    new_message = {
        "sys_id": "",
        "sys_created_by": email or display,
        "sys_created_on": now_iso(),
        "author": display,
        "author_email": email,
        "is_portal_user": True,
        "element": "comments",
        "value": text,
    }
    return {
        "success": True,
        "summary": f"Reply on {number}",
        "result": "Sent.",
        "url": f"/ui/support?ticket={sys_id}",
        "data": new_message,
        "attachments_uploaded": attachment_count,
        "timestamp": now_iso(),
    }


def _producer_sys_id() -> str:
    """sys_id of the 'Open A Ticket' Record Producer (overridable via env)."""
    return (os.getenv("SNOW_PRODUCER_SYS_ID") or "9451f30fc1fe6610b2a83094d023d641").strip()


# Wizard form key -> Record Producer variable name (identity unless listed).
_PRODUCER_VAR_ALIASES = {
    "reason": "what_is_the_reason_for_opening_this_ticket",
    "work_impact": "how_does_this_affect_your_work",
    "help_text": "how_can_we_help",
}

# The group this portal's tickets belong to: the answer to the producer's "Choose A
# Support Group" question when it is mandatory and unanswered (_fill_required).
_DEFAULT_SUPPORT_GROUP = os.getenv("SNOW_DEFAULT_SUPPORT_GROUP", "Devops Support").strip()


def _producer_variable_specs(client: httpx.Client) -> List[Dict[str, Any]]:
    """Fetch the producer's variable definitions (name, label, mandatory, choices),
    flattening container children. Best-effort — returns [] if the item can't be read."""
    try:
        resp = client.get(f"/api/sn_sc/servicecatalog/items/{_producer_sys_id()}")
        resp.raise_for_status()
        data = resp.json().get("result", {}) or {}
    except Exception:
        return []
    return snow_catalog.flatten(data.get("variables"))


def _normalize_var_name(text: Any) -> str:
    """Producer variable names are the question label lowercased with ? ! .
    stripped and spaces turned into underscores. Normalize both sides the same
    way so a wizard field key can be matched to the producer's real variable."""
    s = str(text or "").strip().lower().replace("?", "").replace("!", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


# ServiceNow variable types, the handful that matter here.
_CHECKBOX_TYPES = ("1", "7", "checkbox", "boolean", "yes_no")

# What an unticked box says in the item payload. A checkbox is the one type
# where "has a value" and "is filled in" are different questions.
_UNTICKED = ("false", "0", "no", "n", "")


def _is_checkbox(spec: Dict[str, Any]) -> bool:
    return str(spec.get("type") or "").strip().lower() in _CHECKBOX_TYPES


def _satisfies_mandatory(spec: Dict[str, Any], value: Any) -> bool:
    """Whether this value would get past ServiceNow's mandatory check. For a checkbox,
    mandatory means ticked: `False` is a value and still fails."""
    v = str(value if value is not None else "").strip().lower()
    if not v:
        return False
    if _is_checkbox(spec):
        return v not in _UNTICKED
    return True


def _declared_default(spec: Dict[str, Any]) -> Optional[str]:
    """The item's OWN answer to its own question, if it has one.

    Not a guess and needing no permission -- but only where it is the value the
    form would have SUBMITTED. An unticked mandatory checkbox is the value the
    form would have refused to submit until somebody ticked it, so it is not a
    default here, it is an unanswered question."""
    for key in ("default_value", "value", "displayvalue"):
        val = spec.get(key)
        if val in (None, "", []):
            continue
        if truthy(spec.get("mandatory")) and not _satisfies_mandatory(spec, val):
            # A mandatory box the item ships unticked. Falling through leaves it
            # to the placeholder, which ticks it.
            return None
        return str(val)
    return None


def _first_choice_for_spec(
    spec: Dict[str, Any],
    caller_sys_id: str = "",
    group_sys_id: str = "",
) -> Optional[str]:
    """Something to put in a mandatory question nobody answered. A PLACEHOLDER.

    `submit_producer` over REST enforces the dictionary-level mandatory flag and
    skips the UI policies that hide a question for the selected service, so a
    conditionally-hidden mandatory variable still has to carry something or the
    whole submission is refused.

    A question about a PERSON gets the requester and one about a GROUP this
    portal's support group -- never the first name on a list. Any other record
    question takes its first listed choice or nothing; a ticked box for a checkbox
    (mandatory means ticked); "N/A" for free text."""
    table = snow_catalog.table_of(spec)
    if table == "sys_user":
        return caller_sys_id or None
    if table == "sys_user_group":
        return group_sys_id or None
    for ch in spec.get("choices") or []:
        val = ch.get("value")
        if val not in (None, ""):
            return str(val)
    if snow_catalog.refers_to_record(spec):
        return None
    if _is_checkbox(spec):
        return "true"
    return "N/A"


# What a placeholder may never decide (a declared default is the item's own answer and
# is exempt):
#
#   a PERSON  -- who a ticket is for is the caller, set deliberately (_caller_variable).
#   a STATE   -- where the ticket lands, who owns it, or how loudly it arrives.
_NEVER_GUESSED = (
    "state", "stage", "status", "assign", "assigned", "priority", "impact",
    "urgency", "severity",
    "caller", "openedby", "requestedfor", "requestfor", "openedfor", "onbehalf",
)


def _variable_index(specs: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{normalised name -> real name} and the same by label, first one winning."""
    by_name: Dict[str, str] = {}
    by_label: Dict[str, str] = {}
    for spec in specs:
        real = spec.get("name")
        if not real:
            continue
        by_name.setdefault(_normalize_var_name(real), real)
        label = spec.get("label")
        if label:
            by_label.setdefault(_normalize_var_name(label), real)
    return {"name": by_name, "label": by_label}


def _resolve_variable(key: str, index: Dict[str, Dict[str, str]]) -> str:
    """The producer variable a wizard field key belongs to.

    Four tiers, narrowest first. The last one is why a ticket stopped going in
    at whatever urgency happened to be top of the list: the item calls the
    question `urgency_with_explain` and labels it with a sentence, so matching
    the bare key `urgency` against either found nothing, the answer was filed
    under a variable that does not exist, and the real one was left to a
    placeholder.

    A prefix match is only taken when exactly ONE variable has it. Two
    candidates is a coin toss, and a coin toss puts the requester's answer in
    the wrong question -- which is worse than leaving it out."""
    if key in _PRODUCER_VAR_ALIASES:
        return _PRODUCER_VAR_ALIASES[key]
    nk = _normalize_var_name(key)
    if not nk:
        return key
    if nk in index["name"]:
        return index["name"][nk]
    if nk in index["label"]:
        return index["label"][nk]
    candidates = {real for norm, real in index["name"].items() if norm.startswith(nk)}
    candidates |= {real for norm, real in index["label"].items() if norm.startswith(nk)}
    if len(candidates) == 1:
        return candidates.pop()
    if candidates:
        _log.warning(
            "SNow producer: %r matches more than one question (%s); not guessing "
            "between them. Name it in _PRODUCER_VAR_ALIASES.", key, sorted(candidates),
        )
    return key


def _wizard_variable_names(fields: Dict[str, str], specs: List[Dict[str, Any]]) -> set:
    """Every producer variable the WIZARD is responsible for, answered or not.

    A wizard field left empty is an answer. `azure_devops_project` is blank
    because the requester picked a service that is not Azure DevOps -- filling
    it in does not complete their request, it contradicts it, and on this item
    it switches on a branch whose own questions then go unanswered. So these are
    off limits to _fill_required whatever the item says about them."""
    index = _variable_index(specs)
    return {n for n in (_resolve_variable(key, index) for key in fields) if n}


def _fill_required(
    client: httpx.Client,
    variables: Dict[str, str],
    specs: List[Dict[str, Any]],
    support_group: Optional[str],
    wizard_names: Optional[set] = None,
    caller_sys_id: str = "",
    force: bool = False,
) -> Dict[str, str]:
    """Answer the item's mandatory questions that the wizard left empty.

    Returns {name: value} for what was supplied, so the caller can log it and
    put it in front of somebody. Anything skipped is left for the producer to
    refuse, by design: a refusal names the problem, an invented value hides it."""
    supplied: Dict[str, str] = {}
    guessed: List[str] = []
    skipped: List[str] = []
    wizard_names = wizard_names or set()

    group_sys_id = ""
    for spec in specs:
        name = spec.get("name")
        if not name or not truthy(spec.get("mandatory")):
            continue
        if _satisfies_mandatory(spec, variables.get(name)):
            continue
        if name in wizard_names and not force:
            # The requester answered this by leaving it blank. See
            # _wizard_variable_names. In `force` mode the item has already
            # refused the submission over exactly these, so a placeholder in a
            # question nobody will read beats no ticket at all.
            skipped.append(str(name))
            continue

        # 1. The item's own answer. Always allowed -- it is not ours.
        value = _declared_default(spec)

        # 2. The support group. Also a real answer: it is the queue this
        #    portal's tickets belong to, named in configuration rather than
        #    picked off a list. A queue is not an assignee -- an incident that
        #    names one is still unassigned and still Open.
        if not value and snow_catalog.table_of(spec) == "sys_user_group":
            wanted = (support_group or _DEFAULT_SUPPORT_GROUP).strip()
            value = _resolve_support_group(client, wanted)
            group_sys_id = value
            if value == wanted and not looks_like_sys_id(value):
                # The lookup did not find the group, so this is its NAME going
                # into a reference field. ServiceNow may accept it and may not;
                # if the submission comes back 400, THIS is why, and the group
                # in SNOW_DEFAULT_SUPPORT_GROUP is the thing to check.
                _log.warning(
                    "SNow support group %r did not resolve to a sys_id; sending the name "
                    "into a reference variable. Check the group exists and the account "
                    "can read sys_user_group.", wanted,
                )

        # 3. A placeholder. A GUESS, so on the first attempt it is not made for
        #    anything that decides ownership, state or severity, and never for a
        #    person. In `force` mode the item has already refused without them.
        elif not value:
            haystack = (_normalize_var_name(name) + " "
                        + _normalize_var_name(spec.get("label") or "")).replace("_", "")
            risky = _points_at_people(spec) or any(w in haystack for w in _NEVER_GUESSED)
            if risky and not force:
                skipped.append(str(name))
                continue
            if not group_sys_id:
                group_sys_id = _resolve_support_group(
                    client, (support_group or _DEFAULT_SUPPORT_GROUP).strip())
            value = _first_choice_for_spec(spec, caller_sys_id, group_sys_id)
            if value:
                guessed.append(str(name))

        if value:
            variables[name] = value
            supplied[str(name)] = str(value)
        else:
            skipped.append(str(name))

    if guessed:
        _log.warning(
            "SNow producer: filled mandatory question(s) the wizard does not ask with a "
            "placeholder%s: %s -- if any of these matters to whoever reads the ticket, "
            "ask for it in catalog_forms.py instead.",
            " (SECOND ATTEMPT, after the item refused the submission without them)"
            if force else "", guessed,
        )

    if skipped:
        _log.warning(
            "SNow producer: mandatory variables left for the item to refuse rather "
            "than invented: %s -- if a submission fails on one of these, the fix is "
            "to ASK for it in catalog_forms.py, not to guess it here.", skipped,
        )
    return supplied


def _resolve_support_group(client: httpx.Client, value: str) -> str:
    """The 'Choose A Support Group' producer variable is a reference to
    sys_user_group, so submit_producer needs the group's sys_id. Look it up by
    name; fall back to the raw value if it's already a sys_id or can't be found."""
    value = (value or "").strip()
    if not value or looks_like_sys_id(value) or "^" in value:
        return value
    try:
        resp = client.get(
            "/api/now/table/sys_user_group",
            params={"sysparm_query": f"name={snow_catalog.query_value(value)}", "sysparm_fields": "sys_id",
                    "sysparm_limit": "1"},
        )
        if resp.status_code == 200:
            rows = resp.json().get("result") or []
            if rows and rows[0].get("sys_id"):
                return str(rows[0]["sys_id"])
    except Exception:
        pass
    return value


# ── Who the ticket is FOR ────────────────────────────────────────────────────
#
# The producer inserts the incident as the service account, so the Caller is the
# service account unless the submission names the requester. The producer's own
# script copies its `caller_id` question onto the incident and falls back to the
# submitting account when that question is empty -- which is the "monitor user" a
# ticket shows when the portal did not fill it.
#
# Set AT INSERT only: any later update by the service account reassigns the incident
# to it and moves it to In Progress (SNOW_CALLER_PATCH=1 accepts that trade).

_CALLER_VAR_OVERRIDE = os.getenv("SNOW_CALLER_VAR", "").strip()

# What a question about who is asking tends to be called, best first.
_CALLER_VAR_HINTS = (
    "caller_id", "caller", "requested_for", "request_for", "requester", "opened_for",
    "on_behalf_of", "affected_user", "for_user", "user", "employee",
)


def _points_at_people(var: Dict[str, Any]) -> bool:
    return snow_catalog.table_of(var) == "sys_user"


def _caller_variable(specs: List[Dict[str, Any]]) -> str:
    """The producer question that carries the requester, or "".

    SNOW_CALLER_VAR when set; else a question pointing at sys_user (a Reference, or a
    Lookup Select Box from Catalog Builder) whose name or label reads like a caller;
    else the only such question; else a record-type question named like one. A free
    text question never qualifies: a sys_id in it is a hex string on the ticket."""
    if _CALLER_VAR_OVERRIDE:
        return _CALLER_VAR_OVERRIDE

    people: Dict[str, str] = {}
    records: Dict[str, str] = {}
    people_order: List[str] = []
    for var in specs:
        name = str(var.get("name") or "")
        if not name or not snow_catalog.refers_to_record(var):
            continue
        is_person = _points_at_people(var)
        if is_person and name not in people_order:
            people_order.append(name)
        for text in (name, var.get("label")):
            norm = _normalize_var_name(text)
            if norm:
                records.setdefault(norm, name)
                if is_person:
                    people.setdefault(norm, name)

    for hint in _CALLER_VAR_HINTS:
        if hint in people:
            return people[hint]
    if len(people_order) == 1:
        return people_order[0]
    for hint in _CALLER_VAR_HINTS:
        if hint in records:
            return records[hint]
    return ""


def _record_field(record: Dict[str, Any], field: str) -> Dict[str, str]:
    """One field of a `sysparm_display_value=all` read, as {value, display}.

    That read returns each field as {"value": …, "display_value": …}; the same
    field from a plain read is a bare string. Accept both so the caller check
    does not depend on which read produced the record.
    """
    raw = (record or {}).get(field)
    if isinstance(raw, dict):
        return {
            "value": str(raw.get("value") or ""),
            "display": str(raw.get("display_value") or ""),
        }
    text = str(raw or "")
    return {"value": text, "display": text}


def _caller_value(spec: Dict[str, Any], sys_id: str, names: tuple) -> str:
    """What to send in the caller question: the sys_id, unless the item lists its
    choices and identifies people by another field (a lookup's value field)."""
    values = [str(c.get("value") or "") for c in spec.get("choices") or [] if isinstance(c, dict)]
    if not values or sys_id in values:
        return sys_id
    by_lower = {v.lower(): v for v in values if v}
    for form in snow_catalog.account_forms(*names):
        if form.lower() in by_lower:
            return by_lower[form.lower()]
    return sys_id


def _set_ticket_caller(
    client: httpx.Client,
    table: str,
    sys_id: str,
    wanted_sys_id: str,
    record: Dict[str, Any],
) -> Dict[str, str]:
    """Report who the new ticket is for, and fix it only if SNOW_CALLER_PATCH allows.

    `record` is the read taken right after the insert. Returns {how, sys_id, name},
    where how is insert (it arrived right), unresolved (no ServiceNow user for this
    person), not-set (wrong, and left alone), patched, reverted, refused or
    unverified. Never raises."""
    found = _record_field(record, "caller_id")
    outcome = {"how": "", "sys_id": found["value"], "name": found["display"]}

    if not wanted_sys_id:
        outcome["how"] = "unresolved"
        return outcome
    if found["value"] == wanted_sys_id:
        outcome["how"] = "insert"
        return outcome

    if not truthy(os.getenv("SNOW_CALLER_PATCH", "0")):
        outcome["how"] = "not-set"
        _log.warning(
            "SNow ticket caller NOT set on %s: it is %s (%s), the requester is %s. The "
            "producer did not receive them -- see GET /api/support/ticket-form.",
            sys_id, found["value"] or "(empty)", found["display"] or "?", wanted_sys_id,
        )
        return outcome

    # No query parameters: with sysparm_display_value a sys_id is read as a NAME and
    # silently dropped.
    try:
        resp = client.patch(f"/api/now/table/{table}/{sys_id}", json={"caller_id": wanted_sys_id})
    except Exception as exc:
        _log.warning("SNow caller write failed for %s: %s", sys_id, exc)
        outcome["how"] = "refused"
        return outcome
    if resp.status_code >= 400:
        _log.warning("SNow caller write refused (HTTP %s): %s", resp.status_code, resp.text[:200])
        outcome["how"] = "refused"
        return outcome

    # Read it back: "we sent it" was already true when the field was still wrong.
    try:
        rec = client.get(
            f"/api/now/table/{table}/{sys_id}",
            params={"sysparm_fields": "caller_id", "sysparm_display_value": "all"},
        )
        if rec.status_code == 200:
            back = _record_field(rec.json().get("result", {}) or {}, "caller_id")
            outcome["sys_id"], outcome["name"] = back["value"], back["display"]
            # Accepted and still wrong means a business rule put it back.
            outcome["how"] = "patched" if back["value"] == wanted_sys_id else "reverted"
        else:
            outcome["how"] = "unverified"
    except Exception:
        outcome["how"] = "unverified"

    _log.warning("SNow ticket caller: sys_id=%s how=%s is=%s (%s)",
                 sys_id, outcome["how"], outcome["sys_id"], outcome["name"])
    return outcome


_URGENCY_SYNONYMS = {
    "low": ("low", "3"),
    "medium": ("medium", "moderate", "2"),
    "high": ("high", "1"),
    "urgent": ("urgent", "critical", "high", "1"),
}

# NOTE: there is no urgency mapper here any more. The direct incident insert
# needed one, because the incident's urgency column is a numeric choice and the
# wizard asks for a word. The producer does that conversion itself, from the
# variable's own choice list -- see _map_choice_value and _URGENCY_SYNONYMS --
# which is one more thing that is right for free on this path.


def _map_choice_value(field_key: str, value: str, cmap: Dict[str, str]) -> str:
    """Convert a wizard dropdown value to its stored choice value. Tries the value
    itself, then (for urgency only) a list of synonyms, before giving up and
    sending the raw value unchanged."""
    v = value.lower()
    if v in cmap:
        return cmap[v]
    if _normalize_var_name(field_key) == "urgency":
        for syn in _URGENCY_SYNONYMS.get(v, ()):
            if syn in cmap:
                return cmap[syn]
    return value


def _choice_maps_from_specs(specs: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{var_name: {label_or_value_lower: value}} so dropdown labels convert to
    their stored choice values."""
    out: Dict[str, Dict[str, str]] = {}
    for var in specs:
        name = var.get("name")
        if not name:
            continue
        cmap: Dict[str, str] = {}
        for ch in var.get("choices") or []:
            val = ch.get("value")
            if val is None:
                continue
            cmap[str(val).strip().lower()] = str(val)
            lab = ch.get("label")
            if lab:
                lab_l = str(lab).strip().lower()
                cmap[lab_l] = str(val)
                # Also key on EACH word of the label so a short wizard value like
                # "high"/"low" maps to a numbered or long choice label
                # ("1 - High", "Urgent —— the production is down…"). First match
                # wins, so earlier choices are never overwritten by later ones.
                for word in re.split(r"[^a-z0-9]+", lab_l):
                    if word and word not in cmap:
                        cmap[word] = str(val)
        if cmap:
            out[name] = cmap
    return out


def _build_producer_variables(
    client: httpx.Client,
    fields: Dict[str, str],
    specs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Map wizard fields to producer variables, converting dropdown labels to values."""
    if specs is None:
        specs = _producer_variable_specs(client)
    choice_maps = _choice_maps_from_specs(specs)
    index = _variable_index(specs)

    variables: Dict[str, str] = {}
    for key, value in fields.items():
        value = (value or "").strip()
        if not value:
            continue
        name = _resolve_variable(key, index)
        if name not in index["name"].values():
            # Filed under a variable the item does not have, so it reaches
            # nothing -- and whatever question it SHOULD have answered is then
            # filled in by a placeholder. That is how every ticket went in at
            # the first urgency on the list.
            _log.warning(
                "SNow producer: the wizard's %r does not match any question on the "
                "item; it will not reach the ticket. Add it to _PRODUCER_VAR_ALIASES.",
                key,
            )
        cmap = choice_maps.get(name)
        if cmap:
            value = _map_choice_value(key, value, cmap)
        variables[name] = value
    return variables


# The wizard asks for a WORD; the incident's urgency column is a numeric choice
# (1 High / 2 Medium / 3 Low). Writing "high" into it stores nothing and reads
# back as the field's own default, which looks like the requester chose it.
_INCIDENT_URGENCY = {"low": "3", "medium": "2", "high": "1", "urgent": "1"}


def _incident_urgency(value: str) -> str:
    v = (value or "").strip().lower()
    if v in _INCIDENT_URGENCY:
        return _INCIDENT_URGENCY[v]
    return v if v in ("1", "2", "3") else "2"


def _insert_incident_directly(
	client: httpx.Client,
	fields: Dict[str, str],
	caller_sys_id: str,
	support_group: Optional[str],
):
	"""Create the incident on the Table API, used only when the producer refuses.

	The answers go into the description instead of their own fields; the caller
	travels on the insert, so the ticket still opens in the requester's name."""
	payload: Dict[str, Any] = {
		"short_description": fields["title"],
		"description": _format_ticket_description(fields),
		"caller_id": caller_sys_id,
		"opened_by": caller_sys_id,
		"urgency": _incident_urgency(fields.get("urgency", "")),
		"impact": "2",
	}
	group_value = (support_group or _DEFAULT_SUPPORT_GROUP).strip()
	if group_value:
		# A QUEUE, not a person. An incident that names one is still unassigned.
		try:
			payload["assignment_group"] = _resolve_support_group(client, group_value)
		except Exception as exc:
			_log.warning("SNow support group %r not resolved: %s", group_value, exc)
	# No `state`: a new incident opens Open; "2" would be In Progress.
	payload = {k: v for k, v in payload.items() if str(v or "").strip()}

	_log.warning(
		"SNow incident insert (fallback): caller=%s fields=%s",
		caller_sys_id or "(unresolved -- this ticket opens under the service account)",
		sorted(payload.keys()),
	)
	return client.post(
		"/api/now/table/incident",
		json=payload,
		headers={"Content-Type": "application/json"},
	)


def _submit_via_producer(
	client: httpx.Client,
	fields: Dict[str, str],
	support_group: Optional[str],
	caller_sys_id: str,
	caller_names: tuple = (),
):
	"""Submit the "Open A Ticket" record producer with the wizard's answers.

	Plus the requester in the item's caller question, and an answer for any
	mandatory question the wizard does not ask: over REST the dictionary-level
	mandatory flag is enforced and the UI policies that hide such questions never
	run. A mandatory question naming a person or a state is never guessed
	(_NEVER_GUESSED) -- the producer refuses instead, and the refusal names it.
	Returns (response, mandatory questions still unanswered)."""
	specs = _producer_variable_specs(client)
	variables = _build_producer_variables(client, fields, specs)
	wizard_names = _wizard_variable_names(fields, specs)

	caller_var = _caller_variable(specs)
	if caller_sys_id and caller_var:
		spec = next((v for v in specs if v.get("name") == caller_var), {})
		variables[caller_var] = _caller_value(spec, caller_sys_id, caller_names)
	_log.warning(
		"SNow producer caller: sys_id=%s question=%r%s",
		caller_sys_id or "(unresolved)", caller_var or None,
		"" if (caller_sys_id and caller_var)
		else " -- NOT SENT; record questions on the item: %s" % [
			(v.get("name"), v.get("type"), snow_catalog.table_of(v)) for v in specs
			if snow_catalog.refers_to_record(v)],
	)

	# AFTER the caller, so a mandatory caller question is already answered.
	supplied = _fill_required(client, variables, specs, support_group, wizard_names,
							  caller_sys_id)
	if supplied:
		_log.warning(
			"SNow producer: answered the item's mandatory questions on the requester's "
			"behalf: %s", supplied,
		)

	def _submit(attempt: str):
		_log.warning(
			"SNow producer submit (%s): item=%s vars=%s",
			attempt, _producer_sys_id(), sorted(variables.keys()),
		)
		return client.post(
			f"/api/sn_sc/servicecatalog/items/{_producer_sys_id()}/submit_producer",
			json={"sysparm_quantity": "1", "variables": variables},
			headers={"Content-Type": "application/json"},
		)

	resp = _submit("the requester's answers")
	# What is still unanswered by the item's OWN definition -- judged by whether
	# the value would PASS the mandatory check, not by whether a key is present.
	# An unticked mandatory checkbox has a value and is not filled in.
	unanswered = [
		f"{s.get('name')} ({s.get('label')})" for s in specs
		if truthy(s.get("mandatory")) and not _satisfies_mandatory(s, variables.get(s.get("name")))
	]

	if resp.status_code >= 400:
		# Everything, once: a question can become mandatory because of an answer.
		def _inventory():
			rows = []
			for s in specs:
				name = s.get("name")
				sent = repr(variables.get(name)) if name in variables else "NOT SENT"
				mandatory = truthy(s.get("mandatory"))
				verdict = ""
				if mandatory and not _satisfies_mandatory(s, variables.get(name)):
					verdict = ("   <-- THIS IS WHY: a value that cannot satisfy mandatory"
							   if name in variables else "   <-- unanswered")
				rows.append("%s [%s%s] %s = %s%s" % (
					name,
					s.get("type"),
					" ref:" + str(s.get("reference")) if s.get("reference") else "",
					"MANDATORY" if mandatory else "optional",
					sent, verdict,
				))
			return "\n  ".join(rows)

		_log.warning(
			"SNow producer refused (HTTP %s). Every variable on item %s, and what was "
			"sent for it:\n  %s", resp.status_code, _producer_sys_id(), _inventory(),
		)

		# Second attempt with placeholders for what the item insists on, including
		# questions this requester rightly left blank (the Azure DevOps ones are
		# mandatory whatever service is picked). A worse ticket, but one whose
		# answers still land in their own fields.
		forced = _fill_required(client, variables, specs, support_group,
								wizard_names=None, caller_sys_id=caller_sys_id, force=True)
		if forced:
			_log.warning(
				"SNow producer: second attempt, having been refused. Placeholders for %s. "
				"Every one of these is a question the ticket would read better for being "
				"asked in the wizard (catalog_forms.py).", sorted(forced),
			)
			resp = _submit("with placeholders for what the item insists on")
			_log.warning("SNow producer second attempt: status=%s body=%s",
						 resp.status_code, resp.text[:400])
			if resp.status_code >= 400:
				_log.warning(
					"SNow producer refused the second attempt too (HTTP %s). Everything "
					"sent:\n  %s", resp.status_code, _inventory(),
				)
		else:
			_log.warning(
				"SNow producer: nothing left to fill, so no second attempt. Every "
				"mandatory question already had a value and the item refused anyway.",
			)

	# Recomputed after the retry: what the item still says it wants.
	unanswered = [
		f"{s.get('name')} ({s.get('label')})" for s in specs
		if truthy(s.get("mandatory")) and not _satisfies_mandatory(s, variables.get(s.get("name")))
	]
	return resp, unanswered


@router.get("/ticket-form")
def ticket_form_diagnostics(current_user: AuthUser = Depends(get_current_user)) -> Dict[str, Any]:
	"""What the record producer asks for, which question carries the requester, and
	whether the signed-in admin resolves to a ServiceNow user. Admins only.

	Whether a ticket opens in the requester's name is decided by the producer, not
	by code here, so this is where to look when one opens as the service account."""
	if not has_effective_admin_access_live(current_user):
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="The ticket form's definition is admin-only.",
		)
	try:
		with _snow_ticket_flow_client() as client:
			specs = _producer_variable_specs(client)
			you = snow_catalog.find_user(client, current_user.get("email") or "", current_user.get("username") or "")
	except HTTPException:
		raise
	except Exception as exc:
		raise _safe_snow_error(exc, "reading the ticket form") from exc

	rows = [
		{
			"name": str(v.get("name") or ""),
			"label": str(v.get("label") or ""),
			"type": str(v.get("type") or ""),
			"reference": snow_catalog.table_of(v),
			"mandatory": truthy(v.get("mandatory")),
			"points_at_a_person": _points_at_people(v),
		}
		for v in specs
		if v.get("name")
	]
	chosen = _caller_variable(specs)
	people = [r["name"] for r in rows if r["points_at_a_person"]]

	if _CALLER_VAR_OVERRIDE:
		verdict = (
			f"Set by hand: SNOW_CALLER_VAR names {_CALLER_VAR_OVERRIDE!r}. The "
			f"requester's name is sent to that question with the ticket."
		)
	elif chosen:
		verdict = (
			f"The requester's name is sent to {chosen!r} with the ticket, so it "
			f"arrives with the right caller and nothing is written afterwards."
		)
	elif len(people) > 1:
		verdict = (
			"More than one question points at a person (" + ", ".join(people) + "), "
			"so the portal will not guess between them. Set SNOW_CALLER_VAR to the "
			"one that is the caller."
		)
	else:
		verdict = (
			"No question on this producer points at a person, so tickets open as the "
			"service account. Ask a ServiceNow admin to add a 'caller_id' variable to "
			"the 'Open A Ticket' record producer that references the User (sys_user) "
			"table. The next ticket uses it; nothing here needs redeploying."
		)
	if not you:
		verdict += (" Your own account did not resolve to a ServiceNow user by e-mail "
		            "or user name, so a ticket you open would carry no requester.")

	return {
		"success": True,
		"data": {
			"producer_sys_id": _producer_sys_id(),
			"variables": rows,
			"caller_variable": chosen,
			"people_questions": people,
			"you_resolve_to": you,
			"post_insert_write":
				"caller_id" if truthy(os.getenv("SNOW_CALLER_PATCH", "0"))
				else "nothing (SNOW_CALLER_PATCH is off, so the ticket stays Open)",
			"verdict": verdict,
		},
		"timestamp": now_iso(),
	}


@router.post("/tickets/create-flow")
def create_ticket_flow(
	# Accepted and IGNORED: the name on a ticket is the identity provider's, from
	# identity.trusted_name, never what the browser sent. The display name a person
	# chose for themselves is not who they are to the support team.
	full_name: str = Form(""),
	phone_number: str = Form(...),
	branch: str = Form(...),
	team: str = Form(...),
	section: str = Form(...),
	role: str = Form(...),
	network: str = Form(...),
	devops_services: str = Form(...),
	azure_devops_support_type: Optional[str] = Form(None),
	azure_devops_collection: Optional[str] = Form(None),
	azure_devops_project: Optional[str] = Form(None),
	pipeline_url: Optional[str] = Form(None),
	reason: str = Form(...),
	reason_other: Optional[str] = Form(None),
	title: str = Form(...),
	urgency: str = Form(...),
	description: str = Form(...),
	work_impact: str = Form(...),
	help_text: str = Form(...),
	support_group: Optional[str] = Form(None),
	attachments: Optional[List[UploadFile]] = File(None),
	current_user: AuthUser = Depends(get_current_user),
):
	user_email = (current_user.get("email") or current_user.get("username") or "").strip().lower()
	if not user_email:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User email missing from auth context.")
	files = _checked_attachments(attachments)

	fields = {
		"full_name": identity.trusted_name(user_id=str(current_user.get("id") or ""), email=user_email),
		"phone_number": _required_form_value("phone_number", phone_number),
		"branch": _required_form_value("branch", branch),
		"team": _required_form_value("team", team),
		"section": _required_form_value("section", section),
		"role": _required_form_value("role", role),
		"network": _required_form_value("network", network),
		"devops_services": _required_form_value("devops_services", devops_services),
		"reason": _required_form_value("reason", reason),
		"reason_other": (reason_other or "").strip(),
		"title": _required_form_value("title", title),
		"urgency": _required_form_value("urgency", urgency),
		"description": _required_form_value("description", description),
		"work_impact": _required_form_value("work_impact", work_impact),
		"help_text": _required_form_value("help_text", help_text),
	}
	if not fields["phone_number"].isdigit():
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="phone_number must contain numbers only.")
	if fields["devops_services"] == "azure devops":
		fields["azure_devops_support_type"] = _required_form_value("azure_devops_support_type", azure_devops_support_type)
		fields["azure_devops_collection"] = _required_form_value("azure_devops_collection", azure_devops_collection)
		fields["azure_devops_project"] = _required_form_value("azure_devops_project", azure_devops_project)
		if fields["azure_devops_support_type"] == "pipelines":
			fields["pipeline_url"] = _required_form_value("pipeline_url", pipeline_url)
		else:
			fields["pipeline_url"] = (pipeline_url or "").strip()
	else:
		fields["azure_devops_support_type"] = (azure_devops_support_type or "").strip()
		fields["azure_devops_collection"] = (azure_devops_collection or "").strip()
		fields["azure_devops_project"] = (azure_devops_project or "").strip()
		fields["pipeline_url"] = (pipeline_url or "").strip()

	try:
		_ensure_user_tickets_table()
	except Exception:
		pass

	try:
		with _snow_ticket_flow_client() as client:
			caller_names = (user_email, str(current_user.get("username") or ""))
			caller_sys_id = snow_catalog.find_user(client, *caller_names)
			resp, unanswered = _submit_via_producer(client, fields, support_group, caller_sys_id, caller_names)
			via = "producer"

			_log.warning("SNow producer response: status=%s body=%s", resp.status_code, resp.text[:600])
			if resp.status_code >= 400:
				# The requester still gets a ticket: created directly, answers in the
				# description. The producer's inventory has just been logged.
				_log.warning(
					"SNow producer refused (statically-mandatory and unanswered: %s) -- "
					"creating the incident directly instead. The answers will be in the "
					"description rather than in their own fields.", unanswered,
				)
				resp = _insert_incident_directly(client, fields, caller_sys_id, support_group)
				via = "table"

			if resp.status_code >= 400:
				because = ""
				if unanswered:
					because = (" The item wants an answer to: " + ", ".join(unanswered[:8])
							   + ", which the portal does not guess at.")
				raise HTTPException(
					status_code=status.HTTP_502_BAD_GATEWAY,
					detail=f"ServiceNow refused the ticket (HTTP {resp.status_code}).{because}",
				)
			result = resp.json().get("result", {}) or {}
			incident_sys_id = str(result.get("sys_id") or "")
			table = "incident" if via == "table" else str(result.get("table") or "incident")
			_log.warning("SNow ticket created via %s: sys_id=%s", via, incident_sys_id)
			if not incident_sys_id:
				raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ServiceNow did not return a record sys_id.")

			# One read (never a write): the number, the caller and the state.
			ticket_number = ""
			record: Dict[str, Any] = {}
			try:
				rec = client.get(
					f"/api/now/table/{table}/{incident_sys_id}",
					params={"sysparm_fields": "number,caller_id,state", "sysparm_display_value": "all"},
				)
				if rec.status_code == 200:
					record = rec.json().get("result", {}) or {}
					ticket_number = _record_field(record, "number")["display"]
			except Exception:
				pass

			state_at_insert = _record_field(record, "state")
			_log.warning(
				"SNow ticket at insert: sys_id=%s number=%s state=%s (%s) caller=%s",
				incident_sys_id, ticket_number or "?",
				state_at_insert["value"] or "?", state_at_insert["display"] or "?",
				_record_field(record, "caller_id")["display"] or "?",
			)

			caller = _set_ticket_caller(client, table, incident_sys_id, caller_sys_id, record)
			attachment_count = _upload_snow_attachments(client, incident_sys_id, files, table)
	except HTTPException:
		raise
	except Exception as exc:
		raise _safe_snow_error(exc, "creating ticket") from exc

	try:
		db.execute(
			"""
			INSERT INTO user_tickets (user_email, sys_id, ticket_number)
			VALUES (%s, %s, %s)
			ON CONFLICT (user_email, sys_id) DO NOTHING
			""",
			[_ticket_owner_key(user_email), incident_sys_id, ticket_number],
		)
	except Exception:
		pass
	cache.invalidate(f"snow:tickets:{user_email}")
	try:
		from integrations_cache import invalidate_owner
		invalidate_owner("snow", user_email)
	except Exception:
		pass
	try:
		from observability_tracking import urgency_to_severity_band, record_servicenow_portal_ticket
		# The wizard's urgency is a word (low/medium/high/urgent), not a priority code.
		record_servicenow_portal_ticket(ticket_number or incident_sys_id, user_email, urgency_to_severity_band(fields["urgency"]), fields["title"])
	except Exception:
		pass
	activity.log_activity(
		user_email=user_email,
		action_type=activity.ACTION_CREATE_TICKET,
		item_name=fields["title"] or ticket_number or "Ticket",
		metadata={"sys_id": incident_sys_id, "number": ticket_number, "attachments": attachment_count},
	)
	return {
		"success": True,
		"data": {
			"sys_id": incident_sys_id,
			"number": ticket_number,
			"short_description": fields["title"],
			"attachments_uploaded": attachment_count,
			# Read back off the ticket, and shown only when it is the requester.
			"caller": (caller.get("name") or "") if caller.get("how") in ("insert", "patched") else "",
			"caller_set": caller.get("how") or "",
		},
		"timestamp": now_iso(),
	}
