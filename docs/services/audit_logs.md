# `audit_logs` router

File: `backend/app/api/audit_logs.py` · Prefix: `/api/audit-logs`
Backing module: `backend/app/audit.py` · Backing table: `audit_events`

## Purpose

Admin-only API for the Logs page (`/ui/audit-logs`): a filterable, paginated
view of everything the portal did, and the one place rows can be deleted.

Three sources land in the same table (see `backend/app/request_audit.py`):

* `app` — named business events: a request approved, a ticket created, Safe
  Mode toggled, an announcement posted.
* `http` — every state-changing request, and every request that FAILED,
  recorded by middleware. Successful GETs are never recorded: with ten polling
  widgets they would bury everything else.
* `system` — WARNING and above from the backend's own Python loggers, so an
  Artifactory timeout or a ServiceNow rejection is one row away from the user
  action that triggered it.

A failed request carries the REASON, not just the status (`metadata.detail`),
and identical failing reads from a polling widget are collapsed into one row
per five minutes carrying `metadata.repeated`.

## Main endpoints

| Method | Path                             | Description                                                       |
| ------ | -------------------------------- | ----------------------------------------------------------------- |
| GET    | `/api/audit-logs`                | Paginated list with filters (`user_email`/`user`, `action`, `date_from`, `to`, `limit`, `offset`) |
| GET    | `/api/audit-logs/actions`        | Distinct `action` values (for the filter dropdown)               |
| GET    | `/api/audit-logs/levels`         | The five level names                                             |
| GET    | `/api/audit-logs/export`         | The current view as CSV, capped at 5000 rows                     |
| DELETE | `/api/audit-logs`                | **Deletes** the rows matching the same filters. An unfiltered clear requires `confirm_all=true`; the clear itself is logged afterwards, so it survives its own purge. |

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

Both tables still exist; `audit_events` is the one the Logs page reads.

## Retention

Two cutoffs, swept every six hours: `AUDIT_RETENTION_DAYS` (7) for DEBUG/INFO
and `PROBLEM_RETENTION_DAYS` (30) for WARNING and above — "what just happened"
stops being interesting quickly, "why is this broken" gets asked weeks later.
The same loop checks the database size and warns before the volume fills; see
[../RUNBOOK.md](../RUNBOOK.md) §5.

## Failure handling

`audit.log_event` catches every exception and logs it at `warning`. A
DB outage must never break the user-facing action that triggered the
log.
