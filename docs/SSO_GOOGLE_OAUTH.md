# Google SSO (OAuth 2.0 / OpenID Connect)

This document describes the Single Sign-On (SSO) implementation using **Google as the identity provider** for the DevOps Control Center when deployed on GCP (e.g. GKE). Users sign in with their Google (Gmail) accounts; the backend creates or looks up users by email and issues an internal JWT for the session.

---

## Overview

- **IdP:** Google (OAuth 2.0 / OpenID Connect).
- **Flow:** Authorization code flow. User clicks “Continue with Google” → redirect to Google consent → callback to backend → backend exchanges code for tokens, reads email/name from ID token, creates or finds user in DB, issues app JWT → frontend receives token via `postMessage` (popup) or continues after redirect.
- **Session:** 8 hours (configurable via `JWT_EXPIRY`).
- **Scopes:** `openid`, `email`, `profile`.

---

## Architecture

```
┌─────────────┐     GET /api/auth/login      ┌─────────────┐     redirect      ┌─────────────┐
│   Browser   │ ───────────────────────────► │   Backend   │ ─────────────────► │   Google    │
│  (Frontend) │                               │  (FastAPI)  │                   │   OAuth     │
└─────────────┘                               └─────────────┘                   └──────┬──────┘
        │                                                 ▲                             │
        │  open popup or redirect                         │                             │
        │  to /api/auth/login                             │                             │
        │                                                  │                             │
        │                                                  │  GET /api/auth/callback?code=...
        │                                                  │  (redirect back from Google)
        │                                                  │                             │
        │                                                  └─────────────────────────────┘
        │
        │  postMessage('auth:success', { token, user })  (if popup)
        │  or page load with token in URL/session        (if redirect)
        ▼
  Store JWT + user in localStorage, redirect to /dashboard
```

- **Backend** handles:
  - `GET /api/auth/login` — redirects to Google’s authorization URL with `client_id`, `redirect_uri`, `scope`, `state`.
  - `GET /api/auth/callback` — receives `code` (and `state`), exchanges code for tokens, decodes ID token for email/name, creates or fetches user, issues internal JWT, returns HTML that posts token/user to opener or redirects.
  - `POST /api/auth/verify` — validates the app JWT and returns payload (for frontend or other services).

---

## Backend Changes

### Configuration (`backend/app/config.py`)

- **OAuth / OIDC:**  
  `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `OAUTH_ISSUER`, `OAUTH_AUTH_URL`, `OAUTH_TOKEN_URL`, `OAUTH_JWKS_URL`, `OAUTH_SCOPES`.
- **CORS:**  
  `CORS_ORIGINS` (comma-separated). Used by FastAPI `CORSMiddleware` to allow frontend origins.
- **JWT:**  
  `JWT_SECRET`, `JWT_EXPIRY` (e.g. `8h` for 8-hour sessions).

### Auth API (`backend/app/api/auth.py`)

- **`GET /api/auth/login`**  
  Builds Google OAuth URL and returns `RedirectResponse` to Google consent screen. Requires `OAUTH_CLIENT_ID` and `OAUTH_REDIRECT_URI`.

- **`GET /api/auth/callback`**  
  - Query params: `code` (required), `state` (optional).  
  - Exchanges `code` for tokens via `OAUTH_TOKEN_URL`.  
  - Decodes ID token (payload only; for production, verify signature with JWKS).  
  - Reads `email` and `name` from ID token.  
  - Calls `_get_or_create_user_by_email(email, full_name)` (creates user with default role if not found).  
  - Issues internal JWT via `create_access_token(auth_user)`.  
  - Returns HTML that:  
    - If opener exists: `postMessage({ type: 'auth:success', data: { token, user } }, '*')` and `window.close()`.  
    - Otherwise can be extended for redirect flow (e.g. redirect to frontend with token in fragment or cookie).

- **`POST /api/auth/verify`**  
  Accepts token (e.g. in body or header), calls `decode_access_token`, returns payload.

- **User creation:**  
  Users are created on first login with email as username, default role from `roles` table (e.g. hierarchy_level 7). Role mapping and admin override (e.g. by email) can be implemented in the same module.

### CORS (`backend/app/main.py`)

- CORS middleware uses `settings.cors_origins_list` (from `CORS_ORIGINS`).  
- For GCP dev: e.g. `https://devops.internal.company`. For local dev: `http://localhost:3000`.

