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
│                              pins, metrics, health, devbot*,       │
│                              adminbot                              │
│                                                                    │
│   Cross-cutting helpers:                                           │
│   ─ security.py                 JWT create/decode, cookie check    │
│   ─ db.py (psycopg2 pool)       query_all/execute + ensure_*()     │
│   ─ cache.py + redis_client.py  Redis get_cached / invalidate      │
│   ─ integrations_cache.py       60 s TTL wrapper for ADO/SNOW      │
│   ─ resilient_http.py           timeouts + retries + safe_call     │
│   ─ audit.py                    Writes audit_events rows           │
│   ─ request_audit.py            Middleware + log mirror into audit │
│   ─ widget_registry.py          The ONE dashboard widget catalogue │
│   ─ safe_mode.py                Global "simulate only" toggle      │
│   ─ devbot/                     DevBot / AdminBot engine (§9)      │
└──────────┬──────────────────────────────────┬──────────────────────┘
           │                                  │
           ▼                                  ▼
   ┌───────────────┐                  ┌───────────────┐
   │  PostgreSQL   │                  │     Redis     │
   │  (primary DB) │                  │ (cache layer) │
   └───────────────┘                  └───────────────┘

External systems (always called from the backend, never from the browser):

   ┌──────────────┐ ┌──────────────┐ ┌────────────┐ ┌─────────────┐ ┌────────────┐
   │ Azure DevOps │ │  ServiceNow  │ │ SonarQube  │ │ Artifactory │ │ Confluence │
   │ (REST + PAT) │ │ (Table API + │ │ (Web API + │ │ (REST +     │ │ (REST +    │
   │              │ │  basic auth) │ │  token)    │ │  token)     │ │  token)    │
   └──────────────┘ └──────────────┘ └────────────┘ └─────────────┘ └────────────┘

   ┌──────────────────────────────────────┐
   │ AI gateway (OpenAI-compatible /v1,   │  DevBot and AdminBot only (§9)
   │ e.g. LiteLLM over vLLM)              │
   └──────────────────────────────────────┘
