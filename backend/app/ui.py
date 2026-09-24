"""
HTMX UI routes.

Every ``/ui/…`` path in the browser is handled here. Two flavors coexist:

* **Pages** — full HTML documents rendered from templates under
  ``frontend/templates/``. Each page extends ``base.html`` and fills in a
  ``content`` block; the shell (sidebar, banner, notification bell) is
  shared.
* **Partials / widget endpoints** (``/ui/components/…``) — small HTML
  fragments fetched by HTMX and swapped into the DOM. These are how
  dashboard widgets load their data without a full page refresh.

This module is deliberately kept thin — it calls into the JSON ``api/…``
routers for data and leaves all business logic there. If you find yourself
writing a non-trivial query here, move it into the matching ``api`` module
instead.
"""

import hashlib
import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import audit
import identity
import login_guard
# Imported, not restated: the local sign-in and the SSO callback must decide the auth
# cookie's Secure flag the same way, or one of them silently ships it without Secure.
from api.auth import request_is_https as _forwarded_https
from api.azure_devops import default_admin_principal as _ado_default_admin
from api.azure_devops import get_pipelines as _ado_pipelines
from api.azure_devops import get_pull_requests as _ado_pull_requests
from api.azure_devops import get_work_items as _ado_work_items
from api.azure_devops import probe_user_pat as _ado_probe_pat
# The one definition of "I have already signed this off" — imported rather than
# restated so the widget and the API can never disagree about it.
from api.azure_devops import _VOTE_APPROVED as _ADO_VOTE_APPROVED
from api.notifications import mark_section_seen as _mark_section_seen
from api.dashboards import DashboardUpdateRequest
from api.dashboards import get_default_dashboard as _dash_get_default
from api.dashboards import update_dashboard as _dash_update
from api.servicenow import get_tickets as _snow_get_tickets
from db import query_one
from devbot import config as devbot_config
from secrets_manager import delete_user_azure_devops_pat, get_user_azure_devops_pat, store_user_azure_devops_pat
from security import (
    AuthUser,
    create_access_token,
    decode_access_token,
    has_effective_admin_access,
    has_effective_admin_access_live,
    is_platform_admin_level,
    verify_password,
)

ui_router = APIRouter()
log = logging.getLogger(__name__)

# The widget catalogue lives in one module now — it used to be written out by hand
# here, in api/dashboards.py, and twice more in the dashboard template.
from widget_registry import (  # noqa: E402  (kept beside the other app imports)
    ADMIN_ONLY_WIDGETS,
    HOME_WIDGET_KEYS,
)
import widget_registry


# ── The greeting ─────────────────────────────────────────────────────────────
#
# "Welcome back, X" every single time stops being a greeting after the second day and
# becomes a label. These rotate. Each entry is (prefix, suffix) around the name, which
# is what lets a line put the name anywhere in the sentence rather than always first.
#
# Kept literal and server-side: the text is never user input, and choosing it here
# rather than in the browser means the heading cannot flicker from one wording to
# another as the page loads.
# Kept SHORT on purpose. This renders at page-title size, so a long line wraps to two
# or three rows on a laptop and shoves the Refresh and Customize buttons down with it —
# a joke is not worth a heading that changes height depending on the day.
_GREETINGS: List[Tuple[str, str]] = [
    ("Welcome back, ", "!"),
    ("Good to see you, ", "."),
    ("Right then, ", "."),
    ("At your service, ", "."),
    ("Back in the saddle, ", "."),
    ("Ah, ", ". Punctual as ever."),
    ("The pipelines missed you, ", "."),
    ("Enter ", "."),
    ("Once more unto the dashboard, ", "."),
    ("Hark! ", " approaches."),
    ("Look who it is. Hello, ", "."),
    ("", ", the builds await."),
    ("A wild ", " appears."),
    ("Steady as she goes, ", "."),
    ("Deploy in haste, ", "."),
    ("Nothing is on fire, ", ". Probably."),
    ("", ", your kingdom of YAML awaits."),
    ("Salutations, ", "."),
    ("Here comes ", "."),
    ("Fear not, ", " — the pods are Running."),
]


def _pick_greeting(seed: str) -> Tuple[str, str]:
    """
    A greeting that changes, but not mid-session.

    Seeded by the person and the calendar day, so it is stable across a refresh and
    across every page load that day — a heading that reshuffles on every F5 reads as a
    glitch, not as personality — and different tomorrow.
    """
    key = f"{seed}:{datetime.utcnow().date().isoformat()}"
    index = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % len(_GREETINGS)
    return _GREETINGS[index]


def _stamp_visit(user: Dict[str, Any], section: str) -> None:
    """Clear the sidebar's "something changed" dot for a page by opening it.

    Stamped here, when the PAGE is served, rather than by a script on it: the dot
    means "you have not been here since", and being here is exactly this request.
    """
    _mark_section_seen(str(user.get("email") or ""), section)


def _is_portal_admin(request: Request) -> bool:
    """Admin flag resolved by the portal_admin_nav_context middleware."""
    return bool(getattr(request.state, "portal_is_admin", False))


def _get_templates(request: Request):
    """Retrieve the Jinja2Templates instance stored on the app state."""
    return request.app.state.templates  # type: ignore


def _format_display_name(username: str) -> str:
    """Convert a login name into a friendlier fallback display name."""
    local_part = username.split("@", 1)[0].replace(".", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in local_part.split() if part) or username


