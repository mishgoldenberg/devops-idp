# `dashboards` router

File: `backend/app/api/dashboards.py` · Prefixes: `/api/dashboards` and `/api/dashboard`

## Purpose

Persist each user's dashboard widget selection and order. Today we only
ship a "default" dashboard per user; the API is shaped to support
multiple named dashboards later.

## Main endpoints

| Method | Path                                          | Description                                  |
| ------ | --------------------------------------------- | -------------------------------------------- |
| GET    | `/api/dashboards/default`                     | Return the caller's default dashboard        |
| PUT    | `/api/dashboards/default`                     | Replace the full widget list + layout        |
| POST   | `/api/dashboards/default/widgets`             | Add a widget                                 |
| DELETE | `/api/dashboards/default/widgets/{widget_key}` | Remove a widget                             |

## Tables read / written

- `dashboards` — legacy layout JSON.
- `user_widgets` — the canonical "this user has these widgets right now"
  table used by the observability page's admin view.

## Relationship to the Customize drawer

The dashboard page's Customize drawer fetches `/api/dashboards/default`
to check boxes, then fires the appropriate `POST`/`DELETE` when the user
flips a toggle. It also renders "Suggested" and "Recently used" chips
that come from
[`observability`](./observability.md) — clicking one of those simply
calls `POST /widgets` here.
