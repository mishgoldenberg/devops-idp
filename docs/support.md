# Support (ServiceNow integration)

The Support page lets a user:

- See the ServiceNow tickets they've opened through the portal.
- Open a new incident with a title + description (+ optional priority /
  category).
- View the threaded comment history of a ticket and add replies.

All ServiceNow calls happen in the backend — the browser only talks to
`/api/support/…`. This keeps the ServiceNow credentials out of the
client, and means we can swap the transport (Table API today, OAuth +
REST in the future) without changing the UI.

Code:

- Router: `backend/app/api/servicenow.py` (mounted at `/api/support`)
- Template: `frontend/templates/support.html`
- Partials: `frontend/templates/partials/components/servicenow-container.html`

## How authentication works today

ServiceNow's Table API is called with **Basic auth** using a
service-account user. Every ticket the portal creates is filed under
that service account, and the portal's own `user_tickets` table keeps
the mapping from portal user → `sys_id` so "My Tickets" still works.

Env vars used:

- `SERVICENOW_URL` — full instance URL, preferred.
- `SERVICENOW_INSTANCE` — fallback if URL isn't set.
- `SERVICENOW_USERNAME` (or `SERVICENOW_USER`) + `SERVICENOW_PASSWORD`.
- `USE_MOCK_SERVICENOW` — `true` (default) returns canned data.

The future OAuth flow (`SERVICENOW_CLIENT_ID`/`SECRET` in `env.example`)
is reserved for when SSO-scoped client credentials are available.

## Endpoints

All under `/api/support`:

| Method  | Path                          | Description                                                       |
| ------- | ----------------------------- | ----------------------------------------------------------------- |
| GET     | `/tickets`                    | Current user's tickets (cached 60 s per user via `integrations_cache`) |
| GET     | `/tickets/{sys_id}`           | Full ticket detail incl. status, priority, caller                |
| POST    | `/tickets`                    | Create a new ticket                                              |
| GET     | `/tickets/{sys_id}/messages`  | Journal entries (comments + work notes)                          |
| POST    | `/tickets/{sys_id}/messages`  | Add a new comment to the ticket                                  |
| PATCH   | `/tickets/{sys_id}`           | Update ticket fields (priority, state, assignment)               |

## ServiceNow APIs we call

- **`GET /api/now/table/incident`** — list + detail reads.
- **`POST /api/now/table/incident`** — ticket creation.
- **`PATCH /api/now/table/incident/{sys_id}`** — updates.
- **`GET /api/now/table/sys_journal_field?element_id={sys_id}`** —
  comment history.
- **`POST /api/now/table/incident/{sys_id}` with `{comments}`** —
  append-only comment.

Our portal wraps each call with:

- A 30 s timeout (`httpx.Client(timeout=30.0)`).
- A single retry on transient 5xx via `resilient_http.resilient_request`.
- `try/except`-to-`HTTPException(502, "Service temporarily unavailable")`
  so ServiceNow hiccups never leak as raw errors.

## Ticket flow end-to-end

```
1. User clicks "New Ticket" on Support page
   └─► POST /api/support/tickets {short_description, description, priority}

2. Backend (api/servicenow.py)
   ├─ Validates title/description are non-empty.
   ├─ Safe Mode short-circuit? → return a fake success payload.
   ├─ POST /api/now/table/incident
   ├─ Insert (user_email, sys_id, ticket_number) into user_tickets.
   ├─ observability_tracking.record_servicenow_ticket(...)
   ├─ audit.log(Action.TICKET_CREATED, {sys_id, number})
   └─ create_notification(
          user_email=caller,
          message=f"Ticket {number} opened",
          notif_type="TICKET_CREATED",
          link=f"/ui/support#{sys_id}",
          group_key=f"ticket:{sys_id}")

3. integrations_cache.invalidate_owner("snow", email)
   └─ Next /tickets call goes live so the new ticket shows instantly.

4. User views the ticket
   └─ GET /api/support/tickets/{sys_id}  (cache.get_cached for 60 s)

5. User replies
   └─ POST /api/support/tickets/{sys_id}/messages {comment}
      ├─ POST to sys_journal_field
      ├─ Invalidates snow:detail:{sys_id} and snow:tickets:{email}
      └─ Returns the new message for optimistic UI append
```

## Caching behavior

- **List** (`/tickets`) is cached in Redis for 60 s under the key
  `ext:snow:<email>:tickets:<username>`.
- **Detail** (`/tickets/{sys_id}`) is cached under `snow:detail:{sys_id}`.
- **Writes** (create ticket, add comment, patch fields) always invalidate
  the affected keys immediately so the next read returns fresh data.

## Errors the UI is prepared for

| Backend situation                                          | Returned to UI                                | UI behavior                                    |
| ---------------------------------------------------------- | --------------------------------------------- | ---------------------------------------------- |
| Auth credentials missing                                   | 500 — logged                                   | Toast "Service temporarily unavailable"        |
| ServiceNow 4xx (bad payload)                              | 400 with ServiceNow's error message           | Inline form error                               |
| ServiceNow 5xx, network timeout                            | 502 "Service temporarily unavailable"         | Toast; user can retry                           |
| Ticket not found / not owned by user                      | 404                                            | "Ticket not found" state                        |

## Safe Mode

When Safe Mode is enabled:

- `POST /api/support/tickets` returns a fake `INC9999999`-style success.
- No call leaves the pod.
- `user_tickets` is **not** updated (nothing to track).
- Audit event is still written with `metadata.safe_mode=true` so admins
  can tell simulated tickets apart from real ones.
