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
    ├── base.html                   # Page shell (<html>, head, sidebar, main)
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

Admin-only pages are also hidden from the sidebar for non-admins (see
`sidebar.html`).

## Base layout

`base.html` provides:

- The HTML shell (`<head>`, `<body>`, font / theme / favicon).
- The sidebar (`partials/components/sidebar.html`) on the left.
- The banner (`partials/components/banner.html`) on top — includes the
  notifications bell, the user menu, and the **global JS error boundary**
  (a small `window.addEventListener("error", …)` + HTMX error hooks that
  shows one throttled toast instead of letting the UI die).
- A `{% block content %}` where each page slots its content.

Page templates typically extend `base.html` and fill in `content` with a
container partial plus a small inline `<script>` for page-local behavior.

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

1. Pick a unique `widget_key` (e.g. `ado_sprint_velocity`).
2. Add a row to the `widget_types` table (label, icon, min-role) via
   `deployment/charts/infrastructure/database/03_widget_types.sql` or an
   `ALTER` in an ensure helper.
3. Create a backend endpoint that renders the HTML partial, e.g.
   `GET /ui/components/azure-devops/sprint-velocity`, implemented in
   `backend/app/ui.py`.
4. Add the placeholder + `htmx.ajax` call to
   `partials/components/dashboard-container.html`.
5. Make sure the placeholder matches the shape of
   `partials/components/widget-skeleton.html` so the swap is visually
   stable.

Removing a widget is the reverse: drop the `widget_types` row, drop the
endpoint, drop the placeholder.

### Customize Dashboard drawer

The drawer in `dashboard-container.html`:

- Lists all widgets the user is allowed to see (RBAC-filtered).
- Checkbox changes hit `POST /api/dashboards/default/widgets` which
  writes to `user_widgets`.
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

## Client-side JS

We deliberately don't ship a build step for JS. Inline scripts per page
are the norm. When a script block grows past ~50 lines, split it into a
separate file under `frontend/static/js/` and `<script src="…">` it from
the template.