---

## Frontend Changes

### Login page (`frontend/src/app/login/page.tsx`)

- Single action: **“Continue with Google”**.
- On click: open `/api/auth/login` in a popup (or `window.location` if popup is blocked).  
  - **Important:** The URL must hit the **backend** (e.g. `https://devops.internal.company/api/auth/login`), so the frontend must use the same origin as the backend for `/api` (or proxy `/api` to the backend).
- Listens for `message` events with `event.data.type === 'auth:success'` and `event.data.data` containing `token` and `user`.
- On receipt: calls `apiClient.completeOAuthLoginFromMessage({ token, user })` (stores token and user in localStorage), then redirects to `/dashboard`.

### API client (`frontend/src/lib/api-client.ts`)

- **Base URL:** From `NEXT_PUBLIC_API_BASE_URL` (e.g. `https://devops.internal.company` for GCP dev). Requests go to `${API_BASE_URL}/api`.
- **`completeOAuthLoginFromMessage(data)`**  
  Sets the internal token and stores `user` in localStorage so existing auth helpers (`getUser()`, `isAuthenticated()`) work unchanged.

### Auth helpers (`frontend/src/lib/auth.ts`)

- Unchanged: `getUser()`, `isAuthenticated()`, `hasPermission()`, `hasRoleLevel()`, etc., all rely on token and `user_data` in localStorage populated by the callback.

---

## Kubernetes / GCP Deployment

### Backend deployment (`infrastructure/k8s/base/deployments/backend.yaml`)

- **Secrets** (from `all-secrets` or Secret Manager):  
  `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `JWT_SECRET`.
- **Literal env:**  
  `OAUTH_REDIRECT_URI=https://devops.internal.company/api/auth/callback`  
  `OAUTH_ISSUER`, `OAUTH_AUTH_URL`, `OAUTH_TOKEN_URL`, `OAUTH_JWKS_URL`, `OAUTH_SCOPES` (Google defaults), `JWT_EXPIRY=8h`.

### ConfigMap (`infrastructure/k8s/base/configmaps/application-config.yaml`)

- **CORS:**  
  `CORS_ORIGINS` set to include frontend origin, e.g. `http://localhost:3000,https://devops.internal.company`.

### Ingress

- **Backend** must serve `/api` (including `/api/auth/login` and `/api/auth/callback`).  
- Example: host `devops.internal.company`, path `/api` → backend service; path `/` → frontend.  
- **Redirect URI** in Google Cloud Console must be exactly:  
  `https://devops.internal.company/api/auth/callback`  
  (no trailing slash unless you use it in redirect_uri too).

### Secrets

- **Kustomize:**  
  Base kustomization uses `secretGenerator` with `envs: [secrets/.env]`. The file `infrastructure/k8s/base/secrets/.env` must exist and define at least `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `JWT_SECRET`, plus DB/Redis etc. Do not commit real values; use a local or CI-injected file.
- **Alternative:**  
  Use GCP Secret Manager and inject into Kubernetes (e.g. CSI driver or init container) and reference in deployment env.

---

## Environment Variables Summary

### Backend (local `.env` or k8s secrets/config)

| Variable | Description | Example |
|----------|-------------|---------|
| `OAUTH_CLIENT_ID` | Google OAuth 2.0 client ID | From GCP Console → APIs & Services → Credentials |
| `OAUTH_CLIENT_SECRET` | Google OAuth client secret | Same |
| `OAUTH_REDIRECT_URI` | Callback URL (must match Google Console) | `https://devops.internal.company/api/auth/callback` |
| `OAUTH_ISSUER` | OIDC issuer | `https://accounts.google.com` |
| `OAUTH_AUTH_URL` | Authorization endpoint | `https://accounts.google.com/o/oauth2/v2/auth` |
| `OAUTH_TOKEN_URL` | Token endpoint | `https://oauth2.googleapis.com/token` |
| `OAUTH_JWKS_URL` | JWKS (for ID token verification) | `https://www.googleapis.com/oauth2/v3/certs` |
| `OAUTH_SCOPES` | Scopes | `openid email profile` |
| `JWT_SECRET` | Secret for signing internal JWTs | Random string |
| `JWT_EXPIRY` | Session duration | `8h` |
| `CORS_ORIGINS` | Allowed frontend origins | `https://devops.internal.company` or comma-separated list |

