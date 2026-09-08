# Contributing to DevOps Hub

Thanks for taking the time. This document is short on ceremony and specific about
the few things that will otherwise fail your build.

## Getting set up

```bash
git clone https://github.com/mishgoldenberg/devops-idp.git
cd devops-idp
cp env.example .env          # set JWT_SECRET and the HUB_ADMIN_* pair
docker compose up -d
```

The UI is at http://localhost:8000/ui/. The schema creates itself on first start,
so there is no migration step to run.

To work on the backend without Docker:

```bash
cd backend/app
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Before you open a pull request

Run the guards. CI runs exactly these, and each of them exists because something
got past review once:

```bash
python scripts/check_imports.py          # names resolve, templates parse
python scripts/check_css_classes.py      # no class missing from output.css
python scripts/check_inline_js.py        # inline <script> blocks are valid JS
python scripts/check_nginx_sync.py       # both copies of nginx.conf agree
python scripts/check_requester_fields.py # the shared form section is not duplicated
python scripts/check_changelog.py        # the changelog entry is present and readable
```

## Three things that trip people up

### 1. `output.css` is committed, not built in CI

There is no JavaScript build step in the pipeline. `frontend/static/css/output.css`
is a compiled artifact that ships as-is, so **a Tailwind class that is not already
in it does nothing at all** — silently. `check_css_classes.py` fails the build
rather than let that reach a page.

If you need a class that isn't compiled:

```bash
cd frontend
npm run build:css      # local only
```

…and commit the regenerated `output.css`. If you would rather not, use an inline
`style` attribute — that always works.

### 2. Every change adds a changelog entry

`backend/app/changelog.py` is written by hand and rendered in the app, so it is
addressed to whoever *uses* the portal, not to whoever reviews the diff.

```python
# good — the symptom somebody saw
"Pipelines you do not own no longer appear in your list."

# rejected by check_changelog.py — this is a commit subject
"fix: filter ado_pipeline_status by owner in dashboards.py"
```

No file names, no function names, no HTTP status codes. Pick the bump
deliberately: **major** if people have to relearn something, **minor** for a new
capability, **patch** for everything else.

Work that only an administrator can see is one generic line rather than an
inventory of the admin pages — the release still happened and still needs a
version, but the What's New page is for users.

### 3. Keep a list that two layers must agree on in one module

Dashboard widgets live in `widget_registry.py`, self-service forms in
`catalog_forms.py`, request types in `request_types.py`. Import them. Copying a
list is how the two copies disagree silently — a widget that renders but cannot be
switched off, or a request type the database has never heard of.

## Adding things

**A dashboard widget** — one entry in `backend/app/widget_registry.py` and one
component partial under `frontend/templates/partials/components/`. The Customize
drawer, the admin visibility policy and the render context all read from that
entry.

**A self-service form** — describe it as data in `backend/app/catalog_forms.py`.
One renderer draws it and one endpoint validates it against the same spec. To make
it *do* something on approval, add the type to `request_types.py` and an executor
branch in `api/approvals.py`; the database enum follows on the next start.

**An integration** — a router in `backend/app/api/`, reads wrapped in
`integrations_cache`, outbound calls through `resilient_http` so a failure is
classified (refused, DNS, TLS, 401, 404, timeout) rather than surfaced as a bare
exception class.

## Style

- Python is formatted the way the file around it is formatted. There is no
  enforced formatter; match the neighbours.
- Comments explain **why**, not what. Several of the more surprising decisions in
  this codebase have a paragraph above them explaining the failure that produced
  them — that is deliberate, and worth continuing.
- Never return an integration credential to the frontend or write one to a log.
- A password is Basic auth. Only a token or an API key belongs in a bearer or
  API-key header.

## Commits and pull requests

Conventional-ish subjects (`fix:`, `feat:`, `docs:`, `chore:`) are appreciated but
not enforced. What matters in the pull request body is: what was wrong, what you
changed, and how you checked it.

If it changes the UI, include a screenshot. If it changes behaviour against an
external system, say which system and how you tested — a mock counts, but say
that it was a mock.

## Reporting bugs and asking for features

Use the issue templates. For anything security-related, do **not** open an
issue — see [SECURITY.md](SECURITY.md).
