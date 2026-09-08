#!/usr/bin/env python3
"""
Check backend/app/changelog.py, and print the version the images should be tagged with.

Two jobs, one file, because they read the same thing and disagreeing about it is the
failure worth preventing: CI must not tag an image with a version the changelog does
not actually contain.

    python3 scripts/check_changelog.py            # check; non-zero if anything is wrong
    python3 scripts/check_changelog.py --version  # print the newest version, nothing else

The check itself lives in changelog.check() so the tests and CI run the same code.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend" / "app"))

import changelog  # noqa: E402


def main() -> int:
    if "--version" in sys.argv:
        # Bare, on one line, with nothing else on stdout: this is read straight into
        # a shell variable and used as a Docker tag.
        print(changelog.version())
        return 0

    problems = changelog.check()
    if problems:
        print(f"Changelog: {len(problems)} problem(s) in backend/app/changelog.py\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nEvery release needs a version like 1.2.3, a date, a headline, and at "
            "least one line\nwritten for somebody using the portal rather than "
            "somebody reading the diff."
        )
        return 1

    # all_releases(), not releases(): the page collapses generic-only entries into
    # one card, and "how many releases are there" is a question about the file.
    releases = changelog.all_releases()
    cards = changelog.releases()
    print(f"Changelog: {len(releases)} release(s), newest {changelog.version()} "
          f"({releases[0]['date']}); {len(cards)} card(s) on the page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
