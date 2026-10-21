"""
The Hub's own forms, drafted by DevBot: a self-service request, a support ticket.

The model never submits one. It proposes a DRAFT: which form, and answers for the
questions it can answer from the conversation. The card opens that form (the one
renderer, catalog-form.html) already filled in; the person reads it, completes what
only they can answer, submits it through the form's own endpoint and then confirms
once more in the big confirmation. A request still waits for an admin's approval
exactly as one typed by hand, and a ticket is raised by the same code as the
Support page's.

What a draft may fill is decided here, not by the model: only questions the form
really has, never a file, never the requester's own details (the form remembers
those, and the name on a ticket is the identity provider's), and a pick-list only
with one of its own choices. Anything else is dropped and the model is told which.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import catalog_forms

from . import proposals
from .base import Tool, ToolContext, ToolFailure, register

# The forms a person can be handed a draft of, and what each is for (the model's words).
REQUEST_FORMS = {
    "artifactory_quota": "more storage (quota) for an Artifactory project",
    "artifactory_cleaner": "a scheduled cleaner that deletes old artifacts from an Artifactory repository",
    "pipeline_characterization": "a new pipeline built by the DevOps team (its purpose, trigger, build, quality gate)",
}
TICKET_FORM = "support_ticket"
_NEVER = ("file",)
_MAX_TEXT = 4000


def _choices(field: Dict[str, Any]) -> List[str]:
    return [str(o.get("value")) for o in field.get("options") or [] if isinstance(o, dict)]


def prefill(form_key: str, values: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The answers a draft may carry into ``form_key``, and the keys it may not."""
    spec = catalog_forms.form(form_key)
    if not spec:
        raise ToolFailure(f"There is no form called {form_key}.")
    fields = {f["key"]: f for s in spec.get("sections") or [] if s.get("key") != "requester"
              for f in s.get("fields") or [] if f.get("key")}
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, value in (values or {}).items():
        field = fields.get(str(key))
        if not field or field.get("type") in _NEVER or field.get("readonly") or value in (None, ""):
            dropped.append(str(key))
            continue
        if field.get("type") == "toggle":
            kept[field["key"]] = str(value).strip().lower() in ("1", "true", "yes", "on")
            continue
        text = str(value).strip()[:_MAX_TEXT]
        choices = _choices(field)
        if choices:
            match = next((c for c in choices if c.lower() == text.lower()), None)
            if match is None:
                dropped.append(str(key))
                continue
            text = match
        kept[field["key"]] = text
    return kept, dropped


def _questions(form_key: str) -> str:
    spec = catalog_forms.form(form_key) or {}
    out = []
    for s in spec.get("sections") or []:
        if s.get("key") == "requester":
            continue
        for f in s.get("fields") or []:
            if f.get("type") in _NEVER:
                continue
            choices = _choices(f)
            out.append(f"{f['key']} ({f.get('label') or f['key']}"
                       + (": one of " + " | ".join(choices[:12]) if choices else "") + ")")
    return "; ".join(out)


def _drafted(kind: str, title: str, summary: str, target: Dict[str, Any], dropped: List[str]) -> Dict[str, Any]:
    action = proposals.action(kind, title, summary, target)
    note = ("The person now sees a card with a Review button, which opens the form filled in with your draft. They "
            "complete it, submit it and confirm it themselves. Tell them in one sentence what you drafted and what "
            "they still have to fill in. Do NOT say it is sent.")
    if dropped:
        note += " These answers were left out because the form has no such question or no such choice: " + \
                ", ".join(dropped[:12]) + "."
    return {"data": {"drafted": summary, "filled": sorted(target["values"]), "left_out": dropped,
                     "status": "waiting for the person to submit and confirm"},
            "actions": [action], "note": note, "summary": "Drafted: " + title}


