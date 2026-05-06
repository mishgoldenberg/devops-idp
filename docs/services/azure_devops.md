# `azure_devops` router

File: `backend/app/api/azure_devops.py` · Prefix: `/api/azure-devops`

## Purpose

Everything related to Azure DevOps:

- Per-user PAT storage and "connect / disconnect your ADO account".
- Read endpoints that power the ADO dashboard widgets
  (`work items`, `pull requests`, `pipelines`).
- `POST /projects/create` — the self-service entry point for creating a
  new project (the row lives in `approval_requests`; this endpoint also
  handles the direct-provisioning path when approvals aren't required).

## Main endpoints (selected)

| Method | Path                                   | Description                                          |
| ------ | -------------------------------------- | ---------------------------------------------------- |
| GET    | `/api/azure-devops/pat`                | Is the current user's PAT configured?                |
| POST   | `/api/azure-devops/pat`                | Store a user PAT (Vault or `system_config`)          |
| DELETE | `/api/azure-devops/pat`                | Remove the stored PAT                                |
| GET    | `/api/azure-devops/work-items`         | Work items assigned to the user (60 s cache)         |
| GET    | `/api/azure-devops/pull-requests`      | Open PRs across accessible repos (60 s cache)        |
| GET    | `/api/azure-devops/pipelines`          | Recent pipeline runs across projects (60 s cache)    |
| GET    | `/api/azure-devops/projects`           | List of accessible projects                          |
| POST   | `/api/azure-devops/projects/create`    | Create project + custom process (Safe-Mode aware)    |
| GET    | `/api/azure-devops/projects/create/{job_id}` | Poll the Terraform-backed creation job         |

## External APIs used

- `GET {base}/_apis/projects` — list projects.
- `POST {base}/_apis/wit/wiql` + `GET {base}/_apis/wit/workitems` — work
  items.
- `GET {base}/{project}/_apis/git/repositories` + per-repo PR endpoints.
- `GET {base}/{project}/_apis/build/builds` — pipelines.
- `GET/POST {base}/_apis/work/processes` — custom inherited process used
  by self-service.
- `GET {base}/_apis/graph/users` — resolve admin user to add on project
  creation.

Authentication is PAT + HTTP Basic (`httpx.BasicAuth("", pat)`).

## Performance shaping

- Every read endpoint wraps its live fetch with
  `integrations_cache.cached_external("ado", <owner>, <suffix>, …,
  ttl=60)`.
- Heavy fan-out endpoints (`pull-requests`, `pipelines`) use a
  `ThreadPoolExecutor` so per-project and per-repo requests happen
  concurrently instead of serially.
- On successful `projects/create`, `invalidate_owner("ado", owner)`
  drops every cached ADO read for that user so the new project shows up
  immediately.

## Environment variables

- `AZURE_DEVOPS_BASE_URL` — Azure DevOps organization URL.
- `AZURE_DEVOPS_ADMIN_PAT` — PAT with project/process permissions used by
  self-service write actions.

## Failure handling

- Each outbound call is wrapped by `resilient_http.safe_integration`,
  which normalizes transport errors (timeouts, DNS failures, 5xx) to
  `HTTPException(502)` with `"Service temporarily unavailable"`.
- Widget endpoints use `resilient_http.safe_call` where partial results
  are preferred over failing the whole page.
- 401 from ADO (expired PAT) is forwarded as a structured 401 so the UI
  can prompt the user to reconnect.

## Safe Mode

`POST /projects/create` checks `safe_mode.is_enabled()` first. If on, it
returns a fake job ID and records an audit event with
`metadata.safe_mode=true` — no ADO API call happens.
