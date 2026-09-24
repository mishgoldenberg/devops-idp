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
        "version": "1.14.0",
        "date": "2026-09-24",
        "headline": "Know what DevBot can do, and what it costs you.",
        "features": [
            "Usage -- what your questions to DevBot have cost, per model, today and over the last 30 days, next "
            "to your AI key's limits, budget and expiry. Open it from the AI key panel on the DevBot AI page.",
            "A tour of DevBot -- the first time you open the DevBot AI page it shows you around: picking a model, "
            "what your key allows, your conversations, and how DevBot shows its work. Replay it any time from "
            "What can it do?.",
            "DevBot in the portal tour -- if you have already taken the tour, it shows you the one new stop, once.",
        ],
        "fixes": [],
        "improvements": [
            "When your AI key hits its per-minute limit, DevBot waits a few seconds and carries on by itself; for "
            "a longer wait it says how long and offers Ask again when the time is up.",
            "A question that could not be answered stays in the conversation with the reason, and asking it again "
            "does not count it twice.",
        ],
    },
    {
        "version": "1.13.0",
        "date": "2026-09-24",
        "headline": "DevBot can prepare changes for you -- you check and confirm every one.",
        "features": [
            "Next steps after a failure -- when DevBot explains a failed run or failed tests, it offers to run "
            "the pipeline again and to open a bug that already contains the error, the log excerpt, the run and "
            "the Confluence page with the fix.",
            "Ask DevBot to change things -- run a pipeline again, open or comment on a work item, move or assign "
            "it, review or comment on a pull request, or write a fix up as a Confluence page.",
            "Nothing happens until you confirm -- each change opens the same dialog the widgets use, filled with "
            "DevBot's draft for you to edit. The system checks it first, you confirm it, and it is done in your "
            "name. Dismiss a suggestion to turn it down; DevBot remembers either way.",
        ],
        "fixes": [],
        "improvements": [],
    },
    {
        "version": "1.12.0",
        "date": "2026-09-24",
        "headline": "Ask DevBot why something failed -- it follows the trail through every system.",
        "features": [
            "Why did my pipeline fail? -- DevBot finds the run, reads the error out of the failed step's log, and "
            "looks in Confluence for a page with the fix. When it finds one it quotes the fix and links the page; "
            "when it does not, it says so and offers its own suggestion, marked as its own.",
            "The whole picture of a failure -- open bugs that already mention the error, whether the missing "
            "package is in Artifactory, the failing SonarQube conditions when the quality gate stopped the run, and "
            "when the pipeline last passed.",
            "Failed tests -- which tests failed and why, and which of them are new and which kept failing in "
            "earlier runs.",
            "Is my pull request ready? -- reviews, required reviewers, policies, conflicts, open comments, its "
            "build and its quality gate, and exactly what still blocks it.",
            "A work item at a glance, a project's health, and where an image came from -- the run, the commit and "
            "the work items that built it.",
            "Watch DevBot work -- each step of an investigation appears as it happens, under the lookup it belongs to.",
        ],
        "fixes": [],
        "improvements": [],
    },
    {
        "version": "1.11.0",
        "date": "2026-09-24",
        "headline": "DevBot reads your systems.",
        "features": [
            "DevBot answers from your systems -- pipeline runs, work items, pull requests and repositories in "
            "every Azure DevOps collection, SonarQube quality gates, issues and coverage, Artifactory artifacts and "
            "Docker tags, and Confluence pages, all read with your own access.",
            "Every answer shows what DevBot looked up -- each lookup appears while it runs, and afterwards the "
            "answer lists and links every source it used.",
            "Models that can read your systems are marked Live data -- in the model picker, found out the first "
            "time you ask one something, or at once with Check now.",
        ],
        "fixes": [],
        "improvements": [
            "Without a SonarQube token, DevBot still answers about public projects and tells you that is all it "
            "can see.",
        ],
    },
    {
        "version": "1.10.0",
        "date": "2026-09-24",
        "headline": "Meet DevBot AI, a chat assistant inside the Hub.",
        "features": [
            "DevBot AI -- a new page in the sidebar where you can ask questions in your own words, in "
            "English or Hebrew, and watch the answer being written. Your conversations are listed on the "
            "left and kept for 30 days after you last use them.",
            "Connect your own AI model key -- on the DevBot AI page or on Connections. DevBot offers exactly "
            "the models your key may use, and you can switch model in the middle of a conversation.",
            "See what your AI key allows -- requests and tokens a minute, its budget and when it expires, "
            "and under every answer what it cost.",
        ],
        "fixes": [
            "The ? next to a token box on Connections now names the system it explains.",
        ],
        "improvements": [],
    },
    {
        "version": "1.9.2",
        "date": "2026-09-24",
        "headline": "General fixes.",
        "features": [],
        "fixes": [GENERIC],
        "improvements": [],
    },
    {
        "version": "1.9.1",
        "date": "2026-09-24",
        "headline": "A flame that looks like a flame.",
        "features": [],
        "fixes": [
            "The bell no longer shows a red 0 when you have nothing unread.",
        ],
        "improvements": [
            "The streak flame beside the bell has a new icon.",
            "You will be asked to sign in once more after this update.",
        ],
    },
    {
        "version": "1.9.0",
        "date": "2026-09-24",
        "headline": "Review pull requests, run pipelines again and move work items without leaving the Hub.",
        "features": [
            "Review a pull request from the Hub: approve, approve with suggestions, send it "
            "back to the author or reject it, with an optional comment. The Review button is "
            "on the pull requests waiting for you, and in Needs You.",
            "Run a finished pipeline again, on the same branch with the same parameters, "
            "from the pipelines widget.",
            "Open a work item to move it to another state, assign it to yourself or someone "
            "else, add a paragraph to its description, or comment on it.",
            "Every one of these asks Azure DevOps first and shows you exactly what will "
            "happen. Nothing is sent until you confirm, and it is recorded under your name.",
            "A streak: the small flame beside the bell counts the workdays in a row you have "
            "done something in the Hub. Fridays and Saturdays never count or break it, and "
            "five freezes a month cover holidays. Settings can hide it.",
            "Rate a finished request from My Requests, and see when one has been waiting "
            "longer than usual.",
        ],
        "fixes": [
            "Pull requests in Needs You show how long they have been waiting.",
        ],
        "improvements": [
            "Search in the work items, pull request and pipelines widgets.",
            "To use the new actions, create a new Azure DevOps token with write access: the "
            "guide on the Connections page lists exactly what to tick. Your current token "
            "keeps working for everything you can see today.",
        ],
    },
    {
        "version": "1.8.2",
        "date": "2026-09-24",
        "headline": "A smoother sidebar, and the Azure DevOps page gets room to breathe.",
        "features": [],
        "fixes": [
            "Opening and closing the sidebar no longer stutters on a busy dashboard: "
            "the page now slides into place instead of being redrawn on every frame.",
            "Scrolling to the end of a long SonarQube list no longer slows the page "
            "down: however far you scroll, only the part you are near is drawn.",
            "The reload button on the SonarQube widgets has moved next to Sort, away "
            "from the corner where the move and resize handles appear, so reaching for "
            "one no longer risks the other.",
        ],
        "improvements": [
            "The Azure DevOps page uses the width like the SonarQube page: your work "
            "items across the top, your pull requests and the ones waiting for your "
            "review side by side, and pipelines across the bottom, all taller.",
        ],
    },
    {
        "version": "1.8.1",
        "date": "2026-09-24",
        "headline": "SonarQube widgets open in a moment and no longer freeze the dashboard.",
        "features": [],
        "fixes": [
            "The SonarQube widgets no longer freeze the page while they load, however "
            "many projects your SonarQube holds.",
            "The SonarQube widgets no longer take long enough to give up with an "
            "error on a large SonarQube.",
        ],
        "improvements": [
            "SonarQube widgets show the numbers from the last few minutes straight "
            "away and bring themselves up to date a few seconds later. The reload "
            "button on a widget, and Refresh, always fetch the newest.",
            "Long SonarQube lists show forty at a time and add more as you scroll; "
            "search, filter and sort still cover every project.",
            "Every page and widget arrives faster: the Hub now sends them compressed, "
            "a fraction of the size they were.",
        ],
    },
    {
        "version": "1.8.0",
        "date": "2026-09-23",
        "headline": "Search, filter and pin in every SonarQube widget, and SonarQube has its own page again.",
        "features": [
            "SonarQube is back in the sidebar -- one page with all six SonarQube "
            "views together, including any you have hidden from your dashboard.",
            "Open a project in any SonarQube widget to see its analysis properly -- "
            "the gate result and exactly why it failed, bugs, vulnerabilities, code "
            "smells and hotspots with their A-to-E ratings, coverage and duplication, "
            "unit tests, and a link straight to the project in SonarQube.",
            "Open one of your pull requests in PR Quality Gates to see its own "
            "analysis -- the issues it adds, coverage and duplication with the "
            "estimate after merge, and its unit tests: the same summary SonarQube "
            "writes on the pull request.",
            "A dot next to My Requests and Suggestions in the sidebar tells you "
            "something changed there since you last looked -- a request approved or "
            "rejected, a vote, a reply. Opening the page clears it.",
            "Needs You shows your requests that were just approved or rejected, with "
            "who decided and why, for three days or until you mark them done.",
        ],
        "fixes": [
            "Two SonarQube projects with the same name no longer look like one "
            "project listed twice -- each shows its key underneath.",
            "A SonarQube project whose name is only punctuation shows its key "
            "instead of a lone full stop.",
            "\"View in SonarQube\" on a pull request opens its analysis in SonarQube, "
            "not the pull request again.",
        ],
        "improvements": [
            "Every SonarQube widget has the same search, filter, sort and pins as the "
            "Azure DevOps widgets, and remembers your filter and sort. A link you "
            "send carries them too.",
            "Failed gate conditions read as sentences -- coverage 0.0%, needs at "
            "least 80% -- instead of a raw comparison you had to work out.",
            "The last coloured ovals are gone. Suggestion statuses, the What's New "
            "section labels and the reasons in Needs You use the same quiet dot and "
            "word as everything else.",
        ],
    },
    {
        "version": "1.7.0",
        "date": "2026-09-17",
        "headline": "Five new SonarQube widgets for your dashboard.",
        "features": [
            "Quality Gates -- see which projects are failing and which check failed, "
            "with the number beside the threshold it missed, instead of opening each "
            "project to find out.",
            "New Code -- only what landed in the current period, worst first. The "
            "years of history behind a project stay out of the way.",
            "Security Hotspots -- projects with reviews still outstanding; open one "
            "to see the hotspots themselves.",
            "Your issues -- open findings on lines you last touched, matched by the "
            "author recorded in the commit. If nothing matches, the widget tells you "
            "which names it searched for, so you can see why.",
            "Your pull requests -- the quality gate on each of your open pull "
            "requests, failing ones first, and it says plainly when a pull request "
            "has not been analysed rather than leaving it blank.",
        ],
        "fixes": [
            "The SonarQube project list works for everyone -- it was asking for "
            "something only an administrator is allowed to ask, so most people got "
            "an error telling them to reconnect an account that was never the "
            "problem.",
            "Connections no longer tells you your Azure DevOps token has expired "
            "while your widgets are plainly working with it. It was only asking one "
            "collection whether the token was good; now it asks everywhere your "
            "widgets actually look, and only calls a token dead when nothing "
            "accepts it.",
            "A long run of text with no spaces in it -- a pasted path, a token, a "
            "link -- stays inside its box instead of running off the side of the "
            "page. That applies everywhere you can type, not just where it was "
            "noticed.",
        ],
        "improvements": [
            "The SonarQube widgets share one read, so adding four of them costs no "
            "extra waiting, and they work without connecting anything when the "
            "server lets you browse projects already.",
            "SonarQube Projects now behaves like Artifactory Repos -- search, pin, "
            "and a button that opens the numbers inside the row. They used to "
            "appear on hover, over the name you were reading, and could not be "
            "opened from the keyboard at all.",
            "Status labels across the portal are quieter. Same colours and the same "
            "meanings, without a block of solid colour on every row competing with "
            "the thing you opened the page to read.",
        ],
    },
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
