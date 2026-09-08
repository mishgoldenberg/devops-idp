#!/usr/bin/env python3
"""Fail the build when a template uses a Tailwind class that was never compiled.

frontend/static/css/output.css is committed and shipped as-is — nothing rebuilds it at
deploy time. So a Tailwind/DaisyUI class that is not already in that file silently does
nothing: no error, no warning, the style just never applies. The template looks correct
and the page is wrong, which has cost real debugging time more than once.

This checks the templates against the compiled CSS directly, so it needs no npm, no node
and no network — it runs anywhere Python does, including an offline build agent.

    python scripts/check_css_classes.py

Exit code 1 lists every class that is used but not compiled. There are two ways to fix a
finding, and which one is right depends on where the class came from:

  * The class is a real Tailwind utility that was simply never compiled (because
    output.css is stale) — rebuild it on a machine with npm:
        cd frontend && npm run build:css     (then commit static/css/output.css)

  * The build cannot be rebuilt here — swap the class for one that IS compiled, or move
    the rule into an inline style. Arbitrary-value classes (grid-cols-[auto,1fr],
    h-[calc(...)]) are the usual offenders and are almost always better inline.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "frontend" / "static" / "css" / "output.css"
TEMPLATES = ROOT / "frontend" / "templates"

# Characters Tailwind backslash-escapes when it emits a selector: `.w-1\/2`, `.md\:flex`,
# `.h-\[calc\(100\%\+0\.25rem\)\]`. To look a class up in the compiled CSS we have to
# rebuild that escaping rather than match the class name literally.
ESCAPED = set("/:.[]%!#(),' +*")

# Only audit tokens that actually look like Tailwind utilities. Everything else in a
# class attribute is either a DaisyUI component (btn, card, section-card — those live in
# output.css too, but a miss there is not the bug we are hunting) or a JS/CSS hook.
# The leading `-?` matters: negative utilities like `-left-[14px]` are classes too, and
# an earlier version of this check missed a dead one by forgetting them.
UTILITY = re.compile(
    r"^-?(?:(?:sm|md|lg|xl|2xl|hover|focus|focus-within|focus-visible|active|group-hover"
    r"|disabled|dark|first|last|odd|even|peer-[\w-]+):)*"
    r"(?:bg|text|border|ring|fill|stroke|from|via|to|w|h|min-w|min-h|max-w|max-h"
    r"|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|space|flex|grid|grid-cols|grid-rows"
    r"|col|row|items|justify|self|place|rounded|shadow|opacity|z|top|bottom|left|right"
    r"|inset|translate|rotate|scale|transition|duration|ease|delay|font|leading|tracking"
    r"|truncate|whitespace|break|list|cursor|select|overflow|overscroll|object|order"
    r"|basis|grow|shrink|divide|outline|underline|uppercase|lowercase|capitalize"
    r"|antialiased|aspect|columns|align|indent|decoration|backdrop|blur|animate"
    r"|appearance|pointer|resize|snap)(?:-|$)"
)


# DaisyUI COMPONENT modifiers. These were excluded from the check on the theory that a
# miss here "is not the bug we are hunting" — but it is exactly the same bug, and it had
# already happened three times unnoticed: `menu-compact` (a DaisyUI 2 name that v3
# renamed to `menu-sm`, so the sidebar was never compact), `checkbox-sm` and `kbd-sm`
# (never compiled, so both rendered full size next to `-sm` controls).
#
# Only modifiers are checked, not base components: `btn`, `card`, `menu` are certainly
# in output.css, while `btn-brand` is a hand-written class in a page's own <style>.
# Which is why inline <style> blocks count as "compiled" below.
COMPONENT = re.compile(
    r"^(?:btn|badge|alert|card|input|select|textarea|toggle|tab|tabs|menu|modal|table"
    r"|link|checkbox|radio|range|steps|step|join|drawer|navbar|footer|stat|avatar"
    r"|progress|loading|tooltip|dropdown|collapse|indicator|mask|kbd|chat|timeline"
    r"|carousel|divider|breadcrumbs|swap|artboard|file-input|rating)-[a-z0-9-]+$"
)


def inline_style_text() -> str:
    """Every <style> block in every template, concatenated.

    A class written by hand in a page's own <style> is compiled for our purposes — that
    is the documented way to add styling here, since output.css is never rebuilt. Without
    this the check would flag exactly the workaround it tells you to use.
    """
    out = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        out.extend(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
    return "\n".join(out)


def compiled_classes_lookup(css: str):
    def is_compiled(cls: str) -> bool:
        selector = "." + "".join(
            (r"\\" + re.escape(ch)) if ch in ESCAPED else re.escape(ch) for ch in cls
        )
        return re.search(r"(?<![\w-])" + selector + r"(?![\w-])", css) is not None

    return is_compiled


def collect_classes():
    """Every class token used in a template, mapped to the files that use it.

    This deliberately reads the raw text rather than parsing HTML: a lot of these classes
    live inside JS template literals that build markup at runtime (`class="badge ..."`
    inside a backtick string), and those break in the browser exactly like the ones in
    static markup do. Tokens containing `{`, `}` or `$` are skipped — they are Jinja or
    JS interpolation, not a class name we can resolve statically.
    """
    used: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'class\s*=\s*["\']([^"\']+)["\']', text):
            for token in match.group(1).split():
                if any(ch in token for ch in "{}$"):
                    continue
                used.setdefault(token, set()).add(
                    path.relative_to(ROOT).as_posix()
                )
    return used


def unbalanced_style_comments():
    """
    <style> blocks whose /* */ comments do not pair up.

    An unterminated comment does not throw — CSS error recovery swallows everything
    until it finds something it can parse again, which means the RULE AFTER the comment
    silently stops applying. That is exactly how the sidebar's row padding disappeared:
    a comment edit left a stray `*/`, the following `.portal-nav-link { … }` was eaten
    as part of a bogus selector, and the only symptom was "the spacing looks like it
    did before" — a styling change that reverts itself with no error anywhere.

    Counting delimiters catches both halves of that mistake: a missing close and a
    stray extra close.
    """
    problems = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S):
            css = match.group(1)
            opens, closes = css.count("/*"), css.count("*/")
            if opens != closes:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{path.relative_to(ROOT)}:{line}: <style> block has {opens} '/*' "
                    f"and {closes} '*/' — the rule after the unbalanced comment is "
                    f"silently dropped"
                )
    return problems


def main() -> int:
    if not CSS.exists():
        print(f"error: {CSS.relative_to(ROOT)} not found", file=sys.stderr)
        return 1

    comment_problems = unbalanced_style_comments()
    if comment_problems:
        print("Unbalanced CSS comments:\n", file=sys.stderr)
        for line in comment_problems:
            print("  " + line, file=sys.stderr)
        return 1

    is_compiled = compiled_classes_lookup(
        CSS.read_text(encoding="utf-8", errors="ignore") + "\n" + inline_style_text()
    )
    used = collect_classes()
    missing = {
        cls: files
        for cls, files in used.items()
        if (UTILITY.match(cls) or COMPONENT.match(cls)) and not is_compiled(cls)
    }

    if not missing:
        print(f"OK - {len(used)} classes used, all compiled into output.css")
        return 0

    print(
        f"{len(missing)} class(es) are used in a template but are NOT in the compiled "
        f"output.css or any page's own <style>. They do nothing in the browser:\n"
    )
    for cls, files in sorted(missing.items()):
        print(f"  {cls}")
        for file in sorted(files):
            print(f"      {file}")
    print(
        "\nFix: rebuild the CSS (cd frontend && npm run build:css) and commit "
        "static/css/output.css,\n     or replace the class with one that is already "
        "compiled / an inline style."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
