# Database

DevOps Control Center uses a single PostgreSQL 16 database,
`devops_control_center`, owned by the `devops` role.

There are two creation paths and it's important to understand both:

1. **SQL files** in
   [`deployment/charts/infrastructure/database/`](../deployment/charts/infrastructure/database/)
   run as a Helm `migration-job` on fresh installs. They set up enums,
   roles, the core tables, and views.
2. **Python `ensure_*()` helpers** in `backend/app/db.py` (plus a few
   siblings) run at app startup via `@app.on_event("startup")`. They are
   idempotent (`CREATE TABLE IF NOT EXISTS …`, `ALTER TABLE … IF NOT
   EXISTS …`) so adding a column never requires a manual migration.

If you see a column that isn't in the SQL files, it was added via an
`ensure_*` helper later.

---

## Table reference

Tables are grouped by purpose. For each table we list what it's for, the
columns you're most likely to touch, and how it relates to the others.

### Users & roles (`00_schema.sql`, `01_roles.sql`, `02_users.sql`)

#### `roles`

Lookup table of RBAC roles.

| Column             | Notes                                               |
| ------------------ | --------------------------------------------------- |
| `id`               | Integer PK (1 = Platform Admin, highest level)      |
| `name`             | Display name                                        |
| `hierarchy_level`  | 1–7, lower is more privileged                       |
| `permissions`      | JSON array of permission strings                    |

#### `users`

One row per authenticated user. Populated automatically on first
successful Google login (`api/auth.py`).

| Column           | Notes                                           |
| ---------------- | ----------------------------------------------- |
| `id`             | UUID PK                                         |
| `username`       | Unique — email local part or SSO subject        |
| `email`          | Unique, lowercased                              |
| `role_id`        | FK → `roles.id`                                 |
| `domain_groups`  | JSON — cached from SSO; not currently used      |
| `created_at`     | Timestamp                                       |
| `updated_at`     | Timestamp, updated on role grants               |

#### `system_config`

Small KV store for bootstrap/admin settings, including per-user ADO PAT
storage (when Vault is disabled).

---

### Approvals (`00_schema.sql`, patched by `db.ensure_approval_workflow_tables`)

#### `approval_requests`

Every self-service request lives here — this is the source of truth for
"My Requests", "Approvals", and several observability reads.

