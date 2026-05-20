# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Self-improvement protocol
- After any correction from me, propose a concise rule and append it to the appropriate section of this CLAUDE.md.
- Rule format: one imperative sentence, no rationale, no examples — unless the case is ambiguous.
- Before adding a rule, search this file. If an existing rule already covers the case, refine it instead of duplicating.
- Keep the total size of CLAUDE.md within 2500 tokens. If exceeded, merge overlapping rules and remove outdated ones.
- After fixing a bug, record a rule about the root-cause class, not the specific symptom.

## Code rules
- Always guard `execute_returning(...)` results with an emptiness check before indexing; raise HTTP 500 if empty.
- When a function is called in a module, verify it is present in that module's imports before writing or leaving the call.
- Any floating/fixed-position widget included from `banner.html` must be moved to a direct `<body>` child via `document.body.appendChild()` in its init script; ancestor transforms in the banner (e.g. `portal-fade-in`) will otherwise make `position:fixed` resolve relative to the banner instead of the viewport. Alternative: move the `{% include %}` out of `banner.html` and into each page template just before `</body>` — cleaner semantically but requires editing every page shell.

## What This Project Is

A "single pane of glass" DevOps portal — a FastAPI backend serving HTMX-driven Jinja2 templates. The browser never talks directly to external systems (Azure DevOps, ServiceNow, SonarQube, Artifactory, Confluence); all integration calls go through the backend, which caches responses in Redis.

## Commands

### Running the stack

```bash
# Start everything (Postgres 16, Redis 7, FastAPI backend)
docker compose up -d

# UI: http://localhost:8000/ui/
# API: http://localhost:8000/api/health
```

### Running the backend directly (without Docker)

```bash
cd backend/app
pip install -r requirements.txt
# Set required env vars (see env.example)
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend CSS

The frontend has no JS build step — HTML is served by the backend. Rebuild CSS only when changing Tailwind classes:

```bash
cd frontend
npm run build:css          # one-shot
npm run build:css:watch    # watch mode
```

### Database

```bash
npm run migrate    # run migrations
npm run seed       # seed observability data
npm run db:reset   # full reset
```

## Architecture

### Request flow

```
Browser (HTMX)
  ↓ /ui/…  → Jinja2 HTML responses
  ↓ /api/… → JSON responses
FastAPI (port 8000)
  ↓
PostgreSQL 16  — users, approvals, widgets, audit events
Redis 7        — 60s TTL cache for all external API responses
  ↓
External systems (backend only):
  Azure DevOps · ServiceNow · SonarQube · Artifactory · Confluence
  Kubernetes (Terraform Job runner) · GCP Cloud Storage (Terraform logs)
```

### Key modules

| File | Role |
|---|---|
| `backend/app/main.py` | App factory, startup hooks, `ensure_tables()` |
| `backend/app/config.py` | All settings loaded from environment |
| `backend/app/db.py` | psycopg2 connection pool + DDL helpers |
| `backend/app/cache.py` | Redis client |
| `backend/app/security.py` | JWT auth (`auth_token` cookie, HS256) |
| `backend/app/ui.py` | All HTMX HTML routes (44 KB — the main UI layer) |
| `backend/app/api/` | 25+ domain routers (one file per integration/feature) |
| `backend/app/terraform_runner.py` | Submits K8s Jobs for Terraform execution |
| `frontend/templates/` | Jinja2 page templates |
| `frontend/templates/partials/components/` | Reusable HTMX component fragments |

### Auth

OIDC (RedHat SSO compatible) configured by admin at runtime, plus internal JWT stored as `auth_token` cookie. JWT secret comes from `JWT_SECRET` env var.

### Self-service / Terraform

Terraform runs are submitted as Kubernetes Jobs (via the Python k8s client). Logs and state go to GCP Cloud Storage. Safe Mode (admin toggle) simulates actions without side effects.

### Database schema management

Tables are auto-created on startup via `ensure_tables()`. Migrations for schema changes live in `scripts/` (Node.js helpers, run via `npm run migrate`).

### Audit logging

All user actions are written to the `audit_events` table. A background task purges entries older than 7 days every 6 hours.

## Deployment

Helm umbrella chart at `deployment/`. The backend image bundles both the Python app and the compiled frontend assets.

```
deployment/
  charts/backend/         FastAPI Deployment + Service + Ingress
  charts/infrastructure/  Postgres + Redis + migration Job
  charts/ingress-nginx/   Vendored nginx ingress controller
  values.yaml             Global overrides (image repo, endpoints, replicas)
```

CI/CD (GitHub Actions) builds images, pushes to `europe-west1-docker.pkg.dev/devops-idp-489012/devops-idp`, then runs `helm upgrade`. Secrets are injected as the `all-secrets` Kubernetes Secret via `--set` flags.

## Environment variables

See `env.example` for the full list. Required at minimum:

- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_HOST`, `REDIS_PORT`
- `JWT_SECRET`
- `HUB_ADMIN_USERNAME`, `HUB_ADMIN_PASSWORD`

Integration credentials (Azure DevOps PAT, ServiceNow service account, etc.) are configured at runtime through the admin UI and stored in the database, not as env vars.
