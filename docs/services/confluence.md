# `confluence` integration

File: `backend/app/api/integrations.py` · Prefix: `/api/integrations/confluence`

## Purpose

Powers the **Confluence Pages** dashboard widget. The widget shows the
last edited pages across the spaces the signed-in user can see and lets
them search the Confluence content index without leaving the portal.

The integration follows the same per-user PAT pattern as SonarQube and
Artifactory: the token is stored encrypted in `user_integrations`, all
calls happen server-side, and the token never reaches the browser.

## Main endpoints

| Method | Path                                       | Description                                                          |
| ------ | ------------------------------------------ | -------------------------------------------------------------------- |
| GET    | `/api/integrations/confluence/token`       | Returns whether the current user has saved a Confluence token.       |
| POST   | `/api/integrations/confluence/token`       | Save / update the user's Confluence personal access token (validated against the configured base URL before being stored). |
| DELETE | `/api/integrations/confluence/token`       | Forget the stored token.                                             |
| GET    | `/api/integrations/confluence/recent`      | Last 10 pages ordered by `modified` (uses `GET /rest/api/content`).  |
| GET    | `/api/integrations/confluence/search?q=…`  | Full-text page search via CQL `type=page AND text~"…"`.              |

When no token is stored the recent/search endpoints return HTTP `428` so
the widget can render its inline "Connect to Confluence" prompt — same
contract as the SonarQube and Artifactory widgets.

## External APIs used

- `GET {CONFLUENCE_BASE_URL}/rest/api/space?limit=1` — token reachability test.
- `GET {CONFLUENCE_BASE_URL}/rest/api/content?type=page&orderby=modified&limit=10&expand=space,version,history.lastUpdated`
- `GET {CONFLUENCE_BASE_URL}/rest/api/content/search?cql=…&limit=10&expand=space,version,history.lastUpdated`

All requests use `Authorization: Bearer <token>` against the user's
saved PAT. Errors from Confluence are normalized to a 401 ("Invalid
token or connection failed") so the widget can swap to the connect
prompt without leaking provider-specific error shapes.

## Environment variables

- `CONFLUENCE_BASE_URL` — Confluence base URL used by the widget. Same
  variable also drives the "Open" button on the Confluence system page
  (`/api/system-urls`).
