# Frontend

The UI is rendered server-side by the FastAPI backend. There is no
separate Node / React / Vite app — what ships is Jinja templates +
[HTMX](https://htmx.org) + Tailwind CSS v4 + [DaisyUI](https://daisyui.com).

This is intentional: the portal is an internal tool with ~500 users and a
lot of live server data, so we optimize for "fewer moving parts" over
"rich SPA features".

## Where things live

```
frontend/
├── package.json                    # build:css script (Tailwind v4)
├── static/
│   ├── css/
│   │   ├── app.css                 # Tailwind source + our own classes
│   │   └── output.css              # Generated; served to the browser
│   └── images/                     # Logo, favicons
└── templates/
    ├── index.html                  # Dashboard landing page
    ├── login.html
    ├── approvals.html              # Admin: pending approvals
    ├── my-requests.html            # User's own self-service requests
    ├── observability.html          # Admin metrics page
    ├── support.html                # ServiceNow tickets
    ├── azure-devops.html           # Full ADO explorer
    ├── servicenow.html             # (older SNOW view, kept for deep links)
    ├── sonarqube.html, artifactory.html, etc.  # Per-integration pages
    ├── platform-managing.html      # Admin: Safe Mode toggle etc.
    ├── audit-logs.html             # Admin: audit trail
    ├── profile.html, settings.html # User preferences
    └── partials/
        └── components/             # HTMX-injected fragments
            ├── sidebar.html        # Left nav
            ├── banner.html         # Top bar, bell, user menu, error boundary
            ├── dashboard-container.html
            ├── widget-skeleton.html        # Reusable loading placeholder
            ├── approvals-container.html
            ├── my-requests-container.html
            ├── audit-logs-container.html
            ├── platform-managing-container.html
            ├── observability-container.html
            ├── azure-devops-container.html
            ├── servicenow-container.html
            ├── sonarqube-container.html
            └── artifactory-container.html
```

The backend mounts `frontend/static/` at `/static/` and wires Jinja to
`frontend/templates/`. See `backend/app/main.py` (`app.state.templates`
and the `StaticFiles` mount).

## Pages

Every `/ui/<page>` route in `backend/app/ui.py` does roughly the same
thing: gate on auth, pick a template under `frontend/templates/`, inject
the user object + admin flag + `current_page` (sidebar highlight) into
the context, and return an `HTMLResponse`.

| URL                     | Template                  | Purpose                                        |
| ----------------------- | ------------------------- | ---------------------------------------------- |
| `/ui/`                  | `index.html`              | Dashboard — widget grid + Customize drawer     |
| `/ui/approvals`         | `approvals.html`          | Admin queue of pending self-service requests    |
| `/ui/my-requests`       | `my-requests.html`        | User's own self-service requests                |
| `/ui/support`           | `support.html`            | ServiceNow ticket list + detail + reply         |
| `/ui/observability`     | `observability.html`      | Admin metrics (widgets / self-services / …)     |
| `/ui/azure-devops`      | `azure-devops.html`       | Full ADO explorer                               |
| `/ui/sonarqube`         | `sonarqube.html`          | Sonar integration                               |
| `/ui/artifactory`       | `artifactory.html`        | Artifactory integration                         |
| `/ui/audit-logs`        | `audit-logs.html`         | Admin: searchable audit log                      |
| `/ui/platform-managing` | `platform-managing.html`  | Admin: Safe Mode toggle etc.                     |
| `/ui/settings`          | `settings.html`           | User preferences (ADO PAT, theme)               |
| `/ui/profile`           | `profile.html`            | User profile summary                             |
| `/ui/devbot`            | `devbot.html`             | DevBot AI chat (see [devbot.md](devbot.md))      |
| `/ui/adminbot`          | `adminbot.html`           | Admin: AdminBot AI — the same page, `bot="admin"` |
| `/ui/devbot-monitor`    | `devbot-monitor.html`     | Admin: how DevBot is used, failures, feedback    |

Admin-only pages are also hidden from the sidebar for non-admins (see
`sidebar.html`).

## Page layout

There is no base template: every page in `templates/` is a complete HTML
document that includes the same shared partials, which provide:

- The HTML shell (`<head>`, `<body>`, font / theme / favicon).
- The sidebar (`partials/components/sidebar.html`) on the left.
- The banner (`partials/components/banner.html`) on top — includes the
  notifications bell, the user menu, and the **global JS error boundary**
  (a small `window.addEventListener("error", …)` + HTMX error hooks that
  shows one throttled toast instead of letting the UI die), the widgets' action
  dialog (`ado-actions.html`, `window.portalAdo.open(kind, target, {drafted})`) and
  **the big confirmation** (`action-confirm.html`, `window.portalConfirm.ask({...})`)
  that every change DevBot or AdminBot drafted ends in: what will happen, "drafted by
  AI", whose decision it is, and a tick before the button works.
Each page then includes its own container partial plus a small inline `<script>` for
page-local behaviour.

## Widget system

### Anatomy of a dashboard widget

Each widget on the dashboard is **its own server-rendered partial**. The
outer grid is declared in `dashboard-container.html` and looks roughly
like:

```html
<div id="widget-ado-work-items" class="section-card">
  <!-- HTMX placeholder -->
  {% include "partials/components/widget-skeleton.html" %}
</div>

<script>
  htmx.ajax("GET", "/ui/components/azure-devops/work-items", {
    target: "#widget-ado-work-items",
    swap: "innerHTML",
  });
</script>
```

The fetched partial:

- Does its own backend call (e.g. `api/azure-devops/work-items`).
- Renders a `section-card` with a title, body, and an action row.
- Fires `POST /api/observability/widget` once on mount so
  `widget_usage` increments.
- Handles its own error state — if the backend returned the friendly
  "Service temporarily unavailable" message, the partial shows an inline
  empty state, not a broken widget.

### Adding a widget

1. Add it to `HOME_WIDGETS` in `backend/app/widget_registry.py`: a key, the id of
   its element, the label people see in the Customize drawer, and its `system`.
   That one entry is what the drawer, the admin visibility page, Connections and
   the widget-usage counts all read.
2. Add a route that renders its partial in `backend/app/ui.py`, under
   `/ui/components/…`.
3. Add the partial under `frontend/templates/partials/components/`, built like an
   existing widget (a `section-card`, its own loading, empty and error states).
4. Add its shell to `partials/components/dashboard-container.html`: an element with
   the registry's id, `data-widget-key`, and `hx-get` pointing at the route, with
   `hx-trigger="intersect once, refresh, click from:#refresh-btn"`.
5. Use only classes already in `output.css`; it is never rebuilt in the pipeline.

Removing a widget is the reverse; `scripts/check_code_hygiene.py` will name anything
left behind in Python.

### Customize Dashboard drawer

The drawer in `dashboard-container.html`:

- Lists all widgets the user is allowed to see (RBAC-filtered).
- Checkbox changes are saved in a cookie through `/ui/dashboard/preferences`, and
  mirrored into `user_widgets` (`POST /api/dashboard/widgets/sync`) for the
  Observability page.
- "Suggested for you" and "Recently used" sections pull from
  `/api/observability/widgets/suggested` and `/widgets/recent` for
  one-click add.
- On open, top-3 most-recently-viewed widgets bubble to the front of the
  main grid (client-side DOM reorder — pure UX sugar, no backend state).

## CSS conventions

- Tailwind v4 is the styling system. DaisyUI provides the component
  base classes (`btn`, `card`, `alert`, `toggle`, etc.).
- Our own reusable classes live at the bottom of `frontend/static/css/app.css`:
  - `.section-card` — the standard card container used by every widget.
  - `.section-card-interactive` — hoverable variant.
  - `.widget-skeleton` + `.sk-line` — loading placeholders.
  - `.hoverable` — subtle background on hover for any clickable row.
- Tailwind v4 note: `@apply` **cannot** reference other custom
  component classes. If you want a new card variant, either extend the
  utility list inline or duplicate the underlying utilities.

## Building

```bash
cd frontend
npm install          # first time only
npm run build:css    # regenerates static/css/output.css
```

The CI pipeline runs the same command inside the frontend image build.

## DevBot in the widgets

Three ways into DevBot from a widget, all ending in `window.portalDevbot.ask(question)`
(defined in `banner.html`), which opens `/ui/devbot?ask=…` and asks at once:

- **Explain** on a failed run (`pipelines.html`) and **Ask DevBot** on an open work
  item (`azure-devops-tasks.html`): explicit buttons, which also work on touch and
  from the keyboard.
- **Ask DevBot on hover** (`partials/components/devbot-hover.html`): rest the pointer
  on a row for 0.6 s and the row is outlined with a chip on its top edge; Tab onto a
  row and Alt+A asks from the keyboard. The engine knows no
  widget; a row takes part by carrying its question, written with
  `window.portalDevbot.attr(question)` (escaped, and empty where DevBot is not set
  up). Rows that carry one today: work items, pipeline runs, both pull request
  widgets, tickets, every SonarQube widget, Artifactory repositories and Confluence
  pages. Settings → "Ask DevBot on hover" switches it off (`portal-devbot-hover` in
  localStorage).

## Sidebar

`sidebar.html` holds the navigation as data. An item can stand for several pages
(`match`): **Monitoring** is current on Usage, Users, DevBot and Logs, which share the
tab strip `monitoring-tabs.html`. Rows are kept compact (`portal-chrome.html`) so the
whole list, admin section included, fits a window about 900px tall without scrolling;
add a page to an existing entry's tabs before adding a row. Search is the box in the
banner.

## What's New

The text is `backend/app/changelog.py`. The first pod to start a new version on an
environment records it (`release_notes.record_current_deploy`) and posts "New in
version X" on the dashboard billboard for two days (`ANNOUNCE_DAYS`), with the
release's headline and leads (`changelog.teaser`) and a link to `/ui/changelog`. A
release with only the generic line posts nothing. Restarts and other pods post nothing,
and an admin can edit or take the note down like any announcement.

## Fast pages

What makes a page switch quick, and where it lives. Measure before changing any of it.

- **Files the browser keeps.** Pages link the stylesheet, the theme and htmx as
  `?v={{ asset_v }}`, a fingerprint of their contents taken at startup (`main.py`), and
  those are cached for a year; other `/static` files for an hour, refreshed in the
  background. A deploy that changes one changes the link.
- **Fetched before the click.** `banner.html` carries speculation rules: resting on a
  link to another `/ui/` page fetches it, and the click shows it at once. The server
  leaves "seen" marks alone for such a fetch (`Sec-Purpose: prefetch`, `ui._stamp_visit`),
  and a dotted page that arrived from one says it was opened
  (`/api/notifications/sections/{section}/seen`).
- **A crossfade, not a cut.** `theme.css` turns on cross-document view transitions;
  the banner and the sidebar hold still (named only while a transition runs, so their
  menus are never trapped under the page). Off for "reduce motion".
- **The dashboard.** Widgets below the fold load once the page has settled
  (`portal-chrome.html: prefetchBelowTheFold`, through their own `intersect` event so
  they never load twice); identical `/api/` reads in flight together share one answer
  for four seconds (any write, Refresh, or the timer's refresh forgets them);
  `portal:refreshed` fires once per refresh, not per request; sizes are applied
  only when a widget box changes. New widget content fades in (Web Animations, so a
  skipped animation still shows it).
- **Placeholders that cost nothing.** The skeleton shimmer is a transform (moved by
  the compositor), and a hidden placeholder does not shimmer.
- **Back and forward** restore from the browser's back/forward cache: do not add an
  `unload` listener, which turns it off for the page.

## Client-side JS

We deliberately don't ship a build step for JS. Inline scripts per page
are the norm. When a script block grows past ~50 lines, split it into a
separate file under `frontend/static/js/` and `<script src="…">` it from
the template.
