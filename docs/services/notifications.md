# `notifications` router

File: `backend/app/api/notifications.py` · Prefix: `/api/notifications`

## Purpose

In-app bell notifications. Other modules (approvals, servicenow) call
`create_notification(user_email, message, notif_type, related_id, link,
group_key)` to enqueue a notification; the bell in
`partials/components/banner.html` reads back the list via `GET /`.

## Main endpoints

| Method | Path                             | Description                                          |
| ------ | -------------------------------- | ---------------------------------------------------- |
| GET    | `/api/notifications`             | Caller's notifications, grouped by `group_key`       |
| POST   | `/api/notifications/{id}/read`   | Mark one as read                                     |
| POST   | `/api/notifications/read-all`    | Mark all as read                                     |
| DELETE | `/api/notifications/{id}`        | Delete one                                           |

## Public helper

```python
create_notification(
    user_email: str,
    message: str,
    notif_type: Optional[str] = None,
    related_id: Optional[str] = None,
    link: Optional[str] = None,       # where to navigate on click
    group_key: Optional[str] = None,  # collapse identical types
) -> None
```

This is imported by `approvals.py`, `servicenow.py`, and
`azure_devops.py`. The function is **best-effort** — it never raises,
because the UI action that triggered the notification is more important
than the notification itself.

## Grouping

The list endpoint collapses rows that share a `group_key`, showing the
newest row plus a count badge. If any member of the group is unread,
the group is unread. This avoids "5 copies of 'Ticket created'"
cluttering the bell.

## Retention

`_cleanup_old_notifications` runs opportunistically on each list call
and deletes rows older than 60 days. Deletion is wrapped in `try/except`
so a Postgres error here can't block the user's bell from loading.

## Schema

See [`docs/database.md`](../database.md). The `link` and `group_key`
columns were added via `ALTER TABLE IF NOT EXISTS` so older DB schemas
still work — the create path falls back to an `INSERT` without those
columns when they don't exist.
