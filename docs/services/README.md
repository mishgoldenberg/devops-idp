# Backend services

The "backend" is a single FastAPI app (`backend/app/`), but it's organized
as a set of per-domain routers plus cross-cutting helpers. Each document
in this folder describes one of those pieces: what it's for, its main
endpoints (or public functions), what external systems it touches, what
environment variables it reads, and how it fails.

## Routers (`backend/app/api/`)

| Module            | Prefix              | Doc                                    |
| ----------------- | ------------------- | -------------------------------------- |
| `auth`            | `/api/auth`         | [auth.md](./auth.md)                   |
| `dashboards`      | `/api/dashboards`   | [dashboards.md](./dashboards.md)       |
| `approvals`       | `/api/approvals`    | [approvals.md](./approvals.md)         |
| `azure_devops`    | `/api/azure-devops` | [azure_devops.md](./azure_devops.md)   |
| `servicenow`      | `/api/support`      | [servicenow.md](./servicenow.md)       |
| `sonarqube`       | `/api/sonarqube`    | [sonarqube.md](./sonarqube.md)         |
| `artifactory`     | `/api/artifactory`  | [artifactory.md](./artifactory.md)     |
| `observability`   | `/api/observability`| [observability.md](./observability.md) |
| `notifications`   | `/api/notifications`| [notifications.md](./notifications.md) |
| `audit_logs`      | `/api/audit-logs`   | [audit_logs.md](./audit_logs.md)       |
| `safe_mode`       | `/api/safe-mode`    | [safe_mode.md](./safe_mode.md)         |
| `admin`           | `/api/admin`        | [admin.md](./admin.md)                 |
| `health`          | `/api/health`       | [health.md](./health.md)               |
| `metrics`         | `/api/metrics`      | [metrics.md](./metrics.md)             |
| `pins`            | `/api`              | [pins.md](./pins.md)                   |

## Cross-cutting modules

| Module                | Doc                                         |
| --------------------- | ------------------------------------------- |
| `terraform_runner.py` | [terraform_runner.md](./terraform_runner.md) |
| `resilient_http.py`   | [resilient_http.md](./resilient_http.md)    |
| `integrations_cache.py` | [integrations_cache.md](./integrations_cache.md) |
