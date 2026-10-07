#!/usr/bin/env python3
"""
Fail the build on code that reopens a class of hole the Hub has closed everywhere:

    python scripts/check_security_rules.py

Pure AST and text, like check_code_hygiene.py: nothing is imported or executed.

What it refuses, and what to do instead:

  * an httpx client built outside resilient_http.py  -> resilient_http.Client / AsyncClient,
    (or a module-level httpx.get/post/...)               which refuse a path that climbs
                                                         out of itself ("/x/../../y")
  * an f-string that puts an exception's class or    -> common.failure_text(system, exc) or
    text, or a response's raw .text, anywhere but a      resilient_http.far_message(resp);
    log call                                             log the details instead
  * a ServiceNow sysparm_query f-string with a value  -> snow_catalog.query_value(value),
    not passed through query_value                       since "^" adds conditions
  * a password, secret or token written as a value    -> a variable from the pipeline's
    in code, a script, the chart or the pipeline         variable group
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = REPO_ROOT / "backend" / "app"
CLIENT_MODULE = APP / "resilient_http.py"

HTTPX_CALLS = {"Client", "AsyncClient", "get", "post", "put", "patch", "delete", "request", "stream"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
# The Logs page row (main._remember) is read by admins only and is where the reason belongs.
DIAGNOSTIC_SINKS = {"_remember"}
# Names a response is held in; a form field called "text" is the person's own words.
RESPONSE_NAME = re.compile(r"^(r|rc|pr|push|res|resp|response|reply|\w+_resp|\w+_response)$")
SECRET_NAME = re.compile(r"(password|passwd|secret|token|api_?key)$", re.IGNORECASE)
CONFIG_SECRET = re.compile(
    r"^\s*(?:export\s+|-\s+)?[\"']?([A-Z][A-Z0-9_]*(?:PASSWORD|PASS|SECRET|TOKEN|API_KEY))[\"']?\s*[:=]\s*"
    r"[\"']?([^\s\"'#]+)")
CONFIG_FILES = ["setup.sh", "azure-pipelines.yml", "docker-compose.yml", "deployment/**/*.yaml",
                "deployment/**/*.yml", "scripts/*.sh", "backend/**/*.sh"]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _app_files():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" not in path.parts and "tests" not in path.parts:
            yield path


def direct_clients(path: Path, tree: ast.AST) -> list:
    if path == CLIENT_MODULE:
        return []
    return [f"{_rel(path)}:{node.lineno}: httpx.{node.func.attr}() -- use resilient_http.Client"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"
            and node.func.attr in HTTPX_CALLS]


def _is_exempt_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in LOG_METHODS or name in DIAGNOSTIC_SINKS


def _leaks(value: ast.AST, caught: set) -> str:
    # The exception itself, or str() of it; its attributes (a status code) say nothing internal.
    bare = value.args[0] if (isinstance(value, ast.Call) and getattr(value.func, "id", "") == "str"
                             and value.args) else value
    if isinstance(bare, ast.Name) and bare.id in caught:
        return f"the text of the caught exception '{bare.id}'"
    for node in ast.walk(value):
        if (isinstance(node, ast.Attribute) and node.attr == "__name__" and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "type"):
            return "an exception's class name"
        if (isinstance(node, ast.Attribute) and node.attr == "text" and isinstance(node.value, ast.Name)
                and RESPONSE_NAME.match(node.value.id)):
            return "a response's raw body"
    return ""


def _own_classes() -> set:
    """Exception classes the Hub defines: their messages are the Hub's own wording."""
    names = {"HTTPException"}
    for path in _app_files():
        names |= {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                  if isinstance(n, ast.ClassDef)}
    return names


def _caught_types(handler: ast.ExceptHandler) -> set:
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {k.attr if isinstance(k, ast.Attribute) else getattr(k, "id", "") for k in kinds}


def leaked_errors(path: Path, tree: ast.AST, own: set) -> list:
    problems = []

    def visit(node: ast.AST, caught: set, exempt: bool) -> None:
        if isinstance(node, ast.ExceptHandler) and node.name and not (node.type and _caught_types(node) <= own):
            caught = caught | {node.name}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            caught = set()
        if isinstance(node, ast.Call) and _is_exempt_call(node):
            exempt = True
        if isinstance(node, ast.JoinedStr) and not exempt:
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    what = _leaks(part.value, caught)
                    if what:
                        problems.append(f"{_rel(path)}:{node.lineno}: an f-string carries {what} -- "
                                        "use common.failure_text or resilient_http.far_message")
                        break
            return
        for child in ast.iter_child_nodes(node):
            visit(child, caught, exempt)

    visit(tree, set(), False)
    return problems


def unescaped_snow_queries(path: Path, tree: ast.AST) -> list:
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "sysparm_query"):
                continue
            strings = [value] if isinstance(value, ast.JoinedStr) else [
                v for v in ast.walk(value) if isinstance(v, ast.JoinedStr)]
            for joined in strings:
                for part in joined.values:
                    call = part.value if isinstance(part, ast.FormattedValue) else None
                    if call is None:
                        continue
                    func = getattr(call, "func", None)
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name != "query_value":
                        problems.append(f"{_rel(path)}:{joined.lineno}: a sysparm_query value is not passed "
                                        "through snow_catalog.query_value")
    return problems


def secret_literals(path: Path, tree: ast.AST) -> list:
    problems = []
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Assign):
            pairs = [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.keyword) and node.arg:
            pairs = [(node.arg, node.value)]
        for name, value in pairs:
            if (SECRET_NAME.search(name) and isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and len(value.value) >= 6 and not value.value.startswith(("$", "{{"))):
                problems.append(f"{_rel(path)}:{node.lineno}: '{name}' is set to a literal value")
    return problems


def config_secrets() -> list:
    problems = []
    seen = set()
    for pattern in CONFIG_FILES:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                match = CONFIG_SECRET.match(line)
                if not match:
                    continue
                value = match.group(2)
                if value.startswith(("$", "{{", "(", "<")) or value.lower() in ("true", "false", "null", "|", ">"):
                    continue
                problems.append(f"{_rel(path)}:{number}: {match.group(1)} is set to a literal value")
    return problems


def main() -> int:
    problems = []
    count = 0
    own = _own_classes()
    for path in _app_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count += 1
        problems += direct_clients(path, tree)
        problems += leaked_errors(path, tree, own)
        problems += unescaped_snow_queries(path, tree)
        problems += secret_literals(path, tree)
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        problems += secret_literals(path, ast.parse(path.read_text(encoding="utf-8")))
    problems += config_secrets()

    if problems:
        print(f"FAILED - {len(problems)} problem(s):")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"OK - {count} module(s): every outbound call through the guarded client, no exception text "
          "or raw body in a message, every ServiceNow query value checked, no secret written as a value")
    return 0


if __name__ == "__main__":
    sys.exit(main())
