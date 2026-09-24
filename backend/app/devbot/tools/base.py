"""
The tool framework: what a tool is, the context it runs in, and running one safely.

A tool never raises to the orchestrator. Whatever goes wrong -- a system not connected,
a token refused, a host unreachable, a bug -- comes back as ``{"ok": False, "error": ...}``
in words the model can pass on and a person can act on, because a question that dies
on one failed lookup loses the answers the other lookups found.

Results are cut to size HERE, not by each tool: the model's context window and the
person's tokens-per-minute are both small, and one careless list of 500 repositories
would spend them all.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

import httpx
from fastapi import HTTPException

from resilient_http import explain_integration_failure, tls_verify
from security import AuthUser

log = logging.getLogger(__name__)

SYSTEM_LABELS = {
    "azure": "Azure DevOps",
    "sonarqube": "SonarQube",
    "artifactory": "Artifactory",
    "confluence": "Confluence",
}


class NotConnected(Exception):
    def __init__(self, system: str) -> None:
        label = SYSTEM_LABELS.get(system, system)
        super().__init__(f"{label} is not connected for this person. They can connect it on the Connections page.")
        self.system = system


class ToolFailure(Exception):
    """A tool's own, already-worded refusal: bad arguments, nothing found."""


@dataclass
class Tool:
    name: str
    system: str
    kind: str  # read | investigate | write
    label: str  # the progress line, e.g. "Reading the pipeline log"
    description: str
    params: Dict[str, Dict[str, Any]]
    fn: Callable[["ToolContext", Dict[str, Any]], Dict[str, Any]]
    required: List[str] = field(default_factory=list)
    max_chars: int = 3200
    # Systems besides its own that an investigation reads; it is offered only when its
    # own system is connected, and it reads the others if they are.
    also: List[str] = field(default_factory=list)

    def spec(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required},
            },
        }


REGISTRY: Dict[str, Tool] = {}


def register(*tools: Tool) -> None:
    for tool in tools:
        REGISTRY[tool.name] = tool


# ── the context a tool runs in ───────────────────────────────────────────────

