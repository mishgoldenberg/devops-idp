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
| `OAUTH_CLIENT_ID`      | Google OAuth client ID.                                                                             | `…apps.googleusercontent.com`                                           | Yes      |
| `OAUTH_CLIENT_SECRET`  | Google OAuth client secret.                                                                         | `GOCSPX-…`                                                              | Yes      |
| `OAUTH_REDIRECT_URI`   | Where Google redirects after consent. Must exactly match a value registered in GCP.                 | `https://devops.internal.company/api/auth/callback`                     | Yes      |
| `OAUTH_ISSUER`         | OIDC issuer URL.                                                                                    | `https://accounts.google.com`                                           | No       |
| `OAUTH_AUTH_URL`       | OIDC authorization endpoint. Derived from issuer when unset.                                        | `https://accounts.google.com/o/oauth2/v2/auth`                          | No       |
| `OAUTH_TOKEN_URL`      | OIDC token endpoint.                                                                                | `https://oauth2.googleapis.com/token`                                   | No       |
| `OAUTH_JWKS_URL`       | OIDC JWKS endpoint.                                                                                 | `https://www.googleapis.com/oauth2/v3/certs`                            | No       |
| `OAUTH_SCOPES`         | Space-separated scope list.                                                                         | `openid email profile`                                                  | No       |

## Azure DevOps integration

| Name                          | Description                                                                                    | Example                       | Required |
| ----------------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------- | -------- |
| `USE_MOCK_AZURE_DEVOPS`       | `true` (default) returns canned data without calling ADO. Set to `false` in production.         | `false`                       | No       |
| `AZURE_DEVOPS_ORGANIZATION`   | ADO organization name. Preferred.                                                              | `myorg`                       | Yes (real) |
| `AZURE_DEVOPS_ORG`            | Alias for `AZURE_DEVOPS_ORGANIZATION`.                                                         | `myorg`                       | No       |
| `AZURE_DEVOPS_API_URL`        | Override base URL. Only used if neither of the org vars is set.                                | `https://dev.azure.com`       | No       |
| `AZURE_DEVOPS_PAT`            | Fallback PAT used only for self-service (project creation) when a user hasn't connected theirs. | PAT                           | Yes (real) |
| `AZURE_DEVOPS_ADMIN_PAT`      | PAT with `Process (Read & Manage)` scope used for custom process creation.                      | PAT                           | Yes (real) |
| `AZURE_DEVOPS_QUERY_USER`     | Dev-only: query work items/PRs as this user instead of the caller.                              | `service-account@company.com` | No       |

## ServiceNow integration

| Name                   | Description                                                                          | Example                                 | Required |
| ---------------------- | ------------------------------------------------------------------------------------ | --------------------------------------- | -------- |
| `USE_MOCK_SERVICENOW`  | `true` (default) returns canned tickets without calling SNOW.                         | `false`                                 | No       |
| `SERVICENOW_URL`       | Full instance URL. Preferred.                                                         | `https://mycompany.service-now.com`     | Yes (real) |
| `SERVICENOW_INSTANCE`  | Instance shortname; used only if `SERVICENOW_URL` is unset.                           | `mycompany`                             | No       |
| `SERVICENOW_USERNAME`  | Service-account user for the incident API.                                            | `portal_bot`                            | Yes (real) |
| `SERVICENOW_USER`      | Alias for `SERVICENOW_USERNAME`.                                                      | `portal_bot`                            | No       |
| `SERVICENOW_PASSWORD`  | Password for the service account.                                                     | `********`                              | Yes (real) |

## SonarQube / Artifactory

Both integrations are currently **mock-only** in code. The env vars below
exist in `env.example` for future real integrations.

| Name                   | Description                                        | Example                                   | Required |
| ---------------------- | -------------------------------------------------- | ----------------------------------------- | -------- |
| `USE_MOCK_SONARQUBE`   | Always effectively `true` today.                   | `true`                                    | No       |
| `SONARQUBE_URL`        | Reserved for real integration.                     | `https://sonarqube.internal.company`      | No       |
| `SONARQUBE_TOKEN`      | Reserved for real integration.                     | Token                                     | No       |
| `USE_MOCK_ARTIFACTORY` | Always effectively `true` today.                   | `true`                                    | No       |
| `ARTIFACTORY_URL`      | Reserved for real integration.                     | `https://artifactory.internal.company`    | No       |
| `ARTIFACTORY_API_KEY`  | Reserved for real integration.                     | API key                                   | No       |

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
