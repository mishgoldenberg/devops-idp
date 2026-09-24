#!/usr/bin/env python3
"""Regenerate the parts of the documentation that are facts about the code.

    python scripts/gen_docs.py            # rewrite the generated sections
    python scripts/gen_docs.py --check    # fail if they are out of date

WHY
---
docs/API_REFERENCE.md described 25 endpoints of the 190 the app actually serves,
and every path in it was missing the `/api` prefix — it had been hand-maintained
since before the routers were mounted under a prefix. A reference that is wrong is
worse than no reference: somebody trusts it, writes a client against `/health`,
and finds out in the cluster.

An inventory of endpoints, environment variables and tables is not prose. It is a
fact about the code, so it is derived from the code:

  * docs/API_REFERENCE.md  — regenerated whole from the app's OpenAPI schema.
  * docs/env.md            — the block between the GENERATED markers.
  * docs/database.md       — the block between the GENERATED markers.

Everything outside the markers is hand-written and is never touched, because the
part that explains WHY a variable exists cannot be derived from anything.

WHERE IT RUNS
-------------
Locally, like the CSS build: importing the app needs the backend's dependencies,
and the Azure DevOps agent has none of them. The output is committed. `--check`
is safe to run anywhere the deps exist and is what you use before a release to
find out whether somebody added an endpoint without regenerating.
"""

from __future__ import annotations

import argparse
import ast
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "backend" / "app"

BEGIN = "<!-- GENERATED:{name} — do not edit by hand; run scripts/gen_docs.py -->"
END = "<!-- /GENERATED:{name} -->"


# ── The app ───────────────────────────────────────────────────────────────────


def load_openapi() -> dict:
    sys.path.insert(0, str(APP))
    # Enough environment to import the module tree. config.py logs about the rest
    # and carries on, which is what we want: this is a schema dump, not a boot.
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/db")
    os.environ.setdefault("JWT_SECRET", "x" * 40)
    os.environ.setdefault("REDIS_HOST", "127.0.0.1")
    from main import app  # noqa: E402  (deliberately late)

    # fastapi >= 0.139 defers include_router into lazy objects, so app.routes is
    # not the route table until something forces resolution. openapi() does.
    return app.openapi()


def api_reference(schema: dict) -> str:
    paths = schema.get("paths", {})
    by_tag: dict = {}
    for path, operations in sorted(paths.items()):
        for method, op in operations.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            tag = (op.get("tags") or ["untagged"])[0]
            summary = (op.get("summary") or "").strip()
            if not summary:
                # FastAPI derives a summary from the function name; if it did not,
                # fall back to the first sentence of the docstring it turned into
                # a description.
                summary = (op.get("description") or "").strip().split("\n")[0][:120]
            by_tag.setdefault(tag, []).append((method.upper(), path, summary))

    total = sum(len(v) for v in by_tag.values())
    out = [
        "# API Reference",
        "",
        BEGIN.format(name="API"),
        "",
        f"{total} endpoints across {len(by_tag)} groups, generated from the running "
        "application's OpenAPI schema.",
        "",
        "Everything under `/api/…` returns JSON. Everything under `/ui/…` returns HTML "
        "fragments for HTMX and is not part of this reference — those are not an API, "
        "they are the pages.",
        "",
        "Authentication is the `auth_token` cookie (HS256 JWT) on every route except "
        "`/api/health/*` and the sign-in routes. Admin-only routes are marked in their "
        "own handlers; a non-admin gets 403, never a filtered result.",
        "",
    ]
    for tag in sorted(by_tag):
        out.append(f"## {tag}")
        out.append("")
        out.append("| Method | Path | What it does |")
        out.append("| --- | --- | --- |")
        for method, path, summary in sorted(by_tag[tag], key=lambda r: (r[1], r[0])):
            out.append(f"| `{method}` | `{path}` | {summary or '—'} |")
        out.append("")
    out.append(END.format(name="API"))
    out.append("")
    return "\n".join(out)


# ── Environment variables ─────────────────────────────────────────────────────


