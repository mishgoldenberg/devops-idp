# Support (ServiceNow integration)

The Support page lets a user:

- See the ServiceNow tickets they've opened through the portal.
- Open a new incident through a 4-step form with user info, service details,
  ticket details, attachments, and summary.
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

ServiceNow's Table API is called with **Basic auth** using a service-account
user. The 4-step creation flow resolves the signed-in user's SSO email to a
ServiceNow `sys_user.sys_id`, resolves the `Devops Support` group, and uses
those sys_ids as the caller/opened-by and assignment group.

Env vars used:

- `SNOW_BASE_URL` — full instance URL.
- `SNOW_API_USERNAME` + `SNOW_API_PASSWORD` — service-account credentials.

## Endpoints and ServiceNow calls

Listed once, in [services/servicenow.md](./services/servicenow.md). Each ServiceNow
client has an explicit connect and read timeout; a failure reaches the person as a
sentence that says whose problem it is, never as a raw error.

## Ticket flow end-to-end

```
1. The person fills in the Support wizard (catalog_forms.py, "support")
   └─► POST /api/support/tickets/create-flow  (multipart: answers + attachments)

2. Backend (api/servicenow.py)
   ├─ Validates the answers; the name on the ticket is the identity provider's.
   ├─ Resolves the requester to a sys_user (snow_catalog.find_user).
   ├─ Submits the "Open A Ticket" record producer with the answers and the
   │  requester in its caller question (_caller_variable).
   ├─ If the producer refuses: creates the incident directly, answers in the
   │  description, caller on the insert.
   ├─ Reads the ticket back once: number, state, and who it is for.
   ├─ Uploads the attachments.
   └─ Records the ticket in user_tickets, the Observability counter and the
      person's activity feed.

3. integrations_cache.invalidate_owner("snow", email)
   └─ The next /tickets read goes live, so the new ticket shows at once.

4. The person replies
   └─ POST /api/support/tickets/reply (sys_id, message, attachments)
      ├─ Checks the ticket is theirs (_require_own_ticket).
      ├─ Writes the comment on the incident; on the instance's 403 after the
      │  comment is saved, re-reads to confirm it landed.
      └─ Invalidates the ticket's cached detail and the person's list.
```

## Caching behavior

- **List** (`/tickets`) is cached in Redis for 60 s under the key
  `ext:snow:<email>:tickets:<username>`.
- **Detail** (`/tickets/{sys_id}`) is cached under `snow:detail:{sys_id}`.
- **Writes** (create a ticket, reply) always invalidate
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