class ToolContext:
    """One person, for one question: their tokens, and what has been looked up so far.

    Lookups repeated inside one question -- the list of Azure DevOps collections, the
    projects in them -- are answered once and remembered until the question is done.
    """

    def __init__(self, user: AuthUser, systems: Optional[Dict[str, bool]] = None,
                 progress: Optional[Callable[[str, str], None]] = None) -> None:
        self.user = user
        self.user_id = str(user.get("id") or "")
        self.systems = dict(systems or {})
        self._memo: Dict[str, Any] = {}
        self._progress = progress

    def child(self, progress: Callable[[str, str], None]) -> "ToolContext":
        """The same person and the same remembered lookups, reporting to its own step.

        Tools run side by side, and each one's "now reading X" belongs under its own
        step on the page, not under whichever step happened to be shown last.
        """
        other = ToolContext.__new__(ToolContext)
        other.__dict__.update(self.__dict__)
        other._progress = progress
        return other

    def progress(self, system: str, text: str) -> None:
        """Tell the person what this tool is doing right now. Never fails the tool."""
        if self._progress:
            try:
                self._progress(system, text)
            except Exception:
                pass

    def memo(self, key: str, produce: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = produce()
        return self._memo[key]

    # tokens
    def token(self, system: str) -> str:
        from api.integrations import _get_token

        value = self.memo(f"token:{system}", lambda: _get_token(self.user_id, system) or "")
        if not value:
            raise NotConnected(system)
        return value

    def optional_token(self, system: str) -> str:
        try:
            return self.token(system)
        except NotConnected:
            return ""

    def base(self, system: str) -> str:
        from api.integrations import _base_url

        try:
            return _base_url(system)
        except HTTPException:
            raise ToolFailure(f"{SYSTEM_LABELS.get(system, system)} is not configured on this Hub.")

    # Azure DevOps
    def ado_pat(self) -> str:
        from api.azure_devops import _get_pat_for_user

        def produce() -> str:
            try:
                return _get_pat_for_user(self.user)
            except HTTPException:
                return ""

        pat = self.memo("ado:pat", produce)
        if not pat:
            raise NotConnected("azure")
        return pat

    def ado_client(self, read: float = 20.0) -> httpx.Client:
        return httpx.Client(
            verify=tls_verify(),
            auth=httpx.BasicAuth("", self.ado_pat()),
            timeout=httpx.Timeout(read, connect=5.0),
        )

    def ado_bases(self, client: httpx.Client) -> List[str]:
        from api.azure_devops import _discover_ado_bases

        return self.memo("ado:bases", lambda: [b for b in _discover_ado_bases(client) if b])

    def ado_bases_for(self, client: httpx.Client, collection: str = "") -> List[str]:
        """The collection asked for, or every collection when none was named."""
        from api.azure_devops import _collection_name

        bases = self.ado_bases(client)
        wanted = (collection or "").strip().lower()
        if not wanted:
            return bases
        chosen = [b for b in bases if _collection_name(b).lower() == wanted]
        if not chosen:
            names = ", ".join(_collection_name(b) for b in bases)
            raise ToolFailure(f"There is no collection called '{collection}'. The collections are: {names}.")
        return chosen


# ── running a tool ───────────────────────────────────────────────────────────

def _coerce(tool: Tool, args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, schema in tool.params.items():
        if name not in args or args[name] is None or args[name] == "":
            continue
        value = args[name]
        kind = schema.get("type")
        try:
            if kind == "integer":
                value = int(str(value).strip().lstrip("#"))
            elif kind == "boolean":
                value = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
            elif kind == "string":
                value = str(value).strip()
            elif kind == "array" and isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
        except ValueError:
            raise ToolFailure(f"'{name}' should be a {kind}, not {value!r}.")
        if schema.get("enum") and value not in schema["enum"]:
            raise ToolFailure(f"'{name}' must be one of {', '.join(map(str, schema['enum']))}.")
        out[name] = value
    missing = [r for r in tool.required if r not in out]
    if missing:
        raise ToolFailure("Missing " + ", ".join(missing) + ".")
    return out


def run(ctx: ToolContext, name: str, raw_args: Any) -> Dict[str, Any]:
    """Run one tool. Always returns a dict; never raises."""
    started = time.monotonic()
    tool = REGISTRY.get(name)
    label = SYSTEM_LABELS.get(tool.system, tool.system) if tool else "DevBot"
    result: Dict[str, Any]
    try:
        if tool is None:
            raise ToolFailure(f"There is no tool called {name}.")
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args or "{}")
            except ValueError:
                raise ToolFailure("The arguments were not valid JSON.")
        if not isinstance(raw_args, dict):
            raise ToolFailure("The arguments must be an object.")
        result = tool.fn(ctx, _coerce(tool, raw_args)) or {}
        result.setdefault("ok", True)
    except NotConnected as exc:
        result = {"ok": False, "error": str(exc), "not_connected": exc.system}
    except ToolFailure as exc:
        result = {"ok": False, "error": str(exc)}
    except HTTPException as exc:
        result = {"ok": False, "error": str(exc.detail)}
    except httpx.HTTPError as exc:
        log.warning("devbot tool %s failed: %s: %s", name, type(exc).__name__, exc)
        result = {"ok": False, "error": explain_integration_failure(label, exc)}
    except Exception as exc:  # a bug in a tool must not end the question
        log.exception("devbot tool %s crashed", name)
        result = {"ok": False, "error": f"The {label} lookup failed unexpectedly ({type(exc).__name__})."}
    result["ms"] = int((time.monotonic() - started) * 1000)
    if not result.get("summary"):
        result["summary"] = result.get("error", "Done.") if not result.get("ok") else "Done."
    return result


def for_model(result: Dict[str, Any], max_chars: int) -> str:
    """What the model is handed: the data or the error, cut to ``max_chars``."""
    body: Dict[str, Any] = {"ok": bool(result.get("ok"))}
    if result.get("ok"):
        body["data"] = result.get("data")
        if result.get("note"):
            body["note"] = result["note"]
    else:
        body["error"] = result.get("error") or "Failed."
    return compact(body, max_chars)


def compact(value: Any, max_chars: int) -> str:
    """JSON no longer than ``max_chars``: long strings and long lists shrink first,
    so what survives is the start of everything rather than all of the first thing."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text
    for str_cap, list_cap in ((600, 25), (300, 15), (160, 10), (80, 6), (40, 4)):
        shrunk = _shrink(value, str_cap, list_cap)
        text = json.dumps(shrunk, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= max_chars:
            return text
    return text[: max_chars - 20] + '..."[cut]"'


def _shrink(value: Any, str_cap: int, list_cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= str_cap else value[:str_cap] + "..."
    if isinstance(value, list):
        items = [_shrink(v, str_cap, list_cap) for v in value[:list_cap]]
        if len(value) > list_cap:
            items.append(f"... and {len(value) - list_cap} more")
        return items
    if isinstance(value, dict):
        return {k: _shrink(v, str_cap, list_cap) for k, v in value.items() if v not in (None, "", [], {})}
    return value


# ── which tools to offer ─────────────────────────────────────────────────────

_WORDS: Dict[str, Iterable[str]] = {
    "azure": (
        "azure", "devops", "ado", "pipeline", "build", "run ", "runs", "release", "deploy", "work item",
        "workitem", "bug", "task", "pbi", "backlog", "feature", "epic", "sprint", "iteration", "pull request",
        "pr ", "prs", "repo", "branch", "commit", "test", "collection", "review",
        # Not "project": every system has projects, so it says nothing about which one.
        "פייפליין", "פייפ", "בילד", "ריצה", "ריצות", "באג", "משימ", "ספרינט", "פול ריקווסט", "ריפו", "בדיק",
        "טסט", "ענף", "קומיט", "דיפלוי", "ריוויו",
    ),
    "sonarqube": (
        "sonar", "quality", "gate", "coverage", "smell", "vulnerab", "hotspot", "debt", "duplicat", "code analysis",
        "סונאר", "איכות", "כיסוי", "פגיע", "חוב טכני",
    ),
    "artifactory": (
        "artifactory", "jfrog", "artifact", "docker", "image", "tag", "package", "npm", "maven", "nuget", "pypi",
        "helm", "storage", "binary", "version of",
        "ארטיפקטורי", "ארטיפקט", "אימג", "דוקר", "חבילה", "תג", "גרסה",
    ),
    "confluence": (
        "confluence", "wiki", "page", "doc", "runbook", "how to", "how do", "guide", "solution", "known issue",
        "קונפלואנס", "ויקי", "דף", "תיעוד", "מדריך", "איך", "פתרון",
    ),
}
_INVESTIGATE = (
    "why", "error", "fail", "broken", "crash", "exception", "problem", "issue", "wrong", "fix", "solution",
    "status", "ready", "health", "going on", "happen", "stuck", "latest", "last ",
    "למה", "שגיא", "נכשל", "כשל", "תקל", "בעי", "פתרון", "שבור", "מצב", "סטטוס", "מוכן", "אחרון", "קרה",
)
_WRITE = (
    "create", "open a", "open new", "add ", "comment", "assign", "move ", "rerun", "re-run", "run again", "retry",
    "trigger", "approve", "reject", "vote", "update", "write", "document ", "log a bug", "file a bug",
    "צור", "פתח", "הוסף", "תגובה", "הגב", "שייך", "העבר", "הרץ", "אשר", "דחה", "עדכן", "כתוב", "תעד",
)
MAX_OFFERED = 12


def _hits(text: str, words: Iterable[str]) -> bool:
    return any(w in text for w in words)


def specs_for(question: str, systems: Dict[str, bool], recent: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The tools worth offering for this question, as the model's tool schemas.

    ``recent`` names the systems the previous answer used, so a follow-up ("and the
    tests?") keeps the tools it needs without naming the system again.
    """
    text = " " + re.sub(r"\s+", " ", (question or "").lower()) + " "
    usable = {s for s, on in systems.items() if on}
    mentioned = {s for s, words in _WORDS.items() if _hits(text, words)} | set(recent)
    wants_write = _hits(text, _WRITE)
    investigate = _hits(text, _INVESTIGATE) or bool(mentioned)

    chosen: List[Tool] = []
    for tool in REGISTRY.values():
        if tool.system not in usable:
            continue
        if tool.kind == "read" and tool.system in mentioned:
            chosen.append(tool)
        # By its OWN system, or for a problem that names no system at all. Not by the
        # systems it merely reads on the way: a question about SonarQube does not need
        # the Azure DevOps investigations that happen to check a quality gate.
        elif tool.kind == "investigate" and investigate and (tool.system in mentioned or not mentioned):
            chosen.append(tool)
        elif tool.kind == "write" and wants_write and tool.system in mentioned:
            chosen.append(tool)

    if not chosen:
        # Nothing named a system: offer the broad starting points, one per system.
        starters = ("investigate_pipeline_failure", "investigate_work_item", "ado_work_items",
                    "ado_pipeline_runs", "conf_search", "sonar_projects", "art_search")
        chosen = [REGISTRY[n] for n in starters if n in REGISTRY and REGISTRY[n].system in usable]

    order = {"investigate": 0, "read": 1, "write": 2}
    chosen.sort(key=lambda t: order.get(t.kind, 3))
    return [t.spec() for t in chosen[:MAX_OFFERED]]


def system_of(name: str) -> str:
    tool = REGISTRY.get(name)
    return tool.system if tool else ""


def label_of(name: str) -> str:
    tool = REGISTRY.get(name)
    return tool.label if tool else name


def max_chars_of(name: str) -> int:
    tool = REGISTRY.get(name)
    return tool.max_chars if tool else 2000
