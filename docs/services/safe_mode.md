# `safe_mode` router

File: `backend/app/api/safe_mode.py` · Prefix: `/api/safe-mode`
Backing module: `backend/app/safe_mode.py` · Table: `portal_flags`

## Purpose

Expose the global Safe Mode toggle to the UI and let admins flip it at
runtime from **Admin → Platform Managing → Safe Mode**.

When Safe Mode is on, self-service executors (and integrations that
mutate external systems, like ServiceNow ticket creation) **return
simulated success payloads instead of doing real work**. This is how
demos and dev installs run without touching production systems.

## Main endpoints

| Method | Path                       | Auth              | Description                          |
| ------ | -------------------------- | ----------------- | ------------------------------------ |
| GET    | `/api/safe-mode/status`    | Any authed user   | Current on/off state + source        |
| POST   | `/api/safe-mode/toggle`    | Admin             | Flip state (persisted to `portal_flags`) |

`POST /toggle` writes an `audit_events` row (`SAFE_MODE_TOGGLED`) with
the actor's email and the new state.

## Resolution order

`safe_mode.is_enabled()` resolves in this order:

1. The `SAFE_MODE` row in the `portal_flags` table, if present.
2. The `SAFE_MODE` environment variable (`true`/`false`).
3. Default: `False`.

This means:

- Ops can bake `SAFE_MODE=true` into a dev Helm release.
- An admin can flip Safe Mode at runtime in the UI without redeploying —
  and the new value sticks across pod restarts because it's in the DB.
- Flipping off in the UI writes `flag_value="false"` (it doesn't delete
  the row), so the DB wins and you can still trust the state.

## How it's used elsewhere

Integrated at the top of each mutating code path:

- `approvals._execute_request_worker` — returns simulated success.
- `azure_devops.create_ado_project` — returns a fake job id.
- `servicenow.create_ticket` — returns a fake `INC…` payload.

Each call site writes an audit event with `metadata.safe_mode=true` so
simulated actions are distinguishable from real ones in the Audit Logs.
