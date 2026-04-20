# Observability

The Observability page gives admins a single view of how the portal is
used: which widgets people actually add, which self-services run, how
many tickets are opened, and how quickly requests get approved.

> The Observability page is gated by admin access (see
> `has_effective_admin_access_live` in `api/admin.py`). Two of the
> endpoints (`/widgets/recent`, `/widgets/suggested`) are intentionally
> non-admin — every user should see their own recently-used widgets.

## What's tracked

| Metric                          | Source table             | When it's written                                                               |
| ------------------------------- | ------------------------ | ------------------------------------------------------------------------------- |
| Widget views                    | `widget_usage`           | Every time a widget renders, the partial fires `POST /api/observability/widget` |
| User's current widgets          | `user_widgets`           | Written when the user toggles widgets on the Customize Dashboard drawer         |
| Self-service request outcomes   | `approval_requests`      | Updated through the self-service lifecycle (status column)                      |
| Portal-created ADO projects     | `azure_projects`         | Inserted by `observability_tracking.record_azure_project` on successful run     |
| Portal-created SNOW tickets     | `servicenow_tickets`     | Inserted by `observability_tracking.record_servicenow_ticket` on create         |
| Audit trail                     | `audit_events`           | Every interesting state change (see `audit.py`)                                 |

`observability_tracking.py` is the shared helper that all other modules call
when they want to record a countable event. It's best-effort: a failure
to write observability data never breaks the caller.

## How data is collected

### Client-side → backend

```
┌────────────┐        POST /api/observability/widget
│  Widget    │────────────────────────────────────────► widget_usage
│  partial   │        {widget_key, event_type: "widget_view",
│  (HTMX)    │         session_id}
└────────────┘
```

Each widget partial renders a tiny inline `<script>` that fires the POST
once the widget is in the DOM. This avoids double-counting on swap/retry
because HTMX replaces the partial wholesale.

### Server-side

The approvals workflow, ServiceNow ticket creation, and Azure DevOps
project creation all call into `observability_tracking.py` directly — no
client trigger, no chance to skip it.

## Endpoints

All under `/api/observability`:

| Method | Path                     | Auth                        | Purpose                                                                 |
| ------ | ------------------------ | --------------------------- | ----------------------------------------------------------------------- |
| POST   | `/widget`                | any authed user             | Record one widget view                                                  |
| GET    | `/widgets` / `/usage`    | admin                       | Top widgets by current-dashboard selection (`user_widgets`)             |
| GET    | `/widgets/recent`        | any authed user             | The caller's own recently-used widgets (from `widget_usage`)            |
| GET    | `/widgets/suggested`     | any authed user             | Popular widgets this user hasn't added yet                              |
| GET    | `/self-services`         | admin                       | Counts per `request_type` × status                                      |
| GET    | `/azure-projects`        | admin                       | Completed `ADO_PROJECT_CREATE` requests                                 |
| GET    | `/tickets`               | admin                       | Counts + recent rows from `servicenow_tickets`                          |
| GET    | `/summary`               | admin                       | One-shot KPI summary used by the Observability page header              |

### `/summary` response shape (abridged)

```jsonc
{
  "success": true,
  "data": {
    "top_widgets": [ { "widget_key": "ado_my_work_items", "views": 412 }, ... ],
    "top_self_services": [ { "request_type": "ADO_PROJECT_CREATE", "count": 18 }, ... ],
    "self_services": { "succeeded": 12, "failed": 3, "pending": 2 },
    "ado_projects_created_total": 9,
    "servicenow_tickets_total": 47,
    "avg_approval_seconds": 3421
  },
  "timestamp": "2026-04-19T14:02:10Z"
}
```

`avg_approval_seconds` is `AVG(approved_at - created_at)` for the
approved + completed requests. When there's no data it returns `null`
rather than `0` so the UI can show "no data yet".

## How it's displayed

- **Observability page**: `/ui/observability`, template
  `frontend/templates/observability.html`, container
  `frontend/templates/partials/components/observability-container.html`.
  The container fetches `/api/observability/summary` and renders the KPI
  tiles + the "most used widgets" and "self-services" cards.
- **Smart Dashboard sections**: the Customize Dashboard drawer on the
  home page calls `/widgets/recent` and `/widgets/suggested` and renders
  those as one-click add chips.
- **Top-3 auto-bubble**: the dashboard grid reorders client-side so the
  user's top-3 recently-viewed widgets float to the front. This is pure
  JS (`dashboard-container.html`) — no backend reordering.

## Extending it

To add a new metric:

1. Pick the source table (or create one alongside the existing ones in
   `db.ensure_observability_tables`).
2. Add a `record_<thing>` helper in `observability_tracking.py`.
3. Call that helper from the relevant API route.
4. Surface it in `GET /api/observability/summary` (or a new dedicated
   endpoint).
5. Render it in `observability-container.html`.

Keep writes **best-effort** — wrap them in try/except so a schema drift
never breaks the primary action.
