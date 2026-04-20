# Self-service flow

The self-service system lets a user request a platform action (e.g.
"create me an Azure DevOps project named `billing-api`") and have the
portal execute it after an admin approves, with full auditing and
notifications at each step.

All code lives in `backend/app/api/approvals.py`. Supporting modules:

- `backend/app/audit.py` — writes rows to `audit_events`
- `backend/app/api/notifications.py` — creates bell notifications
- `backend/app/safe_mode.py` — global simulate-only toggle
- `backend/app/terraform_runner.py` — submits Kubernetes Terraform Jobs
- `backend/app/api/azure_devops.py` — pre-flight (custom process creation) for ADO project requests

## Supported request types

Defined by the `approval_request_type` enum in `00_schema.sql`:

| `request_type`                | What it does                                                   | Executor                             |
| ----------------------------- | -------------------------------------------------------------- | ------------------------------------ |
| `ADO_PROJECT_CREATE`          | Creates an Azure DevOps project via Terraform                  | `_execute_ado_project_create(...)`   |
| `SONAR_PR_SCANNING_ENABLE`    | Enables SonarQube PR scanning for a repository (stub)          | `_execute_approved_request(...)`     |
| `AI_MODEL_ACCESS`             | Grants access to an internal AI model (stub)                   | `_execute_approved_request(...)`     |
| `CUSTOM`                      | Catch-all for anything admin-defined (stub)                    | `_execute_approved_request(...)`     |

Today only `ADO_PROJECT_CREATE` has a real executor. The others reach the
"executed" state with a canned success payload — they're scaffolding for
future integrations.

## Lifecycle

```
PENDING ──► APPROVED ──► IN_PROGRESS ──► COMPLETED
                │                    └─► FAILED
                └─► REJECTED
```

`EXECUTED` is a legacy status from before `IN_PROGRESS` / `COMPLETED`
existed; list/filter queries treat it as equivalent to `COMPLETED`.

## Step-by-step

### 1. User submits the request

- Endpoint: `POST /api/approvals/requests`
- Frontend: `frontend/templates/partials/components/self-service-*.html`
- Backend actions:
  1. `_validate_request_payload()` checks:
     - No empty required fields.
     - For `ADO_PROJECT_CREATE`: project name matches
       `^[A-Za-z0-9][A-Za-z0-9 _\-.]{1,62}$` and admin user looks like an
       email.
  2. Duplicate guard: if the same user already has a `PENDING` request with
     the same `request_type` and essentially the same payload, returns a
     409 Conflict. This avoids double-clicks creating two projects.
  3. Inserts a row into `approval_requests` with status `PENDING`.
  4. `audit.log(Action.SELF_SERVICE_REQUEST_CREATED, metadata=…)`.
  5. `create_notification(user_email=requester, message=…,
     link="/ui/my-requests", group_key=f"request:{id}")` — shows up in the
     requester's notifications bell.

### 2. Admin reviews on the Approvals page

- Page: `/ui/approvals` (gated by admin access).
- Endpoint list: `GET /api/approvals/requests?status=PENDING`.
- The UI renders each pending request with a request-specific summary
  (e.g. project name, process type) and two actions: Approve / Reject.

### 3a. Approve

- Endpoint: `POST /api/approvals/requests/{id}/approve`
- Backend actions:
  1. Updates the row to `APPROVED` and records `approver_id`,
     `approved_at`.
  2. `audit.log(Action.SELF_SERVICE_REQUEST_APPROVED, …)`.
  3. `create_notification(user_email=requester, …,
     link="/ui/my-requests", group_key=f"request:{id}")`.
  4. Spawns a background worker: `_execute_request_worker(request_id)`.
     The HTTP call returns immediately.

### 3b. Reject

- Endpoint: `POST /api/approvals/requests/{id}/reject`
- Payload: `{"reason": "…"}` (required).
- Updates the row to `REJECTED`, records `rejection_reason`.
- `audit.log(Action.SELF_SERVICE_REQUEST_REJECTED, …)`.
- Notifies the requester; the flow ends.

### 4. Background execution

`_execute_request_worker` runs in a thread. Its first action is the Safe
Mode short-circuit:

```python
if safe_mode.is_enabled():
    # Mark COMPLETED with a simulated-success payload.
    # Audit event + notification still fire so the user experience matches
    # a real run. No external systems are contacted.
    ...
    return
```

If Safe Mode is off, the worker:

1. Sets status to `IN_PROGRESS`.
2. Dispatches on `request_type`:
   - **`ADO_PROJECT_CREATE`** → `_execute_ado_project_create()`:
     1. `azure_devops.ensure_custom_ado_process(process_type, project_name)`
        creates a per-project inherited process on ADO.
     2. `terraform_runner.submit_terraform_job(payload)` builds a
        ConfigMap with a small Terraform module, creates a BatchV1 `Job`
        using the `hashicorp/terraform:1.6` image, and returns a job id.
        The Job writes its state to GCS
        (`devops-control-center-tfstate`).
     3. `audit.log(Action.TERRAFORM_STARTED, …)`.
     4. Polls the K8s Job status (`_poll_terraform_job`) until it reaches
        a terminal state. The Job also has
        `active_deadline_seconds=TERRAFORM_JOB_TIMEOUT_SECONDS` (default
        1200 s) so a stuck pod can't hang forever — the poller detects
        `DeadlineExceeded` and surfaces a human-readable error.
   - **Other types** → stub success.
3. Writes final status (`COMPLETED` or `FAILED`), updates the
   `approval_requests` row.
4. `audit.log(Action.SELF_SERVICE_REQUEST_COMPLETED | …_FAILED, …)`.
5. `create_notification(...)` to the requester with a
   click-through link back to the request.
6. `observability_tracking.record_self_service(...)` and, for ADO,
   `record_azure_project(...)` increment the observability counters.

### 5. User sees the outcome

- Page: `/ui/my-requests`.
- Endpoint: `GET /api/approvals/my-requests`.
- The page polls (via HTMX) so the row flips from `APPROVED` →
  `IN_PROGRESS` → `COMPLETED` / `FAILED` without a manual refresh.
- The notification bell fires via `GET /api/notifications`.

## Input validation rules

Implemented in `_validate_request_payload()`:

- `title`, `request_type`, `request_payload` are required.
- For `ADO_PROJECT_CREATE`:
  - `project_name` must match
    `^[A-Za-z0-9][A-Za-z0-9 _\-.]{1,62}$` (ADO's own rules, minus a few
    characters we've found to cause friction).
  - `process_type` ∈ `{Scrum, Agile, CMMI, Basic}`.
  - `admin_username` must look like an email.
- Any failure raises `HTTPException(400)` with a user-friendly message.

## Duplicate prevention

A request is considered a duplicate if **all** of the following are true:

- Same `requester_id`.
- Same `request_type`.
- Request is currently `PENDING` or `IN_PROGRESS`.
- Same "identity key" inside `request_payload` (e.g. same `project_name`
  for `ADO_PROJECT_CREATE`).

## Safe Mode: when to use it

- Dev environments where ADO/ServiceNow are unreachable.
- Demos where you want the full UX without side effects.
- Rolling out a new `request_type` before the executor is wired up.

Toggle from **Admin → Platform Managing → Safe Mode**, or set
`SAFE_MODE=true` in the environment. Either source is fine — the DB flag
wins when present.
