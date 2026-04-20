# `auth` router

File: `backend/app/api/auth.py` · Prefix: `/api/auth`

## Purpose

Handles Google OAuth 2.0 / OpenID Connect login, issues the portal's own
HS256 JWT, and stores it in an HttpOnly `auth_token` cookie that every
other router reads via `security.get_current_user`.

## Main endpoints

| Method | Path                    | Description                                                      |
| ------ | ----------------------- | ---------------------------------------------------------------- |
| GET    | `/api/auth/login`       | Redirects the user to Google's consent screen                    |
| GET    | `/api/auth/callback`    | Google redirect target; exchanges code, creates user, sets cookie |
| POST   | `/api/auth/logout`      | Clears the `auth_token` cookie                                   |
| GET    | `/api/auth/me`          | Returns the current user (from cookie/Bearer)                    |

## External APIs used

- Google OAuth token endpoint (`https://oauth2.googleapis.com/token`) —
  authorization-code exchange.
- Google JWKS endpoint — ID token signature verification.

Endpoints are configurable via `OAUTH_TOKEN_URL` / `OAUTH_JWKS_URL` so
an internal OIDC IdP can be swapped in without touching the code.

## Environment variables

See [`docs/env.md`](../env.md) for the full list. The ones that matter
here:

- `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` — **required**.
- `OAUTH_ISSUER`, `OAUTH_AUTH_URL`, `OAUTH_TOKEN_URL`, `OAUTH_JWKS_URL`,
  `OAUTH_SCOPES` — optional overrides.
- `JWT_SECRET`, `JWT_EXPIRY` — used by `security.create_access_token`.

## Failure handling

- OAuth errors render a neutral 400 with a "Sign-in failed" message —
  raw Google payloads are never leaked to the browser.
- First-login user creation is idempotent (`INSERT … ON CONFLICT DO
  NOTHING`) so repeated callbacks never dupe rows.
- The cookie is set `HttpOnly`, `SameSite=Lax`, `Secure` when the
  request came in over HTTPS, `Path=/`.

## Related code

- `backend/app/security.py` — token creation, decoding, and
  `get_current_user` (cookie or `Authorization: Bearer`).
- `docs/SSO_GOOGLE_OAUTH.md` — how to set up the GCP OAuth client.
