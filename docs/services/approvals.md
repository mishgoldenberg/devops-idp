# `approvals` router

File: `backend/app/api/approvals.py` · Prefix: `/api/approvals`

## Purpose

The engine behind the self-service workflow:

- Users create requests (`POST /requests`).
- Admins approve or reject them.
- A background worker executes approved requests (an Azure DevOps project over REST,
  an Artifactory quota, a cleaner pull request).
- Every state change writes to `audit_events` and pushes a notification.

## Main endpoints

| Method | Path                                      | Description                                    |
| ------ | ----------------------------------------- | ---------------------------------------------- |
| GET    | `/api/approvals/requests`                 | `scope=mine` (My Requests) or `scope=all` (admins); filter by `status` |
| POST   | `/api/approvals/requests`                 | Create a new request                           |
| GET    | `/api/approvals/requests/{id}`            | Single request detail                          |
| POST   | `/api/approvals/requests/{id}/approve`    | Admin approve, spawns executor                 |
| POST   | `/api/approvals/requests/{id}/reject`     | Admin reject with reason                       |

## External APIs used

- **Azure DevOps** — the inherited process a new project uses, created first if
  missing (`azure_devops._ensure_inherited_process`).
- **Azure DevOps** — the project itself, created over the REST API with the admin
  PAT in the one collection the request names (`_create_project_in_collection`).
- **Artifactory / Azure Repos** — quota increases, and cleaner specs written to their
  repository through a pull request.
- **ServiceNow** — the ticket raised when the outcome is known.

## Environment variables

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

- Raw exceptions from the target systems are caught by
  `resilient_http.safe_integration` and surfaced as a generic
  "Service temporarily unavailable" 502 to the UI.
- A request stuck in `APPROVED` or `IN_PROGRESS` (a pod restarted mid-run) can be
  force-failed by an admin (`POST /requests/{id}/force-fail`).

## Related docs

- [`docs/self-service-flow.md`](../self-service-flow.md) — the full
  lifecycle diagram.
- [audit_logs.md](./audit_logs.md) — where the audit rows go.
- [notifications.md](./notifications.md) — how users get pinged.
