"""
AdminBot's tools: the Hub's own records, for its admins.

Every tool re-checks that the person asking is an admin NOW (has_effective_admin_access_live),
whatever the page or the conversation says: run() already refuses these outside an
AdminBot conversation, and this is the second lock on the same door.

They read what the admin pages read, through the same functions where there are some
(usage_tracking for Users, audit for Logs, monitor for DevBot, streaks), so AdminBot and
the pages never disagree about a number.

Changes are proposals, like DevBot's: approve or reject a request (or several at
once, each decided on its own), activate or deactivate an account, change a role,
post an announcement or take one down, start or stop the search index build, hide,
show or re-review a past fix. Each becomes a card the admin reviews and
confirms, and only then run_action() performs it -- through the very endpoint function
the admin pages call, with every guard that function has (the last-admin guard, the
pending-only transition, the audit record).
"""

from __future__ import annotations

import logging
import re

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import query_all, query_one

from . import proposals
from .base import Tool, ToolContext, ToolFailure, register

log = logging.getLogger(__name__)

_STATUSES = ("PENDING", "APPROVED", "IN_PROGRESS", "COMPLETED", "FAILED", "REJECTED", "EXECUTED")


def _guard(ctx: ToolContext) -> None:
    from security import has_effective_admin_access_live

    if not has_effective_admin_access_live(ctx.user):
        raise ToolFailure("Only the Hub's admins can read this.")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()[:16] if hasattr(value, "isoformat") else str(value)[:16]


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _user_row(email: str) -> Dict[str, Any]:
    row = query_one(
        "SELECT u.id::text AS id, u.email, COALESCE(NULLIF(u.full_name, ''), u.email) AS name, u.is_active, "
        "r.name AS role, r.hierarchy_level FROM users u JOIN roles r ON u.role_id = r.id "
        "WHERE LOWER(TRIM(u.email)) = %s", [_email(email)])
    if not row:
        raise ToolFailure(f"There is no account {email}. Find the person first with hub_find_users.")
    return row


def _trim(value: Any, cap: int = 400) -> Any:
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "..."
    if isinstance(value, dict):
        return {k: _trim(v, cap) for k, v in list(value.items())[:25] if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_trim(v, cap) for v in value[:15]]
    return value


# ── people ───────────────────────────────────────────────────────────────────