### Frontend

| Variable | Description | Example |
|----------|-------------|---------|
| `NEXT_PUBLIC_API_BASE_URL` | Backend base URL (same origin as ingress for /api) | `https://devops.internal.company` |

---

## GCP Setup Checklist

1. **Create OAuth 2.0 credentials (Web application)**  
   - GCP Console → APIs & Services → Credentials → Create Credentials → OAuth client ID.  
   - Application type: Web application.

2. **Authorized redirect URIs**  
   - Add exactly: `https://devops.internal.company/api/auth/callback`  
   - (Use your real frontend/backend host; no trailing slash unless consistent.)

3. **Authorized JavaScript origins** (if needed for popup)  
   - e.g. `https://devops.internal.company`

4. **Secrets**  
   - Put `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, and `JWT_SECRET` in your secrets (e.g. `infrastructure/k8s/base/secrets/.env` for kustomize, or GCP Secret Manager for GKE).

5. **Ingress / DNS**  
   - Ensure `devops.internal.company` points to your ingress and that `/api` is routed to the backend service.

---

## Troubleshooting

- **404 on `/auth/callback`**  
  - Callback is served by the **backend** at `/api/auth/callback`.  
  - If you use `https://devops.internal.company/auth/callback` (without `/api`), the request goes to the frontend and returns 404.  
  - Fix: Set `OAUTH_REDIRECT_URI` and Google’s redirect URI to `https://devops.internal.company/api/auth/callback`.

- **CORS errors**  
  - Ensure `CORS_ORIGINS` (backend) includes the frontend origin (e.g. `https://devops.internal.company`).  
  - Ensure the frontend uses `NEXT_PUBLIC_API_BASE_URL` so API requests go to the same host (and thus same origin if served behind same ingress).

- **“OAuth configuration is missing”**  
  - Backend is missing `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, or `OAUTH_REDIRECT_URI`. Check env and k8s secrets.

- **Popup blocked**  
  - The login page falls back to full redirect: `window.location.href = '/api/auth/login'`. Ensure that after Google redirects back to `/api/auth/callback`, the backend returns a page that can redirect the user (e.g. to `/dashboard`) and pass the token (e.g. via cookie or redirect with fragment), or that the callback is opened in the same tab so the frontend can read token from URL/session.

- **Users not created**  
  - Check that the `roles` table has a row with `hierarchy_level = 7` (default role).  
  - Check backend logs for errors during `_get_or_create_user_by_email`.

---

## Security Notes

- **ID token:** The current implementation decodes the Google ID token without signature verification (`options={"verify_signature": False}`). For production, verify the signature using `OAUTH_JWKS_URL` and validate `iss`, `aud`, and `exp`.
- **State:** A random `state` is sent to Google; for production, persist it (e.g. in a cookie or server-side session) and verify it in the callback to prevent CSRF.
- **Secrets:** Never commit `.env` or real secrets. Use CI secrets or GCP Secret Manager and inject into k8s.

---

## Related Files

- Backend: `backend/app/config.py`, `backend/app/main.py`, `backend/app/api/auth.py`, `backend/app/security.py`
- Frontend: `frontend/src/app/login/page.tsx`, `frontend/src/lib/api-client.ts`, `frontend/src/lib/auth.ts`
- K8s: `infrastructure/k8s/base/deployments/backend.yaml`, `infrastructure/k8s/base/configmaps/application-config.yaml`, `infrastructure/k8s/base/ingress.yaml`
- Secrets: `infrastructure/k8s/base/secrets/.env` (gitignored), `env.example` for variable list
