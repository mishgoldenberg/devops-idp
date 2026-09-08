# `health` router

File: `backend/app/api/health.py` · Prefix: `/api/health`

## Purpose

Three different "is this service ok?" endpoints for three different
consumers.

| Method | Path                  | Consumer                 | Returns 200 iff…                              |
| ------ | --------------------- | ------------------------ | --------------------------------------------- |
| GET    | `/api/health/live`    | K8s liveness probe       | The Python process is up                      |
| GET    | `/api/health/ready`   | K8s readiness probe      | Postgres is reachable (Redis reported, not required) |
| GET    | `/api/health` / `/`   | External monitoring      | **Both** Postgres and Redis are reachable     |

## Why readiness doesn't require Redis

A Redis blip shouldn't wedge the whole rollout in `NotReady`, so the
readiness probe only fails on Postgres. Redis status is still returned
in the JSON body (`services.redis`) for diagnostic purposes.

External monitoring that wants to alert on Redis should hit `/api/health`,
which returns 503 when either dependency is down.

## Response shape

```json
{
  "status": "ok",
  "timestamp": "2026-04-19T14:02:10Z",
  "services": { "database": "up", "redis": "up", "schema": "ready",
                "credentials": "ok" },
  "ready": true
}
```

## Relevant code

- `db.db_health_check()` — `SELECT 1` against the pool.
- `redis_client.redis_health_check()` — `PING`.

## `credentials` — informational, never part of the decision

`services.credentials` reports whether this pod's `JWT_SECRET` can still decrypt
the credentials already in the database (`sso_config.credential_key_check`, run
once after the schema bootstrap).

`"unreadable"` means every stored Azure DevOps PAT is undecryptable — the symptom
users see is that all their Azure DevOps widgets are empty at once. It deliberately
does **not** make the pod NotReady: a rollout cannot fix a wrong secret, and taking
the portal down would remove the Connections page people need in order to reconnect.
See [../RUNBOOK.md](../RUNBOOK.md) §4.
