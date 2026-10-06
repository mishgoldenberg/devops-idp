# `dashboards` router

File: `backend/app/api/dashboards.py` · Prefix: `/api/dashboard`

## Purpose

The dashboard widgets' reads that combine more than one source, and the record of
which widgets each person has switched on.

## Endpoints

| Method | Path                            | Description                                                   |
| ------ | ------------------------------- | ------------------------------------------------------------- |
| GET    | `/api/dashboard/ado-items`      | The caller's Azure DevOps work items, normalised, with pins and update marks |
| GET    | `/api/dashboard/snow-items`     | The caller's support tickets, normalised the same way         |
| POST   | `/api/dashboard/widgets/sync`   | Replace the caller's list of switched-on widgets (`user_widgets`) |

## Where the widget choice lives

The Customize drawer stores which widgets a person shows in a cookie, through
`/ui/dashboard/preferences` (`ui.py`). `widgets/sync` mirrors that list into
`user_widgets` so the Observability page can count it; it never decides what renders.
Admin policy on which widgets exist at all is `widget_registry.py`.
