# `admin` router

File: `backend/app/api/admin.py` · Prefix: `/api/admin`

## Purpose

The Platform Managing and Users pages: sign-in configuration, the shared quick links,
which dashboard widgets exist, and accounts.

## Main endpoints

| Method          | Path                                   | Description                                   |
| --------------- | -------------------------------------- | --------------------------------------------- |
| GET, POST       | `/api/admin/sso/config`                | Read (secret redacted) / save the OIDC config |
| POST            | `/api/admin/sso/test`                  | Fetch the issuer's discovery document         |
| POST            | `/api/admin/quick-links`               | Add a shared quick link                       |
| PATCH, DELETE   | `/api/admin/quick-links/{id}`          | Edit / remove one                             |
| GET             | `/api/admin/users`                     | Accounts and their roles                      |
| GET             | `/api/admin/roles`                     | The roles an account can hold                 |
| PUT             | `/api/admin/users/role`                | Change an account's role                      |
| PUT             | `/api/admin/users/active`              | Deactivate or reactivate an account           |
| GET, PUT        | `/api/admin/dashboard-widgets`         | Which widgets exist for users                 |

Quick-link names, links and icons go through the same checks as personal ones
(`quick_links.required_text`, `link_or_blank`, `icon_or_blank`).

Every mutating endpoint writes an `audit_events` row.

## RBAC helper

`has_effective_admin_access_live(user)` is the canonical "is this user an admin?"
check: it reads the user's current role from Postgres, so a role change takes effect
at once. Endpoints use it through `common.require_admin(user)`, or
`Depends(common.admin_user)` as a route dependency.
