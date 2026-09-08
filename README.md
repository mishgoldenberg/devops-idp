<div align="center">

# 🛠️ DevOps Hub

**An internal developer portal that puts Azure DevOps, ServiceNow, SonarQube, Artifactory and Confluence behind one login — with self-service provisioning and an approval workflow on top.**

[![CI](https://img.shields.io/github/actions/workflow/status/mishgoldenberg/devops-idp/ci.yml?branch=main&style=for-the-badge&label=CI&logo=githubactions&logoColor=white)](https://github.com/mishgoldenberg/devops-idp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.120-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![HTMX](https://img.shields.io/badge/HTMX-1.9-3D72D7?style=for-the-badge&logo=htmx&logoColor=white)](https://htmx.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Helm](https://img.shields.io/badge/Deploy-Helm-0F1689?style=for-the-badge&logo=helm&logoColor=white)](https://helm.sh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## What is this?

Developers lose a surprising amount of the day to tab-switching. Your work items are in Azure DevOps, your tickets are in ServiceNow, your code quality is in SonarQube, your artifacts are in Artifactory, your runbooks are in Confluence — and each one has its own login, its own idea of who you are, and its own answer to "what needs me today?"

**DevOps Hub** is an internal developer portal that answers that question once. It signs you in through your existing identity provider, reads all five systems on your behalf, and renders a single dashboard of the things actually waiting on you: pull requests needing your review, pipelines that broke, tickets assigned to you, quality gates that failed.

It also goes the other way. Requests that normally mean filing a ticket and waiting — *create me an Azure DevOps project*, *raise my Artifactory quota*, *schedule a cleanup job for my repository* — are self-service forms that route through an approval workflow and are then **actually executed** against the target system, not just recorded as done.

The browser never talks to any external system directly. Every integration call goes through the backend, which holds the credentials, caches responses in Redis, and writes an audit record of what it did.

---

## ✨ Features

| | |
|---|---|
| 🔐 **SSO + RBAC** | OIDC sign-in (Keycloak / Red Hat SSO compatible) with a role hierarchy enforced server-side, plus local accounts as a fallback |
| 🧩 **Personal dashboards** | Twelve widgets across five systems — drag, resize, hide. Admin policy sets what *may* be shown; each user narrows it from there |
| ⚡ **Self-service that executes** | Project creation, quota increases and cleanup schedules run for real against the target system once approved |
| ✅ **Approval workflow** | Rule-driven routing, an approver inbox, and a typed request catalogue that the database enum is generated from |
| 🎫 **Ticketing built in** | Raise and track ServiceNow tickets from the portal, with answers landing in the catalog item's own fields |
| 🛡️ **Safe Mode** | A single admin toggle turns every side-effecting action into a simulated success — demo and test without touching production |
| 📊 **Observability** | Request auditing, outbound-call logging, per-integration health, and a deployment timeline showing which build reached which environment |
| 🔑 **Runtime credentials** | Integration secrets are configured in the admin UI and stored encrypted, not baked into environment variables |
| 🌍 **Bidirectional UI** | Every user-entered value renders `dir="auto"`, so RTL content sits correctly beside its label |
| ⌨️ **Keyboard-first** | Command palette, shortcut overlay, and shortcuts matched on the physical key so they survive a non-Latin layout |
| 🚀 **Deploy anywhere** | Helm umbrella chart for any Kubernetes — OpenShift, EKS, GKE, AKS — or `docker compose up` on a laptop |

---

## 🏗️ How it works

```
                         Browser
                            │
              ┌─────────────┴─────────────┐
              │  /ui/…            /api/…  │
              │  HTMX fragments   JSON    │
              └─────────────┬─────────────┘
                            ▼
              ┌───────────────────────────┐
              │   FastAPI  (one image)    │
              │  ┌─────────────────────┐  │
              │  │ auth · RBAC · audit │  │
              │  └─────────────────────┘  │
              │  26 routers, one per      │
              │  integration or feature   │
              └──┬──────────┬─────────┬───┘
                 │          │         │
       ┌─────────▼──┐  ┌────▼────┐    │
       │ PostgreSQL │  │  Redis  │    │  credentials held server-side
       │            │  │ ~60s TTL│    │  the browser never sees them
       │ users      │  │  cache  │    │
       │ approvals  │  └─────────┘    │
       │ dashboards │                 │
       │ audit      │                 ▼
       └────────────┘   ┌──────────────────────────────┐
                        │ Azure DevOps   ServiceNow    │
                        │ SonarQube      Artifactory   │
                        │ Confluence                   │
                        └──────────────────────────────┘
```

**The read path.** A dashboard widget asks the backend, not the vendor. The backend checks Redis (`ext:<integration>:<owner>:<key>`, ~60s), calls the system if it misses, and returns a rendered HTML fragment that HTMX swaps into place. A Redis outage is swallowed — the cache degrades, the portal doesn't.

**The write path.** A self-service form is declared as data in `catalog_forms.py` and rendered by one generic renderer, so a new form is a dictionary rather than a template. On submit it becomes an approval request of a typed kind; on approval an executor calls the real API — creating the Azure DevOps project, opening the pull request, raising the quota — and the request is only marked complete when that executor says so.

**The trust boundary.** Integration credentials live in the database encrypted at rest, with the key derived from the app's signing secret via HKDF. No integration token is ever returned to the frontend or written to a log.

---

## 🚀 Quick Start

**Prerequisites:** Docker and Docker Compose. Nothing else — there is no JavaScript build step.

### 1. Clone

```bash
git clone https://github.com/mishgoldenberg/devops-idp.git
cd devops-idp
```

### 2. Configure

```bash
cp env.example .env
```

Set at minimum a signing secret and the first admin account:

```bash
JWT_SECRET=<a long random string>
HUB_ADMIN_USERNAME=admin
HUB_ADMIN_PASSWORD=<a password>
```

Integrations are **not** configured here — you add them in the admin UI after signing in, and they are stored encrypted.

### 3. Run

```bash
docker compose up -d
```

| | |
|---|---|
| UI | http://localhost:8000/ui/ |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/api/health |

Sign in with the admin account from step 2. The schema creates itself on first start.

### 4. Connect a system

**Admin → Integrations →** pick a system, paste its base URL and a service credential, and press Test. The dashboard widgets for that system light up as soon as the test passes.

---

## ☸️ Deploying to Kubernetes

A Helm umbrella chart lives in [`deployment/`](deployment/). Backend and frontend are separate images: the backend renders the UI, the frontend is a thin nginx proxy in front of it.

```bash
helm upgrade --install devops-hub ./deployment \
  --namespace devops-hub --create-namespace \
  --set global.imageRepository=<your-registry>/devops-hub \
  --set global.imageTag=1.6.17 \
  --set backend.ingress.host=hub.example.com
```

Or render and apply, which is what CI does:

```bash
helm template devops-hub ./deployment -f deployment/values.yaml | kubectl apply -f -
```

```
deployment/
├── charts/backend/          FastAPI Deployment · Service · Ingress
├── charts/frontend/         nginx proxy Deployment · Service · Ingress
├── charts/infrastructure/   PostgreSQL · Redis · config · secrets
└── values.yaml              image repo, endpoints, replica counts
```

Everything a hardened cluster asks for is already set: non-root containers, `readOnlyRootFilesystem` with a writable `/tmp`, startup/liveness/readiness probes with liveness kept dependency-free, resource requests and limits, and a rolling update sized to fit inside a namespace quota.

Nothing is OpenShift-specific — it is plain Kubernetes with `Ingress`. Swap the ingress annotations for your controller and it runs on EKS, GKE, AKS or a laptop `kind` cluster.

---

## ⚙️ Configuration Reference

### Required

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_HOST` / `REDIS_PORT` | Redis endpoint for the integration cache |
| `JWT_SECRET` | Signs session tokens; also derives the key that encrypts stored credentials |
| `HUB_ADMIN_USERNAME` / `HUB_ADMIN_PASSWORD` | The bootstrap admin account |

### Commonly set

| Variable | Default | Description |
|---|---|---|
| `REDIS_PASSWORD` | — | If your Redis requires auth |
| `JWT_EXPIRY` | `8h` | How long a session lasts |
| `SAFE_MODE` | `false` | Start with every side-effecting action simulated |
| `INTEGRATION_TLS_VERIFY` | `true` | Set `false` only for internal CAs you cannot install |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_SECONDS` | `5` / `900` | Local sign-in rate limiting, per account and per client IP |
| `AUTH_ACTIVE_CHECK_TTL` | `30` | How often a live session is re-checked against account state |

Integration credentials (Azure DevOps PAT, ServiceNow service account, Artifactory token, …) are **not** environment variables. They are entered in the admin UI and stored encrypted in the database, so rotating one is a form, not a redeploy.

The full inventory of endpoints, variables and tables is generated from the code — see [`docs/env.md`](docs/env.md), [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) and [`docs/database.md`](docs/database.md), all regenerated by `python scripts/gen_docs.py`.

### Dashboard widgets

| Widget | System |
|---|---|
| Needs You · Quick Links · Recent Activity | — |
| Tasks · Pull Requests (mine) · Pull Requests (to review) · Pipelines | Azure DevOps |
| My Tickets | ServiceNow |
| Projects | SonarQube |
| Repositories · Storage *(admin)* | Artifactory |
| Pages | Confluence |

### Self-service request types

| Type | What actually happens on approval |
|---|---|
| `ADO_PROJECT_CREATE` | The project is created in the target collection over the Azure DevOps REST API |
| `ARTIFACTORY_QUOTA_INCREASE` | The repository's quota is raised |
| `ARTIFACTORY_CLEANER_CREATE` / `_UPDATE` / `_DELETE` | A retention spec is written to its repository through a pull request |

---

## 🗂️ Project Structure

```
devops-idp/
├── backend/app/
│   ├── main.py                 App factory, startup hooks, schema bootstrap
│   ├── config.py               Every setting, loaded from the environment
│   ├── ui.py                   HTMX HTML routes — the UI layer
│   ├── api/                    26 routers, one per integration or feature
│   ├── db.py                   psycopg2 pool + idempotent DDL
│   ├── cache.py                get_cached(key, ttl, producer)
│   ├── security.py             JWT sessions, RBAC helpers
│   ├── secrets_manager.py      Runtime integration credentials, encrypted
│   ├── safe_mode.py            The simulate-everything toggle
│   ├── widget_registry.py      The one dashboard widget catalogue
│   ├── catalog_forms.py        Self-service forms declared as data
│   ├── request_types.py        Request types the DB enum is built from
│   └── changelog.py            Hand-written release notes, shown in-app
├── frontend/
│   ├── templates/              Jinja2 pages and HTMX partials
│   ├── static/css/output.css   Precompiled Tailwind + DaisyUI
│   └── nginx.conf              Proxy config (mirrored in the chart)
├── deployment/                 Helm umbrella chart
├── scripts/                    Doc generation and CI guards
└── docs/                       Architecture, runbook, API reference
```

### Guards

`scripts/` holds checks that CI runs on every build, each written after a real outage:

| Guard | Refuses a build when |
|---|---|
| `check_imports.py` | A name is used but never defined, or a template fails to parse |
| `check_css_classes.py` | A template uses a class not compiled into `output.css` |
| `check_inline_js.py` | An inline script block is not valid JavaScript |
| `check_nginx_sync.py` | The image's nginx config and the chart's copy disagree |
| `check_changelog.py` | A release entry is missing, malformed, or written for developers |

---

## 🔧 Extending it

**Add a dashboard widget** — one entry in `widget_registry.py` and one HTMX component partial. The Customize drawer, the admin visibility policy and the render context all read from that entry, so there is no second list to update.

**Add a self-service form** — describe it in `catalog_forms.py`:

```python
"my_request": {
    "title": "Request something",
    "sections": [
        {"title": "What you need", "fields": [
            {"key": "project", "label": "Project", "type": "select", "source": "ado_projects"},
            {"key": "reason",  "label": "Why",     "type": "textarea", "required": True},
        ]},
    ],
}
```

One renderer draws it, one endpoint validates it against the same spec. To make it *do* something, add the type to `request_types.py` and an executor branch in `api/approvals.py` — the database enum follows on the next start.

**Add an integration** — a router in `backend/app/api/`, its reads wrapped in `integrations_cache`, its outbound calls through `resilient_http` so failures are classified rather than surfaced as `HTTPStatusError`.

---

## 🤝 Contributing

Issues and pull requests are welcome — start with **[CONTRIBUTING.md](CONTRIBUTING.md)**, which covers setup, the guards, and how to add a widget, a form or an integration.

Two house rules worth knowing before you write anything, because CI enforces both:

- **`output.css` is committed, not built in CI.** If you use a Tailwind class that isn't compiled into it, `check_css_classes.py` fails the build — rebuild it locally with `npm run build:css` in `frontend/` and commit the result, or use an inline style.
- **Every change adds a changelog entry** in `backend/app/changelog.py`, written from the user's side — the symptom they saw, not the function you fixed.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Found a security problem? Please report it privately — see [SECURITY.md](SECURITY.md).

---

## 📄 License

[MIT](LICENSE) © 2026 Michael Goldenberg
