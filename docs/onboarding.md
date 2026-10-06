# Developer onboarding

Welcome. This document is the one-stop guide to get you productive on
DevOps Control Center in under an hour.

If something here is wrong, fix it in this file — it's the doc everyone
reads first.

---

## 1. Get the code running (10 min)

### Prerequisites

- Docker Desktop with Compose v2.
- `git`.
- Python 3.11 and Node 18+ are **only** required if you want to run the
  backend / rebuild Tailwind outside of Docker.

### Boot everything

```bash
git clone git@github.com:mishgoldenberg/devops-idp.git devops-portal
cd devops-portal
cp env.example .env                              # Linux/macOS
# or:  Copy-Item env.example .env                (PowerShell)

docker compose up -d                             # postgres, redis, backend
docker compose logs -f api-gateway               # watch it come up
```

Open:

- UI: <http://localhost:8000/ui/>
- API health: <http://localhost:8000/api/health>

`env.example` turns on mocks for every external system, so the UI works
without real Azure DevOps / ServiceNow credentials. You'll see canned
data.

### When things look wrong

- Backend pod loops? → `docker compose logs -f api-gateway`. Most startup
  crashes are DB-related; check that Postgres is `healthy`.
- UI loads but widgets show errors? → the backend is up but can't reach
  mocks (unlikely) or the user isn't logged in (see next step).

### Login

Use `HUB_ADMIN_USERNAME` and `HUB_ADMIN_PASSWORD` to create the first local
admin, then configure OIDC from Platform Managing. For quick local testing,
set the dev bypass env (`DEV_MODE_BYPASS_AUTH=true`,
`DEV_MODE_DEFAULT_USER=admin@internal`) in `.env` and restart — you'll be
logged in as `admin@internal` with Platform Admin role. **Never** enable
this flag anywhere except your laptop.

---

## 2. How the code is organized (5 min)

See [`README.md`](../README.md) for the full layout. The short version:

- `backend/app/main.py` — FastAPI `create_app()`. Registers routers,
  wires startup hooks, mounts `/static`, templates at `/ui/…`.
- `backend/app/api/*.py` — one router per domain; list in
  `backend/app/api/__init__.py`.
- `backend/app/ui.py` — HTMX HTML routes under `/ui/…`.
- `backend/app/db.py` — psycopg2 pool + `ensure_*()` DDL helpers.
- `frontend/templates/` — Jinja pages + partials.
- `deployment/` — Helm umbrella chart for production.

Spend ~10 min opening each of those files and reading the top of them.

---

## 3. Connect to test APIs (10 min)

### Azure DevOps

To hit a real dev org:

1. Create a PAT in your personal ADO org (scope: `Project & Team
   (Read/Write)`, `Work Items (Read)`, `Code (Read)`, `Build (Read)`).
2. In your running container's `.env`:

   ```env
   AZURE_DEVOPS_BASE_URL=https://dev.azure.com/yourorg
   AZURE_DEVOPS_ADMIN_PAT=<your PAT>
   ```

3. Restart the backend:
   `docker compose restart api-gateway`.

4. The home dashboard's ADO widgets should now show your real data.

### ServiceNow

- Use a **developer instance** from
  <https://developer.servicenow.com/> (free).
- Add to `.env`:

  ```env
  SNOW_BASE_URL=https://devXXXXXX.service-now.com
  SNOW_API_USERNAME=admin
  SNOW_API_PASSWORD=<your dev instance password>
  ```

- Open `/ui/support` and create a ticket — it should land in the dev
  instance's Incidents table.

### SonarQube / Artifactory / Confluence

Set `SONARQUBE_BASE_URL`, `ARTIFACTORY_BASE_URL` and `CONFLUENCE_BASE_URL`, then connect
your own token for each on the Connections page. SonarQube's public projects show even
without a token.

### DevBot

