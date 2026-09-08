#!/usr/bin/env python3
"""
Fail the build when a module the backend imports is not in the tree.

WHY THIS EXISTS
---------------
The backend crash-looped in the cluster because a new module was written to a
working copy but never reached the image. Every import in this app is resolved
at STARTUP — `api/__init__.py` imports every router at module scope — so one
missing file does not degrade a feature, it stops the process before uvicorn
binds a port. Kubernetes then reports CrashLoopBackOff, which says nothing
about the cause, and the only evidence is a traceback in `--previous` logs that
you have to know to ask for.

The usual way for that to happen is `git commit -am`, which stages modified
tracked files and silently skips new ones. The .docx applier writes new files
happily; git just does not pick them up.

This is a pure-AST check: no imports are executed and no dependencies are
needed, so it runs on the Azure DevOps agent, which has no application
packages installed (and no npm).

    python scripts/check_imports.py

Exit code 1 lists every unresolved import with its file and line.
"""
from __future__ import annotations

import ast
import builtins
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "backend" / "app"

# Distribution name -> module name, for the cases where they differ. Anything
# in requirements.txt is trusted; these are the ones whose import name cannot
# be derived from the requirement line.
DIST_TO_MODULE = {
    "psycopg2-binary": "psycopg2",
    "pyjwt": "jwt",
    "python-multipart": "multipart",
    "python-dotenv": "dotenv",
    "google-cloud-storage": "google",
    "uvicorn[standard]": "uvicorn",
}

# Imported by libraries we depend on, or optional extras that are present in
# the image because something else pulled them in.
EXTRA_THIRD_PARTY = {
    "pydantic_core",
    "typing_extensions",
    "anyio",
    "certifi",
    "h11",
    "httpcore",
    "idna",
    "sniffio",
    "yaml",
    "dateutil",
    "requests",
    "urllib3",
    "click",
    "pytest",
}


def third_party_modules() -> set[str]:
    names: set[str] = set(EXTRA_THIRD_PARTY)
    req = APP_ROOT / "requirements.txt"
    if not req.exists():
        return names
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # "fastapi==0.120.4" / "uvicorn[standard]==0.38.0" / "python-multipart"
        dist = line.split("==")[0].split(">=")[0].split("<")[0].strip()
        key = dist.lower()
        names.add(DIST_TO_MODULE.get(key, key.replace("-", "_")))
        # Also accept the bare name without an extras suffix.
        if "[" in key:
            base = key.split("[")[0]
            names.add(DIST_TO_MODULE.get(base, base.replace("-", "_")))
    return names


def stdlib_modules() -> set[str]:
    names = set(getattr(sys, "stdlib_module_names", ()))
    if not names:  # pragma: no cover - only on Python < 3.10
        names = {
            "abc", "argparse", "ast", "asyncio", "base64", "collections",
            "concurrent", "contextlib", "copy", "csv", "datetime", "decimal",
            "email", "enum", "functools", "hashlib", "hmac", "html", "http",
            "importlib", "io", "ipaddress", "itertools", "json", "logging",
            "math", "os", "pathlib", "queue", "random", "re", "secrets",
            "shutil", "socket", "sqlite3", "ssl", "string", "subprocess",
            "sys", "tempfile", "textwrap", "threading", "time", "traceback",
            "types", "typing", "unicodedata", "urllib", "uuid", "warnings",
            "zipfile",
        }
    return names


def local_modules() -> dict[str, Path]:
    """
    Every module importable from the app root, mapped to its file.

    The container sets PYTHONPATH=/app and copies backend/app there, so a
    top-level `import ado_identity` and a package `api.catalog` are both
    resolved against this same root.
    """
    names: dict[str, Path] = {}
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
            if not parts:
                continue
        else:
            parts[-1] = parts[-1][:-3]
        names[".".join(parts)] = path
        names.setdefault(parts[0], path)
    return names


_SYMBOLS: dict[str, "set[str] | None"] = {}