def _get_ui_user(token: str) -> Dict[str, Any]:
    """Resolve the authenticated user's display info for the UI layer.

    Returns display_name (with DB override if present) plus the per-user
    preferences the sidebar + base template rely on (avatar + theme + display
    density) and the effective role from the JWT. The DB lookup is best-effort
    so that missing preference columns never break page rendering.
    """
    payload = decode_access_token(token)
    username = str(payload.get("username", "User"))
    display_name = _format_display_name(username)
    avatar_url: Optional[str] = None
    preferred_theme: Optional[str] = None
    preferred_density: Optional[str] = None
    ticket_name = ""

    user_id = payload.get("id")
    if user_id:
        try:
            user_row = query_one(
                "SELECT full_name, avatar_url, preferred_theme, preferred_density, "
                "sso_name, username, email "
                "FROM users WHERE id = %s",
                [user_id],
            )
            if user_row:
                # What a ticket will carry: the identity provider's name, shown in
                # the ticket forms' read-only "Full name" so nobody is surprised.
                ticket_name = identity.name_from_row(user_row, email=str(payload.get("email") or ""))
                if user_row.get("full_name"):
                    display_name = str(user_row["full_name"])
                if user_row.get("avatar_url"):
                    avatar_url = str(user_row["avatar_url"])
                if user_row.get("preferred_theme"):
                    preferred_theme = str(user_row["preferred_theme"])
                if user_row.get("preferred_density"):
                    preferred_density = str(user_row["preferred_density"])
        except Exception:
            # Column may not yet exist on older DBs — the startup migration in
            # ``ensure_user_preference_columns`` adds it, but we stay resilient.
            pass

    return {
        "username": username,
        "display_name": display_name,
        "ticket_name": ticket_name or display_name,
        "avatar_url": avatar_url,
        "preferred_theme": preferred_theme,
        "preferred_density": preferred_density,
        "email": payload.get("email", ""),
        "role": payload.get("role") or "User",
    }


def _current_user_from_token(token: str) -> Optional[AuthUser]:
    """Decode cookie token to the AuthUser dict shape used by API functions."""
    if not token:
        return None
    try:
        return AuthUser(decode_access_token(token))
    except Exception:
        return None


# A real bcrypt hash of a value nobody knows, used to burn the same ~100ms on an
# unknown username as on a known one. Without it, "no such user" returns immediately
# and "wrong password" does not — which tells anyone with a stopwatch exactly which of
# the portal's two local account names is real, for free.
_TIMING_DECOY_HASH = "$2b$12$psz7TFA6lvR3.90E/cDdrORmL9bMflr8sR3s82u7fms7Ceza3XHgi"