def env_inventory() -> str:
    """Every os.getenv in the backend, with its literal default where there is one."""
    found: dict = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr in ("getenv", "get"):
                value = getattr(func, "value", None)
                is_environ = (
                    isinstance(value, ast.Name) and value.id == "os"
                ) or (
                    isinstance(value, ast.Attribute) and value.attr == "environ"
                )
                if is_environ and node.args and isinstance(node.args[0], ast.Constant):
                    name = node.args[0].value
            if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                continue
            default = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                default = "" if node.args[1].value is None else str(node.args[1].value)
            entry = found.setdefault(name, {"default": default, "files": set()})
            entry["files"].add(rel)
            if default and not entry["default"]:
                entry["default"] = default

    out = [
        BEGIN.format(name="ENV"),
        "",
        f"{len(found)} variables the backend actually reads, found by parsing every "
        "`os.getenv` in `backend/app/`. A variable that is not here is not read by "
        "anything, whatever the deployment sets.",
        "",
        "| Variable | Default | Read by |",
        "| --- | --- | --- |",
    ]
    for name in sorted(found):
        entry = found[name]
        default = f"`{entry['default']}`" if entry["default"] else "_(none)_"
        # Secrets: never print a default even if one exists in the source. TOKENS,
        # plural, is a count (DEVBOT_MAX_PROMPT_TOKENS), not a credential.
        if re.search(r"SECRET|PASSWORD|TOKEN(?!S)|PAT\b", name):
            default = "_(secret — must be set)_"
        files = ", ".join(f"`{f.split('/')[-1]}`" for f in sorted(entry["files"])[:3])
        out.append(f"| `{name}` | {default} | {files} |")
    out += ["", END.format(name="ENV"), ""]
    return "\n".join(out)


# ── Tables ────────────────────────────────────────────────────────────────────


def table_inventory() -> str:
    """Every CREATE TABLE the startup DDL can execute."""
    # The trailing "(" matters: db.py contains the sentence "…the CREATE TABLE IF
    # NOT EXISTS below — which made that guard meaningless", and without it this
    # inventory grew a table called `below`. A DDL statement is always followed by
    # its column list.
    pattern = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)\s*\(", re.I
    )
    found: dict = {}
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            found.setdefault(match.group(1), set()).add(
                path.relative_to(REPO).as_posix()
            )

    out = [
        BEGIN.format(name="TABLES"),
        "",
        f"{len(found)} tables, created idempotently at startup by `ensure_tables()` "
        "and friends. There is no migration Job in the cluster — this DDL *is* the "
        "schema mechanism, and it must stay additive: a `DROP` on this path runs on "
        "every pod start.",
        "",
        "| Table | Created in |",
        "| --- | --- |",
    ]
    for name in sorted(found):
        files = ", ".join(f"`{f}`" for f in sorted(found[name]))
        out.append(f"| `{name}` | {files} |")
    out += ["", END.format(name="TABLES"), ""]
    return "\n".join(out)


# ── Writing ───────────────────────────────────────────────────────────────────


def replace_block(text: str, name: str, body: str) -> str:
    begin, end = BEGIN.format(name=name), END.format(name=name)
    if begin in text and end in text:
        head = text.split(begin)[0]
        tail = text.split(end, 1)[1]
        return head + body.rstrip("\n") + tail
    # First run: append the block rather than guessing where it belongs.
    return text.rstrip("\n") + "\n\n" + body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if the docs are stale")
    args = ap.parse_args()

    targets = {}
    schema = load_openapi()
    targets[REPO / "docs" / "API_REFERENCE.md"] = api_reference(schema)

    env_doc = REPO / "docs" / "env.md"
    targets[env_doc] = replace_block(
        env_doc.read_text(encoding="utf-8"), "ENV", env_inventory()
    )

    db_doc = REPO / "docs" / "database.md"
    targets[db_doc] = replace_block(
        db_doc.read_text(encoding="utf-8"), "TABLES", table_inventory()
    )

    stale = []
    for path, content in targets.items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == content:
            print(f"    unchanged  {path.relative_to(REPO)}")
            continue
        stale.append(path)
        if args.check:
            print(f"    STALE      {path.relative_to(REPO)}")
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"    written    {path.relative_to(REPO)}")

    if args.check and stale:
        print(
            "\nThe generated documentation does not match the code. "
            "Run: python scripts/gen_docs.py"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
