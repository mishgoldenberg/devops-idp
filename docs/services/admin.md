# `admin` router

File: `backend/app/api/admin.py` · Prefix: `/api/admin`

## Purpose

Admin-only utilities that don't belong with any specific domain:

- Granting / revoking the `Platform Admin` role.
- Exposing the `has_effective_admin_access_live` helper used as a
  dependency by every admin-gated endpoint and UI route.
- Small read endpoints for the Admin pages (user list etc.).

## Main endpoints

| Method | Path                                  | Description                                        |
| ------ | ------------------------------------- | -------------------------------------------------- |
| GET    | `/api/admin/users`                    | List users + their roles                           |
| POST   | `/api/admin/users/{email}/grant-admin`| Give a user Platform Admin                         |
| POST   | `/api/admin/users/{email}/revoke-admin`| Demote back to Regular User                       |

Every mutating endpoint writes an `audit_events` row.

## RBAC helper

`has_effective_admin_access_live(user)` is the canonical "is this user
an admin?" check. It reads the user's current role from Postgres every
time (no cache) so role grants take effect immediately. All admin
endpoints — in this router and elsewhere — use it via FastAPI
`Depends`.
