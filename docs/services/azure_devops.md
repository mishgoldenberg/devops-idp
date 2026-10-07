# `azure_devops` router

File: `backend/app/api/azure_devops.py` · Prefix: `/api/azure-devops`

## Purpose

Everything related to Azure DevOps that reads with the person's own token, plus the
project provisioning an approved request runs:

- Per-user PAT storage, encrypted at rest (`secrets_manager`), and its status.
- The reads behind the Azure DevOps widgets and pickers.
- `provision_ado_project`, which `approvals.py` calls to create an approved project
  in the one collection the request names (no endpoint of its own).

## Main endpoints

| Method          | Path                                         | Description                                   |
| --------------- | -------------------------------------------- | --------------------------------------------- |
| GET, POST, DELETE | `/api/azure-devops/pat`                    | The person's PAT: status, save, remove        |
| GET             | `/api/azure-devops/work-items`               | Work items assigned to the person             |
| GET             | `/api/azure-devops/workitem-tasks`           | Child tasks of a work item                    |
| GET             | `/api/azure-devops/work-item-types`, `/area-paths`, `/iterations` | Pickers for the work-items widget |
| GET             | `/api/azure-devops/pull-requests`            | Open PRs across the person's repositories     |
| GET             | `/api/azure-devops/pipelines`                | Recent pipeline runs                          |
| GET             | `/api/azure-devops/projects`, `/repositories`, `/collections` | What the person can see     |
| GET             | `/api/azure-devops/provisioning/collections` | Collections a project request may target      |
| GET             | `/api/azure-devops/provisioning/name-check`  | Is a project name free there (admin PAT)      |

Writes made in the person's name (votes, comments, pipeline runs, work-item edits)
are in `ado_actions.py`, under `/api/azure-devops/actions`.

## External APIs used

- `GET {base}/_apis/projects` — projects.
- `POST {base}/_apis/wit/wiql` + `GET {base}/_apis/wit/workitems` — work items.
- `GET {base}/{project}/_apis/git/repositories` + per-repo PR endpoints.
- `GET {base}/{project}/_apis/build/builds` — pipelines.
- `GET/POST {base}/_apis/work/processes` — the inherited process a new project uses.
- The web UI's `/_api/_identity/` endpoints and `_apis/accesscontrollists` — resolve
  the requested admin and grant them (`_apis/graph` is not routed on this server).

Authentication is PAT + HTTP Basic (`httpx.BasicAuth("", pat)`).

## Performance shaping

- Every read endpoint wraps its live fetch with
  `integrations_cache.cached_external("ado", <owner>, <suffix>, …,
  ttl=60)`.
- Heavy fan-out endpoints (`pull-requests`, `pipelines`) use a
  `ThreadPoolExecutor` so per-project and per-repo requests happen
  concurrently instead of serially.
- After a project is provisioned, `invalidate_owner("ado", owner)` drops every
  cached Azure DevOps read for that person, so the new project shows at once.

## Environment variables

- `AZURE_DEVOPS_BASE_URL` — one collection's URL; the others are discovered from it.
- `AZURE_DEVOPS_ADMIN_PAT` — PAT with project/process permissions used by
  self-service write actions.

## Failure handling

- A transport failure becomes a 502 whose detail says what went wrong and who can
  fix it (`resilient_http.explain_integration_failure`).
- A rejected personal token is answered as 424 (never 401, which the Hub would read
  as its own session ending), with a detail that says to reconnect it.

## Safe Mode

With Safe Mode on, the approval executor returns a simulated success and nothing is
sent to Azure DevOps.
