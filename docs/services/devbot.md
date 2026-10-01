# DevBot and AdminBot

Files: `backend/app/devbot/` (the engine), `api/devbot.py`, `api/devbot_index.py`,
`api/devbot_admin.py`, `api/adminbot.py`.

The whole design, including the search index step by step, is in
[../devbot.md](../devbot.md). This page is the map of the routes.

| Prefix | Router | For |
|---|---|---|
| `/api/devbot` | `devbot.py` | status, usage, conversations, proposals' outcomes, feedback, and `POST /chat` (the answer streams back as server-sent events) |
| `/api/devbot/index` | `devbot_index.py` | admin: the search index's settings, builds (`/build`, `/stop`) and the review of past fixes (`/fixes`, `/fixes/hide`, `/fixes/review`) |
| `/api/devbot/admin` | `devbot_admin.py` | admin: the monitoring page's figures (`days=0` is all time) |
| `/api/adminbot` | `adminbot.py` | admin: AdminBot's status, conversations, chat, and `/actions/{id}/run`, which performs a confirmed proposal |

Every admin route re-checks `has_effective_admin_access_live` itself.

## Environment

`DEVBOT_LLM_BASE_URL` turns it on; the rest of `DEVBOT_*` tunes it (see
[../env.md](../env.md)).

## How it fails

A gateway failure becomes an `LLMError` with a kind (`limit`, `key`, `context`,
`tools`, `server`, …) and a sentence the person can act on; a failed lookup comes back
to the model as an error in words and never ends the question. The index build writes
its outcome to `devbot_index_runs` whatever happens; RUNBOOK §9a lists what each
message means.
