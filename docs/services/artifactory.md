# `artifactory` router

File: `backend/app/api/artifactory.py` · Prefix: `/api/artifactory`

## Purpose

Currently **mock-only**. Returns canned Artifactory repositories and
storage numbers to the frontend widgets so the UI can be developed and
demoed without a real Artifactory instance.

## Main endpoints

| Method | Path                                | Description                          |
| ------ | ----------------------------------- | ------------------------------------ |
| GET    | `/api/artifactory/repositories`     | List fake repositories               |
| GET    | `/api/artifactory/storage`          | Fake storage usage payload           |
| GET    | `/api/artifactory/artifacts`        | Fake recent artifacts                |

## External APIs used

None today. Real integration will go through
`resilient_http.resilient_get`. Reads should be cached with
`integrations_cache.cached_external("artifactory", owner, …, ttl=60)`.

## Environment variables (reserved)

- `USE_MOCK_ARTIFACTORY` — logically always `true` today.
- `ARTIFACTORY_URL`, `ARTIFACTORY_API_KEY` — reserved.
