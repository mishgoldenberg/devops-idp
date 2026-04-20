# Architecture

This document explains how the pieces of DevOps Control Center fit together
and what happens end-to-end when a user performs a typical action.

## 1. High-level view

```
                          ┌────────────────────────────┐
                          │       Browser (HTMX)       │
                          │  Jinja + Tailwind/DaisyUI  │
                          └──────────────┬─────────────┘
                   /ui/… (HTML partials) │  /api/… (JSON)
                                         ▼
┌────────────────────────────────────────────────────────────────────┐
│                 FastAPI app  (backend/app/main.py)                 │
│                                                                    │
│   UI routes (ui.py)        JSON routers (api/*.py)                 │
│   ─ renders Jinja          ─ auth, dashboards, approvals           │
│     pages + partials         azure_devops, servicenow,             │
│                              sonarqube, artifactory,               │
│                              observability, notifications,         │
│                              audit_logs, safe_mode, admin,         │
│                              pins, metrics, health                 │
│                                                                    │
│   Cross-cutting helpers:                                           │
│   ─ security.py                 JWT create/decode, cookie check    │
│   ─ db.py (psycopg2 pool)       query_all/execute + ensure_*()     │
│   ─ cache.py + redis_client.py  Redis get_cached / invalidate      │
│   ─ integrations_cache.py       60 s TTL wrapper for ADO/SNOW      │
│   ─ resilient_http.py           timeouts + retries + safe_call     │
│   ─ terraform_runner.py         Submits a K8s Job to run Terraform │
│   ─ audit.py                    Writes audit_events rows           │
│   ─ safe_mode.py                Global "simulate only" toggle      │
└──────────┬──────────────────────────────────┬──────────────────────┘
           │                                  │
           ▼                                  ▼
   ┌───────────────┐                  ┌───────────────┐
   │  PostgreSQL   │                  │     Redis     │
   │  (primary DB) │                  │ (cache layer) │
   └───────────────┘                  └───────────────┘

External systems (always called from the backend, never from the browser):

   ┌────────────────┐  ┌──────────────┐  ┌────────────┐  ┌───────────────┐
   │  Azure DevOps  │  │  ServiceNow  │  │ SonarQube  │  │  Artifactory  │
   │  (REST + PAT)  │  │ (Table API   │  │  (mocked   │  │   (mocked     │
   │                │  │  + basic     │  │  in code)  │  │   in code)    │
   │                │  │  auth)       │  │            │  │               │
   └────────────────┘  └──────────────┘  └────────────┘  └───────────────┘

Self-service execution lives in its own runtime:

   ┌────────────────────────────────────────────────────────────┐
   │   Kubernetes cluster                                       │
   │                                                            │
   │   backend Deployment ──►  BatchV1 Job (hashicorp/terraform)│
   │                           reads a ConfigMap with the plan, │
   │                           writes state to GCS              │
   └────────────────────────────────────────────────────────────┘
```

## 2. Where code lives and why

| Layer                        | Lives in                          | Why it's separate                                          |
| ---------------------------- | --------------------------------- | ---------------------------------------------------------- |
| HTML rendering               | `backend/app/ui.py` + Jinja       | Keeps HTMX endpoints (`/ui/…`) separate from JSON (`/api`) |
| REST API                     | `backend/app/api/*.py`            | One router per domain, registered in `api/__init__.py`     |
| DB access                    | `backend/app/db.py`               | Single psycopg2 pool + explicit `ensure_*` DDL helpers     |
| External HTTP                | `backend/app/api/*.py` via `httpx`| Per-integration; resilience wrapped by `resilient_http.py` |
| Integration cache            | `backend/app/integrations_cache.py` | 60 s TTL keyed `ext:<integration>:<owner>:<key>`         |
| Auth                         | `backend/app/security.py`         | JWT + `get_current_user` used by every router              |
| Terraform jobs               | `backend/app/terraform_runner.py` | K8s Job is the execution sandbox                           |

## 3. Authentication flow

1. Browser hits `/ui/auth` (login page) and clicks "Continue with Google".
2. Backend route `/api/auth/login` redirects to Google's consent screen.
3. Google redirects to `/api/auth/callback` with a code.
4. The backend exchanges the code for Google tokens, looks up or creates a
   user row in Postgres, and issues an **internal HS256 JWT**
   (`security.create_access_token`).
5. The JWT is stored in an `auth_token` cookie (HttpOnly, SameSite=Lax,
   Secure on HTTPS, `max_age` driven by `JWT_EXPIRY`).
6. Every subsequent request goes through `security.get_current_user`, which
   accepts either the cookie or an `Authorization: Bearer <token>` header.

See `docs/SSO_GOOGLE_OAUTH.md` for the GCP side.

## 4. Frontend ↔ backend communication

- Page navigations hit `/ui/<page>` routes that return a full HTML page
  rendered from a Jinja template under `frontend/templates/`.
- Inside a page, HTMX triggers fetch **partials** (`/ui/components/…`) and
  swaps them into the DOM. Example: the dashboard widgets each fire their
  own `hx-get` and replace a skeleton with a real card.
- Any dynamic data that doesn't fit "render me a fragment" is a regular
  JSON call under `/api/…` made by inline `<script>` blocks. Example: the
  Audit Logs page, the Observability page, the notifications bell.

## 5. External integrations

All four external systems share the same pattern:

1. A router under `backend/app/api/<name>.py`.
2. `USE_MOCK_<NAME>` environment variable defaults to `true` so developers
   can run without credentials.
