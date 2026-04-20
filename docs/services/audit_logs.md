# `audit_logs` router

File: `backend/app/api/audit_logs.py` · Prefix: `/api/audit-logs`
Backing module: `backend/app/audit.py` · Backing table: `audit_events`

## Purpose

Admin-only read API for the Audit Logs page (`/ui/audit-logs`). Reports
a filterable, paginated view of every interesting portal event: requests
created/approved/rejected/executed, Terraform started, tickets created,
Safe Mode toggled, etc.

## Main endpoints

| Method | Path                             | Description                                                       |
| ------ | -------------------------------- | ----------------------------------------------------------------- |
| GET    | `/api/audit-logs`                | Paginated list with filters (`user_email`/`user`, `action`, `date_from`, `to`, `limit`, `offset`) |
| GET    | `/api/audit-logs/actions`        | Distinct `action` values (for the filter dropdown)               |

## Writing audit events

Other modules import `audit.log` (alias for `audit.log_event`) and call:

```python
audit.log(
    user_email=user.email,
    action=audit.Action.SELF_SERVICE_REQUEST_APPROVED,
    metadata={"request_id": row["id"], "approver": admin.email},
)
```

`audit.Action` groups the common event names. Adding a new action is as
simple as inserting a string constant — there's no ENUM to `ALTER`.

## Why a new table instead of reusing `audit_logs`?

The legacy `audit_logs` table constrains `action` to an `audit_action`
ENUM, which makes adding new event types fiddly and risky. `audit_events`
uses `VARCHAR` + `JSONB`, which is flexible enough to capture any future
event shape without schema migrations.

Both tables still exist; `audit_events` is the one the Audit Logs page
reads.

## Failure handling

`audit.log_event` catches every exception and logs it at `warning`. A
DB outage must never break the user-facing action that triggered the
log.
