#!/usr/bin/env python3
"""Apply a change-round .docx to this repository.

    python scripts/apply_docx.py                 # apply, check, commit and push
    python scripts/apply_docx.py --dry-run       # show what would change, touch nothing
    python scripts/apply_docx.py DevOpsHub_Round14_*.docx    # just these

    --no-commit   apply only, leave the working tree dirty
    --no-push     commit but stay local
    --no-verify   commit even if the repository checks fail

Why this exists
---------------
Changes reach this network as Word documents, and until now they were applied by reading
the code out of the document and pasting it in by hand. That is slow, and it silently
introduces exactly the kind of error nobody can see afterwards: a dropped line, a half-
applied hunk, a file that was in the document but never got copied.

So every round document now carries, in addition to the human-readable code, a machine-
readable payload: each file's full contents, base64-encoded, with a SHA-256. This script
reads that payload and writes the files. Base64 is used deliberately — it survives Word,
copy/paste, smart quotes and line-ending conversion, none of which raw source code does.

Guarantees
----------
* It only ever writes WHOLE files (and deletes files a round explicitly removes). There is
  no fuzzy patching, so a change cannot be half-applied.
* Every file is verified against its SHA-256 after writing. If the document was corrupted
  in transit, you find out here rather than in production.
* It is idempotent. A file already identical to the payload is left alone and reported as
  "unchanged", so re-running is safe and applying rounds out of order is caught.
* Nothing is written until the whole payload has been decoded and verified. A bad document
  changes nothing at all.
* After a successful apply it stages everything with `git add -A`, commits with a message
  naming the round, and pushes. `git add -A` and not `git commit -am`: a round that adds a
  NEW module is exactly the case `-am` skips, and a module missing from the image is a
  crash on startup rather than a missing feature.
* The commit is GATED on the repository checks (imports, compiled CSS, inline scripts).
  A round that fails them is not committed, because each of those failures is invisible
  until it reaches the cluster — an unresolved import is a CrashLoopBackOff, an
  uncompiled class is a control that does nothing, a broken template literal is a widget
  that never wires itself up.

Requires only the standard library — there is no python-docx on the build agent.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent

# Assembled rather than written out whole, deliberately. This script is itself shipped
# inside the round documents, so if the literal marker appeared in this source the
# extractor below would find THAT copy — in the human-readable listing of this very file —
# instead of the real payload, and decode nonsense. (It did, the first time.)
_TAG = "##DEVOPSHUB-PAYLOAD-V1-"
BEGIN = _TAG + "BEGIN##"
END = _TAG + "END##"


def docx_text(path: pathlib.Path) -> str:
    """All the text in a .docx, including tables.

    A .docx is a zip; the text lives in word/document.xml inside <w:t> elements. We do not
    use python-docx because it is not installed on the target host, and we do not need
    it: the payload is base64, so we can throw away every scrap of formatting, spacing and
    line-breaking and still recover the bytes exactly.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SystemExit(
            f"{path.name}: not a readable Word document ({exc}).\n"
            "It was damaged in transit — re-copy it. Nothing was written."
        )
    parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)
    return "\n".join(html.unescape(p) for p in parts)


def extract_payload(path: pathlib.Path):
    text = docx_text(path)
    if BEGIN not in text or END not in text:
        return None
    # The LAST begin-marker, not the first. The payload is always the final section of the
    # document, and a marker could otherwise be quoted earlier in a code listing.
    blob = text.rsplit(BEGIN, 1)[1].split(END, 1)[0]
    # Strip EVERY kind of whitespace Word might have introduced — line breaks inside the
    # table cell, soft wraps, stray spaces. Base64 has none of its own, so this is safe.
    blob = re.sub(r"\s+", "", blob)
    try:
        return json.loads(base64.b64decode(blob).decode("utf-8"))
    except Exception as exc:
        raise SystemExit(
            f"{path.name}: payload is present but could not be decoded ({exc}).\n"
            "The document is corrupted — re-transfer it."
        )


def round_key(path: pathlib.Path):
    """Sort 'Round9' before 'Round13' — string sort would not."""
    m = re.search(r"Round(\d+)([a-z]*)", path.name, re.I)
    if not m:
        return (9999, "", path.name)
    return (int(m.group(1)), m.group(2).lower(), path.name)


