# `sonarqube` router

File: `backend/app/api/sonarqube.py` · Prefix: `/api/sonarqube`

## Purpose

Currently **mock-only**. Serves canned SonarQube project and
quality-gate data to the frontend widgets so the UI can be developed and
demoed without a real SonarQube instance.

## Main endpoints

| Method | Path                                | Description                          |
| ------ | ----------------------------------- | ------------------------------------ |
| GET    | `/api/sonarqube/projects`           | List of fake projects                |
| GET    | `/api/sonarqube/projects/{key}`     | Fake detail + quality gate           |
| POST   | `/api/sonarqube/pr-scanning/enable` | No-op success (goes through approvals in the future) |

## External APIs used

None today. When this graduates to real, it should use
`resilient_http.resilient_get` and wrap reads with
`integrations_cache.cached_external("sonar", owner, …, ttl=60)`.

## Environment variables (reserved)

- `USE_MOCK_SONARQUBE` — logically always `true` today.
- `SONARQUBE_URL`, `SONARQUBE_TOKEN` — reserved.