def propose_request(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    form_key = str(args["form"]).strip()
    if form_key not in REQUEST_FORMS:
        raise ToolFailure("The form is one of: " + ", ".join(REQUEST_FORMS) + ".")
    values, dropped = prefill(form_key, args.get("values") or {})
    spec = catalog_forms.form(form_key) or {}
    title = str(spec.get("title") or form_key)
    # What submitting it does, said in the big confirmation: an approval request waits for
    # an admin; the others reach the DevOps team as a request ticket.
    lines = (["It is sent in your name to the DevOps team's approval queue.",
              "Nothing changes until an admin approves it."] if spec.get("request_type") else
             ["It is sent in your name to the DevOps team as a request, which they follow up with you."])
    return _drafted("catalog-request", title,
                    f"A “{title}” request, filled in from this conversation. It goes to the DevOps team like any "
                    "other request.",
                    {"form": form_key, "title": title, "values": values, "lines": lines}, dropped)


def propose_support_ticket(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    values, dropped = prefill(TICKET_FORM, {
        "title": str(args["title"]).strip()[:160], "description": args.get("description"),
        "work_impact": args.get("work_impact"), "urgency": args.get("urgency"),
        "devops_services": args.get("service"), "pipeline_url": args.get("pipeline_url"),
        "help_text": args.get("help_wanted"), "reason": args.get("reason"),
        "azure_devops_support_type": args.get("support_type"), "azure_devops_collection": args.get("collection"),
        "azure_devops_project": args.get("project"),
    })
    return _drafted("support-ticket", "Open a support ticket",
                    f"A support ticket titled “{values.get('title')}”, with what was found here, for the support "
                    "team.", {"form": TICKET_FORM, "title": values.get("title") or "Support ticket", "values": values,
                            "lines": ["A support ticket is opened in your name with these answers.",
                                      "The support team answers you on the ticket (Support page)."]},
                    dropped)


register(
    Tool("propose_request", "requests", "write", "Drafting the request",
         "Draft one of the Hub's self-service requests for the person to review, complete and submit: "
         + "; ".join(f"{k} = {v}" for k, v in REQUEST_FORMS.items())
         + ". values: answers by question key, only what the conversation says (never invent a project, size or "
           "repository). The questions: "
         + " || ".join(f"{k}: {_questions(k)}" for k in REQUEST_FORMS)[:2400],
         {"form": {"type": "string", "enum": list(REQUEST_FORMS)}, "values": {"type": "object"}},
         propose_request, required=["form"],
         words=["quota", "storage", "cleaner", "clean up", "new pipeline", "characterization", "request",
                "מכסה", "אחסון", "ניקוי", "פייפליין חדש", "בקשה"]),
    Tool("propose_support_ticket", "servicenow", "write", "Drafting the support ticket",
         "Draft a support ticket for the person to review, complete and submit, when their problem needs the support "
         "team (a fix only the support team can apply, or nothing else helped). title (short), description (the problem "
         "and what was already found or tried, including the past fix if one matched), work_impact (how it affects "
         "their work), urgency (low | medium | high | urgent), service (artifactory | azure devops | devops portal | "
         "everything | service now | sonarqube | other), support_type for Azure DevOps (pipelines | repository | permissions | "
         "others), collection and project for Azure DevOps (only when known from the conversation), reason (customer issue | usage/configuration issue | data importing | performance issue | bug | "
         "feature request | question | other), pipeline_url, help_wanted (what they want the support team to do).",
         {"title": {"type": "string"}, "description": {"type": "string"}, "work_impact": {"type": "string"},
          "urgency": {"type": "string"}, "service": {"type": "string"}, "pipeline_url": {"type": "string"},
          "help_wanted": {"type": "string"}, "reason": {"type": "string"}, "support_type": {"type": "string"},
          "collection": {"type": "string"}, "project": {"type": "string"}},
         propose_support_ticket, required=["title", "description"],
         words=["open a ticket", "support ticket", "new ticket", "escalate", "raise a", "פתח קריאה", "פתח טיקט"]),
)