def find_users(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    needle = f"%{str(args['query']).strip().lower()}%"
    rows = query_all(
        """
        SELECT u.email, COALESCE(NULLIF(u.full_name, ''), u.email) AS name, u.username, r.name AS role,
               u.is_active, u.last_login_at, s.last_seen
          FROM users u JOIN roles r ON u.role_id = r.id
          LEFT JOIN (SELECT user_email, MAX(last_seen_at) AS last_seen FROM user_sessions GROUP BY user_email) s
                 ON s.user_email = LOWER(u.email)
         WHERE LOWER(u.email) LIKE %s OR LOWER(COALESCE(u.full_name, '')) LIKE %s
            OR LOWER(COALESCE(u.username, '')) LIKE %s OR LOWER(COALESCE(u.sso_name, '')) LIKE %s
         ORDER BY u.is_active DESC, s.last_seen DESC NULLS LAST
         LIMIT 15
        """,
        [needle, needle, needle, needle],
    )
    out = [{"email": r["email"], "name": r["name"], "role": r["role"], "active": bool(r["is_active"]),
            "last_seen": _iso(r.get("last_seen") or r.get("last_login_at"))} for r in rows]
    return {"data": {"users": out, "count": len(out)}, "summary": f"{len(out)} account(s) match",
            "note": "Use the email to ask about one of them." if out else "Nobody matches; accounts appear on first sign-in."}


def user_profile(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import audit
    import streaks
    import usage_tracking

    _guard(ctx)
    email = _email(args["email"])
    ctx.progress("hub", f"Reading {email}")
    detail = usage_tracking.user_detail(email, 30)
    if not detail:
        raise ToolFailure(f"There is no account {email}. Find the person first with hub_find_users.")
    row = _user_row(email)
    minutes = lambda s: round(int(s or 0) / 60)  # noqa: E731
    counts = detail.get("counts") or {}
    ctx.progress("hub", "Reading their requests, DevBot use and log")
    requests = query_all(
        "SELECT id::text AS id, request_title, request_type, status, created_at FROM approval_requests "
        "WHERE requester_id::text = %s ORDER BY created_at DESC LIMIT 5", [row["id"]])
    devbot = query_one(
        "SELECT COUNT(*) AS questions, COUNT(*) FILTER (WHERE outcome = 'failed') AS failed, MAX(created_at) AS last "
        "FROM devbot_events WHERE user_id = %s AND created_at > now() - interval '30 days'", [row["id"]]) or {}
    problems = audit.list_events(user_email=email, level="ERROR,CRITICAL,WARNING", limit=5).get("data") or []
    try:
        streak = streaks.explain(email)
        streak = {"current": streak["current"], "longest": streak["longest"],
                  "freezes_left": streak["freezes_this_month"].get("left"), "last_ended": streak["last_ended"]}
    except Exception:  # a nicety; never the reason the profile fails
        log.warning("adminbot: streak for %s could not be read", email, exc_info=True)
        streak = {"error": "could not be read"}
    data = {
        "email": detail.get("email"), "name": detail.get("display_name"), "username": detail.get("username"),
        "name_from_sign_in": detail.get("sso_name") or None, "role": detail.get("role_name"),
        "active": bool(detail.get("is_active")), "first_seen": detail.get("first_seen"),
        "last_login": detail.get("last_login"),
        "last_30_days": {"minutes_in_hub": minutes(sum(s["active_seconds"] for s in detail.get("series") or [])),
                         "days_in_hub": sum(1 for s in detail.get("series") or [] if s["active_seconds"] or s["page_views"]),
                         "top_pages": [f"{p['page']} ({minutes(p['active_seconds'])} min)" for p in (detail.get("pages") or [])[:5]]},
        "last_sessions": [{"started": _iso(s.get("started_at")), "minutes": minutes(s.get("active_seconds"))}
                          for s in (detail.get("sessions") or [])[:4]],
        "totals": {"requests": counts.get("requests"), "requests_pending": counts.get("requests_pending"),
                   "tickets": counts.get("tickets"), "suggestions": counts.get("suggestions"),
                   "sessions": counts.get("sessions"), "hours_in_hub": round(int(counts.get("total_seconds") or 0) / 3600, 1)},
        "connected_systems": [c["system"] for c in detail.get("connected") or []],
        "recent_actions": (detail.get("recent_actions") or [])[:6],
        "recent_requests": [{"id": r["id"], "title": r["request_title"], "type": r["request_type"],
                             "status": r["status"], "created": _iso(r["created_at"])} for r in requests],
        "devbot_30_days": {"questions": int(devbot.get("questions") or 0), "failed": int(devbot.get("failed") or 0),
                           "last": _iso(devbot.get("last"))},
        "streak": streak,
        "recent_warnings_and_errors": [{"at": _iso(e.get("created_at")), "level": e.get("level"), "action": e.get("action"),
                                        "detail": _trim(e.get("metadata"), 160)} for e in problems],
    }
    return {"data": data, "summary": f"{data['name']} ({data['role']}{', inactive' if not data['active'] else ''})",
            "note": "Lead with who they are and whether anything needs attention (pending requests, errors, inactive).",
            "links": [{"title": f"{data['name']} on the Users page", "url": "/ui/users", "system": "hub"}]}


def streak(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import streaks

    _guard(ctx)
    row = _user_row(args["email"])
    data = streaks.explain(row["email"])
    return {"data": data, "summary": f"{row['email']}: {data['current']} day(s)",
            "note": "Explain the number from last_ended and last_21_days: which day ended it and why."}


# ── requests ─────────────────────────────────────────────────────────────────

def requests(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    where = ["ar.created_at > now() - (%s * interval '1 day')"]
    params: List[Any] = [max(1, min(int(args.get("days") or 30), 365))]
    status = str(args.get("status") or "").upper()
    if status:
        if status not in _STATUSES:
            raise ToolFailure("status is one of " + ", ".join(_STATUSES) + ".")
        where.append("ar.status = %s")
        params.append(status)
    if args.get("email"):
        where.append("LOWER(u.email) = %s")
        params.append(_email(args["email"]))
    if args.get("type"):
        where.append("ar.request_type::text ILIKE %s")
        params.append(f"%{args['type']}%")
    rows = query_all(
        f"""
        SELECT ar.id::text AS id, ar.request_title, ar.request_type::text AS type, ar.status::text AS status,
               ar.created_at, ar.approved_at, ar.executed_at, u.email AS requester, a.email AS approver,
               ar.execution_result->>'error' AS error
          FROM approval_requests ar JOIN users u ON ar.requester_id = u.id
          LEFT JOIN users a ON ar.approver_id = a.id
         WHERE {" AND ".join(where)}
         ORDER BY ar.created_at DESC LIMIT %s
        """,
        params + [max(1, min(int(args.get("top") or 15), 40))],
    )
    by_status = query_all(
        "SELECT status::text AS status, COUNT(*) AS n FROM approval_requests "
        "WHERE created_at > now() - (%s * interval '1 day') GROUP BY status", [params[0]])
    out = [{"id": r["id"], "title": r["request_title"], "type": r["type"], "status": r["status"],
            "requester": r["requester"], "approver": r["approver"], "created": _iso(r["created_at"]),
            "decided": _iso(r["approved_at"]), "finished": _iso(r["executed_at"]),
            "error": (r.get("error") or "")[:200] or None} for r in rows]
    return {"data": {"requests": out, "all_by_status": {r["status"]: int(r["n"]) for r in by_status}},
            "summary": f"{len(out)} request(s)",
            "links": [{"title": "Approvals", "url": "/ui/approvals", "system": "hub"}]}


def request(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    rid = str(args["id"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f-]{1,36}", rid):
        raise ToolFailure(f"There is no request {rid[:40]}.")
    row = query_one(
        """
        SELECT ar.*, ar.request_type::text AS type_text, ar.status::text AS status_text, u.email AS requester,
               a.email AS approver
          FROM approval_requests ar JOIN users u ON ar.requester_id = u.id
          LEFT JOIN users a ON ar.approver_id = a.id
         WHERE ar.id::text = %s OR ar.id::text LIKE %s
         ORDER BY ar.created_at DESC LIMIT 1
        """,
        [rid, rid.lower() + "%" if len(rid) >= 6 else rid],
    )
    if not row:
        raise ToolFailure(f"There is no request {rid}.")
    data = {"id": str(row["id"]), "title": row.get("request_title"), "type": row["type_text"], "status": row["status_text"],
            "requester": row["requester"], "approver": row.get("approver"), "created": _iso(row.get("created_at")),
            "decided": _iso(row.get("approved_at")), "finished": _iso(row.get("executed_at")),
            "approver_comments": row.get("approver_comments") or None, "rejection_reason": row.get("rejection_reason") or None,
            "payload": _trim(row.get("request_payload") or {}, 300), "result": _trim(row.get("execution_result") or {}, 300)}
    return {"data": data, "summary": f"{data['title']}: {data['status']}",
            "note": "Say what was asked, where it stands and, if it failed, why (result.error).",
            "links": [{"title": "Approvals", "url": "/ui/approvals", "system": "hub"}]}


# ── the log, usage, DevBot, health ───────────────────────────────────────────

def logs(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import audit

    _guard(ctx)
    hours = max(1, min(int(args.get("hours") or 24), 24 * 7))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    filters = {"user_email": _email(args.get("email")) or None, "action": args.get("action") or None,
               "level": args.get("level") or None, "q": args.get("contains") or None, "date_from": since}
    got = audit.list_events(**filters, limit=max(1, min(int(args.get("top") or 20), 50)))
    counts = audit.level_counts(user_email=filters["user_email"], q=filters["q"], date_from=since)
    rows = [{"at": _iso(e.get("created_at")), "level": e.get("level"), "user": e.get("user_email"),
             "action": e.get("action"), "detail": _trim(e.get("metadata"), 200)} for e in got.get("data") or []]
    return {"data": {"events": rows, "matching": got.get("total"), "levels_in_window": counts, "hours": hours},
            "summary": f"{got.get('total') or 0} log event(s) in {hours} h",
            "links": [{"title": "Logs", "url": "/ui/audit-logs", "system": "hub"}]}


def usage(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import usage_tracking

    _guard(ctx)
    days = max(1, min(int(args.get("days") or 7), 90))
    s = usage_tracking.summary(days)
    top = sorted(usage_tracking.users_overview(), key=lambda u: -int(u.get("seconds_7d") or 0))[:8]
    data = {"accounts": s.get("users"), "active_accounts": s.get("active_accounts"), "new_last_7_days": s.get("new_7d"),
            "people_today": s.get("dau"), "people_this_week": s.get("wau"), "people_this_month": s.get("mau"),
            "sessions_in_range": s.get("sessions_range"), "range_days": days,
            "top_pages": [f"{p['page']} ({p['users']} people)" for p in (s.get("pages") or [])[:8]],
            "most_active_this_week": [f"{u['email']} ({round(int(u.get('seconds_7d') or 0) / 60)} min)" for u in top
                                      if u.get("seconds_7d")],
            "last_activity_anywhere": s.get("last_beat_at")}
    return {"data": data, "summary": f"{data['people_this_week']} people this week",
            "links": [{"title": "Users", "url": "/ui/users", "system": "hub"}]}


def devbot(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from .. import monitor

    _guard(ctx)
    days = max(1, min(int(args.get("days") or 30), 90))
    o = monitor.overview(days)
    people = monitor.people(days)[:8]
    down = monitor.feedback("down", days, 5)
    data = {"days": days, "totals": o["totals"], "why_questions_failed": o["errors"][:6],
            "models": o["models"][:6], "lookups_failing_most": sorted(
                [t for t in o["tools"] if t.get("failed")], key=lambda t: -t["failed"])[:6],
            "latest_lookup_errors": [{**e, "error": str(e.get("error") or "")[:160]} for e in o["tool_errors"][:6]],
            "top_people": [{"email": p.get("email"), "questions": p["questions"], "failed": p["failed"],
                            "limited": p["limited"], "key_connected": bool(p.get("key_connected")),
                            "last": (p.get("last_at") or "")[:16] or None} for p in people if p["questions"]],
            "recent_thumbs_down": [{"who": f.get("email") or f.get("username"), "question": str(f.get("question") or "")[:200],
                                    "note": f.get("note") or None} for f in down]}
    return {"data": data, "summary": f"{o['totals'].get('questions', 0)} DevBot question(s) in {days} days",
            "links": [{"title": "DevBot monitoring", "url": "/ui/devbot-monitor", "system": "hub"}]}


def health(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    import changelog
    import safe_mode

    from .. import config, knowledge

    _guard(ctx)
    pending = query_one(
        "SELECT COUNT(*) FILTER (WHERE status = 'PENDING') AS pending, "
        "COUNT(*) FILTER (WHERE status::text IN ('APPROVED', 'IN_PROGRESS') AND created_at < now() - interval '1 hour') AS stuck, "
        "COUNT(*) FILTER (WHERE status = 'FAILED' AND created_at > now() - interval '7 days') AS failed_7d "
        "FROM approval_requests") or {}
    backups = query_all(
        "SELECT DISTINCT ON (kind) kind, status, started_at, finished_at, message FROM backup_runs "
        "ORDER BY kind, started_at DESC")
    errors = query_one("SELECT COUNT(*) AS n FROM audit_events WHERE level IN ('ERROR', 'CRITICAL') "
                       "AND created_at > now() - interval '24 hours'") or {}
    try:
        idx = knowledge.admin_status()
        last = (idx.get("runs") or [{}])[0]
        index = {"configured": idx["configured"], "running": idx["running"], "indexed": idx["pages"],
                 "by_source": idx["by_source"], "last_build": last.get("status"), "last_build_at": last.get("finished_at")
                 or last.get("started_at"), "last_error": last.get("error") or None}
    except Exception:
        log.warning("adminbot: index status could not be read", exc_info=True)
        index = {"error": "could not be read"}
    data = {"version": changelog.version(), "safe_mode": safe_mode.is_enabled(), "devbot_set_up": config.enabled(),
            "requests": {k: int(v or 0) for k, v in pending.items()},
            "errors_in_log_24h": int(errors.get("n") or 0),
            "backups": [{"kind": b["kind"], "status": b["status"], "started": _iso(b["started_at"]),
                         "message": (b.get("message") or "")[:160] or None} for b in backups],
            "devbot_search_index": index}
    return {"data": data, "summary": f"Version {data['version']}, {data['requests'].get('pending', 0)} pending request(s)",
            "note": "Lead with anything that needs an admin (stuck or failed requests, a failed backup, errors, Safe Mode on)."}


# ── proposals: the admin confirms, run_action() performs ─────────────────────

def propose_decision(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    decision = str(args["decision"]).lower()
    row = query_one(
        "SELECT ar.id::text AS id, ar.request_title, ar.request_type::text AS type, ar.status::text AS status, "
        "u.email AS requester FROM approval_requests ar JOIN users u ON ar.requester_id = u.id "
        "WHERE ar.id::text = %s", [str(args["request_id"]).strip()])
    if not row:
        raise ToolFailure(f"There is no request {args['request_id']}. Use the full id hub_requests returns.")
    if row["status"] != "PENDING":
        raise ToolFailure(f"That request is {row['status']}, not waiting for a decision.")
    comment = str(args.get("comment") or "").strip()[:500]
    verb = "Approve" if decision == "approve" else "Reject"
    action = proposals.action(
        f"request-{decision}", f"{verb} the request",
        f"{verb} “{row['request_title']}” ({row['type']}) from {row['requester']}"
        + (f", saying: {comment.rstrip('.')}" if comment else "") + ".",
        {"request_id": row["id"], "title": row["request_title"], "requester": row["requester"], "type": row["type"],
         "comment": comment})
    return {"data": {"proposed": action["summary"]}, "actions": [action], "summary": f"{verb} proposed",
            "note": "Tell the admin to review and confirm the card below. It is not done yet."}


def propose_active(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    row = _user_row(args["email"])
    active = bool(args["active"])
    if bool(row["is_active"]) == active:
        raise ToolFailure(f"{row['email']} is already {'active' if active else 'inactive'}.")
    if not active and _email(row["email"]) == _email(ctx.user.get("email")):
        raise ToolFailure("An admin cannot deactivate their own account.")
    verb = "Reactivate" if active else "Deactivate"
    action = proposals.action(
        "user-active", f"{verb} the account",
        f"{verb} {row['email']} ({row['name']})." + ("" if active else " They are signed out within a minute and cannot "
                                                                       "sign in again until reactivated."),
        {"email": row["email"], "name": row["name"], "active": active})
    return {"data": {"proposed": action["summary"]}, "actions": [action], "summary": f"{verb} proposed",
            "note": "Tell the admin to review and confirm the card below. It is not done yet."}


def propose_role(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from security import PLATFORM_ADMIN_LEVEL, REGULAR_USER_LEVEL

    _guard(ctx)
    row = _user_row(args["email"])
    roles = query_all("SELECT name, hierarchy_level FROM roles WHERE hierarchy_level IN (%s, %s)",
                      [PLATFORM_ADMIN_LEVEL, REGULAR_USER_LEVEL])
    wanted = str(args["role"]).strip().lower()
    role = next((r for r in roles if r["name"].lower() == wanted or (wanted.startswith("admin") and
                                                                     r["hierarchy_level"] == PLATFORM_ADMIN_LEVEL)
                 or (wanted in ("user", "regular", "regular user") and r["hierarchy_level"] == REGULAR_USER_LEVEL)), None)
    if not role:
        raise ToolFailure("The role is one of: " + ", ".join(r["name"] for r in roles) + ".")
    if role["name"] == row["role"]:
        raise ToolFailure(f"{row['email']} is already {row['role']}.")
    action = proposals.action(
        "user-role", "Change the role",
        f"Make {row['email']} ({row['name']}) {role['name']} instead of {row['role']}."
        + (" They keep their current access until their session ends (up to 8 hours)."
           if row["hierarchy_level"] == PLATFORM_ADMIN_LEVEL else ""),
        {"email": row["email"], "name": row["name"], "role": role["name"], "was": row["role"]})
    return {"data": {"proposed": action["summary"]}, "actions": [action], "summary": "Role change proposed",
            "note": "Tell the admin to review and confirm the card below. It is not done yet."}


_TOLD = "Tell the admin to review and confirm the card below. It is not done yet."
MAX_BULK = 20


def _proposed(action: Dict[str, Any], summary: str) -> Dict[str, Any]:
    return {"data": {"proposed": action["summary"]}, "actions": [action], "summary": summary, "note": _TOLD}


def propose_decisions(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Several PENDING requests approved or rejected in one card, each one on its own."""
    _guard(ctx)
    decision = str(args["decision"]).lower()
    ids = list(dict.fromkeys(str(i).strip() for i in args.get("request_ids") or [] if str(i).strip()))
    if len(ids) < 2:
        raise ToolFailure("Give at least two request ids; for one request use propose_request_decision.")
    if len(ids) > MAX_BULK:
        raise ToolFailure(f"At most {MAX_BULK} requests in one decision.")
    rows = query_all(
        "SELECT ar.id::text AS id, ar.request_title AS title, ar.request_type::text AS type, ar.status::text AS status, "
        "u.email AS requester FROM approval_requests ar JOIN users u ON ar.requester_id = u.id "
        "WHERE ar.id::text = ANY(%s)", [ids])
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    not_pending = [f"{r['title']} ({r['status']})" for r in rows if r["status"] != "PENDING"]
    items = [{"request_id": r["id"], "title": r["title"], "type": r["type"], "requester": r["requester"]}
             for r in rows if r["status"] == "PENDING"]
    if not items:
        raise ToolFailure("None of those requests is waiting for a decision.")
    comment = str(args.get("comment") or "").strip()[:500]
    verb = "Approve" if decision == "approve" else "Reject"
    shown = "; ".join(f"“{i['title']}” from {i['requester']}" for i in items[:6])
    action = proposals.action(
        f"requests-{decision}", f"{verb} {len(items)} requests",
        f"{verb} {len(items)} requests, each one on its own: {shown}"
        + (f" and {len(items) - 6} more" if len(items) > 6 else "") + ".",
        {"items": items, "comment": comment})
    out = _proposed(action, f"{verb} {len(items)} proposed")
    left = ([f"not found: {', '.join(missing)}"] if missing else []) + \
           ([f"not waiting for a decision: {', '.join(not_pending)}"] if not_pending else [])
    if left:
        out["note"] += " Left out of the card -- " + "; ".join(left) + ". Say so."
    return out


def announcements(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    rows = query_all(
        "SELECT id, title, kind, is_active, starts_at, ends_at, created_by, created_at, "
        "(is_active AND (starts_at IS NULL OR starts_at <= NOW()) AND (ends_at IS NULL OR ends_at > NOW())) AS showing "
        "FROM announcements ORDER BY created_at DESC, id DESC LIMIT 20")
    data = [{"id": r["id"], "title": r["title"], "kind": r["kind"], "showing_now": bool(r["showing"]),
             "active": bool(r["is_active"]), "starts": _iso(r["starts_at"]), "ends": _iso(r["ends_at"]),
             "by": r["created_by"], "posted": _iso(r["created_at"])} for r in rows]
    return {"data": {"announcements": data}, "summary": f"{sum(1 for d in data if d['showing_now'])} showing now",
            "links": [{"title": "Announcements", "url": "/ui/platform-managing", "system": "hub"}]}


def propose_announcement(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from api.announcements import KINDS

    _guard(ctx)
    title = str(args["title"]).strip()[:200]
    if not title:
        raise ToolFailure("An announcement needs a title.")
    kind = str(args.get("kind") or "info").strip().lower()
    if kind not in KINDS:
        raise ToolFailure("kind is one of: " + ", ".join(KINDS) + ".")
    target = {"title": title, "body": str(args.get("body") or "").strip()[:4000], "kind": kind,
              "starts_at": str(args.get("starts_at") or "").strip(), "ends_at": str(args.get("ends_at") or "").strip(),
              "link_url": str(args.get("link_url") or "").strip(), "link_label": str(args.get("link_label") or "").strip()}
    when = ""
    if target["starts_at"] or target["ends_at"]:
        when = f" from {target['starts_at'] or 'now'} until {target['ends_at'] or 'it is taken down'}"
    action = proposals.action("announcement-post", "Post the announcement",
                              f"Show “{title}” ({kind}) at the top of everyone's dashboard{when}.", target)
    return _proposed(action, "Announcement proposed")


def propose_announcement_end(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    row = query_one("SELECT id, title, is_active FROM announcements WHERE id = %s", [int(args["id"])])
    if not row:
        raise ToolFailure(f"There is no announcement {args['id']}. List them with hub_announcements.")
    if not row["is_active"]:
        raise ToolFailure(f"“{row['title']}” is already taken down.")
    action = proposals.action("announcement-end", "Take the announcement down",
                              f"Stop showing “{row['title']}” to everyone. It stays in Platform Managing, "
                              "switched off.", {"id": row["id"], "title": row["title"]})
    return _proposed(action, "Taking it down proposed")


def propose_index(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from devbot import knowledge

    _guard(ctx)
    busy = knowledge.running()
    if str(args["action"]).lower() == "build":
        if busy:
            raise ToolFailure("A build is already running.")
        if not knowledge.configured():
            raise ToolFailure("The index is not set up: it needs the AI key and at least one source (Platform Managing).")
        action = proposals.action("index-build", "Build the search index now",
                                  "Bring DevBot's search index up to date now, in the background: only what is new or "
                                  "changed is read, and past fixes are reviewed on the Hub's AI key.", {})
    else:
        if not busy:
            raise ToolFailure("No build is running.")
        action = proposals.action("index-stop", "Stop the index build",
                                  "Stop the running build within seconds. What it indexed so far stays.", {})
    return _proposed(action, "Proposed")


def past_fixes(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from devbot import knowledge

    _guard(ctx)
    state = str(args.get("state") or "").lower()
    source = {"azure": "ado", "ado": "ado", "tickets": "snow", "snow": "snow"}.get(str(args.get("source") or "").lower(), "")
    out = knowledge.list_fixes(source, state if state in ("shown", "held", "hidden", "waiting") else "",
                               str(args.get("query") or ""), max(1, min(int(args.get("top") or 10), 25)))
    items = [{**{k: i.get(k) for k in ("id", "source", "ref", "title", "state", "held", "audience", "hidden_by")},
              "problem": _trim(i.get("problem") or "", 240), "fix": _trim(i.get("fix") or "", 300)} for i in out["items"]]
    return {"data": {"fixes": items, "total": out["total"], "counts": out["counts"]},
            "summary": f"{out['total']} past fixes", "note": "Use the id exactly as given to propose a change to one."}


def propose_fix(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _guard(ctx)
    fid = str(args["id"]).strip()
    change = str(args["change"]).lower()
    row = query_one("SELECT page_id, title, ref, hidden FROM devbot_index_pages "
                    "WHERE page_id = %s AND source <> 'confluence'", [fid])
    if not row:
        raise ToolFailure(f"There is no past fix {fid}. Find it with hub_past_fixes and use its id.")
    name = f"{row['ref'] or ''} “{row['title']}”".strip()
    target = {"id": fid, "title": row["title"], "ref": row["ref"]}
    if change == "hide":
        if row["hidden"]:
            raise ToolFailure(f"{name} is already hidden.")
        action = proposals.action("fix-hide", "Hide the past fix", f"Stop showing {name} to anyone in DevBot.", target)
    elif change == "show":
        if not row["hidden"]:
            raise ToolFailure(f"{name} is not hidden.")
        action = proposals.action("fix-show", "Show the past fix again", f"Show {name} in DevBot again.", target)
    else:
        action = proposals.action("fix-review", "Review the past fix again",
                                  f"Review {name} again on the next build; until then it is not shown.", target)
    return _proposed(action, "Proposed")


def _decide_each(user: Any, kind: str, t: Dict[str, Any], comment: Optional[str]) -> Dict[str, Any]:
    from fastapi import HTTPException

    from api.approvals import ApproveRejectRequest, approve_request, reject_request

    fn = approve_request if kind == "requests-approve" else reject_request
    note = (comment if comment is not None else t.get("comment") or "").strip()[:500] or None
    done: List[str] = []
    refused: List[str] = []
    for item in (t.get("items") or [])[:MAX_BULK]:
        # Each on its own: one that moved on since the card was drafted is refused by its
        # own guard and says why, and the others still go through.
        try:
            fn(str(item["request_id"]), ApproveRejectRequest(comments=note), current_user=user)
            done.append(str(item.get("title")))
        except HTTPException as exc:
            refused.append(f"{item.get('title')}: {exc.detail}")
        except Exception:
            log.warning("adminbot: deciding %s failed", item.get("request_id"), exc_info=True)
            refused.append(f"{item.get('title')}: failed unexpectedly (see the Hub's log)")
    if not done:
        raise HTTPException(status_code=409, detail="None was done. " + " | ".join(refused))
    result = f"{'Approved' if kind == 'requests-approve' else 'Rejected'} {len(done)} of {len(done) + len(refused)}."
    if refused:
        result += " Not done: " + " | ".join(refused)
    return {"result": result, "url": "/ui/approvals"}


def run_action(user: Any, action: Dict[str, Any], comment: Optional[str] = None) -> Dict[str, Any]:
    """Perform a confirmed proposal as ``user``, through the admin pages' own endpoint
    functions (every guard they have applies). Raises HTTPException with their reason.
    Returns {"result": words for the card, "url": where to see it}."""
    from fastapi import HTTPException

    from security import has_effective_admin_access_live

    if not has_effective_admin_access_live(user):
        raise HTTPException(status_code=403, detail="Admin access required.")
    kind = str(action.get("kind") or "")
    t = action.get("target") or {}
    if kind in ("request-approve", "request-reject"):
        from api.approvals import ApproveRejectRequest, approve_request, reject_request

        note = (comment if comment is not None else t.get("comment") or "").strip()[:500] or None
        fn = approve_request if kind == "request-approve" else reject_request
        fn(str(t["request_id"]), ApproveRejectRequest(comments=note), current_user=user)
        return {"result": f"{'Approved' if kind == 'request-approve' else 'Rejected'}: {t.get('title')}",
                "url": "/ui/approvals"}
    if kind == "user-active":
        from api.admin import SetActiveRequest, set_user_active

        out = set_user_active(SetActiveRequest(email=t["email"], is_active=bool(t["active"])), current_user=user)
        return {"result": str(out.get("message") or "Done."), "url": "/ui/users"}
    if kind == "user-role":
        from api.admin import SetRoleRequest, set_user_role

        out = set_user_role(SetRoleRequest(email=t["email"], role_name=t["role"]), current_user=user)
        warning = ((out.get("data") or {}).get("warning")) or ""
        return {"result": (str(out.get("message") or "Done.") + (" " + warning if warning else "")).strip(), "url": "/ui/users"}
    if kind in ("requests-approve", "requests-reject"):
        return _decide_each(user, kind, t, comment)
    if kind == "announcement-post":
        from api.announcements import AnnouncementBody, create_announcement

        out = create_announcement(AnnouncementBody(
            title=t["title"], body=t.get("body") or "", kind=t.get("kind") or "info",
            link_url=t.get("link_url") or None, link_label=t.get("link_label") or None,
            starts_at=t.get("starts_at") or None, ends_at=t.get("ends_at") or None), admin=user)
        return {"result": f"Posted: {(out.get('data') or {}).get('title') or t['title']}", "url": "/ui/platform-managing"}
    if kind == "announcement-end":
        from api.announcements import AnnouncementBody, update_announcement

        row = query_one("SELECT title, body, kind, link_url, link_label, starts_at, ends_at FROM announcements "
                        "WHERE id = %s", [int(t["id"])])
        if not row:
            raise HTTPException(status_code=404, detail="That announcement no longer exists.")
        update_announcement(int(t["id"]), AnnouncementBody(
            title=row["title"], body=row["body"] or "", kind=row["kind"] or "info", link_url=row["link_url"],
            link_label=row["link_label"], starts_at=row["starts_at"].isoformat() if row["starts_at"] else None,
            ends_at=row["ends_at"].isoformat() if row["ends_at"] else None, is_active=False), admin=user)
        back = query_one("SELECT is_active FROM announcements WHERE id = %s", [int(t["id"])]) or {}
        if back.get("is_active"):
            raise HTTPException(status_code=502, detail="It was saved but still reads as showing. Check it in Platform Managing.")
        return {"result": f"Taken down: {row['title']}", "url": "/ui/platform-managing"}
    if kind in ("index-build", "index-stop"):
        from devbot import knowledge

        if kind == "index-build":
            started = knowledge.start("adminbot")
            if not started["started"]:
                raise HTTPException(status_code=409, detail=started["why"])
            return {"result": "The build started. Its progress is on Platform Managing.", "url": "/ui/platform-managing"}
        outcome = knowledge.request_stop()
        if outcome == "none":
            raise HTTPException(status_code=409, detail="No build is running.")
        return {"result": "Stopping: it stops within seconds." if outcome == "stopping" else
                "The build's pod was gone; it is marked stopped.", "url": "/ui/platform-managing"}
    if kind in ("fix-hide", "fix-show", "fix-review"):
        from devbot import knowledge

        by = str(user.get("username") or user.get("email") or "")
        ok = knowledge.request_review(t["id"]) if kind == "fix-review" else \
            knowledge.set_hidden(t["id"], kind == "fix-hide", by)
        if not ok:
            raise HTTPException(status_code=404, detail="That past fix is no longer in the index.")
        words = {"fix-hide": "Hidden", "fix-show": "Shown again", "fix-review": "Will be reviewed on the next build"}
        return {"result": f"{words[kind]}: {t.get('ref') or ''} {t.get('title') or ''}".replace("  ", " ").strip(),
                "url": "/ui/platform-managing"}
    raise HTTPException(status_code=400, detail=f"AdminBot cannot perform '{kind}'.")


register(
    Tool("hub_find_users", "hub", "read", "Finding the person",
         "Accounts whose email, name or username contains the text: email, name, role, active, last seen.",
         {"query": {"type": "string"}}, find_users, required=["query"], max_chars=2400),
    Tool("hub_user", "hub", "read", "Reading the person's profile",
         "Everything the Hub knows about one account: role, active, sign-ins, time in the Hub and top pages (30 days), "
         "connected systems (names only), recent requests, DevBot use, streak, and recent warnings or errors in the log.",
         {"email": {"type": "string"}}, user_profile, required=["email"], max_chars=4200),
    Tool("hub_streak", "hub", "read", "Explaining the streak",
         "One person's streak with its reason: current and longest, freezes, when and why it last ended, and the last "
         "21 days of what counted.",
         {"email": {"type": "string"}}, streak, required=["email"], max_chars=3200),
    Tool("hub_requests", "hub", "read", "Reading the requests",
         "Self-service requests, newest first, with counts by status. Filter by status (PENDING, APPROVED, IN_PROGRESS, "
         "COMPLETED, FAILED, REJECTED), requester email, type and days back (default 30).",
         {"status": {"type": "string"}, "email": {"type": "string"}, "type": {"type": "string"},
          "days": {"type": "integer"}, "top": {"type": "integer"}}, requests, max_chars=3600),
    Tool("hub_request", "hub", "read", "Reading the request",
         "One request by id (or the start of its id): what was asked, status, who decided, and the result or error.",
         {"id": {"type": "string"}}, request, required=["id"], max_chars=3600),
    Tool("hub_logs", "hub", "read", "Reading the log",
         "The Hub's log (Admin -> Logs): events in the last N hours (default 24, at most 168), filtered by user email, "
         "level (INFO, WARNING, ERROR, CRITICAL; comma-separated), action, or text it contains.",
         {"email": {"type": "string"}, "level": {"type": "string"}, "action": {"type": "string"},
          "contains": {"type": "string"}, "hours": {"type": "integer"}, "top": {"type": "integer"}}, logs, max_chars=3800),
    Tool("hub_usage", "hub", "read", "Reading who uses the Hub",
         "How the Hub is used: accounts, people today / this week / this month, top pages and the most active people "
         "this week.", {"days": {"type": "integer"}}, usage, max_chars=2800),
    Tool("hub_devbot", "hub", "read", "Reading DevBot's figures",
         "DevBot over N days (default 30): questions, how they ended, why they failed, models, failing lookups and their "
         "latest errors, the people asking most, and recent thumbs-down with their notes.",
         {"days": {"type": "integer"}}, devbot, max_chars=4200),
    Tool("hub_health", "hub", "read", "Checking the Hub's health",
         "The Hub's state: version, Safe Mode, pending / stuck / failed requests, errors in the log (24 h), the latest "
         "backups, and DevBot's search index.", {}, health, max_chars=3200),
    Tool("propose_request_decision", "hub", "write", "Preparing the decision",
         "Propose approving or rejecting a PENDING request, with an optional comment to the requester. The admin "
         "reviews and confirms it; nothing happens until then.",
         {"request_id": {"type": "string"}, "decision": {"type": "string", "enum": ["approve", "reject"]},
          "comment": {"type": "string"}}, propose_decision, required=["request_id", "decision"]),
    Tool("propose_requests_decision", "hub", "write", "Preparing the decisions",
         f"Propose approving or rejecting SEVERAL pending requests at once (2 to {MAX_BULK} full ids), with one optional "
         "comment to all requesters. Each is decided on its own when the admin confirms.",
         {"request_ids": {"type": "array", "items": {"type": "string"}},
          "decision": {"type": "string", "enum": ["approve", "reject"]}, "comment": {"type": "string"}},
         propose_decisions, required=["request_ids", "decision"]),
    Tool("hub_announcements", "hub", "read", "Reading the announcements",
         "The latest announcements (the billboard on everyone's dashboard): id, title, kind, showing now, window, author.",
         {}, announcements, max_chars=2400),
    Tool("propose_announcement", "hub", "write", "Preparing the announcement",
         "Propose posting an announcement at the top of everyone's dashboard: title, body, kind (info | success | "
         "warning | critical), optional starts_at / ends_at (ISO date-time, UTC), link_url and link_label. The admin "
         "confirms it.",
         {"title": {"type": "string"}, "body": {"type": "string"}, "kind": {"type": "string"},
          "starts_at": {"type": "string"}, "ends_at": {"type": "string"}, "link_url": {"type": "string"},
          "link_label": {"type": "string"}}, propose_announcement, required=["title"]),
    Tool("propose_announcement_end", "hub", "write", "Preparing to take it down",
         "Propose taking an announcement down (by its id from hub_announcements). The admin confirms it.",
         {"id": {"type": "integer"}}, propose_announcement_end, required=["id"]),
    Tool("propose_index_build", "hub", "write", "Preparing the index build",
         "Propose starting a build of DevBot's search index now (action build) or stopping the running one (action "
         "stop). The admin confirms it.",
         {"action": {"type": "string", "enum": ["build", "stop"]}}, propose_index, required=["action"]),
    Tool("hub_past_fixes", "hub", "read", "Reading the past fixes",
         "Past fixes in DevBot's search index: id, where from (ref), title, state (shown | held | hidden | waiting), why "
         "held, who can apply it, the reviewed problem and fix. Filter by state, source (azure | tickets) and text.",
         {"state": {"type": "string"}, "source": {"type": "string"}, "query": {"type": "string"},
          "top": {"type": "integer"}}, past_fixes, max_chars=3800),
    Tool("propose_past_fix", "hub", "write", "Preparing the change to the fix",
         "Propose hiding a past fix from everyone (hide), showing a hidden one again (show), or having it reviewed "
         "again on the next build (review). By its id from hub_past_fixes. The admin confirms it.",
         {"id": {"type": "string"}, "change": {"type": "string", "enum": ["hide", "show", "review"]}},
         propose_fix, required=["id", "change"]),
    Tool("propose_user_active", "hub", "write", "Preparing the account change",
         "Propose deactivating (active=false) or reactivating (active=true) an account. The admin confirms it.",
         {"email": {"type": "string"}, "active": {"type": "boolean"}}, propose_active, required=["email", "active"]),
    Tool("propose_user_role", "hub", "write", "Preparing the role change",
         "Propose changing an account's role (Admin or Regular User). The admin confirms it.",
         {"email": {"type": "string"}, "role": {"type": "string"}}, propose_role, required=["email", "role"]),
)
