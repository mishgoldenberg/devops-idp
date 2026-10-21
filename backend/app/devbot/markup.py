"""
Markdown -> Confluence storage format, for pages DevBot drafts and a person confirms.

Only what a written-up fix needs: headings, paragraphs, bullet and numbered lists,
code blocks (as Confluence's own code macro, so they keep their formatting and copy
button), inline code, bold, italic and links. Everything a person typed is escaped
first; a link is only kept when it points at http(s).
"""

from __future__ import annotations

import html
import re
from typing import List

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _inline(text: str) -> str:
    slots: List[str] = []

    def keep(markup: str) -> str:
        slots.append(markup)
        return f"\u0000{len(slots) - 1}\u0000"

    out = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    out = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                 lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'), out)
    out = html.escape(out, quote=False)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(^|[^*\w])\*([^*\n]+)\*(?!\w)", r"\1<em>\2</em>", out)
    return re.sub(r"\u0000(\d+)\u0000", lambda m: slots[int(m.group(1))], out)


def _code_macro(code: str, language: str) -> str:
    # CDATA cannot contain its own terminator; split it the way XML allows.
    body = code.replace("]]>", "]]]]><![CDATA[>")
    lang = f'<ac:parameter ac:name="language">{html.escape(language)}</ac:parameter>' if language else ""
    return f'<ac:structured-macro ac:name="code">{lang}<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>'


def markdown_to_storage(markdown: str) -> str:
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            buf: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence.group(1)):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(_code_macro("\n".join(buf), fence.group(2)))
            continue
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _LIST.match(line):
            ordered = bool(re.match(r"^\s*\d", line))
            items: List[str] = []
            while i < len(lines) and _LIST.match(lines[i]):
                items.append(f"<li>{_inline(_LIST.match(lines[i]).group(3))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _FENCE.match(lines[i]) and not _LIST.match(lines[i]) \
                and not re.match(r"^#{1,6}\s", lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br />".join(_inline(p) for p in para) + "</p>")
    return "".join(out)
