# `auth` router

File: `backend/app/api/auth.py` · Prefix: `/api/auth`

## Purpose

Handles admin-configured OpenID Connect login, issues the portal's own HS256
JWT, and stores it in an HttpOnly `auth_token` cookie that every other router
reads via `security.get_current_user`.

## Main endpoints

| Method | Path                    | Description                                                      |
| ------ | ----------------------- | ---------------------------------------------------------------- |
| GET    | `/api/auth/login`       | Redirects to the configured OIDC provider, if enabled             |
| GET    | `/api/auth/sso/callback`| OIDC redirect target; validates token, creates user, sets cookie  |
| POST   | `/api/auth/logout`      | Clears the `auth_token` cookie                                   |
| GET    | `/api/auth/me`          | Returns the current user (from cookie/Bearer)                    |

## External APIs used

- Configured OIDC discovery document (`issuer_uri/.well-known/openid-configuration`).
- Configured token endpoint and JWKS URI from discovery.

## Environment variables

See [`docs/env.md`](../env.md) for the full list. The ones that matter
here:

- `HUB_ADMIN_USERNAME`, `HUB_ADMIN_PASSWORD` — create the first admin when no admin exists.
- `JWT_SECRET`, `JWT_EXPIRY` — used by `security.create_access_token`.
- OIDC provider settings are stored in `sso_config` by Platform Admins.

## Failure handling

- OIDC errors render neutral messages; raw token payloads are never leaked to the browser.
- First-login user creation is idempotent (`INSERT … ON CONFLICT DO
  NOTHING`) so repeated callbacks never dupe rows.
- The cookie is set `HttpOnly`, `SameSite=Lax`, `Secure` when the
  request came in over HTTPS, `Path=/`.

## Related code

- `backend/app/security.py` — token creation, decoding, and
  `get_current_user` (cookie or `Authorization: Bearer`).
