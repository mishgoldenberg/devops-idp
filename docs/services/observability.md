# `observability` router

File: `backend/app/api/observability.py` · Prefix: `/api/observability`

See [`docs/observability.md`](../observability.md) for the end-user
story. This doc describes the router itself.

## Purpose

Write endpoints:

- `POST /widget` — clients call this on every widget render to
  increment `widget_usage`.

Read endpoints:

- Admin-only aggregates (top widgets, self-service counts, ticket
  totals, ADO projects total, avg approval time).
- Per-user widget recommendations (`/widgets/recent`,
  `/widgets/suggested`) used by the dashboard Customize drawer.

## Endpoints

| Method | Path                                     | Auth              | Description                                        |
| ------ | ---------------------------------------- | ----------------- | -------------------------------------------------- |
| POST   | `/api/observability/widget`              | Any authed user   | Record one widget view                             |
| GET    | `/api/observability/widgets`             | Admin             | Widgets currently on users' dashboards             |
| GET    | `/api/observability/widgets/usage`       | Admin             | Alias of above                                     |
| GET    | `/api/observability/widgets/recent`      | Any authed user   | Caller's own recent widget views (from `widget_usage`) |
| GET    | `/api/observability/widgets/suggested`   | Any authed user   | Popular widgets the caller hasn't added yet        |
| GET    | `/api/observability/self-services`       | Admin             | Per-type × status counts from `approval_requests`  |
| GET    | `/api/observability/azure-projects`      | Admin             | Completed `ADO_PROJECT_CREATE` requests            |
| GET    | `/api/observability/tickets`             | Admin             | `servicenow_tickets` counts + recent rows          |
| GET    | `/api/observability/summary`             | Admin             | One-shot KPI tile payload                          |

## Tables read / written

- Writes: `widget_usage`.
- Reads: `widget_usage`, `user_widgets`, `approval_requests`,
  `azure_projects`, `servicenow_tickets`.

## Failure handling

All reads are defensive: if a table is missing (e.g. fresh install
before the ensure helpers have run) the endpoint returns
`{"success": True, "data": []}` rather than 500.
