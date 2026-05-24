<div align="center">

# 🛠️ DevOps Hub

**Self-hosted developer portal that unifies Azure DevOps, SonarQube, Artifactory, ServiceNow, and Confluence behind a single role-aware dashboard — with approval workflows and Kubernetes-powered self-service.**

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Helm](https://img.shields.io/badge/Helm-Kubernetes-0F1689?style=for-the-badge&logo=helm&logoColor=white)](https://helm.sh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## What is this?

Engineering teams that rely on Azure DevOps, SonarQube, Artifactory, ServiceNow, and Confluence spend their day switching between five separate UIs with no central way to request access, track approvals, or monitor the health of all their tools. DevOps Hub is a self-hosted web portal that surfaces all of those systems in one place: a role-aware, widget-based dashboard each user configures for themselves.

When a developer needs a new Azure DevOps project, Confluence space, or SonarQube licence, they submit a self-service request through the portal; it flows through a configurable approval chain and is provisioned automatically by a Terraform Kubernetes Job on approval. All browser traffic goes exclusively through the FastAPI backend — no credentials are ever sent to the browser or stored in logs. Every action is written to an append-only audit log.

---

## ✨ Features

| | |
|---|---|
| 📊 **Role-aware dashboard** | Widget layout adapts to the user's role — 7-level RBAC hierarchy from Platform Admin to Regular User |
| 🔗 **Five integrations** | Azure DevOps, SonarQube, Artifactory, ServiceNow, and Confluence — all responses cached in Redis |
| ✅ **Approval workflows** | Self-service requests route through a configurable approver chain; executed automatically on approval |
| 🤖 **Terraform self-service** | ADO project creation runs as a Kubernetes Job — async, idempotent, GCS-backed state |
| 🔐 **OIDC authentication** | RedHat SSO / any OIDC provider; sessions held in HttpOnly JWT cookies; credentials stored server-side only |
| 📋 **Audit log** | Append-only `audit_events` table records every user action with IP, user agent, and timestamp |
| 🚢 **Helm deployment** | Umbrella Helm chart deploys backend, Nginx proxy, PostgreSQL, Redis, and ingress to Kubernetes |

---

## 🏗️ How it works

```
Browser (HTMX partial renders at /ui/*)
       │
       ▼
FastAPI Backend (:8000)
       │
       ├── /auth/*     ──► OIDC provider ──► JWT cookie set
       │
       ├── RBAC check (7-level hierarchy) ──► allow / 403
       │
       ├── Business logic (approvals, dashboards, audit)
       │          │                    │
       │          ▼                    ▼
       │      PostgreSQL            Redis (60s TTL)
       │   (users, approvals,    (all external API
       │    audit, widgets)       responses cached)
       │
       └── External adapters (never called directly by browser)
                  │
                  ├──► Azure DevOps REST  (work items, repos, pipelines)
                  ├──► SonarQube API      (quality gates, coverage, debt)
                  ├──► Artifactory        (storage stats, artifact info)
                  ├──► ServiceNow OAuth2  (incidents, support tickets)
                  └──► Confluence REST    (pages, spaces)
```

---

## 🚀 Quick Start

### 1. Clone and install

```bash
git clone https://github.com/mishgoldenberg/devops-idp.git
cd devops-idp
npm install
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Required | Description |
|---|:---:|---|
| `POSTGRES_PASSWORD` | ✅ | Password for the local PostgreSQL container |
| `REDIS_PASSWORD` | ✅ | Password for the local Redis container |
| `JWT_SECRET` | ✅ | Random 32+ character string used to sign session tokens |
| `HUB_ADMIN_USERNAME` | ✅ | Email address granted Platform Admin on first startup |
| `HUB_ADMIN_PASSWORD` | ✅ | Password for the bootstrap admin account |
| `AZURE_DEVOPS_BASE_URL` | ✅ | Full Azure DevOps org URL: `https://dev.azure.com/your-org` |
| `AZURE_DEVOPS_ADMIN_PAT` | ✅ | PAT with Project/Process/Build read+write scopes |
| `SNOW_BASE_URL` | ✅ | ServiceNow instance URL |
| `SNOW_API_USERNAME` | ✅ | ServiceNow service account username |
| `SNOW_API_PASSWORD` | ✅ | ServiceNow service account password |
| `SONARQUBE_BASE_URL` | ✅ | SonarQube instance URL |
| `ARTIFACTORY_BASE_URL` | ✅ | Artifactory instance URL |
| `CONFLUENCE_BASE_URL` | ✅ | Confluence instance URL |

> Generate a strong JWT secret with: `openssl rand -hex 32`

### 3. Start the stack

```bash
docker compose up -d
```

PostgreSQL, Redis, and the FastAPI backend start with health checks — the backend waits until both databases are ready.

### 4. Initialize the database

```bash
npm run migrate   # Creates schema: tables, enums, indexes
npm run seed      # Inserts roles, widget types, and seed data
```

### 5. Open the portal

Navigate to [http://localhost:8000/ui/](http://localhost:8000/ui/) and sign in with the `HUB_ADMIN_USERNAME` credentials.

---

## 🔑 OIDC / SSO Setup

DevOps Hub uses OIDC for authentication. The SSO provider and client credentials are configured at runtime through the admin UI — no environment variables needed for OIDC itself.

**Step 1 — Log in as the bootstrap admin**

On first startup, the account at `HUB_ADMIN_USERNAME` is created with the Platform Admin role. Log in with this account using the local login form at `/ui/login`.

**Step 2 — Configure your OIDC provider**

Navigate to **Admin → SSO Configuration** and enter your provider's discovery URL, client ID, and client secret. The portal fetches the OpenID configuration automatically and stores the client secret encrypted in the database.

**Step 3 — Test SSO login**

Click **Test SSO** in the admin UI before enabling it. Once verified, enable SSO — all subsequent logins will redirect to your provider.

---

## 🗂️ Project Structure

```
devops-hub/
├── backend/
│   └── app/                   # FastAPI application root
│       ├── main.py            # Entry point, startup hooks, ensure_tables()
│       ├── config.py          # All settings loaded from environment
│       ├── security.py        # JWT auth (auth_token cookie, HS256)
│       ├── db.py              # PostgreSQL connection pool + DDL helpers
│       ├── cache.py           # Redis client (60s TTL cache)
│       ├── ui.py              # All HTMX/Jinja2 HTML routes
│       ├── sso_config.py      # OIDC discovery and client secret encryption
│       ├── terraform_runner.py# Kubernetes Job submission for ADO provisioning
│       ├── Dockerfile
│       ├── requirements.txt
│       └── api/               # One router per integration or domain
│           ├── auth.py        # OIDC callback, JWT issuance
│           ├── azure_devops.py# Work items, repos, pipelines, project creation
│           ├── sonarqube.py   # Quality gates, coverage, technical debt
│           ├── artifactory.py # Artifact storage and download stats
│           ├── servicenow.py  # Incidents and support tickets
│           ├── approvals.py   # Request submission and approval chain
│           ├── dashboards.py  # Per-user widget layout persistence
│           └── health.py      # /api/health/live and /api/health/ready
├── frontend/
│   ├── nginx.conf             # Nginx reverse proxy (production)
│   ├── static/                # Compiled Tailwind CSS and SVG icons
│   └── templates/             # Jinja2 page and partial templates
├── deployment/                # Helm umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml            # Global values (registry URL, service endpoints)
│   └── charts/                # Sub-charts: backend, frontend, infrastructure, ingress-nginx
├── scripts/
│   └── db.js                  # Migration and seed runner (Node.js)
├── docs/                      # Architecture, API reference, integration guides
├── docker-compose.yml         # Local development orchestration
├── .env.example               # Environment variable template
└── package.json               # npm scripts: migrate, seed, db:reset
```

---

## ⚙️ Customisation

- **Add a new integration** — create a new router in `backend/app/api/` that calls the external API through `cache.py` for Redis caching, register it in `backend/app/main.py`, and add the corresponding Jinja2 partial in `frontend/templates/partials/`.

- **Change the RBAC hierarchy** — the 7-level role structure lives in the `roles` table, seeded by `scripts/db.js`. Edit the seed data and run `npm run db:reset` to rebuild.

- **Add or remove dashboard widgets** — widget types are registered in the `widget_types` table (part of `npm run seed`). Add a new type there, then create the matching HTMX partial in `frontend/templates/partials/components/`.

- **Enable HashiCorp Vault for secret storage** — set `USE_VAULT=true`, `VAULT_ADDR`, `VAULT_TOKEN`, and `VAULT_PATH` in `.env`. The backend switches secret backends automatically.

- **Tune Terraform job limits** — set `TF_GCS_BUCKET`, `TF_GCP_SERVICE_ACCOUNT`, `TF_K8S_NAMESPACE`, and `TERRAFORM_JOB_TIMEOUT_SECONDS` in `.env`. See [docs/SELF_SERVICE_TERRAFORM.md](docs/SELF_SERVICE_TERRAFORM.md) for infrastructure prerequisites.

---

## 🚢 Deployment

Build your container image and push it to your registry, then update `global.customImage.repository` in [deployment/values.yaml](deployment/values.yaml). Deploy with Helm:

```bash
helm dependency build ./deployment

helm upgrade --install devops-hub ./deployment \
  --namespace devops-hub-prod \
  --create-namespace \
  --atomic \
  --timeout 15m \
  --set-string global.customImage.repository="your-registry/devops-hub" \
  --set-string backend.ingress.host="devops.your-domain.example.com" \
  --set secrets.postgresPassword="<strong-password>" \
  --set secrets.jwtSecret="<random-32-char-string>" \
  --set secrets.hubAdminUsername="admin@your-domain.example.com" \
  --set secrets.hubAdminPassword="<strong-password>"
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full variable list and a step-by-step guide.

---

## 📄 License

[MIT](LICENSE) © 2026 Michael Goldenberg
