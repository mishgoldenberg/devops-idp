"""
The changelog. Written by hand, on purpose.

WHY THIS IS NOT GENERATED FROM COMMITS
--------------------------------------
The first attempt at "What's New" read `git log` and grouped the subjects under
Features and Fixes. It produced an accurate list of things nobody wanted to read:

    fix: artifactory storage 403 UX + log SNow producer response
    chore: surface SNow producer diagnostics at WARNING (root logger is WARNING)

That tells a developer what changed in a file. It tells the person who opened the
portal to raise a request precisely nothing -- they do not know what a producer is,
they never saw the 403, and "root logger is WARNING" is a sentence about us. A commit
subject is addressed to whoever reviews the diff. A changelog is addressed to whoever
uses the thing. They are different documents and the second one has to be written.

So this file IS the changelog. Every round adds an entry here, in plain language,
describing what a user can now do or what stopped going wrong for them. The version
is chosen deliberately at the same time:

    major   something people have to relearn, or a request type that no longer works
    minor   a new capability -- a page, a form, a widget, a self-service action
    patch   fixes and refinements to things that already existed

WHAT MAKES A GOOD LINE
----------------------
Write the user's half of the sentence, not ours. "The pipeline is now authorised for
its variable group" is our half; "A scheduled cleaner starts on its own instead of
waiting for someone to press Permit in Azure DevOps" is theirs. Name the symptom
somebody actually saw. No file names, no function names, no HTTP status codes, no
round numbers.

Each line may carry a lead and a detail separated by " -- ". The page renders the lead
in bold and the rest after it, so a reader skimming only the leads still gets the
list. A line without a separator is shown whole.

The newest release is first. `version()` is what the running image calls itself: it is
stamped onto the images at build time and shown on the What's New page, so "which
version is test on" has an answer that does not involve reading a build number.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What a release says when everything in it happened somewhere a user cannot
# look. The release is still listed -- it happened, it has a version, and a
# gap in the numbers reads as a broken page -- but it is one line, not an
# inventory of the admin section.
GENERIC = "General fixes and improvements you would not see directly."

# Newest first. See the module docstring before adding one.
RELEASES: List[Dict[str, Any]] = [
    {
        "version": "1.6.18",
        "date": "2026-09-16",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.17",
        "date": "2026-09-01",
        "headline": "New Azure DevOps projects are created again.",
        "features": [],
        "fixes": [
            "A project you asked for now actually appears once your request is "
            "approved -- approved requests were failing at the very last step, so "
            "the request showed as handled and no project was ever created.",
        ],
        "improvements": [],
    },
    {
        "version": "1.6.16",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.15",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.14",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.13",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.12",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.11",
        "date": "2026-08-26",
        "headline": "The keyboard shortcuts work in Hebrew.",
        "features": [],
        "fixes": [
            "The keyboard shortcuts work whatever your keyboard is set to -- "
            "with it on Hebrew they did nothing at all, silently. R still "
            "refreshes, T still switches the theme, and ? still opens the list "
            "of them, from the key each one is printed on.",
        ],
        "improvements": [],
    },
    {
        "version": "1.6.10",
        "date": "2026-08-26",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.9",
        "date": "2026-08-25",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.8",
        "date": "2026-08-25",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.7",
        "date": "2026-08-25",
        "headline": "The response times are on the Support page.",
        "features": [
            "An SLA button on Support shows what each severity means and how long "
            "until somebody answers -- two days at Low, one hour at Urgent. Pick "
            "the one that matches the effect on your work rather than how urgent "
            "it feels, and you know what to expect before you send it.",
        ],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.6",
        "date": "2026-08-25",
        "headline": "Every release is listed, including the quiet ones.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [
            "Every release the portal has had is listed here again, including the "
            "ones that only changed things you cannot see. Those say so in a line "
            "rather than being left out, so the version numbers run without gaps.",
        ],
    },
    {
        "version": "1.6.5",
        "date": "2026-08-25",
        "headline": "What's New is only what you can see.",
        "features": [],
        "fixes": [],
        "improvements": [
            "This page describes only what you can see for yourself. Work that "
            "happened behind the admin pages, or in a system you do not have an "
            "account on, is now one line rather than a list of things you cannot "
            "look at.",
        ],
    },
    {
        "version": "1.6.4",
        "date": "2026-08-25",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.3",
        "date": "2026-08-25",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.2",
        "date": "2026-08-24",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.6.1",
        "date": "2026-08-24",
        "headline": "The portal stopped calling itself Beta.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [
            "The portal no longer calls itself Beta.",
        ],
        "note": (
            "The version numbers on this page have been renumbered so that the "
            "release where self-service arrived is 1.0. Anything recorded as "
            "deployed before today refers to a version by its old number."
        ),
    },
    {
        "version": "1.6.0",
        "date": "2026-08-24",
        "headline": "Pickers you can type into.",
        "features": [
            "Choosing a project or a repository is now one box you type into -- the "
            "list appears underneath and narrows as you type, and the arrow keys and "
            "Enter pick from it. No more typing in one control and choosing in "
            "another.",
            "A repository that is not in the list can simply be typed. It is accepted "
            "as you type it rather than needing a separate box.",
        ],
        "fixes": [
            "The list under a picker opens right beneath the box and stays there while "
            "you type. It used to appear well below the field, and then jump as the "
            "list narrowed -- moving the row you were about to click.",
        ],
        "improvements": [
            "A step will not let you past a question it needs, instead of accepting "
            "four steps of answers and objecting at the end.",
        ],
    },
    {
        "version": "1.5.0",
        "date": "2026-08-24",
        "headline": "A cleaner starts working the moment its pull request is merged.",
        "features": [
            "Merging a cleaner now runs it once immediately -- and that first run is "
            "what actually creates the scheduled job. Until now the job appeared only "
            "the next time somebody happened to change those files, so a cleaner "
            "could look scheduled for days while deleting nothing.",
        ],
        "fixes": [
            "Removing a cleaner that had never actually run no longer asks you to go "
            "and delete two things that were never created.",
        ],
        "improvements": [
            "A cleaner says it is being applied while its first run is going, and "
            "says so plainly if that run fails, instead of reporting itself as "
            "running either way.",
        ],
    },
    {
        "version": "1.4.1",
        "date": "2026-08-24",
        "headline": "The portal loads again.",
        "features": [],
        "fixes": [
            "Every page failed to load immediately after the last update. A single "
            "mistyped line in the strip that shows where you are in the portal was "
            "enough to stop every page rendering, and it has been corrected.",
        ],
        "improvements": [],
    },
    {
        "version": "1.4.0",
        "date": "2026-08-24",
        "headline": (
            "Removing a cleaner now tells you exactly what is left on the cluster, "
            "and the portal has a What's New page."
        ),
        "features": [
            "What's New -- this page. Every release the portal gets is written up "
            "here in plain language, with the version it landed in and the date it "
            "arrived. You can see which version you are looking at right now.",
            "Cluster cleanup links -- when a cleaner is removed, its scheduled job "
            "and its settings stay on the OpenShift cluster, because the portal has "
            "no route to that cluster and cannot delete them for you. It now gives "
            "you a direct link to each of them so an admin can remove them in two "
            "clicks, and the cleaner keeps saying it is not fully gone until "
            "somebody confirms they have.",
            "Every running cleaner links to its own job and settings on the cluster, "
            "so you can see what it is actually doing without hunting for it.",
        ],
        "fixes": [
            "A new cleaner no longer stops on its first night waiting for "
            "somebody to grant it access. It looked scheduled and deleted "
            "nothing.",
            "Approving the removal of a cleaner now removes its pipeline as well, "
            "and changing your mind puts the pipeline back.",
        ],
        "improvements": [
            "Removing a cleaner is no longer reported as finished while part of it "
            "is still running somewhere -- the request stays open until the cluster "
            "side is confirmed clear.",
        ],
    },
    {
        "version": "1.3.0",
        "date": "2026-08-24",
        "headline": "Several request forms stopped fighting back, and My Requests keeps itself tidy.",
        "features": [
"My Requests clears out requests older than a year that you can no "
            "longer act on, so the list stays readable. A cleaner is never "
            "removed this way -- it describes something that is still running.",
        ],
        "fixes": [
"The details you type into a request now reach the team that acts on "
            "it. Every one of them was being dropped on the way, and the request "
            "came back refused for missing answers you had actually given.",
            "A dropdown with nothing in it is replaced by a box you can type in, "
            "along with the reason the list is empty, instead of a picker offering "
            "'Nothing to choose'.",
"The purpose you type for a pipeline is kept, instead of being "
            "dropped on the way and replaced by a default.",
            "Withdrawing a request whose pull request was already closed now says so "
            "and updates the request, rather than failing with an Azure DevOps error.",
            "Refresh reloads the state of every list on the page, including cleaners.",
        ],
        "improvements": [
            "The Artifactory cleaner form lets you search for a repository the same "
            "way the quota form does.",
            "Filter strips across the portal now start with All on the left and share "
            "one look, so the same control does not appear in three different styles.",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-08-23",
        "headline": "Every request form asks the same questions, and an approved cleaner starts on its own.",
        "features": [
"A cleaner you asked for starts working on its own once it is "
            "approved. It used to need somebody to go and build its schedule by "
            "hand afterwards.",
"Every request form opens with the same questions about you -- the "
            "same fields, in the same order, with the same list of branches to "
            "pick from.",
        ],
        "fixes": [
            "A cleaner waiting for review used to offer nothing you could do to it at "
            "all. There is now always at least one action available.",
        ],
        "improvements": [],
    },
    {
        "version": "1.1.0",
        "date": "2026-08-20",
        "headline": "Failures that say whose problem they are.",
        "features": [
            "A cleaner can be removed from the portal, and each one now appears in My "
            "Requests where its owner looks for it.",
        ],
        "fixes": [
            "When Artifactory refuses something, the message now says which account "
            "was refused and what it was refused for, instead of a word nobody can "
            "act on.",
"A request that is refused now says which field it was refused over, "
            "rather than that something was wrong.",
            "The project picker in the quota form falls back to what it can read "
            "instead of coming back empty when one lookup is refused.",
            "Storage figures no longer put two different kinds of terabyte on one "
            "chart, which made a repository look larger than the disk holding it.",
            "Hebrew text now reads right-to-left beside its label instead of drifting "
            "to the far edge of the field.",
        ],
        "improvements": [],
        "note": (
            "The portal began recording its own deployments at this release. "
            "Everything below is reconstructed from the change records it was built "
            "from, so those entries carry the date the change was written rather than "
            "the day it reached you."
        ),
    },
    {
        "version": "1.0.0",
        "date": "2026-08-19",
        "headline": "Self-service arrives: ask for Artifactory space, or a cleanup schedule, without anyone writing YAML.",
        "features": [
            "Artifactory quota requests, showing the space you are already using "
            "beside the amount you are asking for.",
            "Artifactory cleaners -- describe what should be deleted and when, and "
            "the portal writes the files and opens a pull request for review.",
            "A short address reaches the portal, as well as the full one.",
        ],
        "fixes": [
            "Signing in over HTTPS works. The proxy in front of the portal was "
            "telling it every request had arrived over plain HTTP, which broke the "
            "sign-in round trip.",
            "Two sections that showed invented repositories, artifacts and code "
            "quality figures have been removed. The real ones were next to them the "
            "whole time.",
            "A cleaner with no rules at all is refused -- it would have matched every "
            "artifact in the repository.",
            "Keeping the newest few builds keeps the newest few, rather than deleting "
            "exactly those and keeping the rest.",
        ],
        "improvements": [],
    },
    {
        "version": "0.8.0",
        "date": "2026-08-18",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "0.7.0",
        "date": "2026-08-16",
        "headline": "Quick links become yours, and panels open where you clicked.",
        "features": [
            "Everybody gets their own quick links, alongside the shared ones.",
            "The two panels that used to appear when your pointer passed over them "
            "are now buttons, and they open in place.",
        ],
        "fixes": [
            "The guided tour follows the page instead of trailing a step behind it, "
            "and its highlight now covers what the step is actually describing.",
            "The Suggestions board opens showing everything. It used to open filtered "
            "to Open, so most of the board was invisible until you noticed a filter "
            "you had not set.",
        ],
        "improvements": [
        ],
    },
    {
        "version": "0.6.0",
        "date": "2026-07-22",
        "headline": "A new look, and dashboards you can arrange.",
        "features": [
            "A calmer palette in both light and dark, and a logo that survives light "
            "mode.",
            "Every dashboard widget can be resized, and they rearrange under your "
            "cursor as you drag them.",
            "Quick links wrap into rows and can hold groups of links.",
            "A command palette, a service catalog, and a Needs You strip at the top "
            "of the dashboard that answers what is waiting on you across every system "
            "at once.",
"Announcements from the platform team appear at the top of your "
            "dashboard, and stay dismissed once you have read them.",
            "Undo on a destructive action, instead of a dialog asking whether you are "
            "sure.",
        ],
        "fixes": [
            "Granting somebody access now grants it to the person you picked. It had "
            "been landing on the account the portal signs in with, because the chosen "
            "user was not yet in the collection at all.",
            "The dashboard does a fraction of the work it used to on every frame, "
            "which is most of what made it feel slow.",
            "The SonarQube panel says what is wrong and where instead of spinning "
            "forever.",
        ],
        "improvements": [
            "Hover panels no longer sit on top of the buttons they cover.",
        ],
    },
    {
        "version": "0.5.0",
        "date": "2026-07-15",
        "headline": "Both pull-request widgets find your pull requests.",
        "features": [],
        "fixes": [
            "The two pull-request widgets now find your pull requests.",
            "The Artifactory disk bar no longer shows a figure that could not be true.",
        ],
        "improvements": [
        ],
    },
    {
        "version": "0.4.0",
        "date": "2026-07-14",
        "headline": "A suggestions board people vote on, and Azure DevOps projects that arrive while you wait.",
        "features": [
            "Suggestions becomes a board: post an idea, vote on other ideas, comment, "
            "and hear back. Who posted what is hidden from other users and visible to "
            "admins.",
            "Asking for an Azure DevOps project creates it in both collections while "
            "you wait, instead of queueing work somebody had to come back to.",
            "The sidebar collapses to a rail of icons, and quick links are yours to "
            "arrange.",
            "Notifications stack into a pile that fans out when you point at it, "
            "instead of landing on top of the button you were about to press.",
            "Work item filters that do something, pull-request widgets you can sort, "
            "and pipelines that show their branch.",
        ],
        "fixes": [
            "Work Items stopped failing the moment you picked a project.",
            "The Service temporarily unavailable popup that appeared at random is "
            "gone.",
            "Both pull-request widgets show pull requests again, and load far faster.",
            "The Artifactory Storage widget shows real deduplicated usage rather than "
            "an inflated sum of every repository.",
            "The Artifactory Repos widget keeps only the visible rows on the page, so "
            "scrolling to the end no longer makes the whole tab heavy.",
            "Pressing a button repeatedly can no longer freeze the tab.",
            "Hebrew reads correctly, and the Confluence widget stops jumping.",
        ],
        "improvements": [
            "A request opens to Progress and Request info; the inputs and the raw "
            "result move behind More info.",
            "Auto-refresh reimagined, the shell pinned in place, and the sidebar and "
            "panes yours to size.",
        ],
    },
    {
        "version": "0.3.0",
        "date": "2026-07-13",
        "headline": "Where the record starts.",
        "features": [
"Everything the portal could already do by this date: signing in, "
            "dashboards and widgets, quick links, search, and the Azure DevOps "
            "and Artifactory panels behind them.",
        ],
        "fixes": [],
        "improvements": [],
        "note": (
            "Changes before this date were made, but not written up in a form this "
            "page can show."
        ),
    },
]

SECTIONS = (
    ("features", "New"),
    ("improvements", "Improved"),
    ("fixes", "Fixed"),
)


def _shape(entry: Dict[str, Any]) -> Dict[str, Any]:
    shaped = {
        "version": str(entry.get("version") or ""),
        "date": str(entry.get("date") or ""),
        "headline": str(entry.get("headline") or ""),
        "note": str(entry.get("note") or ""),
    }
    for key, _label in SECTIONS:
        shaped[key] = [str(line).strip() for line in (entry.get(key) or []) if str(line).strip()]
    return shaped


def is_generic(entry: Dict[str, Any]) -> bool:
    """A release with nothing in it but the generic line.

    Everything it changed happened where a user cannot look. It still has a
    version and it still happened -- it just does not deserve a card of its own,
    and six of them in a row is what this page had."""
    lines = [line for key, _ in SECTIONS for line in entry.get(key) or []]
    return bool(lines) and set(lines) == {GENERIC}


def all_releases() -> List[Dict[str, Any]]:
    """Every release exactly as written, newest first. No collapsing.

    For the tests, the round documents and anything that has to reason about
    version numbers. The PAGE uses releases()."""
    return [_shape(entry) for entry in RELEASES]


def releases() -> List[Dict[str, Any]]:
    """What the What's New page shows, newest first.

    Releases with real content keep their own card. The generic-only ones are
    collected into a single entry at the END, which names every version it
    covers -- so the numbering still visibly runs unbroken, and the card at the
    top of the page is always a release with something in it.
    """
    shaped = all_releases()
    real = []
    generic = []
    for entry in shaped:
        if is_generic(entry):
            generic.append(entry)
            continue
        # A release that DID change something visible does not also need to be
        # told it changed things you cannot see. The generic line exists for the
        # releases that have nothing else; on a card with real content it is
        # noise, and it is most of why the page reads as six identical entries.
        for key, _ in SECTIONS:
            entry[key] = [line for line in entry[key] if line != GENERIC]
        real.append(entry)
    if not generic:
        return real

    versions = [e["version"] for e in generic]
    collapsed = {
        "version": versions[0],
        "versions": versions,
        "date": generic[0]["date"],
        "headline": "General fixes.",
        "note": "",
        "collapsed": True,
        "features": [],
        "improvements": [],
        "fixes": [
            GENERIC + " Covers "
            + ("version " + versions[0] if len(versions) == 1
               else "versions " + ", ".join(versions[:-1]) + " and " + versions[-1])
            + "."
        ],
    }
    return real + [collapsed]


def current() -> Dict[str, Any]:
    """The release this image is. Never raises -- an empty changelog is not fatal.

    Reads all_releases(), not releases(): the image is whatever version is newest
    in the file, including a generic-only one that the page collapses away. The
    number stamped on the image and the newest card on the page are two different
    questions."""
    everything = all_releases()
    return everything[0] if everything else {"version": "0.0.0", "date": "", "headline": ""}


def version() -> str:
    return str(current().get("version") or "0.0.0")


def find(wanted: str) -> Optional[Dict[str, Any]]:
    for entry in all_releases():
        if entry["version"] == str(wanted or ""):
            return entry
    return None


def split_line(line: str) -> Dict[str, str]:
    """A changelog line as a lead and the rest of it.

    The separator is written as ``--`` because these lines are also read in a
    terminal, in a commit message and in a round document, and an em dash does not
    survive every one of those intact.
    """
    text = str(line or "").strip()
    if " -- " in text:
        lead, rest = text.split(" -- ", 1)
        return {"lead": lead.strip(), "rest": rest.strip()}
    return {"lead": "", "rest": text}


def check() -> List[str]:
    """Everything wrong with this file, as sentences. Empty means it is fine.

    Run by the tests and by ``scripts/check_changelog.py`` in CI. A changelog whose
    versions go backwards, or whose newest entry is a date in the future, ships an
    image tag that sorts wrongly in Artifactory and a page that reads as broken --
    and both are invisible until somebody looks.
    """
    problems: List[str] = []
    seen: set = set()
    previous: Optional[tuple] = None
    previous_date = ""

    if not RELEASES:
        return ["The changelog has no releases in it."]

    for entry in RELEASES:
        ver = str(entry.get("version") or "")
        date = str(entry.get("date") or "")
        where = f"release {ver or '(no version)'}"

        if not _SEMVER.match(ver):
            problems.append(f"{where}: the version must look like 1.2.3.")
            continue
        if ver in seen:
            problems.append(f"{where}: appears twice.")
        seen.add(ver)

        parts = tuple(int(p) for p in ver.split("."))
        if previous is not None and parts >= previous:
            problems.append(
                f"{where}: is not older than {'.'.join(str(p) for p in previous)}. "
                f"The newest release goes first."
            )
        previous = parts

        if not _DATE.match(date):
            problems.append(f"{where}: the date must be written 2026-08-24.")
        elif previous_date and date > previous_date:
            problems.append(f"{where}: dated after the release above it.")
        else:
            previous_date = date or previous_date

        if not str(entry.get("headline") or "").strip():
            problems.append(f"{where}: needs a headline -- one sentence a user would understand.")

        lines = []
        for key, _label in SECTIONS:
            lines += [str(x) for x in (entry.get(key) or [])]
        if not lines and not str(entry.get("note") or "").strip():
            problems.append(f"{where}: has no entries and no note. An empty release says nothing.")

        for line in lines:
            # The things that keep leaking in from commit subjects. A changelog line
            # naming a file is a commit subject wearing a hat.
            if re.search(r"\b(HTTP\s*\d{3}|\.py\b|\.html\b|[a-z_]+\.[a-z_]+\(\))", line):
                problems.append(
                    f"{where}: \"{line[:60]}...\" reads like a commit message. "
                    f"Say what the user sees, not what the code does."
                )
    return problems
