#!/usr/bin/env python3
"""
Write backend/app/build_info.json for the image about to be built.

Three facts the running pod cannot work out for itself: the branch it was built from,
the pipeline build that made it, and the commit at the tip. The branch is the one
that matters -- it is what tells the backend whether it is test or production, and
therefore which version line to record the deployment against.

This replaced a much larger script that also baked several hundred commit subjects
into the image, back when the changelog was derived from them. It is not: the
changelog is written by hand in backend/app/changelog.py. Commits are not a changelog.

Pure Python and one `git rev-parse` -- the pipeline agent has no npm and no network.

    python3 scripts/gen_build_info.py

Never fails a build. Every value it cannot determine is written as an empty string,
and an empty branch simply means the backend falls back to calling itself "test",
which is the safe direction: production announcing a version it does not have is
worse than test recording one twice.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "backend" / "app" / "build_info.json"


def macro(name: str) -> str:
    """An Azure DevOps variable, treating an unexpanded $(NAME) macro as unset.

    A variable an environment never defines arrives literally as "$(Build.BuildId)",
    and writing that into the image produces a build number that is a piece of YAML
    syntax. Unset is the honest answer.
    """
    value = (os.getenv(name) or "").strip()
    return "" if (value.startswith("$(") and value.endswith(")")) else value


def git(*args: str) -> str:
    """Run git in the repo. Never raises."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(REPO),
            capture_output=True,
            # Never let text=True pick the encoding: the agent's locale is not UTF-8
            # and a commit subject with one accented character kills the whole step.
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            print(f"  git {' '.join(args)} -> exit {result.returncode}", file=sys.stderr)
            return ""
        return (result.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - a build must not fail over a build stamp
        print(f"  git {' '.join(args)} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ""


def branch_name() -> str:
    """The branch, from the pipeline's own ref if it has one.

    Build.SourceBranch is 'refs/heads/dev' on a branch build and 'refs/pull/12/merge'
    on a pull request. Only the first is a branch; a PR build is not deployed, so
    naming it after the merge ref would be wrong in the one case it matters.
    """
    ref = macro("BUILD_SOURCEBRANCH") or macro("Build_SourceBranch")
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    if ref:
        return ""
    head = git("rev-parse", "--abbrev-ref", "HEAD")
    return "" if head in ("HEAD", "") else head


def main() -> int:
    info = {
        "branch": branch_name(),
        "build_id": macro("BUILD_BUILDID") or macro("Build_BuildId"),
        "commit_sha": (git("rev-parse", "HEAD") or "")[:40],
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(
        f"build info: branch={info['branch'] or '(none)'} "
        f"build={info['build_id'] or '(none)'} commit={info['commit_sha'][:8] or '(none)'}"
    )
    print(f"  -> {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
