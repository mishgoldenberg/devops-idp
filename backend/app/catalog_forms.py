"""
The one description of every self-service form the portal offers.

A form is DATA here, not markup: sections, fields, types, options, and the rule that
decides whether a section is shown. One renderer in the browser turns any of these
into a form, which is what makes adding a request a dozen lines rather than another
four hundred lines of hand-written wizard.

That matters beyond tidiness. The Support ticket wizard is four hand-built steps with
its own validation, its own progress bar, its own summary and its own localStorage
handling; every form built that way is a fresh chance to get one of those wrong, and
none of the fixes carry across.

The keys here are also the keys the answers are stored and submitted under. Where a
field maps onto a ServiceNow catalog variable, snow_catalog matches it by name and
then by label, so a field called pipeline_type finds a variable named pipeline_type
or one labelled "Pipeline Type" without anything hardcoded here.

Field types the renderer understands:

    text        one line
    textarea    several lines (rows)
    select      fixed options, or options_source for a list fetched at open time
    number      min / max / suffix
    size        a storage size typed as "250 GB"
    toggle      yes / no
    file        one attachment (image_only marks the ones that must be a picture)

Anything with visible_when is shown only while another field holds a given value,
which is how the conditional Build and SonarQube blocks work. Hidden fields are not
validated and not submitted -- a required field nobody can see is a form that cannot
be sent and does not say why.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# The Support wizard's first step, and nothing else.
#
# This block is not a design decision, it is a COPY. The Support ticket wizard has
# asked these six questions, in this order, with these labels and these branch
# choices, for as long as the portal has existed; people have filled it in dozens of
# times and ServiceNow already routes on the answers. A self-service form that asks
# the same thing in different words is a second vocabulary for one fact -- and the
# version this replaces had invented one: English-only labels, a free-text Branch
# where the wizard offers a fixed list, a different field order, and a "Division"
# field that exists nowhere in the wizard at all.
#
# So: same keys, same labels (Hebrew included -- they are how people recognise the
# question), same order, same widget for each. The keys are also what ServiceNow is
# asked for, and they are the wizard's keys, so the mapping is the wizard's mapping.
#
# Keep this in step with the Requester Details step in support-container.html.
# scripts/check_requester_fields.py fails the build when the two drift apart.
_BRANCH_CHOICES: List[Dict[str, str]] = [
    {"value": v, "label": v} for v in (
        "משוב", "פיתוח תוכנה", "שילובים", "מערכות מידע", "דיגיטל", "סייבר ושתפ",
    )
]

_WHO_YOU_ARE: Dict[str, Any] = {
    "key": "requester",
    "title": "Requester Details",
    "description": "Remembered for next time. Change it here if anything is out of date.",
    "remember": True,
    "fields": [
        # Read-only in the wizard, because the portal already knows who is signed in
        # and a name typed by hand is a name that can disagree with the account.
        {"key": "full_name", "label": "Full Name (שם מלא)", "type": "text",
         "required": True, "prefill": "display_name", "readonly": True},
        {"key": "phone_number", "label": "Phone Number (טלפון)", "type": "text",
         "required": True, "digits": True, "placeholder": "0521234567"},
        {"key": "branch", "label": "Branch (ענף)", "type": "select", "required": True,
         "options": _BRANCH_CHOICES},
        {"key": "team", "label": "Team (צוות)", "type": "text", "required": True},
        {"key": "section", "label": "Section (מדור)", "type": "text", "required": True},
        {"key": "role", "label": "Role (תפקיד)", "type": "text", "required": True},
    ],
}


def _platform_choices() -> List[Dict[str, str]]:
    return [
        {"value": "AzureDevops", "label": "Azure DevOps"},
        {"value": "Gitlab", "label": "GitLab"},
        {"value": "Github", "label": "GitHub"},
    ]


FORMS: Dict[str, Dict[str, Any]] = {

    # ── Artifactory: raise a project's quota ─────────────────────────────────
    "artifactory_quota": {
        "key": "artifactory_quota",
        "title": "Enlarge Artifactory quota",
        "summary": "Raise the storage quota on a JFrog project.",
        "description": (
            "Pick the project and how much to add. What it uses now, what it is "
            "allowed and what would be left are shown before you send it."
        ),
        "icon": "artifactory",
        "submit_label": "Request the increase",
        "submit_note": (
            "Goes to a platform admin for approval. Nothing changes until it is "
            "approved, and the whole record stays in the portal."
        ),
        "request_type": "ARTIFACTORY_QUOTA_INCREASE",
        # No snow_item: this request never leaves the portal. See the note on
        # pipeline_characterization, which is the one form that does.
        "sections": [
            dict(_WHO_YOU_ARE),
            {
                "key": "quota",
                "title": "The increase",
                "fields": [
                    {
                        "key": "project_key",
                        "label": "Project",
                        "type": "select",
                        "required": True,
                        # Loaded when the form opens, with each project's usage
                        # attached, so the panel underneath can show what is actually
                        # free rather than asking the requester to know it.
                        "options_source": "artifactory_my_projects",
                        "searchable": True,
                        # A picker whose list is read from another system can come
                        # back empty for reasons that have nothing to do with the
                        # person filling the form in. When it does, they type the key
                        # -- a request an approver can read beats a form that cannot
                        # be submitted at all.
                        "allow_custom": True,
                        "custom_label": "Type the project key",
                        "custom_placeholder": "my-project",
                        "help": "The projects you are a member of, read live from Artifactory.",
                    },
                    {
                        "key": "increase_by",
                        "label": "Add",
                        "type": "size",
                        "required": True,
                        "placeholder": "250 GB",
                        "help": "How much to add to the current quota. GB unless you say otherwise.",
                    },
                    {
                        "key": "justification",
                        "label": "Why",
                        "type": "textarea",
                        "rows": 3,
                        "required": True,
                        "placeholder": "What is filling it up, and what have you already cleaned?",
                        "help": "An admin reads this to decide. It is also the record of why the quota grew.",
                    },
                ],
            },
        ],
    },

    # ── Artifactory: schedule a cleaner ──────────────────────────────────────
    "artifactory_cleaner": {
        "key": "artifactory_cleaner",
        "title": "Schedule an Artifactory cleaner",
        "summary": "Delete old artifacts from one repository on a schedule.",
        "description": (
            "Creates the ConfigMap, CronJob and pipeline for a nightly cleanup, as a "
            "pull request against artifact-cleaner-automation. Often cheaper than a "
            "bigger quota."
        ),
        "icon": "artifactory",
        "submit_label": "Request the cleaner",
        "submit_note": (
            "Goes to a platform admin for approval, then opens a pull request. "
            "Nothing is deleted until that pull request is merged."
        ),
        "request_type": "ARTIFACTORY_CLEANER_CREATE",
        # No snow_item, as above. It also never fitted: this was ordering the
        # "Enlarge Quota in Artifactory" item to describe a cleaner, which is a
        # request for storage raised every time somebody asked for the opposite.
        "sections": [
            dict(_WHO_YOU_ARE),
            {
                "key": "cleaner",
                "title": "What to clean",
                "fields": [
                    {"key": "cleaner_name", "label": "Name", "type": "text", "required": True,
                     "placeholder": "mashov docker daily",
                     "help": "Names the folder, the CronJob and the ConfigMap. It has to be free."},
                    # Project first, then the repository, because there are hundreds of
                    # repositories and the wrong key here is a nightly delete against
                    # somebody else's artifacts. Optional: not every repository belongs
                    # to a project, and an unreadable project list must not block the
                    # form.
                    {"key": "project_key", "label": "Project", "type": "select",
                     "options_source": "artifactory_my_projects",
                     # Everything the quota form's picker has, because it is the same
                     # picker reading the same list: typing to filter it, and typing
                     # the key outright when the list cannot answer.
                     "searchable": True,
                     "allow_custom": True,
                     "custom_label": "Type the project key",
                     "custom_placeholder": "my-project",
                     "help": "Optional. Choosing one narrows the repository list below."},
                    {"key": "repository", "label": "Repository", "type": "select", "required": True,
                     "options_source": "artifactory_repositories",
                     "depends_on": "project_key",
                     "searchable": True,
                     "allow_custom": True,
                     "custom_label": "Type the repository key",
                     "custom_placeholder": "mashov-docker-local",
                     "help": "The Artifactory repository to delete from. Type to filter."},
                    {"key": "older_than_days", "label": "Older than",
                     "type": "number", "default": "30",
                     "min": 1, "max": 3650, "suffix": "days",
                     "help": "Delete artifacts created before this many days ago."},
                    {"key": "keep_last", "label": "Always keep the newest",
                     "type": "number", "min": 1, "max": 10000, "suffix": "artifacts",
                     "help": "Optional. The newest this many survive every run, whatever the other rules say."},
                    {"key": "name_pattern", "label": "Name matches", "type": "text",
                     "placeholder": "my-app-*",
                     "help": "Optional wildcard on the artifact name."},
                    {"key": "file_types", "label": "Only these file types", "type": "text",
                     "placeholder": "zip, jar, tar.gz",
                     "help": "Optional, comma separated."},
                    {"key": "path_pattern", "label": "Only paths matching", "type": "text",
                     "placeholder": "my-app/*",
                     "help": "Optional wildcard on the folder inside the repository."},
                    {"key": "schedule", "label": "Schedule", "type": "select", "required": True,
                     "default": "0 22 * * *",
                     "options": [
                         {"value": "0 22 * * *", "label": "Every day at 22:00"},
                         {"value": "0 3 * * *", "label": "Every day at 03:00"},
                         {"value": "0 3 * * 5", "label": "Every Friday at 03:00"},
                         {"value": "0 3 * * 0", "label": "Every Sunday at 03:00"},
                     ],
                     "allow_custom": True,
                     "custom_label": "A cron expression",
                     "custom_placeholder": "0 22 * * *"},
                ],
            },
        ],
    },

    # ── Pipeline characterization ────────────────────────────────────────────
    "pipeline_characterization": {
        "key": "pipeline_characterization",
        "title": "Pipeline characterization",
        "summary": "Specify a new CI/CD pipeline for the DevOps team to build.",
        "description": (
            "Fill this in after your consultation with the team. It goes to the DevOps "
            "Support queue as a requested item, and is kept here so the answers stay "
            "readable afterwards."
        ),
        "icon": "pipeline",
        "submit_label": "Submit the characterization",
        "submit_note": "Opens a requested item (RITM) in the DevOps Support queue.",
        # The ONE form that reaches ServiceNow. The others are the portal's own work:
        # a quota is set by this backend and a cleaner becomes a pull request, so
        # raising a requested item for either announced work that had already been
        # done, to a queue with nothing to do about it. This one is different in kind
        # -- it is a piece of work being handed to the DevOps team, and the ticket is
        # how they receive it.
        "snow_item": "Pipeline Characterization",
        "snow_item_env": "SNOW_CATALOG_ITEM_PIPELINE_CHARACTERIZATION",
        "sections": [
            dict(_WHO_YOU_ARE),
            {
                "key": "purpose",
                "title": "What it is for",
                "fields": [
                    {"key": "problem_declaration", "label": "Problem declaration",
                     "type": "textarea", "rows": 3, "required": True,
                     "placeholder": "What is painful today?"},
                    # Labelled as the catalog item labels it. The mapping is on the KEY
                    # and no longer depends on this, but a field the requester sees
                    # under one name and the team sees under another is a needless way
                    # to make two people describe the same answer differently.
                    {"key": "pipeline_purpose", "label": "Pipeline's purpose",
                     "type": "textarea", "rows": 3, "required": True,
                     "placeholder": "What should it do, end to end?"},
                    {"key": "definition_of_done", "label": "Definition of done",
                     "type": "textarea", "rows": 3, "required": True,
                     "placeholder": "How will we agree it is finished?"},
                ],
            },
            {
                "key": "shape",
                "title": "Shape of it",
                "fields": [
                    {"key": "pipeline_flow", "label": "Pipeline flow", "type": "file",
                     "required": True, "image_only": True,
                     "help": "A diagram of the flow. PNG or JPG."},
                    {"key": "trigger", "label": "Trigger", "type": "textarea", "rows": 2,
                     "required": True,
                     "placeholder": "On every push to main, nightly, on a tag..."},
                    {"key": "development_platform", "label": "Development platform",
                     "type": "select", "required": True, "options": _platform_choices()},
                    {"key": "pipeline_type", "label": "Pipeline type", "type": "select",
                     "required": True,
                     "options": [
                         {"value": "CI", "label": "CI"},
                         {"value": "CD", "label": "CD"},
                         {"value": "Continuous Testing", "label": "Continuous Testing"},
                     ]},
                ],
            },
            {
                "key": "build_toggle",
                "fields": [
                    {"key": "add_build", "label": "Include a build stage",
                     "type": "toggle", "default": True},
                ],
            },
            {
                "key": "build",
                "title": "Build",
                "visible_when": {"field": "add_build", "equals": True},
                "fields": [
                    {"key": "programming_language", "label": "Programming language",
                     "type": "select", "required": True,
                     "options": [
                         {"value": "Python", "label": "Python"},
                         {"value": "Java \\ Kotlin", "label": "Java / Kotlin"},
                         {"value": "Node \\ Javascript \\ Typescript", "label": "Node / JavaScript / TypeScript"},
                         {"value": "C# \\ .NET core \\ .NET framework", "label": "C# / .NET"},
                         {"value": "C\\C++", "label": "C / C++"},
                         {"value": "Other", "label": "Other"},
                     ]},
                    {"key": "building_engine", "label": "Building engine", "type": "select",
                     "required": True,
                     "options": [
                         {"value": "Npm", "label": "npm"},
                         {"value": "Maven", "label": "Maven"},
                         {"value": "Gradle", "label": "Gradle"},
                         {"value": "MSBuild", "label": "MSBuild"},
                         {"value": "PyInstaller", "label": "PyInstaller"},
                         {"value": "Other", "label": "Other"},
                     ]},
                    {"key": "artifact_type", "label": "Artifact type", "type": "select",
                     "required": True,
                     "options": [
                         {"value": "docker image", "label": "Docker image"},
                         {"value": "zip", "label": "zip"},
                         {"value": "sln", "label": "sln"},
                         {"value": "exe", "label": "exe"},
                         {"value": "jar", "label": "jar"},
                         {"value": "tar.gz", "label": "tar.gz"},
                         {"value": "OVF", "label": "OVF"},
                         {"value": "Other", "label": "Other"},
                     ]},
                    {"key": "version_tagging", "label": "Version tagging", "type": "textarea",
                     "rows": 3, "required": True,
                     "placeholder": "Major.Minor.Patch, gitflow: patch on merge to dev, minor on merge to main, major by hand"},
                    {"key": "registry", "label": "Registry", "type": "select", "required": True,
                     "options": [
                         {"value": "Artifactory", "label": "Artifactory"},
                         {"value": "Azure Artifacts", "label": "Azure Artifacts"},
                         {"value": "DFS", "label": "DFS"},
                         {"value": "Nexus", "label": "Nexus"},
                     ]},
                    {"key": "upload_path", "label": "Upload path", "type": "textarea", "rows": 2,
                     "required": True,
                     "placeholder": "repo: devops-generic-prod\npath in repo: /my-app.zip"},
                    {"key": "unit_tests", "label": "Run unit tests", "type": "toggle"},
                    {"key": "specifics", "label": "Anything specific", "type": "textarea",
                     "rows": 2,
                     "placeholder": "Packaging quirks, files that have to end up inside the artifact..."},
                ],
            },
            {
                "key": "sonar_toggle",
                "fields": [
                    {"key": "add_sonarqube", "label": "Include SonarQube", "type": "toggle"},
                ],
            },
            {
                "key": "sonarqube",
                "title": "SonarQube",
                "visible_when": {"field": "add_sonarqube", "equals": True},
                "fields": [
                    {"key": "sonarqube_trigger", "label": "SonarQube trigger", "type": "textarea",
                     "rows": 2, "required": True,
                     "placeholder": "Runs before every merge, and on main / dev / test"},
                    {"key": "service_connection_name", "label": "Service connection name",
                     "type": "text", "required": True},
                    {"key": "scanner_mode", "label": "Scanner mode", "type": "select",
                     "required": True,
                     "options": [
                         {"value": "CLI", "label": "CLI"},
                         {"value": "MSBuild", "label": "MSBuild"},
                         {"value": "Other", "label": "Other"},
                     ]},
                    {"key": "sonarqube_project_key", "label": "SonarQube project key",
                     "type": "text", "required": True},
                    {"key": "integrated_unit_tests", "label": "Unit tests are integrated",
                     "type": "toggle"},
                ],
            },
        ],
    },

    # ── Support: open a ticket ───────────────────────────────────────────────
    #
    # THE ONE FORM THAT IS STILL A WIZARD, and deliberately.
    #
    # Every other request here is asked of somebody who knows what they want: a
    # quota, a cleaner, a project. A ticket is asked of somebody having a problem,
    # who does not know what the portal needs to hear, and for whom four short
    # questions at a time is kinder than eighteen at once. So this spec sets
    # `wizard` and the renderer shows one section per step. Nothing else differs --
    # same controls, same validation, same Hebrew labels, same requester section.
    #
    # It used to be nine hundred lines of hand-written markup with its own step
    # machinery, its own validation and its own copy of the six requester questions
    # -- the copy that drifted on all four axes and needed a guard to catch it. The
    # copy is gone; there is one of everything now.
    #
    # THE FIELD KEYS ARE THE ENDPOINT'S OWN NAMES. /api/support/tickets/create-flow
    # takes named multipart fields and creates the incident through the ServiceNow
    # record producer, which is the path that works on this instance and is not
    # being rewritten to look tidier. `submit_as: form` is what tells the renderer
    # to send it that way.
    "support_ticket": {
        "key": "support_ticket",
        "title": "Open a Ticket",
        "summary": "Raise a support ticket with the DevOps team.",
        "description": "Four short steps. Everything you type is kept if you go back.",
        "icon": "support",
        "wizard": True,
        "submit_as": "form",
        "submit_label": "Open a Ticket",
        "submit_note": "It reaches the DevOps Support team straight away — there is no approval step.",
        # WHERE THIS ONE ENDS UP, said once, here.
        #
        # A ticket is not a request: nobody approves it, it exists the moment the
        # wizard is submitted, and it is listed under My Tickets on the Support
        # page -- not under My Requests, which is where every other form in this
        # catalogue is answered. The result screen used to say "Sent for approval"
        # and link to My Requests for this form too, which sent people to a page
        # their ticket was never going to appear on.
        "done": {
            "kind": "ticket",
            "lead": "Opened with the DevOps Support team as",
            "lead_pending": "Opened with the DevOps Support team.",
            "href": "/ui/support",
            "label": "My Tickets",
        },
        "sections": [
            dict(_WHO_YOU_ARE),
            {
                "key": "service",
                "title": "Service Info",
                "description": "Which system, and why you are getting in touch.",
                "fields": [
                    {"key": "network", "label": "Network (רשת)", "type": "select",
                     "required": True,
                     "options": [{"value": v, "label": v} for v in
                                 ("סודי ביותר", "סודי", "מדן")]},
                    {"key": "devops_services", "label": "DevOps Services (שירותים)",
                     "type": "select", "required": True,
                     "options": [{"value": v, "label": v} for v in
                                 ("artifactory", "azure devops", "devops portal",
                                  "everything", "service now", "sonarqube", "other")]},
                    # Only when the answer above is Azure DevOps. Asking a SonarQube
                    # ticket which collection it is about is how a form teaches people
                    # that most of its questions do not apply to them.
                    {"key": "azure_devops_support_type",
                     "label": "Azure DevOps Support Type (סוג תמיכה)", "type": "select",
                     "visible_when": {"field": "devops_services", "equals": "azure devops"},
                     "options": [{"value": v, "label": v} for v in
                                 ("pipelines", "repository", "permissions", "others")]},
                    {"key": "azure_devops_collection",
                     "label": "Azure DevOps Collection (אוסף)", "type": "select",
                     "visible_when": {"field": "devops_services", "equals": "azure devops"},
                     "options": [{"value": v, "label": v} for v in
                                 ("devcollection-inheritance", "devcollection",
                                  "devcollection17", "tikshuvcollection-inheritance",
                                  "tikshuvcollection")]},
                    {"key": "azure_devops_project", "label": "Azure DevOps Project (פרויקט)",
                     "type": "select", "searchable": True, "allow_custom": True,
                     "options_source": "azure_devops_projects",
                     "depends_on": "azure_devops_collection",
                     "visible_when": {"field": "devops_services", "equals": "azure devops"},
                     "custom_placeholder": "MyProject"},
                    {"key": "pipeline_url", "label": "Pipeline URL (קישור Pipeline)",
                     "type": "text", "wide": True,
                     "visible_when": {"field": "azure_devops_support_type", "equals": "pipelines"}},
                    {"key": "reason", "label": "Reason (סיבת פתיחת הקריאה)",
                     "type": "select", "required": True,
                     "options": [{"value": v, "label": v} for v in
                                 ("customer issue", "usage/configuration issue",
                                  "data importing", "performance issue", "bug",
                                  "feature request", "question", "other")]},
                    {"key": "reason_other", "label": "Please specify (נא לפרט)",
                     "type": "text", "visible_when": {"field": "reason", "equals": "other"}},
                ],
            },
            {
                "key": "ticket",
                "title": "Ticket Info",
                "description": "What is wrong, and what it is stopping you doing.",
                "fields": [
                    {"key": "title", "label": "Title (כותרת)", "type": "text",
                     "required": True},
                    {"key": "urgency", "label": "Urgency (דחיפות)", "type": "select",
                     "required": True,
                     "options": [{"value": "low", "label": "Low"},
                                 {"value": "medium", "label": "Medium"},
                                 {"value": "high", "label": "High"},
                                 {"value": "urgent", "label": "Urgent"}]},
                    {"key": "description", "label": "Description (תיאור)",
                     "type": "textarea", "rows": 4, "required": True, "wide": True},
                    {"key": "work_impact", "label": "Work Impact (השפעה על העבודה)",
                     "type": "textarea", "rows": 3, "required": True, "wide": True},
                ],
            },
            {
                "key": "attachments",
                "title": "Attachments",
                "description": "Anything that shows the problem. Screenshots help most.",
                "fields": [
                    {"key": "help_text", "label": "Help Text (איך נוכל לעזור?)",
                     "type": "textarea", "rows": 3, "required": True, "wide": True},
                    {"key": "attachments", "label": "Attachments (קבצים מצורפים)",
                     "type": "file", "multiple": True, "wide": True},
                ],
            },
        ],
    },
}


# ── What the "+" button offers ───────────────────────────────────────────────

# The two things you can ask for that are NOT described by a form in this module:
# the Azure DevOps project request, which is still its own dialog, and a suggestion,
# which is not a request at all but is the other thing people arrive wanting to do.
#
# Everything else is derived from FORMS. That is the point: the "+" menu used to be a
# hand-written list of two, so every request added since simply never appeared in it,
# and nothing anywhere said the two lists were meant to agree.
_EXTRA_ACTIONS: List[Dict[str, str]] = [
    {
        "key": "ado_project",
        "label": "Request an Azure DevOps project",
        "href": "/ui/automations?new=ado_project",
        "opens": "ado",
        "icon": "azure-devops",
    },
    {
        "key": "suggestion",
        "label": "Suggest a fix or a feature",
        "href": "/ui/suggestions?new=1",
        "opens": "suggestion",
        "icon": "suggestions",
        "group": "Tell us",
    },
]


def quick_actions() -> List[Dict[str, str]]:
    """Every "create" the portal offers, for the + menu in the banner.

    Derived from FORMS rather than written out again. A menu that has to be edited
    whenever a request is added is a menu that stops being true on the first request
    somebody adds and forgets it -- which is exactly what happened: three self-service
    forms shipped and the + button went on offering the same two things it always had.

    Each entry says how to act on it. `opens` is the kind of thing behind it, so the
    banner can open it in place when the page it lives on is already the current one,
    and fall back to the href otherwise.
    """
    actions: List[Dict[str, str]] = [_EXTRA_ACTIONS[0]]
    for spec in all_forms():
        # The ticket goes to Support, which is where its answers are posted and where
        # the reply arrives. The rest live on the Requests page.
        support = spec["key"] == "support_ticket"
        actions.append({
            "key": spec["key"],
            "label": spec.get("title") or spec["key"],
            "href": ("/ui/support?new=1" if support
                     else f"/ui/automations?new={spec['key']}"),
            "opens": "support" if support else "form",
            "icon": spec.get("icon") or "",
        })
    actions.append(_EXTRA_ACTIONS[1])
    return actions


def form(key: str) -> Optional[Dict[str, Any]]:
    return FORMS.get(str(key or "").strip())


def all_forms() -> List[Dict[str, Any]]:
    return list(FORMS.values())


def fields_of(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for section in spec.get("sections") or []:
        out.extend(section.get("fields") or [])
    return out


def apply_defaults(spec: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in every default the form declares, before anything reads the answers.

    The browser seeds these when it renders, so a person who leaves a toggle alone is
    submitting its default. If the server does not do the same, an answer that was
    never touched is absent here and the two sides disagree about which sections were
    even visible -- which is how a required field ends up not required, silently, for
    exactly the requests nobody edited.
    """
    filled = dict(answers or {})
    for section in spec.get("sections") or []:
        for field in section.get("fields") or []:
            if field.get("default") is not None and field["key"] not in filled:
                filled[field["key"]] = field["default"]
    return filled


