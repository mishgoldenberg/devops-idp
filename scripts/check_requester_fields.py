#!/usr/bin/env python3
"""
The requester questions exist ONCE.

This guard used to diff two hand-written copies of the six requester-detail questions:
the Support wizard's markup and _WHO_YOU_ARE in catalog_forms.py. They had drifted on
all four axes at once -- free text where the wizard had a fixed list of branches, a
different field order, English-only labels, and a seventh field that existed nowhere
in the wizard.

There is no second copy any more. The Support ticket is a spec like every other
request and its first section IS _WHO_YOU_ARE, so drift is not a thing that can
happen rather than a thing that gets caught. What is checked now is that this stays
true -- that the ticket form still reuses the object instead of quietly growing its
own list again -- plus the properties the ServiceNow mapping depends on, since those
are matched by NAME and a renamed key silently stops reaching the ticket.

    python3 scripts/check_requester_fields.py
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend" / "app"))

import catalog_forms  # noqa: E402

EXPECTED_KEYS = ["full_name", "phone_number", "branch", "team", "section", "role"]


def main() -> int:
    problems: list[str] = []
    who = catalog_forms._WHO_YOU_ARE
    keys = [f["key"] for f in who["fields"]]

    if keys != EXPECTED_KEYS:
        problems.append(
            f"the requester section asks {keys}, not {EXPECTED_KEYS}. "
            "ServiceNow matches these by name; renaming one stops it reaching the ticket."
        )
    if not all(f.get("required") for f in who["fields"]):
        problems.append("every requester question is mandatory in ServiceNow; one here is not.")

    branch = [f for f in who["fields"] if f["key"] == "branch"]
    if not branch or branch[0].get("type") != "select" or not branch[0].get("options"):
        problems.append("Branch must be a fixed list. Free text there matches no ServiceNow choice.")

    for field in who["fields"]:
        if "(" not in field["label"]:
            problems.append(
                f"{field['key']} is labelled {field['label']!r} with no Hebrew. "
                "The Hebrew is how people recognise the question."
            )

    # The point of the whole exercise: one object, reused.
    for key, spec in catalog_forms.FORMS.items():
        first = (spec.get("sections") or [{}])[0]
        if first.get("key") != "requester":
            problems.append(f"{key} does not open with the requester section.")
            continue
        if [f["key"] for f in first.get("fields") or []] != EXPECTED_KEYS:
            problems.append(
                f"{key} has grown its own copy of the requester questions. "
                "Use dict(_WHO_YOU_ARE) so there is one."
            )

    if problems:
        print(f"Requester questions: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"OK - the requester section is one object, reused by "
          f"{len(catalog_forms.FORMS)} form(s) ({len(EXPECTED_KEYS)} fields, "
          f"{len(branch[0]['options'])} branches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
