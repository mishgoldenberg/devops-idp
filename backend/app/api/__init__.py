"""
Aggregate router for the portal's JSON API.

Every domain has its own module under ``backend/app/api/`` which exposes an
``APIRouter`` called ``router``. This file imports all of them and mounts
them under ``/api`` with a per-domain prefix. ``backend/app/main.py`` picks
up ``api_router`` and calls ``app.include_router(api_router)``.

Adding a new domain: create ``backend/app/api/<name>.py`` with
``router = APIRouter()``, then import it here and include it below.
"""

from fastapi import APIRouter

from . import (
    admin,
    announcements,
    release_notes,
    auth,
    backups,
    catalog,
    search,
    dashboards,
    health,
    inbox,
    metrics,
    observability,
    azure_devops,
    servicenow,
    approvals,
    pins,
    notifications,
    audit_logs,
    safe_mode,
    suggestions,
    user_prefs,
    system_urls,
    favorites,
    activity,
    integrations,
    quick_links,
    usage,
    ado_actions,
    streak,
    devbot,
    confluence_actions,
)


api_router = APIRouter(prefix="/api")

# Health
api_router.include_router(health.router, prefix="/health", tags=["health"])

# Auth
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Dashboards (mounted at both /dashboards and /dashboard so the observability
# sync endpoint is reachable at /api/dashboard/widgets/sync per spec).
api_router.include_router(dashboards.router, prefix="/dashboards", tags=["dashboards"])
api_router.include_router(dashboards.router, prefix="/dashboard", tags=["dashboards"], include_in_schema=False)

# Metrics
api_router.include_router(metrics.router, prefix="/metrics", tags=["metrics"])

# Observability (Admin-only usage & integration analytics)
api_router.include_router(observability.router, prefix="/observability", tags=["observability"])

# Admin operations (grant roles, etc.)
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

# External integrations
api_router.include_router(azure_devops.router, prefix="/azure-devops", tags=["azure-devops"])
# Writes to Azure DevOps as the signed-in person, each checked and confirmed first.
api_router.include_router(ado_actions.router, prefix="/azure-devops/actions", tags=["azure-devops"])
api_router.include_router(servicenow.router, prefix="/support", tags=["support"])

# Approvals
api_router.include_router(approvals.router, prefix="/approvals", tags=["approvals"])

# Self-service catalog (the forms, their live option lists, and submissions).
api_router.include_router(catalog.router, prefix="/catalog", tags=["catalog"])

# "Needs me today" — everything across every system that is waiting on the caller.
api_router.include_router(inbox.router, prefix="/inbox", tags=["inbox"])

# User-scoped item tracking (pins + seen-markers) shared by dashboard widgets.
# Routes: GET/POST/DELETE /api/pins and POST /api/items/mark-seen.
api_router.include_router(pins.router, tags=["pins"])

# In-app notifications (bell + dropdown, fed by the approval workflow).
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])

# Admin-only: portal audit log read API + Safe Mode toggle.
api_router.include_router(audit_logs.router, prefix="/audit-logs", tags=["audit-logs"])
api_router.include_router(safe_mode.router, prefix="/safe-mode", tags=["safe-mode"])

# Admin-only: read-only status of the nightly dump and the weekly restore test.
# Read from the backup_runs table the CronJobs write into; this process never
# takes a backup itself and offers no way to trigger a restore.
api_router.include_router(backups.router, prefix="/backups", tags=["backups"])

# Per-user preferences (/api/me/*): theme, display density, avatar, display name.
api_router.include_router(user_prefs.router, prefix="/me", tags=["user-prefs"])

# External console URLs consumed by every system page's "Open" button.
api_router.include_router(system_urls.router, tags=["system-urls"])

# User-submitted suggestions (POST from Settings page, GET/PATCH admin only).
api_router.include_router(suggestions.router, prefix="/suggestions", tags=["suggestions"])

# Per-user favorites (dashboard ⭐ Favorites section). Item-level bookmark
# list; orthogonal to /api/pins (which is the widget-local sort store).
api_router.include_router(favorites.router, prefix="/favorites", tags=["favorites"])

# Per-user activity feed (dashboard Recent Activity widget). Read-only
# from the client — writes happen server-side at the three tracked call
# sites (ADO project created, ServiceNow ticket created, self-service).
api_router.include_router(activity.router, prefix="/activity", tags=["activity"])

# Per-user external integration credentials and real SonarQube/Artifactory reads.
api_router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])

# Globally visible Quick Links for the dashboard. Admin CRUD lives under /api/admin.
api_router.include_router(quick_links.router, prefix="/quick-links", tags=["quick-links"])

# Admin announcements shown on every dashboard, and the per-user "I have read it".
api_router.include_router(announcements.router, prefix="/announcements", tags=["announcements"])

# "What's New": the version this deployment is running and the changelog that
# got it here. Written once at start-up, read by everyone.
api_router.include_router(release_notes.router, prefix="/release-notes", tags=["release-notes"])

# Portal usage: the once-a-minute activity beat, and the admin Users page's reads.
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])

# The banner flame: the signed-in person's own daily streak.
api_router.include_router(streak.router, prefix="/streaks", tags=["streaks"])

# DevBot, the chat assistant: conversations, and questions answered from every
# connected system with the person's own tokens and their own model key.
api_router.include_router(devbot.router, prefix="/devbot", tags=["devbot"])
# Writes to Confluence as the signed-in person, checked and confirmed first -- the same
# dialog and the same rules as the Azure DevOps actions above.
api_router.include_router(confluence_actions.router, prefix="/confluence/actions", tags=["confluence"])

# Portal-wide search: fans out across every integration above, so it is registered last.
api_router.include_router(search.router, prefix="/search", tags=["search"])


