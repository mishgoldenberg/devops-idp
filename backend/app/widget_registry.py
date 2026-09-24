"""
The home dashboard's widget catalogue, and the admin policy for which of them
users may see at all.

WHY THIS MODULE EXISTS
----------------------
The list of dashboard widgets was written out by hand in four places: the render
context in ``ui.py``, the accepted-keys set in ``api/dashboards.py``, a JS object
in the dashboard template, and the checkbox markup of the Customize drawer. Four
copies of one list is four chances for them to disagree, and they disagree
silently — a key present in one and missing from another produces a widget that
renders but cannot be switched off, or one that can be switched on and never
appears. This module is the single source; the others read from it.

TWO LAYERS OF VISIBILITY, WHICH ARE NOT THE SAME QUESTION
---------------------------------------------------------
  * ADMIN POLICY (here): may anyone see this widget? Set by a Platform Admin in
    Platform Managing, stored in the DB, applies to every user.
  * USER PREFERENCE (the Customize drawer, a cookie): does *this* user want to
    see it? Only ever narrows what policy already allows.

Policy is enforced server-side in the render path, not by hiding markup in the
browser: a hidden widget must not be fetchable by a user who kept an old cookie,
and CSS is not an access decision.

STORAGE
-------
Reuses the ``portal_flags`` key/value table that Safe Mode created, one row per
hidden widget under the key ``widget_hidden:<widget_key>``. A missing row means
visible, so the default state needs no rows at all and a fresh database behaves
exactly as it did before this feature existed.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Set

from db import execute, query_all


log = logging.getLogger(__name__)

_FLAG_PREFIX = "widget_hidden:"

# key -> component element id + the label shown to admins and in the drawer.
# Order is the order the widgets appear in, both on the dashboard and in the lists.
HOME_WIDGETS: "OrderedDict[str, Dict[str, object]]" = OrderedDict(
    [
        # Not a grid widget — a full-width strip above everything else. It is in this
        # list anyway because the list is what the Customize drawer and the admin policy
        # page are built from: a section that cannot be switched off is the one thing
        # users ask about first. No "system": it spans all of them.
        ("needs_you", {"component": "needs-you-component", "label": "Needs You"}),
        ("quick_links", {"component": "quick-links-component", "label": "Quick Links"}),
        ("ado_my_work_items", {"component": "ado-tasks-component", "label": "Azure DevOps Tasks", "system": "azure"}),
        ("snow_my_tickets", {"component": "servicenow-tickets-component", "label": "ServiceNow Tickets", "system": "servicenow"}),
        ("ado_my_pull_requests", {"component": "pull-requests-component", "label": "Pull Requests (Opened by me)", "system": "azure"}),
        ("ado_prs_for_review", {"component": "pull-requests-review-component", "label": "Pull Requests (Need my review)", "system": "azure"}),
        ("ado_pipeline_status", {"component": "pipelines-component", "label": "Pipelines", "system": "azure"}),
        ("sonar_projects", {"component": "sonarqube-projects-component", "label": "SonarQube Projects", "system": "sonarqube"}),
        # Four of the five SonarQube widgets are views over ONE cached read
        # (/api/integrations/sonarqube/overview). They differ in what they show,
        # not in what they fetch, so adding them costs no extra calls upstream.
        ("sonar_quality_gates", {"component": "sonarqube-gates-component", "label": "SonarQube Quality Gates", "system": "sonarqube"}),
        ("sonar_new_code", {"component": "sonarqube-new-code-component", "label": "SonarQube New Code", "system": "sonarqube"}),
        ("sonar_hotspots", {"component": "sonarqube-hotspots-component", "label": "SonarQube Security Hotspots", "system": "sonarqube"}),
        ("sonar_my_issues", {"component": "sonarqube-my-issues-component", "label": "SonarQube Issues (Yours)", "system": "sonarqube"}),
        ("sonar_pr_gates", {"component": "sonarqube-pr-gates-component", "label": "SonarQube Gate on My Pull Requests", "system": "sonarqube"}),
        ("artifactory_repos", {"component": "artifactory-repos-component", "label": "Artifactory Repos", "system": "artifactory"}),
        (
            "artifactory_storage",
            {
                "component": "artifactory-storage-component",
                "label": "Artifactory Storage",
                "admin_only": True,
                "system": "artifactory",
            },
        ),
        ("confluence_pages", {"component": "confluence-pages-component", "label": "Confluence Pages", "system": "confluence"}),
        ("recent_activity", {"component": "recent-activity-component", "label": "Recent Activity"}),
    ]
)

# Widgets only usable by Platform Admins. Artifactory's /api/storageinfo is
# admin-only, so a normal user's token could never populate the storage widget.
ADMIN_ONLY_WIDGETS: Set[str] = {
    key for key, meta in HOME_WIDGETS.items() if meta.get("admin_only")
}

# Kept for the call sites that only ever wanted key -> component id.
HOME_WIDGET_KEYS: Dict[str, str] = {
    key: str(meta["component"]) for key, meta in HOME_WIDGETS.items()
}

ALL_KEYS: Set[str] = set(HOME_WIDGETS.keys())


_lock = threading.Lock()
_cached_hidden: Optional[Set[str]] = None


def _load_hidden() -> Set[str]:
    """Read the hidden set from the DB. Any failure means 'nothing hidden'."""
    try:
        rows = query_all(
            "SELECT key, value FROM portal_flags WHERE key LIKE %s",
            [_FLAG_PREFIX + "%"],
        )
    except Exception as exc:
        # A degraded database must not black out the dashboard. Failing towards
        # "everything visible" keeps the portal usable; failing the other way
        # would empty every user's home page over a transient DB blip.
        log.warning("widget_registry: hidden-widget read failed, showing all: %s", exc)
        return set()

    hidden: Set[str] = set()
    for row in rows or []:
        if not row.get("value"):
            continue
        key = str(row.get("key") or "")[len(_FLAG_PREFIX):]
        if key in ALL_KEYS:
            hidden.add(key)
    return hidden


def hidden_keys() -> Set[str]:
    """Widget keys an admin has switched off for everyone."""
    global _cached_hidden
    if _cached_hidden is not None:
        return set(_cached_hidden)
    with _lock:
        if _cached_hidden is None:
            _cached_hidden = _load_hidden()
        return set(_cached_hidden)


def invalidate_cache() -> None:
    """Force the next read to go back to the DB."""
    global _cached_hidden
    with _lock:
        _cached_hidden = None


def set_hidden(key: str, hidden: bool, *, actor: str = "system") -> bool:
    """
    Hide or show one widget for every user. Returns the stored state.
    Raises KeyError for a key that is not a real widget, so a typo in an API
    call is a 400 rather than a row nobody will ever read again.
    """
    if key not in ALL_KEYS:
        raise KeyError(key)

    execute(
        """
        INSERT INTO portal_flags (key, value, updated_by, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (key) DO UPDATE SET
          value = EXCLUDED.value,
          updated_by = EXCLUDED.updated_by,
          updated_at = CURRENT_TIMESTAMP
        """,
        [_FLAG_PREFIX + key, bool(hidden), (actor or "system")[:255]],
    )
    invalidate_cache()
    return bool(hidden)


def visible_keys(*, is_admin: bool) -> List[str]:
    """
    Every widget this user is allowed to see, in dashboard order: the catalogue
    minus what policy hides, minus admin-only widgets when the user is not one.
    """
    hidden = hidden_keys()
    return [
        key
        for key in HOME_WIDGETS
        if key not in hidden and (is_admin or key not in ADMIN_ONLY_WIDGETS)
    ]


def catalogue(*, is_admin: bool, include_hidden: bool = False) -> List[Dict[str, object]]:
    """
    The widget list, as key / label / admin_only / hidden.

    ``include_hidden`` is the difference between the screen that SETS policy and
    every screen that OBEYS it, and it defaults to obeying:

      * Platform Managing passes True — it must list a hidden widget in order to
        offer the switch that un-hides it.
      * The Customize drawer takes the default — offering a hidden widget there
        would give the user a checkbox that ticks and then changes nothing,
        because the render path drops the widget anyway.

    Defaulting the other way would mean a new caller leaks hidden widgets unless
    it remembers to opt out, which is the wrong direction for a visibility rule.
    """
    hidden = hidden_keys()
    return [
        {
            "key": key,
            "label": meta["label"],
            "component": meta["component"],
            "admin_only": bool(meta.get("admin_only")),
            "hidden": key in hidden,
        }
        for key, meta in HOME_WIDGETS.items()
        if (is_admin or not meta.get("admin_only"))
        and (include_hidden or key not in hidden)
    ]


def systems_with_visible_widgets(*, is_admin: bool) -> Set[str]:
    """
    Which integrations still have at least one widget this user can see.

    Connections exists to let people connect the systems the portal USES. Once an
    admin hides every widget belonging to a system, the portal no longer shows that
    system's data anywhere — so asking the user for a token to it is asking them to
    do setup work with no visible effect, for a tool the portal has stopped
    surfacing. The page hides those entries instead.

    Derived from the same catalogue as everything else, so hiding the last SonarQube
    widget takes SonarQube off Connections with no second list to remember.
    """
    return {
        str(HOME_WIDGETS[key]["system"])
        for key in visible_keys(is_admin=is_admin)
        if HOME_WIDGETS[key].get("system")
    }
