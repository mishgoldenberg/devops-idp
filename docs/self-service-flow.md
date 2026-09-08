# Self-service flow

The self-service system lets a user request a platform action (e.g.
"create me an Azure DevOps project named `billing-api`") and have the
portal execute it after an admin approves, with full auditing and
notifications at each step.

All code lives in `backend/app/api/approvals.py`. Supporting modules:

- `backend/app/audit.py` — writes rows to `audit_events`
- `backend/app/api/notifications.py` — creates bell notifications
- `backend/app/safe_mode.py` — global simulate-only toggle
- `backend/app/terraform_runner.py` — **legacy**. Nothing in the live path calls it;
  projects are created by direct REST calls in `azure_devops.py`.
- `backend/app/api/azure_devops.py` — pre-flight (custom process creation) for ADO project requests

## Supported request types

The single source of truth is `SUPPORTED_REQUEST_TYPES` in
`backend/app/api/approvals.py`. A type must be in that set **and** have a branch
in `_execute_approved_request` to run.

| `request_type`                | What it does                                                   | Executor                             |
| ----------------------------- | -------------------------------------------------------------- | ------------------------------------ |
| `ADO_PROJECT_CREATE`          | Creates an Azure DevOps project via the REST API (no Terraform) | `_execute_ado_project_create(...)`   |

`ADO_PROJECT_CREATE` is the only executable type. The `approval_request_type`
enum in `00_schema.sql` still carries the historical values
(`SONAR_PR_SCANNING_ENABLE`, `AI_MODEL_ACCESS`, `CUSTOM`) so old rows remain
valid, but they are **not** offered in the UI, are refused at submission by
`create_request`, and fail loudly if somehow approved — a request is never
reported as done unless a real executor actually did the work. Adding a new
self-service means adding its type to `SUPPORTED_REQUEST_TYPES` **and** wiring
its executor branch, never one without the other.

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
- Frontend: `frontend/templates/partials/components/automations-container.html`
- Backend actions:
  1. `_validate_request_payload()` checks:
     - No empty required fields.
     - For `ADO_PROJECT_CREATE`: project name matches
       `^[A-Za-z0-9][A-Za-z0-9 _\-.]{1,62}$`, and `admin_username` is any
       non-empty principal (an e-mail *or* an Active-Directory `DOMAIN\user`;
       the real existence check happens against Azure DevOps).
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

`_run_execution_safely(request_id)` runs in a daemon thread and never
raises. `_execute_approved_request` then dispatches. Its checks, in order:

1. **Unsupported type → fail loudly.** If `request_type` is not in
   `SUPPORTED_REQUEST_TYPES`, the request is marked `FAILED` with a clear
   message and nothing runs. This is checked *before* Safe Mode, because Safe
   Mode simulates *available* actions, not absent ones. It only ever fires for
   a legacy row created before this gate existed — the submission endpoint
   already refuses unsupported types.
2. **Safe Mode short-circuit.** If `safe_mode.is_enabled()`, the request is
   marked `COMPLETED` with a `safe_mode: true` payload. The audit event and
   notification still fire so the user experience matches a real run, and no
   external systems are contacted. (`provision_ado_project` re-checks Safe Mode
   itself, so the guard also holds if the orchestrator is called directly.)
3. **Dispatch on `request_type`:**
   - **`ADO_PROJECT_CREATE`** → `_execute_ado_project_create()` →
     `azure_devops.provision_ado_project()`. This is **REST-only**: for each
     target collection it ensures a per-project inherited process, creates the
     project via the ADO REST API under the admin PAT, polls the returned
     operation to completion, and best-effort-adds the requested administrator
     to the project's Project Administrators group. There is **no Terraform, no
     Kubernetes Job and no tfstate** — that legacy path (`terraform_runner.py`)
     is retained but not used here. A per-collection failure is recorded and
     does not undo the others; the executor raises only if *every* collection
     failed, otherwise it returns a `partial` result.
4. `_finish_completed` / `_finish_failed` write the terminal status, emit the
   audit event, notify the requester with a click-through link, and (for a
   completed ADO create) add a "Created project" entry to the activity feed.

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
  - `admin_username` must be non-empty (e-mail or `DOMAIN\user`).
- Any failure raises `HTTPException(400)` with a user-friendly message.

## Duplicate prevention

A request is considered a duplicate if **all** of the following are true:

- Same `requester_id`.
- Same `request_type`.
- Request is currently `PENDING`.
- Same `request_title` (which encodes the project name for
  `ADO_PROJECT_CREATE`, e.g. `Azure DevOps project: <name>`).

Submitting a duplicate returns `409 Conflict`.

## Safe Mode: when to use it

- Dev environments where ADO/ServiceNow are unreachable.
- Demos where you want the full UX without side effects.
- Dry-running a supported action (e.g. an admin verifying the flow) without
  touching Azure DevOps. Note that Safe Mode only simulates *supported* types;
  an unsupported type fails whether Safe Mode is on or off.

Toggle from **Admin → Platform Managing → Safe Mode**, or set
`SAFE_MODE=true` in the environment. Either source is fine — the DB flag
wins when present.
