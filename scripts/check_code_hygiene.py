#!/usr/bin/env python3
"""
Fail the build on code that only adds weight: unused imports, private helpers nothing
calls, a function copied into a second module, and comments that narrate history.

    python scripts/check_code_hygiene.py

Pure AST, like check_imports.py: nothing is imported or executed, so it runs on the
Azure DevOps agent with no application packages installed.

What it refuses, and what to do instead:

  * an import the module never uses                  -> delete it
  * a private top-level function, class or constant   -> delete it, or use it
    (``_name``) that nothing in the repository names
  * a function body that also exists, identically,    -> import the one copy
    in another module (common.py holds the shared ones)
  * a comment or docstring that cites a round number  -> describe the code as it is;
                                                         the history is in git and in
                                                         changelog.py
"""
from __future__ import annotations

import ast
import hashlib
import io
import re
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN = [REPO_ROOT / "backend" / "app", REPO_ROOT / "scripts"]

# Packages whose __init__ imports ARE their interface (re-exports, tool registration).
REEXPORTING_INIT = {"backend/app/devbot/tools/__init__.py"}
# Functions this small are allowed to coincide (a one-line property, a trivial wrapper).
MIN_DUPLICATE_NODES = 25
ROUND_REFERENCE = re.compile(r"\bRounds? \d{2,3}\b")


def _python_files():
    for root in SCAN:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _names_used(tree: ast.AST) -> set:
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A quoted annotation ("httpx.Client") or __all__ entry names a symbol too.
            used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value)[:8])
    return used


def unused_imports(path: Path, tree: ast.Module) -> list:
    if _rel(path) in REEXPORTING_INIT:
        return []
    used = _names_used(tree)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if bound not in used:
                problems.append(f"{_rel(path)}:{node.lineno}: '{alias.name}' is imported and never used")
    return problems


def private_definitions(tree: ast.Module) -> dict:
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.decorator_list:
                continue  # routes, validators and fixtures are reached through the decorator
            found[node.name] = node.lineno
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.lineno
    return {n: line for n, line in found.items() if n.startswith("_") and not n.startswith("__")}


def body_fingerprint(node) -> tuple:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]  # a docstring is not behaviour
    if not body:
        return "", 0
    module = ast.Module(body=body, type_ignores=[])
    size = sum(1 for _ in ast.walk(module))
    text = ast.dump(node.args) + ast.dump(module, annotate_fields=False)
    return hashlib.sha1(text.encode()).hexdigest(), size


def round_references(path: Path, source: str) -> list:
    problems = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return problems
    for tok in tokens:
        is_doc = tok.type == tokenize.STRING and tok.string.lstrip("rbuRBU").startswith(('"""', "'''"))
        if (tok.type == tokenize.COMMENT or is_doc) and ROUND_REFERENCE.search(tok.string):
            problems.append(f"{_rel(path)}:{tok.start[0]}: a comment cites a round number")
    return problems


def main() -> int:
    problems = []
    trees = {}
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            problems.append(f"{_rel(path)}:{exc.lineno}: does not parse: {exc.msg}")
            continue
        trees[path] = tree
        problems += unused_imports(path, tree)
        problems += round_references(path, source)

    # A private name is dead when no file in the repository mentions it outside its
    # own definition (tests count: a helper kept for a test is still used).
    corpus = defaultdict(int)
    extra = list((REPO_ROOT / "backend" / "tests").rglob("*.py")) + list((REPO_ROOT / "frontend" / "templates").rglob("*.html"))
    for path in list(trees) + extra:
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", path.read_text(encoding="utf-8", errors="replace")):
            corpus[word] += 1
    for path, tree in trees.items():
        for name, line in private_definitions(tree).items():
            if corpus[name] <= 1:
                problems.append(f"{_rel(path)}:{line}: '{name}' is defined and never used")

    copies = defaultdict(list)
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                digest, size = body_fingerprint(node)
                if size >= MIN_DUPLICATE_NODES:
                    copies[digest].append(f"{_rel(path)}:{node.lineno} {node.name}")
    for places in copies.values():
        if len({p.split(":")[0] for p in places}) > 1:
            problems.append("the same function is written out in more than one module: " + ", ".join(places))

    if problems:
        print(f"FAILED - {len(problems)} problem(s):")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"OK - {len(trees)} module(s): no unused imports or private helpers, no copied "
          "functions, no round numbers in comments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