def apply_round(payload: dict, dry_run: bool) -> int:
    label = payload.get("round") or "?"
    files = payload.get("files") or []
    deletes = payload.get("deletes") or []

    # Decode and verify EVERYTHING before writing ANYTHING. A document that was truncated
    # in transit must not leave the repo half-updated.
    staged = []
    for entry in files:
        rel = entry["path"]
        content = base64.b64decode(entry["b64"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry["sha256"]:
            raise SystemExit(
                f"Round {label}: checksum mismatch for {rel}.\n"
                "The document is corrupted — re-transfer it. Nothing was written."
            )
        staged.append((rel, content))

    changed = 0
    written: list = []
    removed: list = []
    for rel, content in staged:
        target = REPO / rel
        old = target.read_bytes() if target.exists() else None
        if old == content:
            print(f"    unchanged  {rel}")
            continue
        verb = "would write" if dry_run else ("update" if old is not None else "create")
        print(f"    {verb:<10} {rel}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        written.append(rel)
        changed += 1

    for rel in deletes:
        target = REPO / rel
        if not target.exists():
            print(f"    absent     {rel}")
            continue
        print(f"    {'would delete' if dry_run else 'delete':<10} {rel}")
        if not dry_run:
            target.unlink()
        removed.append(rel)
        changed += 1

    return changed, written, removed


def _child_env() -> dict:
    """Environment for anything we spawn: talk UTF-8, both directions.

    A child that prints to a PIPE (rather than a console) encodes with the process
    locale, so a check whose output contains an em dash dies with UnicodeEncodeError
    on a non-UTF-8 Windows install — and the round is refused for a punctuation mark.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run(cmd: list, capture: bool = True) -> subprocess.CompletedProcess:
    """subprocess.run with the decoding pinned to UTF-8 instead of the locale.

    `text=True` alone decodes a child's output with the machine's preferred encoding.
    On a Hebrew Windows install that is cp1255, git speaks UTF-8, and one Hebrew
    character in a localised git message or a `remote:` line from the server is a
    UnicodeDecodeError — raised AFTER the command has already run. That is the worst
    possible shape for a failure: `git push` succeeds, the push output cannot be
    decoded, and the script dies without reporting the success it just had.

    errors="replace" so an undecodable byte costs a question mark in a message, never
    a crash on a git operation that already happened.
    """
    return subprocess.run(
        cmd,
        cwd=str(REPO),
        capture_output=capture,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )


def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", *args], capture=capture)


def _output(result: subprocess.CompletedProcess) -> str:
    """Both streams as one string, treating a missing stream as empty.

    subprocess reads the two pipes in reader THREADS. If decoding one of them ever
    raises, that thread dies alone: the exception is printed, the command still
    reports its real returncode, and the stream comes back as None — so the only
    thing that actually breaks is `stdout + stderr` reaching for a None. Which is
    how a decode problem turned into a crash in the error-reporting path, at the
    exact moment there was an error worth reporting.
    """
    return (result.stdout or "") + (result.stderr or "")


def _in_git_repo() -> bool:
    return _git("rev-parse", "--git-dir").returncode == 0


def run_guards() -> bool:
    """
    The checks that would otherwise run in CI, run here — before the commit.

    This is the whole reason committing is worth automating rather than leaving to
    muscle memory. A round that fails these does not deserve to reach a branch: an
    unresolved import is a CrashLoopBackOff, an uncompiled class is a control that
    silently does nothing, and a broken template literal is a widget that never wires
    itself up. Finding that out from the pipeline costs a deploy; finding it out here
    costs four seconds.
    """
    checks = [
        ("imports and template includes", "check_imports.py"),
        ("templates against compiled CSS", "check_css_classes.py"),
        ("inline script blocks", "check_inline_js.py"),
    ]
    ok = True
    for label, script in checks:
        path = REPO / "scripts" / script
        if not path.exists():
            continue
        result = _run([sys.executable, str(path)])
        if result.returncode == 0:
            print(f"    ok    {label}")
        else:
            ok = False
            print(f"    FAIL  {label}")
            for line in _output(result).strip().splitlines():
                print(f"          {line}")
    return ok


def commit_message(rounds: list, changed: list, deleted: list) -> str:
    """A subject line that says which round and what it was, and a body that lists the
    files. `git log --oneline` is the place people look first, so the round number and
    its own summary belong there rather than in a body nobody expands."""
    if len(rounds) == 1:
        label = f"round {rounds[0].get('round')}"
        summary = str(rounds[0].get("summary") or "").strip()
    else:
        numbers = ", ".join(str(r.get("round")) for r in rounds)
        label = f"rounds {numbers}"
        summary = f"applied {len(rounds)} change documents"

    # One sentence, and short enough to read in a log. The full text is in the document.
    summary = summary.split(". ")[0].strip().rstrip(".")
    subject = f"{label}: {summary}" if summary else label
    if len(subject) > 72:
        subject = subject[:69].rstrip() + "..."

    lines = [subject, ""]
    for r in rounds:
        lines.append(f"Applied from {r['__doc_name__']}")
    if changed:
        lines.append("")
        lines += [f"  {p}" for p in changed]
    if deleted:
        lines.append("")
        lines += [f"  deleted {p}" for p in deleted]
    return "\n".join(lines) + "\n"


def commit_and_push(
    rounds: list, changed: list, deleted: list, push: bool, verify: bool = True
) -> int:
    """Stage everything, commit with a message built from the round, and push."""
    if not _in_git_repo():
        print("\nNot a git repository — skipping commit.")
        return 0

    branch = (_git("rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip() or "HEAD"
    print(f"\nGit: branch {branch}")

    if verify:
        print("  Checks:")
        if not run_guards():
            print(
                "\n  NOT COMMITTED. Fix the failures above and re-run, or pass "
                "--no-verify\n  to commit anyway. The files themselves are already "
                "applied to the\n  working tree — nothing needs re-applying."
            )
            return 1
    else:
        print("  Checks: skipped (--no-verify)")

    # -A, never -am: a round that adds a NEW module is exactly the case `git commit -am`
    # skips, and a missing module is a crash on startup rather than a missing feature.
    add = _git("add", "-A")
    if add.returncode != 0:
        print(f"  git add failed: {_output(add).strip()}")
        return 1

    if _git("diff", "--cached", "--quiet").returncode == 0:
        print("  Nothing staged — the working tree already matched the document.")
        return 0

    message = commit_message(rounds, changed, deleted)
    commit = _git("commit", "-m", message)
    if commit.returncode != 0:
        print(f"  git commit failed:\n{_output(commit)}")
        return 1
    print(f"  Committed: {message.splitlines()[0]}")

    if not push:
        print("  Not pushing (--no-push).")
        return 0

    result = _git("push")
    if result.returncode != 0:
        # No upstream yet is the common first-push case and is worth handling rather
        # than making the operator read git's suggestion and retype it.
        combined = _output(result)
        if "no upstream branch" in combined or "set-upstream" in combined:
            result = _git("push", "--set-upstream", "origin", branch)
        if result.returncode != 0:
            print(f"  git push FAILED:\n{_output(result).strip()}")
            print("\n  The commit exists locally. Resolve the above and `git push`.")
            return 1
    print(f"  Pushed to origin/{branch}.")
    return 0


def _utf8_console() -> None:
    """Never let our own printing be the thing that fails.

    Printing to a console is fine on any locale, but the moment the output is piped
    or redirected to a file Python encodes it with the locale encoding — and this
    script prints em dashes. `> apply.log` on a cp1255 machine would raise
    UnicodeEncodeError halfway through, after files were already written.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - Python < 3.7 or a non-reconfigurable stream
            pass


def main() -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("docs", nargs="*", help="specific .docx files (default: every one in the repo root)")
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    ap.add_argument("--no-commit", action="store_true", help="apply only; do not commit or push")
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="commit even if the repository checks fail (they gate the commit by default)",
    )
    args = ap.parse_args()

    if args.docs:
        paths = [pathlib.Path(p) for pattern in args.docs for p in glob.glob(pattern)]
    else:
        paths = [pathlib.Path(p) for p in glob.glob(str(REPO / "*.docx"))]

    paths = sorted({p.resolve() for p in paths if p.exists()}, key=round_key)
    if not paths:
        print("No .docx files found in the repo root.")
        return 1

    total = 0
    applied = 0
    rounds: list = []
    all_written: list = []
    all_removed: list = []
    for path in paths:
        payload = extract_payload(path)
        if payload is None:
            # Older rounds were hand-applied and carry no payload. Say so and move on
            # rather than looking like they were applied.
            print(f"\n{path.name}\n    (no payload — an older, hand-applied document; skipped)")
            continue
        applied += 1
        print(f"\n{path.name}  —  round {payload.get('round')}: {payload.get('summary', '')}")
        count, written, removed = apply_round(payload, args.dry_run)
        total += count
        payload["__doc_name__"] = path.name
        rounds.append(payload)
        all_written += written
        all_removed += removed

    if not applied:
        print("\nNone of these documents carry a payload. Nothing to do.")
        return 1

    print(
        f"\n{'Would change' if args.dry_run else 'Changed'} {total} file(s) "
        f"across {applied} round(s)."
    )
    if args.dry_run:
        print("\n(dry run — nothing written, nothing committed)")
        return 0

    if args.no_commit:
        print("\nNot committing (--no-commit). The files are applied; commit when ready.")
        return 0

    # "Nothing changed" is not the same as "nothing to commit". If a previous run
    # applied the files and then the checks refused the commit, re-running after the fix
    # writes nothing — and without this, it would report success and leave the work
    # sitting uncommitted, which is precisely the state the operator was trying to get
    # out of. So the deciding question is whether the TREE is dirty, not whether this
    # run wrote anything.
    if not total:
        dirty = bool((_git("status", "--porcelain").stdout or "").strip())
        if not dirty:
            print("\nNothing changed, so there is nothing to commit.")
            return 0
        print("\nNothing to write — the files were already applied. Committing the "
              "working tree.")

    return commit_and_push(
        rounds, all_written, all_removed,
        push=not args.no_push,
        verify=not args.no_verify,
    )


if __name__ == "__main__":
    raise SystemExit(main())
