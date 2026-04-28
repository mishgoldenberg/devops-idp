# Environment variables

Every configurable aspect of the backend is driven by an environment
variable. This document lists them all, grouped by purpose, with the
default behavior and whether they are required.

> The single source of truth for values in development is
> [`env.example`](../env.example). Production values live in GitHub
> Actions secrets and are injected into Kubernetes via the umbrella Helm
> chart. **Never check real secrets into the repo.**

---

## Core

| Name           | Description                                                                 | Example                                   | Required |
| -------------- | --------------------------------------------------------------------------- | ----------------------------------------- | -------- |
| `HOST`         | Uvicorn bind address.                                                       | `0.0.0.0`                                 | No       |
| `PORT`         | Uvicorn port.                                                               | `8000`                                    | No       |
| `NODE_ENV`     | Environment label (`development` / `production`). Used by logging & cookie `secure`. | `development`                     | No       |
| `ENVIRONMENT`  | Alias used by some modules; same meaning as `NODE_ENV`.                      | `production`                              | No       |
| `LOG_LEVEL`    | Passed to Python logging config.                                             | `debug`                                   | No       |
| `CORS_ORIGINS` | Comma-separated list of allowed origins. `*` to allow all.                   | `https://devops.internal.company`         | No       |

## Database (PostgreSQL)

| Name                 | Description                                                                             | Example                                                                                                 | Required |
| -------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | -------- |
| `DATABASE_URL`       | Full libpq URL. Takes precedence over the individual `POSTGRES_*` vars when set.        | `postgresql://devops:devops@postgres:5432/devops_control_center`                                        | Yes      |
| `POSTGRES_HOST`      | Host name; used by `docker-compose` and the migration job.                              | `postgres`                                                                                              | No       |
| `POSTGRES_PORT`      | Port.                                                                                    | `5432`                                                                                                  | No       |
| `POSTGRES_DB`        | Database name.                                                                           | `devops_control_center`                                                                                 | No       |
| `POSTGRES_USER`      | DB user.                                                                                 | `devops`                                                                                                | No       |
| `POSTGRES_PASSWORD`  | DB password.                                                                             | `********`                                                                                              | Yes (prod) |

## Redis (cache)

| Name             | Description                                                      | Example     | Required |
| ---------------- | ---------------------------------------------------------------- | ----------- | -------- |
| `REDIS_HOST`     | Redis hostname.                                                  | `redis`     | Yes      |
| `REDIS_PORT`     | Redis port.                                                      | `6379`      | No       |
| `REDIS_PASSWORD` | Redis AUTH password. Omit if instance has no auth.               | `********`  | No       |

## Authentication

| Name                   | Description                                                                                         | Example                                                                 | Required |
| ---------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------- |
| `JWT_SECRET`           | HS256 signing secret for the portal's own session JWT.                                              | A 40+ char random string                                                | Yes      |
| `JWT_EXPIRY`           | JWT TTL. Accepts `8h`, `1d`, etc. Drives the `auth_token` cookie `max_age`.                         | `8h`                                                                    | No       |
| `HUB_ADMIN_USERNAME`   | Bootstrap admin username/email used only when no admin exists.                                      | `admin@example.com`                                                     | Yes      |
| `HUB_ADMIN_PASSWORD`   | Bootstrap admin password, bcrypt-hashed on first startup.                                           | From GitHub Secrets                                                     | Yes      |

## Azure DevOps integration

| Name                     | Description                                                               | Example                         | Required |
| ------------------------ | ------------------------------------------------------------------------- | ------------------------------- | -------- |
| `AZURE_DEVOPS_BASE_URL`  | Azure DevOps organization URL used for API calls and external links.       | `https://dev.azure.com/myorg`   | Yes      |
| `AZURE_DEVOPS_ADMIN_PAT` | PAT with project/process permissions used for self-service write actions.  | PAT                             | Yes      |

## ServiceNow integration

| Name                   | Description                                                                          | Example                                 | Required |
| ---------------------- | ------------------------------------------------------------------------------------ | --------------------------------------- | -------- |
| `SNOW_BASE_URL`        | Full instance URL used for all ServiceNow API calls.                                  | `https://mycompany.service-now.com`     | Yes (real) |
| `SNOW_API_USERNAME`    | Service-account user for incident and attachment APIs.                                | `portal_bot`                            | Yes (real) |
| `SNOW_API_PASSWORD`    | Password for the service account.                                                     | `********`                              | Yes (real) |

## SonarQube / Artifactory / Confluence

| Name                   | Description                                            | Example                                | Required |
| ---------------------- | ------------------------------------------------------ | -------------------------------------- | -------- |
| `SONARQUBE_BASE_URL`   | SonarQube base URL used by the connected widget.       | `https://sonarqube.internal.company`   | Yes      |
| `ARTIFACTORY_BASE_URL` | Artifactory base URL used by connected widgets.        | `https://artifactory.internal.company` | Yes      |
| `CONFLUENCE_BASE_URL`  | Confluence base URL used by the Confluence Pages widget. | `https://confluence.internal.company`  | Yes      |

## Self-service / Terraform

| Name                               | Description                                                                                    | Example                              | Required |
| ---------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------ | -------- |
| `K8S_NAMESPACE`                    | Namespace where Terraform Jobs are submitted. Defaults to the pod's own namespace.              | `devops-control-center-prod`         | No       |
| `TERRAFORM_JOB_TIMEOUT_SECONDS`    | Max wall-clock time for a Terraform Job before Kubernetes kills it with `DeadlineExceeded`.     | `1200`                               | No       |

## Safe Mode

| Name        | Description                                                                                                                                              | Example  | Required |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------- |
| `SAFE_MODE` | Global simulate-only toggle. Starts `true`? Defaults to `false`. The DB-backed flag in `portal_flags` wins over this env var if present. See `safe_mode.py`. | `false`  | No       |

## Vault (optional secret store)

| Name          | Description                                                       | Example                             | Required |
| ------------- | ----------------------------------------------------------------- | ----------------------------------- | -------- |
| `USE_VAULT`   | If `true`, look up integration secrets in Vault instead of env.   | `true`                              | No       |
| `VAULT_ADDR`  | Vault address.                                                    | `https://vault.internal.company`    | No       |
| `VAULT_TOKEN` | Token with read access to `VAULT_PATH`.                           | `s.xxxxx`                           | No       |
| `VAULT_PATH`  | KV path that stores the per-user secrets.                         | `secret/devops-control-center`      | No       |

---

## How these are delivered in production

1. Values come from **GitHub Actions secrets**.
2. `deploy-workflow.yml` passes them as `--set secrets.<name>=…` into
   `helm upgrade --install`.
3. `deployment/charts/backend/templates/secrets.yaml` renders them into a
   single `Secret` named `all-secrets`.
4. The backend `Deployment` references it with `envFrom: secretRef:
   name: all-secrets`, plus a `checksum/secrets` annotation so pods roll
   whenever the secret changes.