def _local_login_payload(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate one of the env bootstrap LOCAL accounts against its password hash.

    Two accounts qualify: the Platform Admin (``is_bootstrap_admin``) and the optional
    regular service account (``is_bootstrap_user``). Everyone else signs in through SSO
    and has no password_hash at all, so they can never match here.

    The role is taken from the account's own DB role, never assumed from the fact that
    the sign-in was local — that is what keeps the second account a regular user.
    """
    log.debug("Login attempt")
    if not username or not password:
        return None
    row = query_one(
        """
        SELECT u.id, u.username, u.email, u.password_hash,
               u.is_bootstrap_admin,
               r.name AS role_name, r.hierarchy_level
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE LOWER(u.username) = %s
          AND u.is_active = true
          AND (u.is_bootstrap_admin = true OR u.is_bootstrap_user = true)
        LIMIT 1
        """,
        [username.strip().lower()],
    )
    if not row or not row.get("password_hash"):
        verify_password(password, _TIMING_DECOY_HASH)
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    role_name = str(row.get("role_name") or "")
    try:
        hierarchy_level = int(row.get("hierarchy_level") or 99)
    except (TypeError, ValueError):
        hierarchy_level = 99
    # Derived from the account's DB role and nothing else — the same source the live
    # admin check reads. The service account is a regular user because its ROW says so,
    # not because this function special-cases it.
    role = (
        "Admin"
        if is_platform_admin_level(hierarchy_level)
        or role_name.strip().lower() == "platform admin"
        else "User"
    )
    query_one("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id", [row["id"]])
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "role": role,
        "hierarchy_level": hierarchy_level,
    }



def _time_ago(iso_str: str) -> str:
    """Convert an ISO-8601 datetime string to a human-readable relative time."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ""


@ui_router.get("/ui/", response_class=HTMLResponse)
def ui_index(request: Request):
    """Render the HTMX-based UI landing page (home)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    # Resolve widget preferences from cookie — zero DB round-trip, no flash on F5.
    # Dashboard customisation is purely a UI concern now; observability is driven
    # by real widget_view events posted from the browser, not by preferences.
    is_admin = _is_portal_admin(request)

    # What this user is ALLOWED to see: the catalogue, minus anything a Platform
    # Admin has switched off for everyone, minus admin-only widgets for non-admins.
    allowed = widget_registry.visible_keys(is_admin=is_admin)
    _greeting = _pick_greeting(str(user.get("display_name") or user.get("username") or ""))

    # What they have CHOSEN to see, narrowed to what they are allowed. Intersecting
    # rather than trusting the cookie is the point: a user who had a widget enabled
    # before an admin hid it still carries it in their cookie, and a policy that only
    # filtered the drawer would leave it on their dashboard forever.
    saved = _get_home_widget_prefs_from_cookie(request)
    chosen = saved if saved else list(HOME_WIDGET_KEYS.keys())
    enabled_widgets: list = [w for w in allowed if w in chosen]

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "current_page": "home",
            "now": datetime.utcnow().isoformat() + "Z",
            "greeting": _greeting[0],
            "greeting_suffix": _greeting[1],
            "enabled_widgets": enabled_widgets,
            # Drives both the drawer's checkbox list and its JS map, so neither can
            # offer a widget the server would refuse to render.
            "widget_catalogue": widget_registry.catalogue(is_admin=is_admin),
            "widget_component_map": {
                w["key"]: w["component"]
                for w in widget_registry.catalogue(is_admin=is_admin)
            },
        },
    )


@ui_router.get("/ui/azure-devops", response_class=HTMLResponse)
def ui_azure_devops_page(request: Request):
    """Render the Azure DevOps tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "azure-devops.html",
        {
            "request": request,
            "user": user,
            "current_page": "azure-devops",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/artifactory", response_class=HTMLResponse)
def ui_artifactory_page(request: Request):
    """Render the Artifactory tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "artifactory.html",
        {
            "request": request,
            "user": user,
            "current_page": "artifactory",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/connections", response_class=HTMLResponse)
def ui_connections_page(request: Request):
    """Integration health: which systems are connected, which tokens still work.

    The portal's most common failure is a missing or silently-expired token, which
    surfaces as an empty widget somewhere and leaves the user guessing which system to
    fix. This page answers it in one place and lets them reconnect on the spot.
    """
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "connections.html",
        {
            "request": request,
            "user": user,
            "current_page": "connections",
            "now": datetime.utcnow().isoformat() + "Z",
            # Only offer to connect systems the portal still shows data from. Once an
            # admin hides every widget belonging to a system, asking for a token to it
            # is asking for setup work with no visible result anywhere. DevBot has no
            # widget: its key is offered whenever DevBot itself is set up.
            "connectable_systems": sorted(
                widget_registry.systems_with_visible_widgets(
                    is_admin=_is_portal_admin(request)
                )
                | ({"devbot"} if devbot_config.enabled() else set())
            ),
        },
    )


@ui_router.get("/ui/devbot", response_class=HTMLResponse)
def ui_devbot_page(request: Request):
    """DevBot, the chat assistant. The page loads everything it shows from /api/devbot
    with its own script, so this only renders the shell."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "devbot.html",
        {
            "request": request,
            "user": user,
            "current_page": "devbot",
            "now": datetime.utcnow().isoformat() + "Z",
            "devbot_enabled": devbot_config.enabled(),
        },
    )


@ui_router.get("/ui/search", response_class=HTMLResponse)
def ui_search_page(request: Request):
    """Full-page search results.

    The header dropdown shows a few hits per system and links here with ?q=… so the
    query survives the jump and the page can run it immediately — the user never types
    the same thing twice.
    """
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "user": user,
            "current_page": "search",
            "query": request.query_params.get("q", ""),
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/sonarqube", response_class=HTMLResponse)
def ui_sonarqube_page(request: Request):
    """Render the SonarQube tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "sonarqube.html",
        {
            "request": request,
            "user": user,
            "current_page": "sonarqube",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/servicenow", response_class=HTMLResponse)
def ui_servicenow_page(request: Request):
    """Render the ServiceNow tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "servicenow.html",
        {
            "request": request,
            "user": user,
            "current_page": "servicenow",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/support", response_class=HTMLResponse)
def ui_support_page(request: Request):
    """Render the Support page (ServiceNow tickets and conversation)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    _stamp_visit(user, "support")
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "support.html",
        {
            "request": request,
            "user": user,
            "current_page": "support",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/confluence", response_class=HTMLResponse)
def ui_confluence_page(request: Request):
    """Render the Confluence tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "confluence.html",
        {
            "request": request,
            "user": user,
            "current_page": "confluence",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/automations", response_class=HTMLResponse)
def ui_automations_page(request: Request):
    """Render the Requests page (the route keeps its /ui/automations path: renaming
    it would break every link, bookmark and notification already pointing at it)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "automations.html",
        {
            "request": request,
            "user": user,
            # Prefill the project-admin field with DOMAIN\<username> (the form the
            # AD-backed lookup resolves) when a NetBIOS domain is configured, else the
            # bare username. Editable — a full e-mail or another account also works.
            "ado_admin_default": _ado_default_admin(user.get("email") or ""),
            "current_page": "automations",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/suggestions", response_class=HTMLResponse)
def ui_suggestions_page(request: Request):
    """The suggestions board. Deliberately NOT admin-gated: a feedback board nobody
    can read is a suggestions box, which is what this used to be."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    _stamp_visit(user, "suggestions")
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "suggestions.html",
        {
            "request": request,
            "user": user,
            "current_page": "suggestions",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/changelog", response_class=HTMLResponse)
def ui_changelog_page(request: Request):
    """What's New. Signed in is the only requirement -- a changelog is for users.

    Rendered on the SERVER, unlike most of this portal's lists. The content is a
    Python module baked into the image, not an integration read: there is nothing to
    poll, nothing to cache and nothing that can be slow, so fetching it over the wire
    afterwards would only add a way for the page to arrive empty.
    """
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    import changelog
    import release_notes

    releases = changelog.releases()
    # When each version arrived HERE, which is the half the image cannot know. The
    # page renders in full without it -- see deployed_map, which never raises.
    arrived = release_notes.deployed_map()
    for entry in releases:
        entry["arrived_at"] = arrived.get(entry["version"], "")

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "changelog.html",
        {
            "request": request,
            "user": user,
            "current_page": "changelog",
            "releases": releases,
            "sections": changelog.SECTIONS,
            "running_version": changelog.version(),
            "environment": release_notes.environment(),
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/my-requests", response_class=HTMLResponse)
def ui_my_requests_page(request: Request):
    """Render the user-facing 'My Requests' page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    # Admin capability flag — powers the force-fail / delete buttons in the
    # detail modal for stuck requests. Uses the same live DB check as the
    # Approvals page so we don't drift from the backend authorization.
    is_admin = has_effective_admin_access_live(AuthUser(payload))

    _stamp_visit(user, "my-requests")
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "my-requests.html",
        {
            "request": request,
            "user": user,
            "current_page": "my-requests",
            "now": datetime.utcnow().isoformat() + "Z",
            "is_admin": is_admin,
        },
    )


@ui_router.get("/ui/approvals", response_class=HTMLResponse)
def ui_approvals_page(request: Request):
    """Render the Approvals tab page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)

    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    if not has_effective_admin_access_live(AuthUser(payload)):
        return RedirectResponse(url="/ui/", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "approvals.html",
        {
            "request": request,
            "user": user,
            "current_page": "approvals",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/observability", response_class=HTMLResponse)
def ui_observability_page(request: Request):
    """Admin-only observability dashboard."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)
    if not has_effective_admin_access_live(AuthUser(payload)):
        return RedirectResponse(url="/ui/", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "observability.html",
        {
            "request": request,
            "user": user,
            "current_page": "observability",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/platform-managing", response_class=HTMLResponse)
def ui_platform_managing_page(request: Request):
    """Admin-only platform management (e.g. grant Admin to other users)."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)
    if not has_effective_admin_access_live(AuthUser(payload)):
        return RedirectResponse(url="/ui/", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "platform-managing.html",
        {
            "request": request,
            "user": user,
            "current_page": "platform-managing",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/users", response_class=HTMLResponse)
def ui_users_page(request: Request):
    """Admin-only: every account, its role, and how much it uses the portal."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)
    if not has_effective_admin_access_live(AuthUser(payload)):
        return RedirectResponse(url="/ui/", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "user": user,
            "current_page": "users",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/audit-logs", response_class=HTMLResponse)
def ui_audit_logs_page(request: Request):
    """Admin-only Audit Logs browser."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)
    if not has_effective_admin_access_live(AuthUser(payload)):
        return RedirectResponse(url="/ui/", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "audit-logs.html",
        {
            "request": request,
            "user": user,
            "current_page": "audit-logs",
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


@ui_router.get("/ui/profile", response_class=HTMLResponse)
def ui_profile_page(request: Request):
    """Render the user profile page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "current_page": "profile",
            "email": payload.get("email", ""),
            "saved": request.query_params.get("saved", ""),
        },
    )


@ui_router.post("/ui/profile")
def ui_profile_update(request: Request, display_name: str = Form("")):
    """Update the authenticated user's display name."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        payload = decode_access_token(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    user_id = payload.get("id")
    if user_id:
        try:
            query_one(
                "UPDATE users SET full_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
                [display_name.strip() or None, user_id],
            )
        except Exception:
            pass
    return RedirectResponse(url="/ui/profile?saved=1", status_code=303)


@ui_router.get("/ui/settings", response_class=HTMLResponse)
def ui_settings_page(request: Request):
    """Render placeholder settings page."""
    token = request.cookies.get("auth_token")
    if not token:
        return RedirectResponse(url="/ui/auth", status_code=303)
    try:
        user = _get_ui_user(token)
    except HTTPException:
        return RedirectResponse(url="/ui/auth", status_code=303)

    templates = _get_templates(request)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "current_page": "settings"},
    )


_HOME_WIDGETS_COOKIE = "home_widgets"
_HOME_WIDGETS_COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 year


# Widgets introduced after the cookie format was already in the wild. A saved list
# records what is ON, so a key that did not exist when it was written is absent for
# exactly the same reason a switched-off widget is absent — the two are impossible to
# tell apart. Without this, shipping a new widget silently hides it from every existing
# user, who then reports it as missing rather than as off.
_WIDGETS_ADDED_IN_V2 = ["needs_you"]

# The SonarQube set, added once the firewall to SonarQube was opened. Same
# reasoning as V2 and the same trap: without this every existing user keeps a v2
# cookie that predates these keys, so five new widgets would ship switched off
# for everyone who has ever opened the Customize drawer -- and be reported as
# missing rather than as off.
_WIDGETS_ADDED_IN_V3 = [
	"sonar_quality_gates",
	"sonar_new_code",
	"sonar_hotspots",
	"sonar_my_issues",
	"sonar_pr_gates",
]


def _get_home_widget_prefs_from_cookie(request: Request) -> list:
    """
    Read home widget preferences from the browser cookie.

    Two shapes are accepted. ``{"v": 2, "keys": [...]}`` is current and trusted
    exactly as written. A bare ``[...]`` predates the widgets listed above, so those
    are added — this is a migration, not a default, and it happens once: the next save
    writes v2 and a user who then switches the widget off stays switched off.

    Returns the valid saved list, or [] if no cookie / invalid.
    """
    import json as _json
    raw = request.cookies.get(_HOME_WIDGETS_COOKIE, "")
    if not raw:
        return []
    try:
        prefs = _json.loads(raw)
        if isinstance(prefs, dict):
            version = int(prefs.get("v") or 0)
            keys = prefs.get("keys") if version >= 2 else None
            if isinstance(keys, list):
                saved = [k for k in keys if k in HOME_WIDGET_KEYS]
                if version < 3:
                    # Written before the SonarQube widgets existed, so their absence
                    # says nothing about what this user wants. Migrate once; the next
                    # save writes v3 and a switch-off then sticks.
                    saved = saved + [k for k in _WIDGETS_ADDED_IN_V3 if k not in saved]
                return saved
            return []
        if isinstance(prefs, list):
            saved = [k for k in prefs if k in HOME_WIDGET_KEYS]
            if not saved:
                return []
            added = _WIDGETS_ADDED_IN_V2 + _WIDGETS_ADDED_IN_V3
            return saved + [k for k in added if k not in saved]
    except Exception:
        pass
    return []


@ui_router.get("/ui/dashboard/preferences")
def ui_dashboard_preferences(request: Request):
    """Return enabled home widgets (read from cookie — kept for JS compat)."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})

    allowed = widget_registry.visible_keys(is_admin=_is_portal_admin(request))
    saved = _get_home_widget_prefs_from_cookie(request)
    chosen = saved if saved else list(HOME_WIDGET_KEYS.keys())
    # Same narrowing as the render path. Two places answering "which widgets?" must
    # answer it the same way, or the drawer and the dashboard disagree.
    return {"success": True, "data": {"enabled": [k for k in allowed if k in chosen]}}


@ui_router.post("/ui/dashboard/preferences")
def ui_dashboard_preferences_save(
    request: Request,
    payload: Dict[str, Any] = Body(default={}),
):
    """Save enabled home widgets into a browser cookie."""
    import json as _json

    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})

    requested = payload.get("enabled") or []
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="'enabled' must be a list")

    # Saving is filtered by policy too, not only reading: a stale browser tab open
    # from before a widget was hidden would otherwise write it straight back in.
    allowed = set(widget_registry.visible_keys(is_admin=_is_portal_admin(request)))
    enabled_keys = [
        k for k in requested if isinstance(k, str) and k in HOME_WIDGET_KEYS and k in allowed
    ]

    response = JSONResponse({"success": True, "data": {"enabled": enabled_keys}})
    response.set_cookie(
        _HOME_WIDGETS_COOKIE,
        # v2: an explicit list, taken literally on read. Writing the version is what
        # lets the reader tell "switched this off" apart from "wrote this before the
        # widget existed" — see _get_home_widget_prefs_from_cookie.
        _json.dumps({"v": 3, "keys": enabled_keys}),
        max_age=_HOME_WIDGETS_COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return response


@ui_router.get("/ui/azure-devops/pat")
def ui_get_ado_pat_status(request: Request):
    """Cookie-auth endpoint for PAT status used by HTMX templates."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    # Same answer as /api/azure-devops/pat, from the same cached probe. These two
    # endpoints reported different things — this one only knew whether a token was
    # STORED — so the dashboard prompt stayed quiet while the Connections page said
    # the token was dead. It is also the hook that raises the "reconnect" notice, so
    # it has to be the health check and not the existence check.
    return {"success": True, "data": _ado_probe_pat(current_user)}


@ui_router.post("/ui/azure-devops/pat")
def ui_save_ado_pat(request: Request, payload: Dict[str, Any] = Body(default={})):
    """Cookie-auth endpoint to save PAT from template widgets."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    pat = str(payload.get("pat", "")).strip()
    if len(pat) < 10:
        raise HTTPException(status_code=400, detail="PAT is too short")
    store_user_azure_devops_pat(str(current_user.get("id")), pat)
    return {"success": True}


@ui_router.delete("/ui/azure-devops/pat")
def ui_delete_ado_pat(request: Request):
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    if not current_user:
        return JSONResponse(status_code=401, content={"success": False, "detail": "Not authenticated"})
    delete_user_azure_devops_pat(str(current_user.get("id")))
    return {"success": True}


@ui_router.get("/ui/auth", response_class=HTMLResponse)
def ui_auth_page(request: Request):
    """Render the login page for username-based authentication."""
    templates = _get_templates(request)

    token = request.cookies.get("auth_token")
    if token:
        try:
            decode_access_token(token)
            return RedirectResponse(url="/ui/", status_code=303)
        except HTTPException:
            pass

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": request.query_params.get("error", ""),
            "username": request.query_params.get("username", ""),
        },
    )


@ui_router.get("/ui/auth/login")
def ui_auth_login():
    """Redirect SSO button clicks to the API auth entrypoint."""
    return RedirectResponse(url="/api/auth/login", status_code=303)


@ui_router.post("/ui/auth/login")
def ui_auth_local_login(request: Request, username: str = Form(""), password: str = Form("")):
    """Authenticate a local bootstrap account, otherwise preserve SSO fallback."""
    attempted = (username or "").strip()
    client_ip = login_guard.client_ip_of(request)

    # Refuse before touching the password at all: an account (or a source address) that
    # has spent its attempts gets no further guesses and no timing signal either.
    if login_guard.is_locked(attempted, client_ip):
        audit.log(
            audit.Action.LOGIN_FAILED,
            level=audit.Level.WARNING,
            user_email=attempted,
            metadata={
                "username": attempted, "method": "local", "reason": "locked_out",
                "client_ip": client_ip,
            },
        )
        qs = urlencode({"error": "locked_out", "username": attempted})
        return RedirectResponse(url=f"/ui/auth?{qs}", status_code=303)

    auth_user = _local_login_payload(username, password)
    if not auth_user:
        login_guard.record_failure(attempted, client_ip)
        # A failed sign-in is logged by name, at WARNING. The HTTP middleware would
        # only ever see this as a 303 redirect — a success, as far as it can tell —
        # because that is how a failed form post looks from the outside. A repeated
        # failure against one account is the single most useful thing a portal log
        # can show an admin, and it would have been invisible.
        audit.log(
            audit.Action.LOGIN_FAILED,
            level=audit.Level.WARNING,
            user_email=attempted,
            metadata={
                "username": attempted, "method": "local",
                "reason": "invalid_credentials", "client_ip": client_ip,
            },
        )
        qs = urlencode({"error": "invalid_credentials", "username": attempted})
        return RedirectResponse(
            url=f"/ui/auth?{qs}",
            status_code=303,
        )
    login_guard.record_success(attempted, client_ip)
    token = create_access_token(auth_user)
    # A local ADMIN sign-in is break-glass once SSO is live: the account's password
    # comes from a pipeline variable, never rotates, and is shared by whoever set the
    # portal up. It is the credential an attacker most wants and the one nobody
    # watches, so it is recorded at WARNING — visible in the default Logs view and in
    # the pod log — while an ordinary local sign-in stays at INFO.
    is_admin_login = str(auth_user.get("role") or "").strip().lower() == "admin"
    audit.log(
        audit.Action.LOGIN_SUCCEEDED,
        level=audit.Level.WARNING if is_admin_login else audit.Level.INFO,
        user_email=auth_user.get("email") or attempted,
        metadata={
            "what": (
                "Signed in with the local ADMIN account (break-glass)"
                if is_admin_login else "Signed in locally"
            ),
            "username": attempted, "method": "local",
            "role": auth_user.get("role"), "client_ip": client_ip,
            "break_glass": is_admin_login,
        },
    )
    response = RedirectResponse(url="/ui/", status_code=303)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        samesite="lax",
        # TLS terminates at the proxy, so the backend hop is plain HTTP and
        # request.url.scheme is always "http" in the cluster — reading it here left the
        # session cookie without Secure on every real deployment. X-Forwarded-Proto is
        # the browser-facing scheme; this is the same rule /api/auth already follows.
        secure=_forwarded_https(request),
        path="/",
    )
    return response


@ui_router.post("/ui/auth/logout")
def ui_auth_logout(request: Request):
    """Clear auth cookie and return user to login page."""
    try:
        payload = decode_access_token(request.cookies.get("auth_token") or "")
        audit.log(
            audit.Action.LOGOUT,
            user_email=(payload or {}).get("email"),
            metadata={"method": "local"},
        )
    except Exception:
        # An expired or malformed cookie is not a reason to refuse to sign someone out.
        pass
    response = RedirectResponse(url="/ui/auth", status_code=303)
    response.delete_cookie("auth_token")
    return response


@ui_router.get("/ui/components/banner", response_class=HTMLResponse)
def ui_banner_component(request: Request):
    """Render the top banner for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/banner.html",
        {"request": request},
    )


@ui_router.get("/ui/components/sidebar", response_class=HTMLResponse)
def ui_sidebar_component(request: Request):
    """Render the navigation sidebar for HTMX partial loading."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/sidebar.html",
        {"request": request},
    )


@ui_router.get("/ui/components/quick-links", response_class=HTMLResponse)
def ui_quick_links_component(request: Request):
    """Render the Quick Links dashboard component for HTMX partial loading.

    No admin check any more. The widget used to carry the add/edit/delete UI and so
    needed to know whether you were an admin — which cost a live privilege lookup on
    every dashboard load, for every user, to decide whether to draw three buttons.
    Managing quick links now lives in Platform Managing, so this component just shows
    the links, and the lookup is gone with the buttons that needed it.
    """
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/quick-links.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


def _describe_ado_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail.strip():
            return detail
    return "Unable to load Azure DevOps data right now."


def _ado_needs_pat(error: str) -> bool:
    """Only show the PAT prompt for the actual missing-token error."""
    return "not connected" in (error or "").lower() or "personal access token" in (error or "").lower()


def _get_ado_task_counts(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    empty_counts: Dict[str, int] = {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0}
    if not current_user:
        return {"counts": empty_counts, "error": "Sign in to load your Azure DevOps tasks."}
    try:
        # Work-items endpoint accepts optional project filter (not username).
        result = _ado_work_items(project=None, current_user=current_user)
        items = result.get("data", []) if isinstance(result, dict) else []
        counts: Dict[str, int] = {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0}
        state_map = {
            "to do": "todo",
            "new": "todo",
            "proposed": "todo",
            "active": "in_progress",
            "in progress": "in_progress",
            "committed": "in_progress",
            "doing": "in_progress",
            "resolved": "done",
            "done": "done",
            "closed": "done",
            "completed": "done",
            "blocked": "blocked",
            "impediment": "blocked",
        }
        for item in items:
            bucket = state_map.get((item.get("state") or "").lower(), "todo")
            counts[bucket] += 1
        return {"counts": counts, "error": ""}
    except Exception as exc:
        return {"counts": empty_counts, "error": _describe_ado_error(exc)}


@ui_router.get("/ui/components/azure-devops-tasks", response_class=HTMLResponse)
def ui_azure_devops_tasks_component(request: Request):
    """Render the Azure DevOps Tasks dashboard widget for HTMX partial loading."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    templates = _get_templates(request)
    widget_state = _get_ado_task_counts(current_user)
    return templates.TemplateResponse(
        "partials/components/azure-devops-tasks.html",
        {
            "request": request,
            "counts": widget_state["counts"],
            "error": widget_state["error"],
            "needs_pat": _ado_needs_pat(widget_state["error"]),
            "now": datetime.utcnow().isoformat() + "Z",
        },
    )


def _get_pr_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    if not current_user:
        return {"prs": [], "error": "Sign in to load your Azure DevOps pull requests."}
    try:
        result = _ado_pull_requests(username=None, current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        return {
            "prs": [
                {
                    "id": pr.get("id"),
                    "title": pr.get("title"),
                    "repository": pr.get("repository"),
                    "collection": pr.get("collection", ""),
                    "project": pr.get("project", ""),
                    "source_branch": pr.get("source_branch"),
                    "target_branch": pr.get("target_branch"),
                    "age": _time_ago(pr.get("created_date", "")),
                    "created_date": pr.get("created_date", ""),
                    "url": pr.get("url", ""),
                    "created_by_email": pr.get("created_by_email", ""),
                    "is_creator": bool(pr.get("is_creator")),
                    "is_reviewer": bool(pr.get("is_reviewer")),
                    "my_vote": int(pr.get("my_vote") or 0),
                }
                for pr in rows
            ],
            "error": "",
        }
    except Exception as exc:
        return {"prs": [], "error": _describe_ado_error(exc)}


def _get_pr_created_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    """PRs the signed-in user opened.

    Trusts the API's ``is_creator`` flag. This used to compare the PR's
    ``created_by_email`` against the portal username as raw lowercase strings — but
    on-prem Azure DevOps writes an author as ``DOMAIN\\user`` while the portal knows
    the user by email, so the comparison never matched and this widget was ALWAYS
    empty, even for a PR the user had just opened. The API already resolves identity
    across both namespaces; re-deriving it here only reintroduced the bug.
    """
    state = _get_pr_data(current_user)
    if state.get("error"):
        return state
    created = [pr for pr in state.get("prs", []) if pr.get("is_creator")]
    return {"prs": created, "error": ""}


def _get_pr_review_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    """PRs still waiting on this user's review.

    A PR the user has already approved is not waiting on them, so it is dropped.
    Rejected, waiting-for-author and not-yet-voted all remain — those still want
    something from the reviewer.
    """
    state = _get_pr_data(current_user)
    if state.get("error"):
        return state
    review = [
        pr for pr in state.get("prs", [])
        if pr.get("is_reviewer") and int(pr.get("my_vote") or 0) < _ADO_VOTE_APPROVED
    ]
    return {"prs": review, "error": ""}


def _get_pipeline_data(current_user: Optional[AuthUser]) -> Dict[str, Any]:
    if not current_user:
        return {"runs": [], "error": "Sign in to load your Azure DevOps pipelines."}
    try:
        result = _ado_pipelines(project=None, current_user=current_user)
        rows = result.get("data", []) if isinstance(result, dict) else []
        mapped = [
                {
                    "name": run.get("name", ""),
                    "project": run.get("project", ""),
                    "result": run.get("result", "in progress"),
                    "age": _time_ago(run.get("created_date", "")),
                    "url": run.get("url", ""),
                }
                for run in rows
            ]
        return {
            "runs": mapped[:1],
            "error": "",
        }
    except Exception as exc:
        return {"runs": [], "error": _describe_ado_error(exc)}


@ui_router.get("/ui/components/pull-requests", response_class=HTMLResponse)
def ui_pull_requests_component(request: Request):
    """Render the Pull Requests dashboard widget for HTMX partial loading."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    templates = _get_templates(request)
    widget_state = _get_pr_created_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pull-requests.html",
        {
            "request": request,
            "prs": widget_state["prs"],
            "error": widget_state["error"],
            "needs_pat": _ado_needs_pat(widget_state["error"]),
        },
    )


@ui_router.get("/ui/components/recent-activity", response_class=HTMLResponse)
def ui_recent_activity_component(request: Request):
    """
    Render the Recent Activity dashboard widget.

    The partial self-fetches from ``/api/activity`` on mount, so this
    handler just returns the shell — no server-side DB hit — keeping
    parity with how servicenow-tickets.html / azure-devops-tasks.html
    load their data after the skeleton swap.
    """
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/recent-activity.html",
        {"request": request},
    )


@ui_router.get("/ui/components/pull-requests-review", response_class=HTMLResponse)
def ui_pull_requests_review_component(request: Request):
    """Render the PRs-to-review dashboard widget."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    templates = _get_templates(request)
    widget_state = _get_pr_review_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pull-requests-review.html",
        {
            "request": request,
            "prs": widget_state["prs"],
            "error": widget_state["error"],
            "needs_pat": _ado_needs_pat(widget_state["error"]),
        },
    )


@ui_router.get("/ui/components/pipelines", response_class=HTMLResponse)
def ui_pipelines_component(request: Request):
    """Render the Pipelines dashboard widget for HTMX partial loading."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    templates = _get_templates(request)
    widget_state = _get_pipeline_data(current_user)
    return templates.TemplateResponse(
        "partials/components/pipelines.html",
        {
            "request": request,
            "runs": widget_state["runs"],
            "error": widget_state["error"],
            "needs_pat": _ado_needs_pat(widget_state["error"]),
        },
    )


def _sonar_web_base() -> str:
	"""SonarQube's own URL, for links out of the widgets, or "".

	Empty is a real state: an installation that has not configured SonarQube has
	nowhere to send anybody, and the widgets render the project name WITHOUT a link
	rather than one that 404s. A link to something that does not exist teaches
	people to stop checking whether links work.
	"""
	return (os.getenv("SONARQUBE_BASE_URL") or "").strip().rstrip("/")


def _sonar_widget(request: Request, mode: str):
	"""Every SonarQube widget is one partial, scoped by a root attribute.

	Six widgets that each carried their own row, marker and controls offered six
	different sets of things to do. One partial means one search, one filter, one
	pin and one expanding card everywhere; the modes differ only in configuration.
	See partials/components/sonarqube-widget.html.
	"""
	templates = _get_templates(request)
	return templates.TemplateResponse(
		"partials/components/sonarqube-widget.html",
		{"request": request, "mode": mode, "sonar_base": _sonar_web_base()},
	)


@ui_router.get("/ui/components/sonarqube-projects", response_class=HTMLResponse)
def ui_sonarqube_projects_component(request: Request):
	"""SonarQube Projects -- every project, its gate and its numbers."""
	return _sonar_widget(request, "projects")


@ui_router.get("/ui/components/sonarqube-gates", response_class=HTMLResponse)
def ui_sonarqube_gates_component(request: Request):
	"""Quality Gates -- projects by gate result, failing first."""
	return _sonar_widget(request, "gates")


@ui_router.get("/ui/components/sonarqube-new-code", response_class=HTMLResponse)
def ui_sonarqube_new_code_component(request: Request):
	"""New Code -- what landed in the current period."""
	return _sonar_widget(request, "newcode")


@ui_router.get("/ui/components/sonarqube-hotspots", response_class=HTMLResponse)
def ui_sonarqube_hotspots_component(request: Request):
	"""Security Hotspots -- projects with reviews outstanding."""
	return _sonar_widget(request, "hotspots")


@ui_router.get("/ui/components/sonarqube-my-issues", response_class=HTMLResponse)
def ui_sonarqube_my_issues_component(request: Request):
	"""Issues on lines this user last touched."""
	return _sonar_widget(request, "issues")


@ui_router.get("/ui/components/sonarqube-pr-gates", response_class=HTMLResponse)
def ui_sonarqube_pr_gates_component(request: Request):
	"""The SonarQube gate on this user's open pull requests."""
	return _sonar_widget(request, "prs")


@ui_router.get("/ui/components/artifactory-storage", response_class=HTMLResponse)
def ui_artifactory_storage_component(request: Request):
    """Render the Artifactory Storage dashboard widget for HTMX partial loading.

    Admin-only: /api/storageinfo requires an admin-scoped token, so the widget
    is hidden from non-admins and the endpoint refuses them directly too.
    """
    if not _is_portal_admin(request):
        return HTMLResponse("", status_code=403)
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/artifactory-storage.html",
        {"request": request},
    )


@ui_router.get("/ui/components/artifactory-repos", response_class=HTMLResponse)
def ui_artifactory_repos_component(request: Request):
    """Render the Artifactory repositories widget; the storage widget is separate."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/artifactory-repos.html",
        {"request": request},
    )


@ui_router.get("/ui/components/confluence-pages", response_class=HTMLResponse)
def ui_confluence_pages_component(request: Request):
    """Render the Confluence Pages dashboard widget; data loads client-side."""
    templates = _get_templates(request)
    return templates.TemplateResponse(
        "partials/components/confluence-pages.html",
        {"request": request},
    )


@ui_router.get("/ui/components/servicenow-tickets", response_class=HTMLResponse)
def ui_servicenow_tickets_component(request: Request):
    """Render the ServiceNow Tickets dashboard widget for HTMX partial loading."""
    current_user = _current_user_from_token(request.cookies.get("auth_token", ""))
    templates = _get_templates(request)
    stats = {"open": 0, "in_progress": 0, "resolved": 0, "total": 0}
    error = ""
    if current_user:
        try:
            res = _snow_get_tickets(current_user=current_user)
            tickets = (res.get("data") or []) if isinstance(res, dict) else []
            for t in tickets:
                state = str(t.get("state", "")).lower()
                stats["total"] += 1
                if state == "new":
                    stats["open"] += 1
                elif state == "in progress":
                    stats["in_progress"] += 1
                elif state in {"resolved", "closed"}:
                    stats["resolved"] += 1
        except Exception as exc:
            if isinstance(exc, HTTPException):
                detail = exc.detail
                error = detail if isinstance(detail, str) else "ServiceNow unavailable"
            else:
                error = "ServiceNow unavailable"
    return templates.TemplateResponse(
        "partials/components/servicenow-tickets.html",
        {
            "request": request,
            "now": datetime.utcnow().isoformat() + "Z",
            "stats": stats,
            "error": error,
        },
    )
