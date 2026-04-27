# `approvals` router

File: `backend/app/api/approvals.py` · Prefix: `/api/approvals`

## Purpose

The engine behind the self-service workflow:

- Users create requests (`POST /requests`).
- Admins approve or reject them.
- A background worker executes approved requests (today mainly
  `ADO_PROJECT_CREATE` via Terraform).
- Every state change writes to `audit_events` and pushes a notification.

## Main endpoints

| Method | Path                                      | Description                                    |
| ------ | ----------------------------------------- | ---------------------------------------------- |
| GET    | `/api/approvals/requests`                 | Admin list; filter by `status`, `request_type` |
| POST   | `/api/approvals/requests`                 | Create a new request                           |
| GET    | `/api/approvals/requests/{id}`            | Single request detail                          |
| POST   | `/api/approvals/requests/{id}/approve`    | Admin approve, spawns executor                 |
| POST   | `/api/approvals/requests/{id}/reject`     | Admin reject with reason                       |
| GET    | `/api/approvals/my-requests`              | Requests created by the current user           |

## External APIs used

- **Azure DevOps** — pre-flight during `ADO_PROJECT_CREATE` to create a
  custom inherited process (`azure_devops.ensure_custom_ado_process`).
- **Kubernetes** — `terraform_runner.submit_terraform_job` creates a
  `BatchV1 Job` + `ConfigMap` that runs `hashicorp/terraform:1.6`.

## Environment variables

- `K8S_NAMESPACE`, `TERRAFORM_JOB_TIMEOUT_SECONDS` — used indirectly via
  `terraform_runner`.
- `AZURE_DEVOPS_BASE_URL` / `AZURE_DEVOPS_ADMIN_PAT` — required for real ADO
  project creation.
- `SAFE_MODE` — if truthy (env or DB flag), the executor short-circuits
  with a simulated-success payload.

## Data written

- `approval_requests` — the request itself (insert + status updates).
- `audit_events` — one row per state change.
- `notifications` — one row per state change to the requester.
- `azure_projects` / `self_service_usage` — incremented on success via
  `observability_tracking`.

## Validation

`_validate_request_payload` enforces per-type rules. For
`ADO_PROJECT_CREATE`:

- `project_name` matches `^[A-Za-z0-9][A-Za-z0-9 _\-.]{1,62}$`.
- `process_type` ∈ `{Scrum, Agile, CMMI, Basic}`.
- `admin_username` looks like an email.

Duplicates (`PENDING`/`IN_PROGRESS` with the same type + identity key)
return 409 Conflict.

## Failure handling

- Raw exceptions from ADO / Terraform / K8s are caught by
  `resilient_http.safe_integration` and surfaced as a generic
  "Service temporarily unavailable" 502 to the UI.
- A stuck Terraform pod is killed by `active_deadline_seconds` and
  reported as a `FAILED` status with a human-readable reason
  (see [terraform_runner.md](./terraform_runner.md)).

## Related docs

- [`docs/self-service-flow.md`](../self-service-flow.md) — the full
  lifecycle diagram.
- [audit_logs.md](./audit_logs.md) — where the audit rows go.
- [notifications.md](./notifications.md) — how users get pinged.
