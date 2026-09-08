#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Package a change round as a .docx for transfer into an offline environment.

    python scripts/gen_round_docx.py

Produces DevOpsHub_Round<N>_<date>.docx in the repo root, in the same shape as
the earlier rounds: prose explaining the change, an excerpt of each file, and a
machine-readable payload that scripts/apply_docx.py reads to write the files.

The payload is the part that matters — base64-encoded JSON with a SHA-256 per
file, because base64 is the only encoding that survives Word, copy/paste, smart
quotes and line-ending conversion intact.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

REPO = Path(__file__).resolve().parent.parent

# Assembled rather than spelled out, for the same reason apply_docx.py does it:
# the literal marker must appear exactly once in the finished document.
_TAG = "##DEVOPSHUB-PAYLOAD-V1-"
BEGIN = _TAG + "BEGIN##"
END = _TAG + "END##"

APPLY_SHADE = "FFF4CE"
CODE_SHADE = "F4F5F7"
PAYLOAD_SHADE = "EEF2F7"


# ── docx helpers ───────────────────────────────────────────────────────────

def shade(cell, fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    el = OxmlElement("w:shd")
    el.set(qn("w:val"), "clear")
    el.set(qn("w:fill"), fill)
    tcPr.append(el)


def mono_box(doc, text: str, fill: str = CODE_SHADE, size: float = 7.0) -> None:
    """A single-cell table holding preformatted text, like the earlier rounds."""
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    cell = table.rows[0].cells[0]
    shade(cell, fill)

    para = cell.paragraphs[0]
    para.paragraph_format.space_after = Pt(0)
    para.paragraph_format.line_spacing = 1.0
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            para.add_run().add_break()
        run = para.add_run(line)
        run.font.name = "Consolas"
        run.font.size = Pt(size)
        rPr = run._r.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        rFonts.set(qn("w:ascii"), "Consolas")
        rFonts.set(qn("w:hAnsi"), "Consolas")
    doc.add_paragraph()


def payload_box(doc, blob: str, per_line: int = 110, per_para: int = 40) -> None:
    """The payload, as many ordinary paragraphs rather than one huge one.

    NOT mono_box: that writes everything into a single paragraph joined by manual
    line breaks, and at payload size that is one paragraph of several thousand
    runs which Word has to lay out as a single unbreakable unit. Documents built
    that way open slowly, hang, or do not survive the transfer.

    Splitting is safe because of what the READER does, not what Word does:
    apply_docx.py collects every <w:t> in the document, joins them with newlines,
    and strips every whitespace character between the two markers before
    decoding. Base64 has no whitespace of its own, so any arrangement of
    paragraphs, breaks or table cells recovers the identical bytes. Do not
    "tidy" this back into one box.
    """
    lines = [blob[i:i + per_line] for i in range(0, len(blob), per_line)]
    chunks = [lines[i:i + per_para] for i in range(0, len(lines), per_para)]

    for index, chunk in enumerate([[BEGIN]] + chunks + [[END]]):
        para = doc.add_paragraph()
        fmt = para.paragraph_format
        fmt.space_before = Pt(0)
        fmt.space_after = Pt(0)
        fmt.line_spacing = 1.0
        # Every paragraph is its own page-break candidate; without this Word still
        # tries to keep each one whole and we are back to the same problem, just
        # forty lines at a time.
        fmt.keep_together = False
        fmt.keep_with_next = False
        for i, line in enumerate(chunk):
            if i:
                para.add_run().add_break()
            run = para.add_run(line)
            run.font.name = "Consolas"
            run.font.size = Pt(5.5)
            rPr = run._r.get_or_add_rPr()
            rFonts = rPr.find(qn("w:rFonts"))
            if rFonts is None:
                rFonts = OxmlElement("w:rFonts")
                rPr.insert(0, rFonts)
            rFonts.set(qn("w:ascii"), "Consolas")
            rFonts.set(qn("w:hAnsi"), "Consolas")


def body(doc, text: str, bold: bool = False) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold


# -- the round ---------------------------------------------------------------

ROUND = "116"

SUMMARY = ("An approved Azure DevOps project request creates the project again: the "
           "constant naming the four valid processes was lost in the move off Terraform "
           "while the line reading it survived, and the build now refuses any name "
           "nothing defines")

DELETES = []

FILES = [
    "backend/app/api/azure_devops.py",
    "backend/app/changelog.py",
    "scripts/check_imports.py",
    "CLAUDE.md",
    "scripts/gen_round_docx.py",
]

PREAMBLE = ""


def build() -> Path:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)

    doc.add_paragraph("DevOps Hub - Round " + ROUND, style="Title")
    body(doc, SUMMARY + ".")

    if PREAMBLE.strip():
        mono_box(doc, PREAMBLE.rstrip(), fill=APPLY_SHADE, size=9.0)

    mono_box(doc,
             "APPLY THIS AUTOMATICALLY. Put this file in the repo root and run:\n"
             "\n"
             "    python scripts/apply_docx.py\n"
             "\n"
             "It writes every file in the payload, verifies each against its\n"
             "SHA-256, runs the repository checks, then commits and pushes.",
             fill=APPLY_SHADE, size=8.5)

    doc.add_paragraph("Files", style="Heading 1")
    for path in FILES:
        doc.add_paragraph(path, style="List Bullet")
    for path in DELETES:
        doc.add_paragraph(path + "  (DELETED)", style="List Bullet")

    entries = []
    for path in FILES:
        raw = (REPO / path).read_bytes()
        entries.append({
            "path": path,
            "b64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    payload = {"round": ROUND, "summary": SUMMARY, "files": entries, "deletes": DELETES}
    blob = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    doc.add_paragraph("Payload - do not edit", style="Heading 1")
    payload_box(doc, blob)

    # Rounds are produced from more than one session, so the next free number
    # cannot be inferred from this one. Ask the directory.
    taken = sorted(REPO.glob("DevOpsHub_Round" + ROUND + "_*.docx"))
    if taken:
        raise SystemExit(
            "Round " + ROUND + " already exists: "
            + ", ".join(t.name for t in taken)
            + "\nPick the next free number."
        )
    out = REPO / ("DevOpsHub_Round" + ROUND + "_" + date.today().isoformat() + ".docx")
    doc.save(out)
    return out


if __name__ == "__main__":
    path = build()
    print("wrote " + str(path.relative_to(REPO)) + "  (" + format(path.stat().st_size, ",") + " bytes)")
