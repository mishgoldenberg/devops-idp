"""The system prompt. Short on purpose: it is sent with every request, and every
token of it is paid for against the person's per-minute allowance."""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable

SYSTEM_LABELS = {
    "azure": "Azure DevOps",
    "sonarqube": "SonarQube",
    "artifactory": "Artifactory",
    "confluence": "Confluence",
}


def system_prompt(name: str, systems: Dict[str, bool], offered: Iterable[str], today: date, *,
                  model_can_call_tools: bool = True) -> str:
    """``offered`` names the tools sent with this question; the prompt only talks about
    the kinds that are really there, so the model is never pointed at a missing tool."""
    offered = set(offered)
    tools_on = bool(offered)
    connected = [label for key, label in SYSTEM_LABELS.items() if systems.get(key)]
    missing = [label for key, label in SYSTEM_LABELS.items() if not systems.get(key)]
    lines = [
        f"You are DevBot, the assistant built into DevOps Hub. You are talking to {name}. Today is {today.isoformat()}.",
        "You help people with Azure DevOps, SonarQube, Artifactory and Confluence.",
    ]
    if tools_on:
        lines += [
            "Use the tools for anything about live systems: projects, pipeline runs, work items, pull requests, code "
            "quality, artifacts and pages. Never invent names, IDs, numbers, statuses or links.",
            "Call several tools at once when they do not depend on each other.",
        ]
        if any(n.startswith("investigate_") for n in offered):
            lines.append("Prefer an investigate_* tool for a question that needs several systems (why a pipeline or "
                         "a test failed, what is going on with a work item or a pull request); it gathers everything in "
                         "one step. When it found a Confluence page with a fix, quote the fix and link the page.")
        lines.append("When a tool fails, say plainly what failed and what the person can do about it.")
        if any(n.startswith("propose_") for n in offered):
            lines.append("You cannot change anything yourself. To change something, call a propose_* tool: the "
                         "person sees exactly what will happen and confirms it themselves. Never say it is done.")
        else:
            lines.append("You cannot change anything; say so if asked to.")
    elif not model_can_call_tools:
        lines += [
            "You have NO access to live data with this model. If asked about projects, runs, work items, pages or "
            "anything else in those systems, say you cannot see them with this model and suggest picking a model "
            "marked 'Live data'. Never guess such details.",
        ]
    else:
        lines += [
            "You have no access to live data in this conversation. If asked about projects, runs, work items, "
            "pages or anything else in those systems, say you cannot see them. Never guess such details.",
        ]
    lines += [
        "Be concise: short paragraphs, lists or small tables. Link to what you mention when a tool gave you its URL.",
        "Answer in the language the question is written in.",
    ]
    if missing:
        lines.append(
            "Not connected for this person: " + ", ".join(missing)
            + ". If asked about those, tell them to connect it on the Connections page."
        )
    if connected and tools_on:
        lines.append("Connected: " + ", ".join(connected) + ".")
    return "\n".join(lines)
