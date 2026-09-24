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
| `CORS_ORIGINS` | Comma-separated list of allowed origins. `*` to allow all.                   | `https://your-devops-hub.example.com`         | No       |

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
| `SONARQUBE_BASE_URL`   | SonarQube base URL used by the connected widget.       | `https://your-sonarqube.example.com`   | Yes      |
| `ARTIFACTORY_BASE_URL` | Artifactory base URL used by connected widgets.        | `https://your-artifactory.example.com` | Yes      |
| `CONFLUENCE_BASE_URL`  | Confluence base URL used by the Confluence Pages widget. | `https://your-confluence.example.com`  | Yes      |

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
| `VAULT_ADDR`  | Vault address.                                                    | `https://your-vault.example.com`    | No       |
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

<!-- GENERATED:ENV — do not edit by hand; run scripts/gen_docs.py -->

86 variables the backend actually reads, found by parsing every `os.getenv` in `backend/app/`. A variable that is not here is not read by anything, whatever the deployment sets.

| Variable | Default | Read by |
| --- | --- | --- |
| `ADO_CLEANER_ARTIFACTORY_URL` | _(none)_ | `artifactory_cleaner.py` |
| `ADO_CLEANER_BASE_BRANCH` | _(none)_ | `ado_repo.py`, `artifactory_cleaner.py` |
| `ADO_CLEANER_COLLECTION` | _(none)_ | `ado_repo.py` |
| `ADO_CLEANER_IMAGE` | _(none)_ | `artifactory_cleaner.py` |
| `ADO_CLEANER_NAMESPACE` | _(none)_ | `artifactory_cleaner.py` |
| `ADO_CLEANER_POOL` | _(none)_ | `ado_pipeline.py`, `artifactory_cleaner.py` |
| `ADO_CLEANER_PROJECT` | _(none)_ | `ado_repo.py` |
| `ADO_CLEANER_REPO` | _(none)_ | `ado_repo.py` |
| `ADO_CLEANER_VARIABLE_GROUP` | _(none)_ | `ado_pipeline.py`, `artifactory_cleaner.py` |
| `ADO_DEFAULT_COLLECTION` | _(none)_ | `ado_repo.py`, `azure_devops.py` |
| `ADO_NETBIOS_DOMAIN` | _(none)_ | `azure_devops.py` |
| `ADO_PROVISION_COLLECTIONS` | `DevCollection-Inheritance,TikshuvCollection-Inheritance` | `azure_devops.py` |
| `ADO_REVIEWER_FIELD` | _(none)_ | `azure_devops.py` |
| `ADO_REVIEW_STATES` | `Pending Review,Needs Review,Ready for Review,In Review,Code Review,Review,Awaiting Review,Pending Approval,Ready for Test` | `azure_devops.py` |
| `APP_BUILD` | `unknown` | `health.py` |
| `ARTIFACTORY_ADMIN_PASSWORD` | _(secret — must be set)_ | `artifactory_admin.py` |
| `ARTIFACTORY_ADMIN_TOKEN` | _(secret — must be set)_ | `artifactory_admin.py` |
| `ARTIFACTORY_ADMIN_USERNAME` | _(none)_ | `artifactory_admin.py` |
| `ARTIFACTORY_BACKUP_TOKEN` | _(secret — must be set)_ | `artifactory_admin.py` |
| `ARTIFACTORY_BACKUP_USERNAME` | _(none)_ | `artifactory_admin.py` |
| `ARTIFACTORY_BASE_URL` | _(none)_ | `integrations.py`, `artifactory_admin.py`, `artifactory_cleaner.py` |
| `ARTIFACTORY_DISK_TOTAL` | _(none)_ | `integrations.py`, `artifactory_admin.py` |
| `ARTIFACTORY_UNLIMITED_PROJECTS` | `DevOps` | `artifactory_admin.py` |
| `AUTH_ACTIVE_CHECK_TTL` | `30` | `security.py` |
| `AZURE_DEVOPS_ADMIN_PAT` | _(secret — must be set)_ | `ado_repo.py`, `azure_devops.py`, `config.py` |
| `AZURE_DEVOPS_BASE_URL` | _(none)_ | `azure_devops.py`, `config.py` |
| `BACKUP_BACKEND_DEPLOYMENT` | _(none)_ | `backups.py` |
| `BACKUP_BACKEND_REPLICAS` | _(none)_ | `backups.py` |
| `BACKUP_KEEP_LAST` | `3` | `backups.py` |
| `BACKUP_PG_STATEFULSET` | _(none)_ | `backups.py` |
| `CONFLUENCE_BASE_URL` | _(none)_ | `integrations.py`, `config.py` |
| `CORS_ORIGINS` | `*` | `config.py` |
| `DATABASE_URL` | _(none)_ | `backups.py`, `config.py` |
| `DB_CONNECT_TIMEOUT` | `5` | `db.py` |
| `DB_POOL_MAX` | `20` | `db.py` |
| `DB_POOL_MIN` | `2` | `db.py` |
| `DB_POOL_RETRY_COOLDOWN` | `5` | `db.py` |
| `DB_SIZE_WARN_MB` | `600` | `audit.py` |
| `DB_VOLUME_MB` | `1024` | `audit.py` |
| `DEVBOT_CONTEXT_TOKENS` | `16384` | `config.py` |
| `DEVBOT_DEFAULT_MODEL` | _(none)_ | `config.py` |
| `DEVBOT_HISTORY_DAYS` | `30` | `config.py` |
| `DEVBOT_KEY_HELP_URL` | _(none)_ | `config.py` |
| `DEVBOT_LLM_BASE_URL` | _(none)_ | `config.py` |
| `DEVBOT_MAX_ANSWER_TOKENS` | `2000` | `config.py` |
| `DEVBOT_MAX_PROMPT_TOKENS` | `12000` | `config.py` |
| `DEVBOT_MAX_STREAMS` | `8` | `config.py` |
| `DEVBOT_MAX_TOOL_ROUNDS` | `4` | `config.py` |
| `DEVBOT_STREAM` | `true` | `config.py` |
| `ENVIRONMENT` | `development` | `config.py`, `release_notes.py` |
| `HOST` | `0.0.0.0` | `config.py` |
| `HUB_ADMIN_PASSWORD` | _(secret — must be set)_ | `config.py`, `db.py` |
| `HUB_ADMIN_USERNAME` | _(none)_ | `config.py`, `db.py` |
| `HUB_NAMESPACE` | _(none)_ | `backups.py` |
| `HUB_USER_PASSWORD` | _(secret — must be set)_ | `db.py` |
| `HUB_USER_USERNAME` | _(none)_ | `db.py` |
| `INTEGRATION_TLS_VERIFY` | `true` | `config.py` |
| `JWT_EXPIRY` | `8h` | `config.py` |
| `JWT_SECRET` | _(secret — must be set)_ | `config.py` |
| `NODE_ENV` | _(none)_ | `config.py` |
| `OPENSHIFT_CONSOLE_URL` | _(none)_ | `artifactory_cleaner.py` |
| `PORT` | `8000` | `config.py` |
| `PORTAL_ENVIRONMENT` | _(none)_ | `release_notes.py` |
| `REDIS_HOST` | `redis` | `config.py` |
| `REDIS_PASSWORD` | _(secret — must be set)_ | `config.py` |
| `REDIS_PORT` | `6379` | `config.py` |
| `REQUEST_RETENTION_DAYS` | _(none)_ | `retention.py` |
| `REQUEST_SLA_DAYS` | _(none)_ | `approvals.py` |
| `SAFE_MODE` | _(none)_ | `safe_mode.py` |
| `SNOW_API_PASSWORD` | _(secret — must be set)_ | `servicenow.py`, `config.py`, `snow_catalog.py` |
| `SNOW_API_USERNAME` | _(none)_ | `servicenow.py`, `config.py`, `snow_catalog.py` |
| `SNOW_BASE_URL` | _(none)_ | `servicenow.py`, `config.py`, `snow_catalog.py` |
| `SNOW_CALLER_PATCH` | `0` | `servicenow.py` |
| `SNOW_CALLER_VAR` | _(none)_ | `servicenow.py` |
| `SNOW_DEFAULT_SUPPORT_GROUP` | `Devops Support` | `servicenow.py` |
| `SNOW_JOURNAL_ELEMENTS` | `comments` | `servicenow.py` |
| `SNOW_PATCH_REQUESTED_FOR` | _(none)_ | `snow_catalog.py` |
| `SNOW_PRODUCER_SYS_ID` | _(none)_ | `servicenow.py` |
| `SNOW_SERVICE_ACCOUNT_DISPLAY` | _(none)_ | `servicenow.py` |
| `SNOW_SUPPORT_GROUP_VAR` | `choose_a_support_group` | `servicenow.py` |
| `SONARQUBE_BASE_URL` | _(none)_ | `integrations.py`, `config.py`, `ui.py` |
| `STREAK_TIMEZONE` | _(none)_ | `streaks.py` |
| `USE_VAULT` | `false` | `secrets_manager.py` |
| `VAULT_ADDR` | _(none)_ | `secrets_manager.py` |
| `VAULT_PATH` | `secret/devops-control-center` | `secrets_manager.py` |
| `VAULT_TOKEN` | _(secret — must be set)_ | `secrets_manager.py` |

<!-- /GENERATED:ENV -->