def section_visible(section: Dict[str, Any], answers: Dict[str, Any]) -> bool:
    """The same rule the browser applies, applied again on the server.

    The browser decides what to show; it does not get to decide what is required. A
    hidden section's fields are not validated -- and a caller that skips the browser
    entirely cannot make a section required by pretending it was visible.
    """
    return _rule_holds(section.get("visible_when"), answers)


def field_visible(field: Dict[str, Any], answers: Dict[str, Any]) -> bool:
    """Same rule, one level down.

    A ticket about SonarQube is not asked which Azure DevOps collection it concerns.
    Conditional FIELDS are how that works, and they need the same treatment sections
    already had: hidden means not shown, not required, and not submitted -- on the
    server as well as in the browser, because a required field nobody can see is a
    form that cannot be sent and does not say why.
    """
    return _rule_holds(field.get("visible_when"), answers)


def _rule_holds(rule: Optional[Dict[str, Any]], answers: Dict[str, Any]) -> bool:
    if not rule:
        return True
    value = answers.get(rule.get("field"))
    wanted = rule.get("equals", True)
    # A rule comparing against a STRING compares strings. Only a boolean rule gets
    # the truthiness treatment -- without this, equals="azure devops" asked whether
    # the answer was truthy and every non-empty service matched.
    if isinstance(wanted, bool):
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value) == wanted
    return str(value or "").strip().lower() == str(wanted).strip().lower()


def validate_answers(spec: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, str]:
    """field key -> what is wrong with it. Empty when the form is complete."""
    answers = apply_defaults(spec, answers)
    errors: Dict[str, str] = {}
    for section in spec.get("sections") or []:
        if not section_visible(section, answers):
            continue
        for field in section.get("fields") or []:
            if not field_visible(field, answers):
                continue
            if field.get("type") == "toggle":
                continue  # a toggle always holds a value, and False is one
            value = answers.get(field["key"])
            blank = value is None or not str(value).strip()
            if field.get("required") and blank:
                errors[field["key"]] = f"{field['label']} is required."
                continue
            # Checked on the server as well as in the browser, and with exactly the
            # same rule. The Support wizard enforces digits on the phone number with a
            # pattern attribute; a browser rule with no server rule behind it is
            # advice, not a contract, and anything not going through the browser was
            # free to send a sentence.
            if field.get("digits") and not blank and not str(value).strip().isdigit():
                errors[field["key"]] = f"{field['label']} takes digits only."
    return errors
