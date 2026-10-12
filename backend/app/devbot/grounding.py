"""
Before DevBot answers: what the Hub already knows about the question.

Every question is looked up in the search index first (knowledge.py): the Confluence
pages that mean the same thing -- only those this person can open, checked with their
own token -- and the past fixes of closed bugs and support tickets, in their reviewed
form. What matches goes to the model with the question, marked as what the Hub knows,
and the model answers from it and cites it; when nothing matches, it answers as it
would have anyway. Without this, a person had to ask "has this happened before?" in so
many words, or ask about a failed run, before any of it was looked at.

Costs the person nothing against their own key's per-minute limits beyond the few
hundred tokens of what was found: the search itself is one embedding request on the
Hub's key. Skipped for very short messages ("thanks", "and the tests?") and when the
index holds nothing, and never allowed to hold the question up: it is bounded, and a
failure is only a line in the steps.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

MIN_CHARS = 15
PAGES = 3
FIXES = 3


def gather(ctx: Any, question: str) -> Optional[Dict[str, Any]]:
    """What the index holds about ``question`` that this person may see, or None when
    there is nothing to search (the index is empty, or the message is too short)."""
    from . import knowledge
    from .tools import confluence, investigate

    text = " ".join(str(question or "").split())
    if len(text) < MIN_CHARS or not knowledge.ready():
        return None
    out: Dict[str, Any] = {"pages": [], "fixes": []}
    if ctx.systems.get("confluence") and knowledge.ready("confluence"):
        for p in confluence.meaning_search(ctx, text, limit=PAGES):
            out["pages"].append({"title": p.get("title"), "url": p.get("url"), "space": p.get("space"),
                                 "updated": p.get("updated"), "match": f"{p.get('score')}%",
                                 "passage": str(p.get("passage") or "")[:600]})
    if knowledge.ready("ado") or knowledge.ready("snow"):
        out["fixes"] = investigate.past_fixes(ctx, text, limit=FIXES)
    return out


def summary(found: Dict[str, Any]) -> str:
    pages, fixes = len(found.get("pages") or []), len(found.get("fixes") or [])
    if not pages and not fixes:
        return "Nothing in the Hub matched"
    parts = []
    if pages:
        parts.append(f"{pages} page{'s' if pages != 1 else ''}")
    if fixes:
        parts.append(f"{fixes} past fix{'es' if fixes != 1 else ''}")
    return "Found " + " and ".join(parts)


def for_prompt(found: Dict[str, Any]) -> str:
    """The part of the system prompt that carries what was found, or "" for nothing."""
    from .tools.investigate import SUPPORT_NOTE

    pages, fixes = found.get("pages") or [], found.get("fixes") or []
    if not pages and not fixes:
        return ""
    lines: List[str] = [
        "WHAT THE HUB ALREADY KNOWS that may be about this question (found by meaning, so check it really fits "
        "before using it). When it fits, answer from it first: give the fix or the steps, and cite each one you use "
        "(a page by its link; a past fix by where it was recorded). When none of it fits, ignore it and answer as "
        "usual. You may still look things up with the tools.",
    ]
    for p in pages:
        lines.append(f"- Confluence page [{p['title']}]({p['url']}) (space {p.get('space')}, {p.get('match')} match): "
                     + " ".join(str(p.get("passage") or "").split()))
    for f in fixes:
        where = f.get("recorded_in") or "a past fix"
        lines.append(f"- Past fix recorded in {where}{' (' + f['url'] + ')' if f.get('url') else ''}, {f.get('match')} "
                     f"match: problem: {f.get('problem')} Fix: {f.get('fix')}"
                     + (" [who can apply it: the support team only]" if f.get("who_can_apply") else ""))
    if any(f.get("who_can_apply") for f in fixes):
        lines.append(SUPPORT_NOTE)
    return "\n".join(lines)


def links(found: Dict[str, Any]) -> List[Dict[str, str]]:
    out = [{"title": str(p.get("title") or ""), "url": str(p.get("url") or ""), "system": "confluence"}
           for p in found.get("pages") or [] if p.get("url")]
    out += [{"title": f"{f.get('recorded_in')}: {f.get('problem')}"[:160], "url": f["url"], "system": "azure"}
            for f in found.get("fixes") or [] if f.get("url")]
    return out
