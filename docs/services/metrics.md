# `metrics` router

File: `backend/app/api/metrics.py` · Prefix: `/api/metrics`

## Purpose

Older platform metrics endpoint that predates `observability`. Reads
`usage_metrics` and `daily_metrics`. Kept because a few admin pages and
the settings page still consume it.

## Main endpoints

| Method | Path                        | Description                          |
| ------ | --------------------------- | ------------------------------------ |
| GET    | `/api/metrics/usage`        | Per-user / per-metric rollups        |
| GET    | `/api/metrics/daily`        | Daily buckets                        |

## Tables

- `usage_metrics`, `daily_metrics`, `service_health` — all defined in
  `deployment/charts/infrastructure/database/00_schema.sql`.

## Note

For new tracking needs, prefer the Observability router — it's the
"official" place for usage analytics and its data model is simpler.
