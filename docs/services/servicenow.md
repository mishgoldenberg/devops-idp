# `servicenow` router

File: `backend/app/api/servicenow.py` · Prefix: `/api/support`

See [`docs/support.md`](../support.md) for the user-facing flow. This
document focuses on the backend module itself.

## Purpose

Read and write ServiceNow incidents on behalf of the portal user, using
a service-account Basic auth. Maintains a local `user_tickets` mapping so
"My Tickets" works even though the incidents are technically opened by
the shared service account.

## Main endpoints

| Method | Path                                       | Description                                    |
| ------ | ------------------------------------------ | ---------------------------------------------- |
| GET    | `/api/support/tickets`                     | Current user's tickets (60 s cache)            |
| GET    | `/api/support/tickets/{sys_id}`            | Ticket detail (Redis-cached)                   |
| POST   | `/api/support/tickets`                     | Create a new incident                          |
| PATCH  | `/api/support/tickets/{sys_id}`            | Update a ticket                                |
| GET    | `/api/support/tickets/{sys_id}/messages`   | Journal (comment + work-note) history          |
| POST   | `/api/support/tickets/{sys_id}/messages`   | Append a comment                               |

## External APIs used

- `GET/POST/PATCH {SERVICENOW_URL}/api/now/table/incident[/{sys_id}]`
- `GET/POST {SERVICENOW_URL}/api/now/table/sys_journal_field`

Authentication: HTTP Basic (`SERVICENOW_USERNAME` / `SERVICENOW_PASSWORD`).

## Environment variables

- `USE_MOCK_SERVICENOW` — default `true`; returns canned data.
- `SERVICENOW_URL` (or `SERVICENOW_INSTANCE`) — instance URL.
- `SERVICENOW_USERNAME` (or `SERVICENOW_USER`) + `SERVICENOW_PASSWORD`.

## Data written

- `user_tickets` — mapping insert on every successful create.
- `servicenow_tickets` — observability counter via
  `observability_tracking.record_servicenow_ticket`.
- `audit_events` — `TICKET_CREATED` on create; `TICKET_UPDATED` on
  patch.
- `notifications` — `TICKET_CREATED` with `link=/ui/support#{sys_id}`
  and `group_key=ticket:{sys_id}`.

## Performance / caching

- `GET /tickets` is wrapped with
  `integrations_cache.cached_external("snow", email, "tickets:<user>", …, ttl=60)`.
- `GET /tickets/{sys_id}` uses `cache.get_cached("snow:detail:{sys_id}", …)`.
- Writes invalidate both keys + call `integrations_cache.invalidate_owner("snow", email)`.

## Failure handling

- All HTTP calls go through `resilient_http.resilient_request` (single
  retry on 5xx / network errors, 30 s timeout).
- Transport failures → 502 with the generic "Service temporarily
  unavailable" message.
- 4xx is forwarded with the ServiceNow error message so form validation
  errors reach the UI.

## Safe Mode

- `POST /tickets` returns a simulated success payload
  (`INC9999999`-style) without contacting SNOW.
- Nothing is written to `user_tickets` (the simulated sys_id isn't
  real).
- An audit event is still written with `metadata.safe_mode=true`.