```

Self-service runs inside the backend: an approved request is executed by a background
thread calling the target system's REST API (§6). There is no Terraform and no
Kubernetes Job.

## 2. Where code lives and why

| Layer                        | Lives in                          | Why it's separate                                          |
| ---------------------------- | --------------------------------- | ---------------------------------------------------------- |
| HTML rendering               | `backend/app/ui.py` + Jinja       | Keeps HTMX endpoints (`/ui/…`) separate from JSON (`/api`) |
| REST API                     | `backend/app/api/*.py`            | One router per domain, registered in `api/__init__.py`     |
| DB access                    | `backend/app/db.py`               | Single psycopg2 pool + explicit `ensure_*` DDL helpers     |
| External HTTP                | `backend/app/api/*.py` via `httpx`| Per-integration; resilience wrapped by `resilient_http.py` |
| Integration cache            | `backend/app/integrations_cache.py` | 60 s TTL keyed `ext:<integration>:<owner>:<key>`         |
| Auth                         | `backend/app/security.py`         | JWT + `get_current_user` used by every router              |
| Provisioning                 | `backend/app/api/azure_devops.py` | REST calls with the admin PAT; no Terraform, no K8s Job    |
| Widget catalogue             | `backend/app/widget_registry.py`  | One list; a second copy always drifts                      |
| Dashboard announcements      | `backend/app/api/announcements.py`| Admin → everyone, dismissed per person and per version     |
| AI assistant                 | `backend/app/devbot/`             | Gateway client, orchestrator, tools, search index (§9)     |

## 3. Authentication flow

1. Browser hits `/ui/auth` (login page) and clicks "Sign in with SSO".
2. Backend route `/api/auth/login` reads the enabled `sso_config` row.
3. The configured OIDC provider redirects to `/api/auth/sso/callback` with a code.
4. The backend exchanges the code, validates the ID token, looks up or creates a
   user row in Postgres, and issues an **internal HS256 JWT**
   (`security.create_access_token`).
5. The JWT is stored in an `auth_token` cookie (HttpOnly, SameSite=Lax,
   Secure on HTTPS, `max_age` driven by `JWT_EXPIRY`).
6. Every subsequent request goes through `security.get_current_user`, which
   accepts either the cookie or an `Authorization: Bearer <token>` header.

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

All five external systems share the same pattern:

1. A router under `backend/app/api/<name>.py` (SonarQube, Artifactory and
   Confluence share `api/integrations.py`).
2. Each person's own token, entered on the Connections page and stored
   encrypted; service accounts (the Azure DevOps admin PAT, the ServiceNow
   account) come from the environment.
3. Outbound calls use `httpx` with an explicit connect and read timeout, and a
   failure is classified in words (`resilient_http.explain_integration_failure`).
4. Read endpoints are wrapped with the 60-second cache in
   `integrations_cache.py` when they're hit by more than one widget or
   page at once (e.g. ADO work items, ADO pull requests, SNOW tickets).
5. Write paths invalidate the owner's cache so the next read reflects the
   change immediately (`integrations_cache.invalidate_owner(...)`).

## 6. Request lifecycle: self-service "create ADO project"

This is the canonical end-to-end flow that exercises most of the system.

```
Browser                  Backend                                       Azure DevOps
   │                        │                                               │
   │ POST /api/approvals/   │                                               │
   │   requests             │                                               │
   │   {request_type:       │                                               │
   │    ADO_PROJECT_CREATE} │                                               │
   ├───────────────────────►│                                               │
   │                        │  validate payload                             │
   │                        │  check for duplicate                          │
   │                        │  INSERT approval_requests                     │
   │                        │    status=PENDING                             │
   │                        │  audit.log()                                  │
   │                        │  create_notification()                        │
   │◄───────────────────────┤  {"id":...}                                   │
   │                        │                                               │
   │ (Admin opens Approvals) │                                              │
   │ POST /requests/:id/    │                                               │
   │   approve              │                                               │
   ├───────────────────────►│                                               │
   │                        │  UPDATE status=APPROVED                       │
   │                        │  audit.log()                                  │
   │                        │  spawn _execute_request                       │
   │                        │    _worker (thread)                           │
   │◄───────────────────────┤                                               │
   │                        │                                               │
   │              [Worker]  │                                               │
   │              ───────── │                                               │
   │              if SAFE_MODE: fake success, return                        │
   │              else:     │                                               │
   │                        │  azure_devops.ensure_                         │
   │                        │    custom_ado_process()───────────────────────► (process create)
   │                        │  _create_project_in_                          │
   │                        │    collection()  ─────────────────────────────► POST _apis/projects
   │                        │  (REST, admin PAT, ONE                        │   (the collection
   │                        │   collection - the one                        │    the request named)
   │                        │   the request named)                          │
   │                        │  UPDATE status=IN_PROGRESS                    │
   │                        │                                               │
   │                        │  poll operation status                        │
   │                        │  UPDATE status=COMPLETED                      │
   │                        │  audit.log()                                  │
   │                        │  create_notification()                        │
   │                        │  observability_tracking.                      │
   │                        │    record_…                                   │
   │                        │                                               │
   │ (user sees bell update via /api/notifications)                         │
```

Key things to notice:

- **Nothing about the execution blocks the HTTP request**: approval
  returns immediately, execution happens in a background worker that polls
  Azure DevOps for the operation's result.
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

### Under load (500 people)

One backend process answers about 120 requests a second on a developer machine with
everything else on it too; production runs at least two and scales to ten
(`charts/backend` HPA). What keeps it there:

- **Connections are kept and waited for** (`db.py`). psycopg2's pool closes every
  connection it gets back beyond `minconn`, so under load nearly every query opened a
  new one; `_KeepingPool` keeps up to `DB_POOL_MAX`. A request that finds them all in
  use waits up to `DB_POOL_WAIT` seconds instead of failing with "pool exhausted".
- **100 request threads** (`WORKER_THREADS`): most requests wait on another system,
  and with Starlette's 40 a slow Azure DevOps held up everybody's page render.
- **Per-request work only where it is used**: the sidebar's admin check and system
  links run for pages, not for `/static` and `/api`; the live admin answer is cached
  for 30 s like the active-account check; Jinja does not look at template files on
  disk per render (`TEMPLATES_AUTO_RELOAD`); gzip runs at level 2.
- **One request where there were many**: the dashboard reports which widgets were
  drawn in one post, rewrites the widget list in one transaction and only when it
  changed, and the notification list is read when the bell is about to be opened.

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

## 9. The AI assistant (DevBot and AdminBot)

`backend/app/devbot/` answers questions in chat on `/ui/devbot` (and `/ui/adminbot`
for admins). A question streams back over one POST as server-sent events. The
orchestrator plans it to fit the AI key's per-minute limits and the model's context,
offers the model only the tools the question needs, runs the lookups it asks for with
the person's own tokens (the same integration code the widgets use), and makes the
last round without tools so every question ends in an answer. Changes are only ever
proposed: the person reviews them in the widgets' own dialog or the Hub's own form and
confirms them in one loud confirmation (`action-confirm.html`) that says an AI drafted
it and whose decision it is; at most 5 are proposed in an answer, 30 in a conversation.

Search by meaning over Confluence pages and past fixes (closed work items, ticket
resolution notes) uses an index the Hub builds in the background with its own AI key:
int8 vectors in ordinary Postgres tables, ranked in Python in each pod, no vector
database. Past fixes are rewritten by the model before they are kept.

The full design, including every step of the index build and who is shown what:
[devbot.md](devbot.md).
