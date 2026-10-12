"""
The person's own support tickets, as the Support page shows them.

People here have no account on the ticketing system: the Hub reads it with its own
service account and shows each person only the tickets that are theirs (raised by them
there, or through the Hub). DevBot reads exactly that list, through the same code the
Support page uses (api/servicenow.get_tickets), and opens a ticket only when it is on
it -- a ticket number somebody types is not proof it is theirs.

Other people's tickets are never read here. What the support team learned from them
reaches DevBot only as reviewed past fixes (find_past_fixes, knowledge.review_fix).
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import HTTPException

from .base import Tool, ToolContext, ToolFailure, register

_STATES_DONE = ("resolved", "closed", "canceled", "cancelled")


def _mine(ctx: ToolContext) -> List[Dict[str, Any]]:
    from api.servicenow import get_tickets

    def produce() -> List[Dict[str, Any]]:
        try:
            return list(get_tickets(refresh=False, current_user=ctx.user).get("data") or [])
        except HTTPException as exc:
            raise ToolFailure(f"The support tickets could not be read: {exc.detail}")

    return ctx.memo("support:mine", produce)


def _link(ticket: Dict[str, Any]) -> str:
    return f"/ui/support?ticket={ticket.get('sys_id')}"


def _row(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"number": t.get("number"), "title": t.get("short_description"), "state": t.get("state"),
            "severity": t.get("urgency") or t.get("priority"), "assigned_to": t.get("assigned_to") or None,
            "opened": str(t.get("opened_at") or "")[:16], "updated": str(t.get("updated_at") or "")[:16],
            "url": _link(t)}


def my_tickets(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.progress("servicenow", "Reading your support tickets")
    rows = _mine(ctx)
    wanted = str(args.get("state") or "open").lower()
    if wanted == "open":
        rows = [t for t in rows if str(t.get("state") or "").lower() not in _STATES_DONE]
    elif wanted == "closed":
        rows = [t for t in rows if str(t.get("state") or "").lower() in _STATES_DONE]
    top = max(1, min(int(args.get("top") or 10), 25))
    out = [_row(t) for t in rows[:top]]
    return {
        "data": {"tickets": out, "count": len(rows)},
        "note": "Only this person's own tickets. Link each one (its url opens it on the Hub's Support page).",
        "summary": f"{len(rows)} {wanted if wanted != 'all' else ''} ticket{'s' if len(rows) != 1 else ''}".replace("  ", " "),
        "links": [{"title": f"{t['number']}: {t['title']}", "url": t["url"], "system": "servicenow"} for t in out[:6]],
    }


def ticket(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from api.servicenow import get_ticket_detail

    number = str(args["number"]).strip().upper()
    ctx.progress("servicenow", f"Opening {number}")
    mine = next((t for t in _mine(ctx) if str(t.get("number") or "").upper() == number), None)
    if not mine:
        raise ToolFailure(f"{number} is not one of your tickets, so it cannot be opened here. "
                          "Only your own tickets are shown.")
    try:
        detail = get_ticket_detail(str(mine["sys_id"]), current_user=ctx.user).get("data") or {}
    except HTTPException as exc:
        raise ToolFailure(f"{number} could not be read: {exc.detail}")
    talk = [{"from": m.get("author") or "", "at": str(m.get("sys_created_on") or "")[:16],
             "text": str(m.get("value") or "")[:600]}
            for m in (detail.get("conversation") or [])[-8:]]
    data = {**_row(mine), "description": str(detail.get("description") or "")[:1500], "conversation": talk}
    return {
        "data": data,
        "note": "Say where it stands (state, who has it) and what was said last. Link it.",
        "summary": f"{number}: {mine.get('state')}",
        "links": [{"title": f"{number}: {mine.get('short_description')}", "url": _link(mine), "system": "servicenow"}],
    }


def propose_reply(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from . import proposals

    number = str(args["number"]).strip().upper()
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolFailure("Say what the reply should say.")
    mine = next((t for t in _mine(ctx) if str(t.get("number") or "").upper() == number), None)
    if not mine:
        raise ToolFailure(f"{number} is not one of your tickets, so no reply can be drafted on it.")
    state = str(mine.get("state") or "").lower()
    if state in ("closed", "canceled", "cancelled"):
        raise ToolFailure(f"{number} is {state}; a reply would reach nobody. Suggest opening a new ticket instead.")
    action = proposals.action(
        "ticket-reply", "Reply on your ticket", f"Reply on {number} “{mine.get('short_description')}”.",
        {"sys_id": mine.get("sys_id"), "number": number, "title": mine.get("short_description"), "text": text[:4000]})
    return {"data": {"proposed": action["summary"], "status": "waiting for the person to confirm"},
            "actions": [action], "summary": f"Reply drafted on {number}",
            "note": "The person now sees a card with a Review button: they check the words and confirm them "
                    "themselves. Tell them in one sentence what you drafted. Do NOT say it is sent."}


register(
    Tool("propose_ticket_reply", "servicenow", "write", "Drafting the reply",
         "Draft a reply on one of the person's OWN open support tickets (by number), for them to check and send.",
         {"number": {"type": "string"}, "text": {"type": "string"}}, propose_reply, required=["number", "text"],
         words=["reply", "respond", "answer the", "tell them", "השב", "תענה", "תגיב"]),
    Tool("support_my_tickets", "servicenow", "read", "Reading your support tickets",
         "The person's OWN support tickets (never anyone else's): number, title, state, severity, who has it, when it "
         "was opened and updated. state: open (default), closed or all.",
         {"state": {"type": "string", "enum": ["open", "closed", "all"]}, "top": {"type": "integer"}},
         my_tickets, max_chars=2800,
         words=["ticket", "tickets", "incident", "support", "טיקט", "קריאה",
                "קריאות", "תמיכה"]),
    Tool("support_ticket", "servicenow", "read", "Opening your support ticket",
         "One of the person's own support tickets by number (for example INC0012345): its state, who has it, the "
         "description and the latest messages.",
         {"number": {"type": "string"}}, ticket, required=["number"], max_chars=3200,
         words=["inc0", "ticket", "טיקט", "קריאה"]),
)
