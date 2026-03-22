# Frontend Structure

This frontend is server-rendered HTML using FastAPI templates, with DaisyUI/Tailwind classes for styling and HTMX for incremental UI loading.

## Tech Stack

- HTML templates (Jinja2 via FastAPI `TemplateResponse`)
- HTMX for partial/component loading
- Tailwind CSS + DaisyUI for component styling
- Static assets served from `/static`

## Folder Layout

```text
frontend/
  static/
    css/
      app.css
      output.css
    images/
    fonts/
  templates/
    index.html
    partials/
      components/
        quick-links.html
```

## How Rendering Works

1. Backend route `GET /ui/` renders `templates/index.html`.
2. `index.html` contains an HTMX target placeholder:
   - `hx-get="/ui/components/quick-links"`
   - `hx-trigger="load"`
   - `hx-swap="innerHTML"`
3. On page load, HTMX calls `GET /ui/components/quick-links`.
4. Backend returns `templates/partials/components/quick-links.html`.
5. HTMX injects the returned HTML into the placeholder.

This pattern keeps the main page light and lets components evolve independently.

## Component Conventions

- Put reusable UI blocks in `templates/partials/components/`.
- Keep each component self-contained where practical.
- Use DaisyUI component classes first (`card`, `menu`, `avatar`, `btn`, etc.).
- Use Tailwind utility classes for spacing/layout fine-tuning.
- Prefer semantic color tokens (`base-*`, `*-content`) for theme compatibility.

## Styling Notes

- `static/css/output.css` is the compiled stylesheet consumed by templates.
- Keep styles mostly class-based (Tailwind/DaisyUI), avoid custom CSS unless needed.
- Icon SVGs are embedded inline in components so they can be styled with CSS.

## Adding a New HTMX Component

1. Create a new partial in `templates/partials/components/`.
2. Add a backend route in `backend/python_backend/app/ui.py` that returns the partial.
3. Add an HTMX placeholder in `index.html` (or another template) with `hx-get` to that route.
4. Optionally add a loading skeleton/spinner as fallback content.
