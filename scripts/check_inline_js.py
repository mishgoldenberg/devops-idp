#!/usr/bin/env python3
"""Catch the two ways an inline <script> block silently stops running.

A syntax error in a <script> block does not degrade — the browser discards the WHOLE
block, so every handler it was going to attach never exists. The widget still renders
its shell, still shows its loading state, and simply never does anything. There is no
error on the page and nothing in the backend logs.

That is what happened to the SonarQube Projects widget: an HTML comment inside a JS
template literal contained a backtick, which closed the literal early and left the rest
of the markup being parsed as JavaScript. The widget was dead — no connect prompt, no
token save, no search, no pinning — and it looked merely empty.

There is no JS parser here (the build agent has no node and no npm registry), so this
does not attempt to parse. It checks the two specific shapes that break template
literals, both of which are pure text problems:

  1. A backtick inside an HTML comment that sits inside a <script> block. Inside a
     template literal a backtick is not a comment character, it is a delimiter.
  2. An odd number of backticks in a block, which means a literal opens and never
     closes.

    python scripts/check_inline_js.py

Exit code 1 lists every offending block.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "frontend" / "templates"

SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def check_file(path: pathlib.Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    rel = path.relative_to(ROOT).as_posix()
    problems: list[str] = []

    for block in SCRIPT.finditer(text):
        body = block.group(1)
        block_start = block.start(1)

        # 1. Backtick inside an HTML comment inside the script.
        for comment in HTML_COMMENT.finditer(body):
            if "`" in comment.group(0):
                line = line_of(text, block_start + comment.start())
                snippet = " ".join(comment.group(0).split())[:80]
                problems.append(
                    f"{rel}:{line}  backtick inside an HTML comment in a <script> "
                    f"block — it closes the template literal.\n"
                    f"      {snippet}"
                )

        # 2. Unbalanced backticks. Escaped ones (\`) do not open or close anything.
        ticks = len(re.findall(r"(?<!\\)`", body))
        if ticks % 2:
            line = line_of(text, block_start)
            problems.append(
                f"{rel}:{line}  <script> block has an odd number of backticks "
                f"({ticks}) — a template literal is left open."
            )

    return problems


def main() -> int:
    if not TEMPLATES.exists():
        print(f"error: {TEMPLATES} not found", file=sys.stderr)
        return 1

    problems: list[str] = []
    blocks = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        blocks += len(SCRIPT.findall(path.read_text(encoding="utf-8", errors="ignore")))
        problems.extend(check_file(path))

    if not problems:
        print(f"OK - {blocks} inline <script> block(s) checked, no broken template literals")
        return 0

    print(
        f"{len(problems)} inline <script> problem(s). A syntax error discards the whole "
        f"block, so every handler in it silently never attaches:\n"
    )
    for problem in problems:
        print(f"  {problem}")
    print(
        "\nFix: remove the backticks from the comment (say 'the border-error/30 utility' "
        "instead\n     of quoting it), or move the comment outside the template literal."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
