# `pins` router

File: `backend/app/api/pins.py` · Prefix: `/api` (no extra prefix)

## Purpose

Per-user "starred" items and "seen" markers used by several widgets so
the UI can show "new since last visit" indicators and let users pin a
work item / PR / ticket to the top of a list.

## Main endpoints

| Method | Path                          | Description                                  |
| ------ | ----------------------------- | -------------------------------------------- |
| GET    | `/api/pins`                   | List the caller's pinned items               |
| POST   | `/api/pins`                   | Add a pin (`{item_type, item_id}`)           |
| DELETE | `/api/pins?item_type=…&item_id=…` | Remove a pin                             |
| POST   | `/api/items/mark-seen`        | Update last-seen timestamp for an item list |

## Tables

- `user_item_pins` — (user_email, item_type, item_id, created_at).
- `user_item_seen` — (user_email, item_type, last_seen_at).

Both are created by `db.ensure_item_tables()` at startup.
