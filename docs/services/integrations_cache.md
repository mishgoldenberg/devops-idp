# `integrations_cache` (cross-cutting)

File: `backend/app/integrations_cache.py`

## Purpose

Thin wrapper around `cache.get_cached` scoped to external-integration
reads (Azure DevOps, ServiceNow, SonarQube, Artifactory). It gives us:

- One place to set the default TTL (60 s — see the polish spec).
- A consistent cache-key namespace — `ext:<integration>:<owner>:<key>` —
  so ops can `redis-cli KEYS 'ext:*'` when debugging.
- A per-(integration, owner) invalidation function used after write
  paths.

## Public API

- `cached_external(integration, owner, suffix, producer, *, ttl=60)`

  Return the cached result, or call `producer()` and cache it for `ttl`
  seconds. Keys are `ext:<integration>:<owner>:<suffix>`.

- `invalidate_owner(integration, owner)`

  Drop every cached entry for a single user under a single integration.
  Called from write paths (e.g. after `POST /azure-devops/projects/create`
  we call `invalidate_owner("ado", email)` so the next read shows the
  new project immediately).

## Typical usage

```python
def _fetch_work_items_live(...):
    # Hits ADO; raises on errors.
    ...

@router.get("/work-items")
def get_work_items(current_user=Depends(get_current_user)):
    owner = str(current_user.get("email") or "").lower()
    return cached_external(
        "ado",
        owner,
        "work-items:",
        lambda: _fetch_work_items_live(current_user),
    )
```

## When **not** to use it

- Writes. Always hit upstream directly.
- Endpoints whose response is already trivial to compute (DB-only
  joins). Cache only pays for itself when the underlying call is slow
  or rate-limited.
- Data you absolutely need to be fresh (rare — 60 s is usually fine).

## Why per-user keying?

External systems return user-specific views (my work items vs. yours).
Caching globally would cross-contaminate. Using `owner` as the user's
email forces correctness; the downside is that one user can't "warm"
the cache for another, which is fine for our load profile.