3. Real mode uses `httpx` with a **fixed timeout** (5–30 s per endpoint).
4. Read endpoints are wrapped with the 60-second cache in
   `integrations_cache.py` when they're hit by more than one widget or
   page at once (e.g. ADO work items, ADO pull requests, SNOW tickets).
5. Write paths invalidate the owner's cache so the next read reflects the
   change immediately (`integrations_cache.invalidate_owner(...)`).

SonarQube and Artifactory routers currently only have mock payloads — there
are no outbound HTTP clients to harden.

## 6. Request lifecycle: self-service "create ADO project"

This is the canonical end-to-end flow that exercises most of the system.

```
Browser                  Backend                    Kubernetes         Azure DevOps
   │                        │                          │                    │
   │ POST /api/approvals/   │                          │                    │
   │   requests             │                          │                    │
   │   {request_type:       │                          │                    │
   │    ADO_PROJECT_CREATE} │                          │                    │
   ├───────────────────────►│                          │                    │
   │                        │  validate payload        │                    │
   │                        │  check for duplicate     │                    │
   │                        │  INSERT approval_requests│                    │
   │                        │    status=PENDING        │                    │
   │                        │  audit.log()             │                    │
   │                        │  create_notification()   │                    │
   │◄───────────────────────┤  {"id":...}              │                    │
   │                        │                          │                    │
   │ (Admin opens Approvals) │                         │                    │
   │ POST /requests/:id/    │                          │                    │
   │   approve              │                          │                    │
   ├───────────────────────►│                          │                    │
   │                        │  UPDATE status=APPROVED  │                    │
   │                        │  audit.log()             │                    │
   │                        │  spawn _execute_request  │                    │
   │                        │    _worker (thread)      │                    │
   │◄───────────────────────┤                          │                    │
   │                        │                          │                    │
   │              [Worker]  │                          │                    │
   │              ───────── │                          │                    │
   │              if SAFE_MODE: fake success, return   │                    │
   │              else:     │                          │                    │
   │                        │  azure_devops.ensure_    │                    │
   │                        │    custom_ado_process()──┼────────────────────► (process create)
   │                        │  terraform_runner.submit_│                    │
   │                        │    terraform_job()───────┼──► create BatchV1  │
   │                        │                          │    Job + CM        │
   │                        │  UPDATE status=IN_PROGRESS                    │
   │                        │  audit.log(TERRAFORM_STARTED)                 │
   │                        │                          │ Job runs:          │
   │                        │                          │ ─ plan             │
   │                        │                          │ ─ apply ──────────► project created
   │                        │                          │ ─ writes log to GCS│
   │                        │                          │                    │
   │                        │  poll job status         │                    │
   │                        │  UPDATE status=COMPLETED │                    │
   │                        │  audit.log()             │                    │
   │                        │  create_notification()   │                    │
   │                        │  observability_tracking. │                    │
   │                        │    record_…              │                    │
   │                        │                          │                    │
   │ (user sees bell update via /api/notifications)    │                    │
```

Key things to notice:

- **Nothing about the Terraform run blocks the HTTP request**: approval
  returns immediately, execution happens in a background worker that polls
  the K8s Job.
- **Status transitions are strictly one-way**
  (`PENDING → APPROVED | REJECTED`, `APPROVED → IN_PROGRESS → COMPLETED |
  FAILED`) so the UI can reason about progress without race conditions.
- **Audit events are appended at every state change** so admins can later
  reconstruct exactly what happened.
- **Safe Mode short-circuits the worker** before any real call, returning a
  faked success. This is how demos run without touching real ADO.

## 7. Data flow for read-heavy dashboards

A single dashboard render can fire ~8 parallel HTMX requests (one per
widget). The backend handles that load by:

1. **Per-widget HTMX target**: each widget partial is its own endpoint, so
   the browser fans them out over HTTP/2.
2. **60 s in-memory/Redis cache** keyed on the user's email and the external
   call signature. Two widgets that both need "my ADO pull requests" share
   one upstream call.
3. **Parallelization inside each call**: e.g. the ADO PR endpoint uses a
   `ThreadPoolExecutor` to fan out per-repo PR queries.
4. **Skeleton placeholders**: while the partial is loading, the outer shell
   renders `partials/components/widget-skeleton.html` so the page layout is
   stable.

## 8. Failure-mode design

- Every outbound HTTP call has a **timeout** (see `resilient_http.py` for
  the default 5 s connect / 10 s read).
- `safe_integration` / `safe_call` wrappers turn exceptions into a
  user-friendly 502 (`"Service temporarily unavailable"`) instead of a raw
  stack trace.
- A global FastAPI exception handler in `main.py` logs the full traceback
  and returns a generic message to the client.
- The frontend has a matching **global JS error boundary** in
  `partials/components/banner.html` that catches uncaught JS errors,
  unhandled promise rejections, and HTMX failures, and shows a single
  throttled toast ("Something went wrong. Please try again.") instead of
  letting the UI disappear.
- Kubernetes liveness/readiness are served by `api/health.py`:
  - `GET /api/health/live` — always 200 if the process is up.
  - `GET /api/health/ready` — 200 iff PostgreSQL is reachable. Redis is
    reported in the JSON but does not fail the probe; otherwise a Redis
    blip would wedge every pod into `NotReady`.
  - `GET /api/health` — 503 if either Postgres or Redis is down (used by
    external monitoring, not by K8s probes).
