# Templates Guide

This directory contains the HTML layer for the DevOps IDP UI.

The frontend is server-rendered with Jinja2 templates and progressively enhanced with HTMX.

## High-Level Architecture

- Page templates in this folder define the document shell (`<html>`, `<head>`, layout regions).
- Reusable UI blocks live under `partials/components`.
- The backend route layer renders page templates and component templates from `backend/python_backend/app/ui.py`.
- Styling is Tailwind + DaisyUI via `../static/css/output.css`.

## Directory Structure

- `index.html`: Home page shell.
- `login.html`: Login page.
- `azure-devops.html`, `artifactory.html`, `sonarqube.html`, `servicenow.html`, etc.: Page shells per section.
- `partials/components/`: Shared and page-specific components.
  - Layout/navigation: `banner.html`, `sidebar.html`.
  - Home container: `dashboard-container.html`.
  - Section containers: `*-container.html` (for example `azure-devops-container.html`).
  - Widget content partials: `azure-devops-tasks.html`, `pull-requests.html`, `pipelines.html`, `servicenow-tickets.html`, etc.

## Render Flow

1. A request hits a UI route (for example `/ui/azure-devops`) in `ui.py`.
2. The route renders a page shell template (for example `azure-devops.html`).
3. The page shell includes common components (sidebar/banner) and a section container partial.
4. Container partials load widget partials with HTMX using `hx-get` and `hx-trigger`.
5. Widget endpoints in `ui.py` return HTML snippets from `partials/components/*`.

## Common Template Pattern

Most page shells follow this pattern:

1. Include `output.css` and HTMX script.
2. Include widget loading-state style block used by `.widget-shell`.
3. Render a top banner via HTMX.
4. Render sidebar + one container partial in a two-column layout.

## HTMX Conventions

- Widgets generally use:
  - `hx-get="/ui/components/<widget-endpoint>"`
  - `hx-trigger="load, every 60s[, click from:#refresh-btn]"`
  - `hx-target="find .widget-content"`
  - `hx-swap="innerHTML"`
- Container refresh button convention:
  - Button id: `refresh-btn`
  - Widget triggers include `click from:#refresh-btn` when manual refresh is needed.
- Loading states use:
  - Wrapper class: `widget-shell`
  - Child placeholders: `.widget-loading` and `.widget-content`

## How To Add a New Page

1. Create a page shell in this folder (copy an existing page template).
2. Create a container partial in `partials/components/<name>-container.html`.
3. Add a route in `ui.py` for `/ui/<name>` that renders `<name>.html` and sets `current_page`.
4. Add sidebar entry in `partials/components/sidebar.html`.
5. If the page has widgets, add `/ui/components/...` routes and widget partial templates.
6. Rebuild container and verify page render, polling, and sidebar active state.

## How To Add or Change a Widget

1. Add or update widget partial HTML in `partials/components`.
2. Add or update its component route in `ui.py`.
3. Wire the widget into the right container with `hx-get`, `hx-trigger`, `hx-target`, `hx-swap`.
4. Keep the `widget-shell` + loading/content structure for consistent behavior.
5. Validate:
   - Initial load works.
   - Auto-refresh interval works (if configured).
   - Manual refresh works (if `refresh-btn` is used).

## Maintenance Rules

- Keep page shells thin: move repeated sections to partials.
- Keep component names aligned with route names for easy tracing.
- Avoid self-linking active sidebar items (active item should be non-link element).
- Preserve `current_page` values across routes and sidebar checks.
- Prefer small, isolated partial updates over editing multiple page shells.
- Reuse existing spacing, border, and card patterns for visual consistency.

## Troubleshooting Checklist

- Widget does not update:
  - Check `hx-get` endpoint exists in `ui.py`.
  - Check `hx-target` points to `.widget-content`.
  - Check `hx-trigger` includes the expected trigger (`load`, `every 60s`, `click from:#refresh-btn`).
- Widget spinner never hides:
  - Confirm widget loading CSS block exists in the page shell.
  - Confirm endpoint returns valid HTML partial.
- Sidebar active state wrong:
  - Verify route sets `current_page` correctly.
  - Verify sidebar condition names match route values exactly.

## Related Files

- Backend UI routes: `backend/python_backend/app/ui.py`
- Templates root: `frontend/templates`
- Components: `frontend/templates/partials/components`
- Compiled CSS: `frontend/static/css/output.css`