Set `DEVBOT_LLM_BASE_URL` (an OpenAI-compatible `/v1`) and `DEVBOT_DEFAULT_MODEL`, then
connect your AI key on the DevBot page. See [devbot.md](./devbot.md).

---

## 4. Add a new widget (20 min — example task)

This is the canonical "I'm new, make me productive" task. We'll add a
widget that shows the number of tickets the user opened in the last
7 days, for example.

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

5. Rebuild Tailwind if you used new classes, and commit `output.css`:
   `cd frontend && npm run build:css`.

Removing a widget is the reverse; `scripts/check_code_hygiene.py` will name anything
left behind in Python.

---

## 5. Add a new self-service (30 min — example task)

This is the "I want to be dangerous" task.

1. **Pick a `request_type`**: e.g. `SONAR_PROJECT_CREATE`.
2. **Extend the enum** in
   `deployment/charts/infrastructure/database/00_schema.sql` (or use
   `ALTER TYPE … ADD VALUE IF NOT EXISTS` in an ensure helper so you
   don't need a migration).
3. **Teach the UI about it**:
   - Add a card to the self-services page with a form.
   - Form POSTs to the existing `POST /api/approvals/requests` endpoint
     with `request_type: "SONAR_PROJECT_CREATE"` and a `request_payload`
     matching the schema you've decided on.
4. **Add input validation** in
   `approvals.py::_validate_request_payload` for the new type (name
   regex, required fields).
5. **Wire the executor**: add an `elif request_type ==
   "SONAR_PROJECT_CREATE":` branch in `_execute_request_worker` (or a
   dedicated `_execute_sonar_project_create`). The executor must:
   - Respect Safe Mode (the short-circuit at the top of the worker
     already covers this).
   - Call external systems only via the backend (use
     `resilient_http.resilient_request`).
   - Emit audit events via `audit.log(Action.…)`.
   - Fire a completion/failure notification.
6. **Wire observability**: update
   `observability_tracking.record_self_service(...)` to handle the new
   type if it needs its own counter.
7. **Add admin approval UI**: the existing Approvals page reads all
   request types, so you only need to make sure the request renders a
   sensible summary in the card.

---

## 6. Testing and quality gates

- **Python**:
  - `python -m py_compile backend/app/**/*.py`
  - `ruff`/`flake8` if configured (check `backend/app/Dockerfile`).
- **Frontend**:
  - `npm run build:css` must succeed.
- **End-to-end**: run `docker compose up -d`, then manually walk the
  path you changed (dashboard → submit request → approve → observe
  status).
- **CI**: `azure-pipelines.yml` runs the guards, builds both images and deploys;
  `.github/workflows/ci.yml` runs the same guards on GitHub and deploys nothing.

---

## 7. Common gotchas

- **"attempted relative import with no known parent package"** — you
  ran `python backend/app/main.py` directly. Use
  `uvicorn main:app --app-dir backend/app` or Docker.
- **Widgets load forever** — you probably forgot the `htmx.ajax(...)`
  call for the new widget, or the new endpoint returns 500.
- **Cache seems stale** — see
  `backend/app/integrations_cache.py`. The TTL is 60 s; write paths
  should call `invalidate_owner(...)` immediately after a successful
  write.
- **My changes aren't in the running pod** — you forgot to rebuild the
  backend image or (in Compose) to restart the service:
  `docker compose restart api-gateway`.
- **Admin pages 403** — your user row's role isn't admin. Either change
  `role_id` in Postgres manually or sign in as the seeded admin user.

---

## 8. Where to go next

- [`docs/architecture.md`](./architecture.md) — the big picture.
- [`docs/devbot.md`](./devbot.md) — the AI assistant and its search index.
- [`docs/self-service-flow.md`](./self-service-flow.md) — the canonical
  flow.
- [`docs/services/`](./services/) — one doc per backend module.
- [`docs/database.md`](./database.md) — every table we own.
- [`docs/env.md`](./env.md) — every environment variable.

Happy hacking.