| Column                 | Notes                                                                            |
| ---------------------- | -------------------------------------------------------------------------------- |
| `id`                   | UUID PK                                                                          |
| `requester_id`         | FK → `users.id`                                                                  |
| `request_type`         | ENUM `approval_request_type` (`ADO_PROJECT_CREATE`, `SONAR_PR_SCANNING_ENABLE`, `AI_MODEL_ACCESS`, `CUSTOM`) |
| `request_payload`      | JSONB — shape depends on `request_type`                                          |
| `status`               | ENUM `approval_status` (`PENDING`, `APPROVED`, `REJECTED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, legacy `EXECUTED`) |
| `approver_id`          | FK → `users.id`, nullable                                                        |
| `approver_comments`    | Free text                                                                        |
| `rejection_reason`     | Populated on reject (added via `ALTER TABLE IF NOT EXISTS`)                      |
| `approved_at`          | Timestamp                                                                        |
| `executed_at`          | Timestamp                                                                        |
| `created_at`           | Timestamp                                                                        |

`IN_PROGRESS` and `COMPLETED` are added by the Python `ensure_*` helper
(`ALTER TYPE … ADD VALUE IF NOT EXISTS …`) because they didn't exist in
the original SQL enum.

#### `approval_rules`

Static rule table — which `request_type` needs approval from which role.

---

### Audit

Two tables exist, deliberately.

#### `audit_logs` (from `00_schema.sql`, legacy)

Older audit sink with an `audit_action` ENUM column. Still written to by
some code paths for backwards compatibility.

| Column           | Notes                                       |
| ---------------- | ------------------------------------------- |
| `id`             | UUID PK                                     |
| `user_id`        | FK → `users.id`                             |
| `action`         | ENUM `audit_action` (fixed set)             |
| `resource_type`  | String                                      |
| `resource_id`    | UUID                                        |
| `details`        | JSONB                                       |
| `ip_address`     | String                                      |
| `user_agent`     | String                                      |
| `success`        | Boolean                                     |
| `error_message`  | Text                                        |
| `created_at`     | Timestamp                                   |

#### `audit_events` (created by `audit.ensure_table()`)

Newer, more flexible audit table used by the "Audit Logs" admin page.
`action` is a free `VARCHAR` and `metadata` is `JSONB`, so adding a new
event type is a one-liner instead of an `ALTER TYPE`.

| Column        | Notes                                  |
| ------------- | -------------------------------------- |
| `id`          | BIGSERIAL PK                           |
| `user_email`  | VARCHAR(255), indexed                  |
| `action`      | VARCHAR(128), indexed                  |
| `metadata`    | JSONB                                  |
| `created_at`  | Timestamp, indexed                     |

---

### Dashboards & widgets

#### `dashboards`

Per-user dashboard definition (layout + selected widgets). Used by the
React-era and transitional pages; the HTMX home page now tracks selection
via cookie + `user_widgets`.

#### `user_widgets` (from `db.ensure_observability_tables`)

Current state: which widgets does a user have on their dashboard
**right now**. One row per (user, widget).

| Column        | Notes                                        |
| ------------- | -------------------------------------------- |
| `id`          | BIGSERIAL PK                                 |
| `user_id`     | Text — email or id                           |
| `widget_key`  | String (e.g. `ado_my_work_items`)            |
| `session_id`  | Nullable                                     |
| `created_at`  | Timestamp                                    |

#### `widget_usage` (from `db.ensure_observability_tables`)

Event stream: one row per widget render. Used for
"Recently used" + "Suggested for you" and for the observability page's
"most used widgets" metric.

| Column        | Notes                                            |
| ------------- | ------------------------------------------------ |
| `id`          | BIGSERIAL PK                                     |
| `user_id`     | Text                                             |
| `widget_key`  | String                                           |
| `event_type`  | String (currently `widget_view`)                 |
| `session_id`  | Nullable                                         |
| `created_at`  | Timestamp, indexed                               |

#### `widget_types`

Lookup table describing each widget (label, RBAC minimums, icon).

---

### Notifications (`db.ensure_approval_workflow_tables`)

In-app bell notifications.

| Column       | Notes                                                                      |
| ------------ | -------------------------------------------------------------------------- |
| `id`         | BIGSERIAL PK                                                               |
| `user_email` | Recipient                                                                  |
| `message`    | Text                                                                       |
| `notif_type` | String (`REQUEST_SUBMITTED`, `REQUEST_APPROVED`, `TICKET_CREATED`, …)      |
| `related_id` | Optional FK reference (UUID/string)                                        |
| `link`       | Optional URL to navigate to on click (added via `ALTER TABLE IF NOT EXISTS`) |
| `group_key`  | Optional grouping key — notifications sharing this collapse in the bell (added via `ALTER`) |
| `is_read`    | Boolean                                                                    |
| `created_at` | Timestamp                                                                  |

Old notifications (older than 60 days) are pruned opportunistically on
list.

---

### Integrations

#### `user_tickets`

Maps portal users to the ServiceNow tickets they created through the UI so
"My Tickets" can find them even if ServiceNow's own `opened_by` is the
shared service account.

| Column          | Notes                                       |
| --------------- | ------------------------------------------- |
| `id`            | SERIAL PK                                   |
| `user_email`    | The portal user                             |
| `sys_id`        | ServiceNow sys_id                           |
| `ticket_number` | Human-readable ticket number                |
| `created_at`    | Timestamp                                   |
| (unique)        | `(user_email, sys_id)`                      |

#### `servicenow_tickets`

Observability-only counter; tracks tickets opened via the portal.

#### `azure_projects`

Observability-only counter; tracks Azure DevOps projects provisioned via
the portal's self-service flow.

---

### Safe Mode

#### `portal_flags` (from `safe_mode.ensure_table()`)

Very small KV table for global feature flags that admins can toggle at
runtime.

| Column        | Notes                                  |
| ------------- | -------------------------------------- |
| `flag_key`    | PK (e.g. `SAFE_MODE`)                  |
| `flag_value`  | Text (`true` / `false`)                |
| `updated_by`  | Email of the admin who flipped it     |
| `updated_at`  | Timestamp                              |

If a row exists it wins over the `SAFE_MODE` environment variable,
otherwise the env var is used, otherwise Safe Mode defaults to off.

---

### Pins / seen markers (`db.ensure_item_tables`)

Small per-user interaction state used by several widgets (starred work
items, "new since last visit" markers, etc.).

- `user_item_pins` — one row per (user, item).
- `user_item_seen` — last time a user viewed a widget/resource.

---

### Metrics tables (`00_schema.sql`)

These are the older "aggregated metrics" tables used by
`api/metrics.py`. They pre-date the `widget_usage` event stream and
remain for the platform metrics dashboard.

- `usage_metrics` — one row per metric event.
- `daily_metrics` — daily rollups.
- `service_health` — service status snapshots.

---

## Relationships at a glance

```
users ──┬── approval_requests (requester_id, approver_id)
        ├── user_widgets / widget_usage (user_id = email)
        ├── user_tickets (user_email)
        └── notifications (user_email)

approval_requests ──► audit_events (via action = REQUEST_*)
servicenow tickets ──► audit_events (action = TICKET_CREATED)
portal_flags   — standalone, used by safe_mode module.
```

---

## Where to look next

- For the actual DDL, see
  [`deployment/charts/infrastructure/database/`](../deployment/charts/infrastructure/database/)
  and the `ensure_*` helpers in `backend/app/db.py`,
  `backend/app/audit.py`, `backend/app/safe_mode.py`.
- To understand which endpoints read/write which tables, see the matching
  [`docs/services/`](./services/) file.
