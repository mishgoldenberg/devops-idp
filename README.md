# DevOps Control Center

Internal developer portal that gives engineers a single place to see their
Azure DevOps work, ServiceNow tickets, SonarQube quality, and Artifactory
status, and to kick off self-service platform actions (e.g. "create me an
Azure DevOps project") through an approval workflow.

The portal is a single FastAPI app that:

- Serves the UI (HTMX + Jinja templates + Tailwind/DaisyUI) under `/ui/…`
- Exposes a JSON API under `/api/…`
- Calls external systems (Azure DevOps, ServiceNow) from the backend only —
  the browser never talks to them directly

---

## What this project is for

Before this portal, a typical engineer had to:

- Log into Azure DevOps for work items and pull requests
- Log into ServiceNow for tickets
- Log into SonarQube for code quality
- Open a ticket and wait a few days to get an ADO project provisioned
- Ask admins to toggle platform features

DevOps Control Center solves that by:

- **Aggregating** all those views into one personalizable dashboard
- **Automating** common platform requests through a self-service + approval
  workflow that ultimately runs Terraform
- **Tracking** how the platform is used (widget views, self-service usage,
  ticket volume, ADO project count) on an Observability page
- **Keeping an audit trail** of every approval/execution and making it
  searchable from the Admin section

---

## Key features

| Area             | What it does                                                                     |
| ---------------- | -------------------------------------------------------------------------------- |
| Dashboard        | Customizable widget grid (work items, PRs, tickets, pipelines, sonar, storage…) |
| Self-Service     | Request templates that run Terraform after approval (ADO project create, etc.) |
| Approvals        | Admin review queue + "My Requests" page; triggers execution on approve         |
| Support          | ServiceNow ticket list, creation, and threaded reply view                      |
| Observability    | Most-used widgets, self-service usage, ticket counts, ADO projects created     |
| Audit Logs       | Admin-only searchable log of portal events                                     |
| Notifications    | In-app bell with click-to-navigate + basic grouping                            |
| Safe Mode        | Admin toggle that simulates self-service execution without real side effects    |

More detail for each is in [`docs/`](./docs).

---

## Architecture at a glance

```
          ┌────────────────────────────────────────────────────┐
          │                    Browser (HTMX)                  │
          │    Jinja templates + Tailwind/DaisyUI + HTMX       │
          └───────────────────────┬────────────────────────────┘
                                  │  /ui/… (HTML)   /api/… (JSON)
                                  ▼
┌───────────────────────────────────────────────────────────────────┐
│                    FastAPI app (backend/app)                      │
│  Routers: auth / dashboards / approvals / azure_devops /          │
│           servicenow / sonarqube / artifactory / observability /  │
│           notifications / audit_logs / safe_mode / admin / health │
│  Cross-cutting: security.py (JWT)   db.py (psycopg2 pool)         │
│                 cache.py (Redis)    terraform_runner.py (K8s Job) │
│                 resilient_http.py   integrations_cache.py         │
└───────┬──────────────────────────────┬────────────────────────────┘
        │                              │
        ▼                              ▼
 ┌───────────────┐              ┌───────────────┐
 │  PostgreSQL   │              │     Redis     │
 │  users,       │              │  cache,       │
 │  approvals,   │              │  ext. API     │
 │  widget usage │              │  cache (60s)  │
 │  audit, …     │              └───────────────┘
 └───────────────┘

         External systems (called only from backend)
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
  │ Azure DevOps │ │  ServiceNow  │ │  SonarQube*  │ │ Artifactory* │
  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
             ┌──────────────────────────────────────┐
             │  Kubernetes (Terraform Job runner)   │
             └──────────────────────────────────────┘
```

\* SonarQube and Artifactory are currently mock-only in code.

See [`docs/architecture.md`](./docs/architecture.md) for the full picture and
the request lifecycle of a self-service action.

---

## Tech stack

- **Backend:** Python 3.11, FastAPI, Uvicorn, `psycopg2`, `httpx`, `redis-py`
- **Frontend:** Jinja2 templates, [HTMX](https://htmx.org), Tailwind CSS v4,
  [DaisyUI](https://daisyui.com) (no React, no Next.js)
- **Database:** PostgreSQL 16
- **Cache:** Redis 7 (with a 60-second layer for external-API reads)
- **Auth:** Google OAuth 2.0 / OIDC → internal HS256 JWT stored in an
  `auth_token` cookie
- **Self-service execution:** Terraform, run as a Kubernetes Job by
  `terraform_runner.py`
- **Deployment:** Helm umbrella chart under `deployment/`, GitHub Actions in
  `.github/workflows/`, container images pushed to Google Artifact Registry

---

## Repository layout

```
devops-portal/
├── backend/
│   └── app/                       # FastAPI application
│       ├── main.py                # create_app(), routers, startup hooks
│       ├── config.py              # Settings (env → typed config)
│       ├── db.py                  # psycopg2 pool + ensure_*() DDL helpers
│       ├── cache.py               # Redis get_cached/invalidate
│       ├── integrations_cache.py  # 60s cache wrapper for ADO/SNOW reads
│       ├── resilient_http.py      # Timeouts + retries + safe_integration
│       ├── security.py            # JWT create/decode + get_current_user
│       ├── safe_mode.py           # Safe-Mode toggle (env + DB flag)
│       ├── audit.py               # Audit events (new audit_events table)
│       ├── notifications.py       # (helpers) — API in api/notifications.py
│       ├── terraform_runner.py    # Submits Terraform K8s Jobs
│       ├── ui.py                  # /ui/… HTML routes (Jinja)
│       └── api/                   # FastAPI routers (see docs/services/)
├── frontend/
│   ├── templates/                 # Jinja pages + partials/components
│   └── static/                    # Tailwind output, images, icons
├── deployment/                    # Helm umbrella chart
│   └── charts/
│       ├── backend/               # Backend Deployment / Service / Ingress
│       ├── frontend/              # (legacy frontend chart; UI is served by backend)
│       ├── infrastructure/        # Postgres, Redis, migration Job, SQL
│       └── ingress-nginx/         # Vendored ingress-nginx chart
├── docs/                          # This documentation set
├── scripts/                       # Node-based DB bootstrap helpers
├── .github/workflows/             # CI: build/push images + Helm deploy
├── docker-compose.yml             # Local dev stack (optional)
└── env.example                    # Template for .env
```

---

## Local development

### Prerequisites

- Docker Desktop (Compose v2)
- `git`
- Optional for direct backend run: Python 3.11 + `pip`
- Optional for CSS changes: Node 18+ + `npm`

### Option A — Docker Compose (recommended for first run)

```bash
cp env.example .env       # Linux/macOS
# or:  Copy-Item env.example .env     (PowerShell)

docker compose up -d
# ⇒ Postgres, Redis, and the FastAPI backend/UI come up together
```

Open:

- UI: <http://localhost:8000/ui/>
- API health: <http://localhost:8000/api/health>

### Option B — Run the backend directly

```bash
cd backend/app
pip install -r requirements.txt

# Export the minimum env (Postgres + Redis must be reachable):
export DATABASE_URL=postgresql://devops:devops@localhost:5432/devops_control_center
export REDIS_HOST=localhost
export REDIS_PORT=6379
export JWT_SECRET=change-me

uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Option C — Rebuild CSS

If you change anything in `frontend/static/css/app.css` or the Jinja
templates and want the Tailwind output to be regenerated:

```bash
cd frontend
npm install
npm run build:css
```

Full environment variable reference: [`docs/env.md`](./docs/env.md).

---

## Deployment overview

Production / staging runs on GKE. The `deployment/` directory is a single
umbrella Helm chart that installs:

- `infrastructure` (PostgreSQL, Redis, migration Job, DB grants)
- `backend` (the FastAPI + UI pod)
- `frontend` (legacy — can be disabled when UI is served by backend)
- `ingress-nginx` (vendored)

Deployments are driven by `.github/workflows/deploy-workflow.yml` which
calls `template-workflow.yml`. The workflow:

1. Builds and pushes backend + frontend images to Artifact Registry.
2. Runs `helm upgrade --install devops-stack ./deployment` with secrets
   injected via `--set secrets.*="${{ secrets.* }}"`.
3. The Helm templates pull those into an `all-secrets` Kubernetes `Secret`,
   and the backend `Deployment` uses a `checksum/secrets` annotation so
   pods roll when any secret changes.

Full deployment guide: [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

Secrets handling in one sentence: **nothing secret lives in the repo** —
`env.example` documents shape only, real values come from GitHub Actions
secrets → Helm `--set` → the `all-secrets` K8s Secret → pod env.

---

## Documentation map

Start here depending on what you need:

- **New to the project?** — [`docs/onboarding.md`](./docs/onboarding.md)
- **How does it hang together?** — [`docs/architecture.md`](./docs/architecture.md)
- **What does each backend module do?** — [`docs/services/`](./docs/services/)
- **Database tables** — [`docs/database.md`](./docs/database.md)
- **Env vars** — [`docs/env.md`](./docs/env.md)
- **Self-service lifecycle** — [`docs/self-service-flow.md`](./docs/self-service-flow.md)
- **Observability metrics** — [`docs/observability.md`](./docs/observability.md)
- **ServiceNow integration** — [`docs/support.md`](./docs/support.md)
- **Frontend structure** — [`docs/frontend.md`](./docs/frontend.md)
- **Deployment** — [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)
- **API reference** — [`docs/API_REFERENCE.md`](./docs/API_REFERENCE.md)
- **Azure DevOps integration** — [`docs/AZURE_DEVOPS.md`](./docs/AZURE_DEVOPS.md)
- **Google SSO setup** — [`docs/SSO_GOOGLE_OAUTH.md`](./docs/SSO_GOOGLE_OAUTH.md)
- **Troubleshooting** — [`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md)

---

## Contributing

1. Branch off `dev` (not `main`).
2. Keep business logic unchanged unless the task explicitly calls for it.
3. Don't introduce mock data unless `SAFE_MODE=true`.
4. External calls must go through the backend — the browser must not hit
   Azure DevOps, ServiceNow, etc. directly.
5. Before opening a PR: run `npm run build:css` if you changed
   Tailwind/templates, and check `ReadLints` / `python -m py_compile` on
   Python changes.

---

## License

Internal — not licensed for external distribution.
