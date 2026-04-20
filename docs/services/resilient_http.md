# `resilient_http` (cross-cutting)

File: `backend/app/resilient_http.py`

## Purpose

One module that every external-integration caller uses to get:

- Sane **timeouts** (connect 5 s, read 10 s) — so a dead external system
  can't hold a request thread forever.
- A **single retry** on transient failures (5xx, network errors) — so a
  Wi-Fi blip doesn't break a dashboard render.
- A **try/except wrapper** (`safe_integration`, `safe_call`) that turns
  transport exceptions into a user-friendly 502
  (`"Service temporarily unavailable"`) instead of leaking stack traces.

## Public API

- `default_timeout() -> httpx.Timeout`

  Default `(connect=5.0, read=10.0, write=10.0, pool=5.0)`. Callers can
  override per-request.

- `resilient_request(method, url, ..., retries=2) -> httpx.Response`

  `httpx.request` with timeouts + retries. Raises on non-2xx so callers
  can decide what to do.

- `resilient_get(url, **kw)` / `resilient_post(...)` — convenience
  wrappers.

- `safe_integration(label: str) -> contextmanager`

  Drop a block of code inside `with safe_integration("azure-devops"):`
  — any exception raised inside becomes an `HTTPException(502, "Service
  temporarily unavailable")` with the label recorded in server logs.

- `safe_call(fn, *args, default=None, label="") -> Any`

  Best-effort helper: run `fn()`, log on failure, return `default`.
  Useful when a partial UI (e.g. one widget out of ten) should still
  render if a non-critical upstream is down.

## Usage guidance

- Use `resilient_request` for **writes** (create ticket, create project)
  — you want the caller to handle failures.
- Use `safe_integration` around the whole handler body for **reads**
  where the UI should show "service unavailable" on failure.
- Use `safe_call` when building a widget that aggregates multiple
  sources and missing one shouldn't nuke the others.

## Why not a 3rd-party lib?

`tenacity` + `httpx` already exists, but we intentionally keep this
module small so the behavior is grep-able and the retry policy is
documented in one place. If you need anything fancier (circuit breakers,
jitter), add it here rather than inline in a router.
