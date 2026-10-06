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

Every endpoint that takes a ticket id first checks it is on the caller's own list
(`_require_own_ticket`): the service account can open any ticket.

| Method | Path                                                  | Description                                     |
| ------ | ----------------------------------------------------- | ----------------------------------------------- |
| GET    | `/api/support/tickets`                                | Current user's tickets (60 s cache)             |
| GET    | `/api/support/tickets/{sys_id}`                       | Ticket detail and its conversation              |
| GET    | `/api/support/tickets/{sys_id}/attachments/{id}`      | Download one attachment                         |
| POST   | `/api/support/tickets/reply`                          | Add a comment and attachments to a ticket       |
| POST   | `/api/support/tickets/create-flow`                    | Open a ticket through the record producer       |
| GET    | `/api/support/ticket-form`                            | Admins: the producer's questions, which one carries the requester, and whether you resolve to a ServiceNow user |

## Who a ticket is for

The producer inserts the incident as the service account. The requester is sent in
the producer's caller question (a Reference or Lookup Select Box pointing at
`sys_user`, named `caller_id` on this instance, or `SNOW_CALLER_VAR`), resolved from
their e-mail and login (`snow_catalog.find_user`). Nothing is written to the incident
afterwards, because any update by the service account reassigns it and moves it to In
Progress; `SNOW_CALLER_PATCH=1` accepts that trade. When a ticket opens as the service
account, `GET /api/support/ticket-form` says why.

## External APIs used

- `POST {SNOW_BASE_URL}/api/sn_sc/servicecatalog/items/{producer}/submit_producer`
- `GET/PATCH {SNOW_BASE_URL}/api/now/table/incident[/{sys_id}]`
- `GET {SNOW_BASE_URL}/api/now/table/sys_user`
- `GET {SNOW_BASE_URL}/api/now/table/sys_user_group`
- `POST {SNOW_BASE_URL}/api/now/attachment/file`

Authentication: HTTP Basic (`SNOW_API_USERNAME` / `SNOW_API_PASSWORD`).

## Data written

- `user_tickets` — who opened which ticket, on every successful create.
- `servicenow_tickets` — the Observability counter
  (`observability_tracking.record_servicenow_portal_ticket`).
- `activity` — the person's own "opened a ticket" entry.

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