def module_symbols(module: str, path: Path) -> "set[str] | None":
    """
    The module-level names a local module actually defines.

    This is the half the module check misses. The backend crash-looped on
    `ImportError: cannot import name 'cached_external' from
    'integrations_cache'` — the FILE was there, the function in it was not,
    because the helper had been written against a newer copy than the one that
    shipped. A missing module and a missing name in a present module are the
    same delivery mistake and produce the same crash.

    Returns None when the module cannot be analysed (a star-import means names
    can come from anywhere), which switches the name check off for it rather
    than guessing.
    """
    if module in _SYMBOLS:
        return _SYMBOLS[module]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        _SYMBOLS[module] = None
        return None

    names: set[str] = set()
    star = False

    def collect(body):
        nonlocal star
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(stmt.name)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for el in target.elts:
                            if isinstance(el, ast.Name):
                                names.add(el.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    if alias.name == "*":
                        star = True
                    else:
                        names.add(alias.asname or alias.name)
            # A name defined in either branch/handler is still importable.
            elif isinstance(stmt, ast.If):
                collect(stmt.body)
                collect(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                collect(stmt.body)
                collect(stmt.orelse)
                collect(stmt.finalbody)
                for handler in stmt.handlers:
                    collect(handler.body)

    collect(tree.body)
    _SYMBOLS[module] = None if star else names
    return _SYMBOLS[module]


_CLASS_MEMBERS: dict[str, "dict[str, set[str] | None]"] = {}


def class_members(module: str, path: Path) -> "dict[str, set[str] | None]":
    """
    The names each module-level class defines in its own body.

    The third way to reference something that is not there. `import audit` resolves,
    `audit.Action` resolves, and `audit.Action.SETTINGS_UPDATED` raises AttributeError
    at the moment that line runs — which for a constant passed to a logging call is
    after the work it was logging has already been done. An Artifactory cleaner
    committed its files, opened its pull request, and was then reported FAILED,
    because the label naming the audit entry did not exist.

    A class is only enumerable when nothing outside its body can add to it, so one
    with base classes (it inherits names this cannot see) or with any control flow in
    its body (names defined conditionally) maps to None, which switches the check off
    for that class rather than guessing at it.
    """
    if module in _CLASS_MEMBERS:
        return _CLASS_MEMBERS[module]
    out: "dict[str, set[str] | None]" = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        _CLASS_MEMBERS[module] = out
        return out

    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            continue
        if stmt.bases or stmt.keywords or stmt.decorator_list:
            out[stmt.name] = None
            continue
        names: set[str] = set()
        dynamic = False
        for item in stmt.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                    else:
                        dynamic = True
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                names.add(item.target.id)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(item.name)
            elif isinstance(item, (ast.Expr, ast.Pass)):
                continue
            else:
                dynamic = True
        out[stmt.name] = None if dynamic else names

    _CLASS_MEMBERS[module] = out
    return out


def local_module_aliases(tree: ast.Module, local: dict, package: str) -> dict[str, str]:
    """Which names in this file refer to which local module."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                if target in local:
                    aliases[bound] = target
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                target = f"{base}.{node.module}" if (base and node.module) else (node.module or base)
            else:
                target = node.module or ""
            for alias in node.names:
                candidate = f"{target}.{alias.name}" if target else alias.name
                if candidate in local:
                    aliases[alias.asname or alias.name] = candidate
    return aliases


def attribute_problems(path: Path, tree: ast.Module, local: dict, package: str) -> list[str]:
    """Every `module.name` and `module.Class.NAME` in this file that does not exist."""
    aliases = local_module_aliases(tree, local, package)
    if not aliases:
        return []

    # A name rebound in this file is no longer the module: `audit = get_audit()`
    # means audit.anything is out of this check's reach.
    rebound = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For))
        for target in (node.targets if isinstance(node, ast.Assign)
                       else [getattr(node, "target", None)])
        if isinstance(target, ast.Name)
    }

    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        value = node.value

        # module.Class.NAME
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            root = value.value.id
            module = aliases.get(root)
            if not module or root in rebound:
                continue
            members = class_members(module, local[module]).get(value.attr)
            if members is None or node.attr in members:
                continue
            problems.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                f"{root}.{value.attr}.{node.attr} - {module}.{value.attr} defines no "
                f"'{node.attr}'; it raises AttributeError the moment this line runs"
            )
            continue

        # module.name
        if isinstance(value, ast.Name):
            module = aliases.get(value.id)
            if not module or value.id in rebound:
                continue
            symbols = module_symbols(module, local[module])
            if symbols is None or node.attr in symbols:
                continue
            if f"{module}.{node.attr}" in local:  # a submodule of a package
                continue
            problems.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                f"{value.id}.{node.attr} - the module {module} defines no "
                f"'{node.attr}'; it raises AttributeError the moment this line runs"
            )
    return problems


PYTHON_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__",
}


def _bound_names(node: ast.AST) -> "set[str]":
    """Every name this subtree BINDS: assignments, imports, defs, loops, excepts."""
    out: set[str] = set()

    def target(t) -> None:
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for element in t.elts:
                target(element)
        elif isinstance(t, ast.Starred):
            target(t.value)

    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(child, ast.Assign):
            for t in child.targets:
                target(t)
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            target(child.target)
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
            target(child.target)
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(child, ast.ExceptHandler):
            if child.name:
                out.add(child.name)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            out.update(child.names)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pass
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = child.args
            for arg in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                        + ([args.vararg] if args.vararg else [])
                        + ([args.kwarg] if args.kwarg else [])):
                out.add(arg.arg)
    return out


def undefined_names(path: Path, tree: ast.Module) -> "list[str]":
    """Names this module READS that nothing in it ever defines.

    VALID_PROCESS_TYPES was declared by the Terraform-era module, and the REST
    rewrite kept the line that reads it while dropping the line that sets it. The
    module imported fine, started fine and served every other route fine; it raised
    NameError only inside `provision_ado_project`, which runs when an APPROVER
    approves a project request -- after the requester has gone, in a code path no
    test and no import check reached.

    Deliberately coarse: a name is fine if it is bound ANYWHERE in the file, at any
    scope. That under-reports (a local in one function excuses a global read in
    another) and never over-reports, which is the only tolerable bias for a guard
    that fails the build. Modules with a star import are skipped -- what it brought
    in cannot be known without importing it, and nothing here imports anything.
    """
    if any(isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
           for node in ast.walk(tree)):
        return []

    bound = _bound_names(tree)
    problems: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        if node.id in bound or node.id in PYTHON_BUILTINS or node.id in seen:
            continue
        seen.add(node.id)
        problems.append(
            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
            f"{node.id} - nothing in this module defines it; it raises NameError "
            f"the moment this line runs"
        )
    return problems


def module_name_of(path: Path) -> str:
    rel = path.relative_to(APP_ROOT)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def startup_imports(tree: ast.Module):
    """
    Yield only the imports that run when the module is first imported.

    That is the whole point of the check: an import inside a function fails when
    that function is called, which is a normal error on a normal request. An
    import at module scope fails during startup, before the process can serve
    anything, and Kubernetes reports it as a crash loop.

    Two module-level forms are deliberately skipped:
      * inside `try:` — optional by construction (secrets_manager imports hvac
        that way), and the surrounding code already handles its absence;
      * inside `if TYPE_CHECKING:` — never executed at runtime at all.
    """
    def is_type_checking(test: ast.expr) -> bool:
        return (
            (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        )

    def walk_body(body):
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                yield stmt
            elif isinstance(stmt, ast.If) and not is_type_checking(stmt.test):
                yield from walk_body(stmt.body)
                yield from walk_body(stmt.orelse)
            # ast.Try is skipped entirely: an optional import.

    yield from walk_body(tree.body)


TEMPLATE_ROOT = REPO_ROOT / "frontend" / "templates"
_INCLUDE_RE = None


def check_template_includes() -> list[str]:
    """
    Same failure, one layer up: a template that includes a file which is not in
    the tree. It does not crash the pod — it 500s the page that includes it,
    which is arguably worse because only that one page breaks and only for
    whoever opens it.
    """
    global _INCLUDE_RE
    import re

    if _INCLUDE_RE is None:
        _INCLUDE_RE = re.compile(
            r"{%-?\s*(?:include|extends|import|from)\s+[\"']([^\"']+)[\"']"
        )
    problems: list[str] = []
    if not TEMPLATE_ROOT.is_dir():
        return problems
    for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _INCLUDE_RE.finditer(text):
            target = match.group(1)
            if not (TEMPLATE_ROOT / target).exists():
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: includes '{target}' "
                    f"- no such template; is the file missing from the commit?"
                )
    return problems


def check_template_syntax() -> list[str]:
    """
    Every template must PARSE. Reading it for includes, as the check above does, is
    not the same thing and never was: a file can be scanned line by line and still
    be unparseable.

    This exists because of a one-line change that shipped. A Jinja comment was put
    inside a `{% set %}` expression -- comments cannot go there -- and the template
    it lives in is the breadcrumb strip, which the banner includes on EVERY page. So
    a note nobody needed took down the whole portal, and everything that could have
    caught it was looking elsewhere: the import check resolved the include (it
    exists), the CSS check found its classes (they are compiled), and the tests read
    the file as text (it contained what they searched for). Nothing compiled it.

    Parsing is also the only check here that needs no context, no request and no
    database, so it costs nothing and covers every template rather than the handful
    a render happens to exercise.
    """
    problems: list[str] = []
    if not TEMPLATE_ROOT.is_dir():
        return problems
    try:
        from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
    except ImportError:
        # Jinja2 is a runtime dependency of the backend, so this only happens on a
        # machine that has not installed them. Skipping is right: a guard that
        # cannot run must not fail a build for its own absence.
        print("  (jinja2 not installed - template syntax not checked)", file=sys.stderr)
        return problems

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_ROOT)))
    for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            env.parse(text, filename=str(path))
        except TemplateSyntaxError as exc:
            problems.append(
                f"{path.relative_to(REPO_ROOT)}:{exc.lineno}: template syntax error: "
                f"{exc.message} - every page including this file would 500"
            )
    return problems


def check() -> int:
    if not APP_ROOT.is_dir():
        print(f"ERROR - {APP_ROOT} does not exist", file=sys.stderr)
        return 1

    local = local_modules()
    known = stdlib_modules() | third_party_modules()
    problems: list[str] = []
    checked = 0

    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(REPO_ROOT)}:{exc.lineno}: syntax error: {exc.msg}")
            continue
        checked += 1
        here = module_name_of(path)
        # For a package's __init__.py the module IS the package, so `from . import x`
        # resolves inside it — not inside its parent.
        if path.name == "__init__.py":
            package = here
        else:
            package = here.rsplit(".", 1)[0] if "." in here else ""

        for node in startup_imports(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in known or root in local or alias.name in local:
                        continue
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"import {alias.name} - no such module in backend/app, "
                        f"not stdlib, not in requirements.txt"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: from . import x / from .y import z
                    base = package
                    for _ in range(node.level - 1):
                        base = base.rsplit(".", 1)[0] if "." in base else ""
                    if node.module:
                        # `from .mod import name`: name may be an attribute rather
                        # than a module, so it is `.mod` itself that must exist.
                        target = f"{base}.{node.module}" if base else node.module
                        if target not in local:
                            problems.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f"from {'.' * node.level}{node.module} import ... "
                                f"- unresolved; is the file missing from the commit?"
                            )
                        continue
                    # `from . import x, y` — here every name IS a module, which is
                    # how api/__init__.py imports all 26 routers. Checking only the
                    # package would pass while a router file was missing.
                    for alias in node.names:
                        candidate = f"{base}.{alias.name}" if base else alias.name
                        if candidate in local:
                            continue
                        problems.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                            f"from {'.' * node.level} import {alias.name} "
                            f"- unresolved; is the file missing from the commit?"
                        )
                else:
                    root = (node.module or "").split(".")[0]
                    if not root or root in known or root in local or node.module in local:
                        continue
                    problems.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"from {node.module} import ... - no such module in "
                        f"backend/app, not stdlib, not in requirements.txt"
                    )

        # ── Names, not just modules ──────────────────────────────────────
        # Checked over EVERY import in the file, not only module-scope ones.
        # A local module is never an optional dependency, and a lazy
        # `from integrations_cache import cached_external` inside a function
        # fails at request time instead of startup — quieter, not better. That
        # is exactly how the caching in azure_devops and servicenow could be
        # dead in the cluster without anyone seeing an error.
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                target = f"{base}.{node.module}" if (base and node.module) else (node.module or base)
            else:
                target = node.module or ""
            if target not in local or target == here:
                continue
            symbols = module_symbols(target, local[target])
            if symbols is None:
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name in symbols:
                    continue
                # `from api import catalog` — a submodule, not a name in the file.
                if f"{target}.{alias.name}" in local:
                    continue
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                    f"from {target} import {alias.name} - the module exists but "
                    f"defines no '{alias.name}'; is {target} an older copy than "
                    f"the code importing it?"
                )

        problems.extend(attribute_problems(path, tree, local, package))
        problems.extend(undefined_names(path, tree))

    problems.extend(check_template_includes())
    problems.extend(check_template_syntax())

    if problems:
        print("FAIL - unresolved imports:\n", file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        print(
            "\nAn import resolved at startup that is not satisfied does not "
            "degrade a feature -\nit stops the backend before it binds a port "
            "(CrashLoopBackOff).\n"
            "\n  missing module  -> the file is not in the commit: `git add -A` "
            "(git commit -am skips NEW files)\n"
            "  missing name    -> the file shipped is OLDER than the code "
            "importing it; ship that\n                     module too, along with "
            "anything IT imports names from.\n"
            "  missing attribute -> a constant or helper that was never defined. This "
            "one does not\n                     crash at startup: it raises where it "
            "is USED, which can be after\n                     the work it was "
            "labelling has already happened.\n"
            "  missing name    -> a BARE global nothing defines. Usually a definition "
            "deleted in a\n                     rewrite while the line reading it "
            "survived; same failure, and it\n                     lands in whichever "
            "function happens to run first.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK - {checked} module(s), every template include resolves, every template parses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
