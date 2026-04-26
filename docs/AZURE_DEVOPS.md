# Azure DevOps Integration

This document describes the complete Azure DevOps integration implemented in the DevOps Control Center, including authentication, backend endpoints, frontend components, environment configuration, and CI/CD deployment.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Authentication Model](#2-authentication-model)
3. [Environment Variables](#3-environment-variables)
4. [Backend Implementation](#4-backend-implementation)
5. [Frontend Implementation](#5-frontend-implementation)
6. [Local Development Setup](#6-local-development-setup)
7. [Kubernetes / Production Setup](#7-kubernetes--production-setup)
8. [CI/CD Pipeline](#8-cicd-pipeline)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Architecture Overview

```
Browser (Next.js)
  │
  ├── useAzureDevOpsConnection hook  ← checks PAT status, caches result 60 s
  │
  ├── AzureConnectPrompt component   ← shown in every ADO widget
  │     └── POST /api/azure-devops/pat  ← user saves their PAT
  │
  ├── AzureDevOpsWorkItems widget    ← work items for @Me, project filter
  ├── LatestPRWidget                 ← latest PR across all projects
  └── PipelineWidget                 ← latest pipeline run

FastAPI backend
  │
  ├── GET  /api/azure-devops/pat        ← PAT status (configured / personal)
  ├── POST /api/azure-devops/pat        ← save per-user PAT
  ├── DELETE /api/azure-devops/pat      ← remove per-user PAT
  ├── GET  /api/azure-devops/projects   ← list ADO projects for dropdown
  ├── GET  /api/azure-devops/work-items ← items assigned to @Me
  ├── GET  /api/azure-devops/pull-requests
  └── GET  /api/azure-devops/pipelines

  _get_pat_for_user()
    ├── 1. per-user PAT from system_config / Vault
    └── 2. server env fallback (AZURE_DEVOPS_PAT) — admin convenience only

Azure DevOps REST API  (https://dev.azure.com/{AZURE_DEVOPS_ORG})
```

---

## 2. Authentication Model

### Per-User PAT (primary)

Each user provides their own [Personal Access Token](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate).  
The PAT is stored **server-side only** — it is never returned to the browser or written to any log.

Storage backends (selected via `USE_VAULT`):

| `USE_VAULT` | Storage location |
|---|---|
| `false` (default) | PostgreSQL `system_config` table, key `user:{id}:azure_devops_pat`, `is_sensitive = true` |
| `true` | HashiCorp Vault KV v2 at `{VAULT_PATH}/azure-devops/{user_id}` |

### Server PAT fallback (admin convenience)

If `AZURE_DEVOPS_PAT` is set in the environment and the user has no personal PAT stored, the server-level PAT is used automatically.  
This means the admin account (`golden.mihel@gmail.com`) works immediately after deployment without pasting anything in the UI — the PAT from `.env` / the Kubernetes secret acts as the default.

Other users will see **"Connect via PAT"** in each Azure DevOps widget until they save their own token.

### Why not OAuth / "Sign in with Microsoft"?

Azure DevOps uses Microsoft accounts (Azure AD / MSA), while the portal uses
admin-configured OIDC for Hub sign-in. There is **no automatic link** between a
user's Hub SSO session and their Azure DevOps identity.

A proper Microsoft OAuth2 integration (Azure AD app registration, `499b84ac…/.default` scope) can be added as a future enhancement once an Azure AD Application is provisioned — it would replace PATs entirely with delegated access tokens.

---

## 3. Environment Variables

### Required

| Variable | Example | Description |
|---|---|---|
| `AZURE_DEVOPS_ORG` | `DevCollection-Inheritance` | Organisation name in `https://dev.azure.com/{org}` |
| `USE_MOCK_AZURE_DEVOPS` | `false` | Set to `false` to call the real API |

### Optional

| Variable | Example | Description |
|---|---|---|
| `AZURE_DEVOPS_PAT` | `5P6m3Ay…` | Server-level PAT used as fallback for admin users |
| `AZURE_DEVOPS_QUERY_USER` | `test-user@company.com` | Override the WIQL `@Me` identity (useful for service accounts) |
| `USE_VAULT` | `false` | Store PATs in HashiCorp Vault instead of PostgreSQL |
| `VAULT_ADDR` | `https://vault.internal.company` | Vault server URL (only when `USE_VAULT=true`) |
| `VAULT_TOKEN` | `s.xxx` | Vault token (only when `USE_VAULT=true`) |
| `VAULT_PATH` | `secret/devops-control-center` | Vault KV base path |

> The backend builds the API base URL from `AZURE_DEVOPS_ORG`:
> ```
> https://dev.azure.com/{AZURE_DEVOPS_ORG}
> ```
> A 500 error is raised at request time if `AZURE_DEVOPS_ORG` is missing and `USE_MOCK_AZURE_DEVOPS=false`.

---

## 4. Backend Implementation

### File: `backend/app/api/azure_devops.py`

#### PAT resolution (`_get_pat_for_user`)

```python
def _get_pat_for_user(current_user: AuthUser) -> str:
    user_id = str(current_user.get("id"))
    pat = get_user_azure_devops_pat(user_id)   # per-user from DB / Vault
    if pat:
        return pat
    if _ENV_PAT:                                # server fallback (admin)
        return _ENV_PAT
    raise HTTPException(400, "Azure DevOps is not connected. Please add your PAT.")
```

#### PAT management endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/azure-devops/pat` | Returns `{ configured, has_personal_pat }` — value never exposed |
| `POST` | `/api/azure-devops/pat` | Body: `{ "pat": "…" }` — saves per-user PAT |
| `DELETE` | `/api/azure-devops/pat` | Removes per-user PAT (server fallback still applies) |

#### Work items (`GET /api/azure-devops/work-items`)

Key behaviours:

- Uses `[System.AssignedTo] = @Me` in WIQL — Azure DevOps resolves `@Me` against the **PAT owner's identity**, so results are always scoped to the user regardless of what email the frontend passes.
- Accepts optional `?project=ProjectName` to filter by team project.
- Fetches state categories from `/{project}/_apis/wit/workitemtypes/{type}/states` for each unique `(project, type)` pair. Returns `state_category` (`Proposed`, `InProgress`, `Resolved`, `Completed`, `Removed`) alongside the raw state name so the frontend can count correctly even when teams use custom state names like "Doing".
- Falls back gracefully: if state-category lookup fails, a warning is logged and the frontend name-based heuristic takes over.

#### Projects (`GET /api/azure-devops/projects`)

Calls `/_apis/projects?$top=200` and returns `[{ id, name }]`. Used by the widget dropdown.

### File: `backend/app/secrets_manager.py`

Abstracts PAT storage behind two functions used by `azure_devops.py`:

```python
store_user_azure_devops_pat(user_id: str, pat: str) -> None
get_user_azure_devops_pat(user_id: str) -> Optional[str]
delete_user_azure_devops_pat(user_id: str) -> None
```

The PAT is stored in the `system_config` table under the key `user:{user_id}:azure_devops_pat` with `is_sensitive = true`. When Vault is enabled, KV v2 is used instead.

---

## 5. Frontend Implementation

### Hook: `frontend/src/hooks/useAzureDevOpsConnection.ts`

Shared across all three ADO widgets. Calls `GET /api/azure-devops/pat` once and caches the result in `sessionStorage` for 60 seconds so multiple widgets on the same page don't fire redundant requests.

```typescript
const { connected, checking, saving, hasPersonalPat, savePat, disconnect } =
  useAzureDevOpsConnection();
```

| Field | Type | Meaning |
|---|---|---|
| `connected` | `boolean` | PAT is configured (personal or server fallback) |
| `checking` | `boolean` | Initial status check in flight |
| `hasPersonalPat` | `boolean` | User has stored their own PAT (not just the server fallback) |
| `savePat(pat)` | `async → boolean` | Saves PAT, updates cache |
| `disconnect()` | `async` | Deletes personal PAT, re-checks status |

### Component: `frontend/src/components/common/AzureConnectPrompt.tsx`

Rendered inside the footer of every ADO widget. Behaviour changes based on connection state:

| State | UI shown |
|---|---|
| Checking | Spinner |
| Not connected | "Connect via PAT" button → inline PAT input form |
| Connected via server PAT | Subtle footer: "Using shared token · Use your own PAT →" |
| Connected via personal PAT | Subtle footer: "Personal PAT active · Disconnect" |

### Widget: `AzureDevOpsWorkItems`

Extra features beyond the other two widgets:

- **Project dropdown** in the widget header — populated from `GET /api/azure-devops/projects`. Defaults to "All projects".
- **State category counting** using `getEffectiveCategory()`:
  1. Uses `state_category` from the backend if it is a known value.
  2. Falls back to name matching (`"doing"` → In Progress, `"closed"` → done, etc.) when the backend category is missing.

```typescript
const toDoCount      = workItems.filter(wi => TODO_CATEGORIES.has(getEffectiveCategory(wi))).length;
const inProgressCount = workItems.filter(wi => IN_PROGRESS_CATEGORIES.has(getEffectiveCategory(wi))).length;
```

### API client methods (`frontend/src/lib/api-client.ts`)

```typescript
getWorkItems(project?: string)          // GET /azure-devops/work-items
getAzureDevOpsProjects()                // GET /azure-devops/projects
getPullRequests(username: string)       // GET /azure-devops/pull-requests
getPipelines()                          // GET /azure-devops/pipelines
getAzureDevOpsPatStatus()              // GET /azure-devops/pat
saveAzureDevOpsPat(pat: string)        // POST /azure-devops/pat
deleteAzureDevOpsPat()                 // DELETE /azure-devops/pat
```

---

## 6. Local Development Setup

### 1. Environment file

The backend loads `.env` files in this priority order (first file wins):

1. `backend/.env`
2. `backend/app/.env`
3. `infrastructure/k8s/base/secrets/.env`

Create or edit whichever is most convenient. The minimum required for real Azure DevOps data:

```bash
USE_MOCK_AZURE_DEVOPS=false
AZURE_DEVOPS_ORG=YourOrgName
AZURE_DEVOPS_PAT=your-personal-access-token   # admin fallback; other users enter theirs in the UI
```

### 2. PAT scopes

Your PAT needs at minimum:

| Scope | Required for |
|---|---|
| **Work Items** → Read | Work items widget |
| **Code** → Read | Pull requests widget |
| **Build** → Read | Pipelines widget |
| **Project and Team** → Read | Project list dropdown |

### 3. Run

```bash
# Backend
cd backend
pip install -r app/requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`. Log in via configured SSO, then:

- If your email is `golden.mihel@gmail.com` (admin), data loads automatically using the env PAT.
- Other users see a "Connect via PAT" prompt inside each Azure DevOps widget.

---

## 7. Kubernetes / Production Setup

### Secrets

The Azure DevOps PAT is stored in the `all-secrets` Kubernetes Secret:

```yaml
# infrastructure/k8s/base/secrets/secrets.example.yaml
AZURE_DEVOPS_PAT: <base64-encoded-pat>
```

Apply secrets:

```bash
kubectl apply -k infrastructure/k8s/overlays/dev
```

### ConfigMap

Organisation name is stored in `azure-devops-config`:

```yaml
# infrastructure/k8s/base/configmaps/azure-devops-config.yaml
data:
  organization: DevCollection-Inheritance
  query-user: ""
```

### Backend deployment env vars

`infrastructure/k8s/base/deployments/backend.yaml` maps these to the pod:

```yaml
- name: USE_MOCK_AZURE_DEVOPS
  value: "false"
- name: AZURE_DEVOPS_ORGANIZATION
  valueFrom:
    configMapKeyRef:
      name: azure-devops-config
      key: organization
- name: AZURE_DEVOPS_PAT
  valueFrom:
    secretKeyRef:
      name: all-secrets
      key: AZURE_DEVOPS_PAT
```

### Per-user PAT storage in production

PATs entered by users are written to the `system_config` PostgreSQL table (no extra infrastructure required). If you want stronger isolation, set `USE_VAULT=true` and configure the Vault env vars — the `secrets_manager.py` module switches backends automatically.

---

## 8. CI/CD Pipeline

### How it works

Both `frontend-workflow.yml` and `backend-workflow.yml` use the shared `template-workflow.yml` via `workflow_call`.

On every push to `frontend/**` or `backend/app/**`:

1. **Build** — Docker image built with two tags:
   - `:latest` — for human convenience
   - `:sha-<7-char-commit>` — immutable tag used by the deployment
2. **Push** — both tags pushed to Google Artifact Registry (`europe-west1-docker.pkg.dev/devops-idp-489012/devops-idp/`)
3. **Deploy** — `google-github-actions/get-gke-credentials@v2` authenticates to `devops-idp-cluster` in `europe-west1`, then:
   ```bash
   kubectl set image deployment/<service> <service>=<registry>/<service>:sha-<sha> -n devops-control-center-dev
   kubectl rollout status deployment/<service> -n devops-control-center-dev --timeout=300s
   ```

Using the commit SHA tag (not `:latest`) means `kubectl` always sees a changed value and restarts pods on every merge — no manual `kubectl rollout restart` needed.

### Manual trigger

`workflow_dispatch` is defined in both caller workflows.  
The **"Run workflow"** button appears in the GitHub Actions tab once the branch is merged to `main`.  
Until then, trigger manually via GitHub CLI:

```bash
gh workflow run "Frontend CI" --ref feat/mish/sso
gh workflow run "Service Backend CI" --ref feat/mish/sso
```

Or touch a file in the watched path and push:

```bash
# Use your editor (NOT PowerShell echo >>) to add a blank line to any file under frontend/
git add frontend/src/...
git commit -m "ci: retrigger"
git push
```

> **Warning:** Never use PowerShell `echo "" >> file` to touch files — it writes UTF-16 LE with null bytes which breaks Python source parsing and ESLint.

### Required GitHub secrets

| Secret | Value |
|---|---|
| `GCP_SA_KEY` | JSON key of a GCP service account with `Artifact Registry Writer` + `Kubernetes Engine Developer` roles |

---

## 9. Troubleshooting

### Widgets show "Using shared token" but data looks wrong

The server-level `AZURE_DEVOPS_PAT` is being used. Verify the PAT in your `.env` / Kubernetes secret is valid and has the required scopes. Test directly:

```bash
curl -u ":$AZURE_DEVOPS_PAT" \
  "https://dev.azure.com/$AZURE_DEVOPS_ORG/_apis/projects?api-version=7.0"
```

### Work items show items for all users, not just me

The `@Me` macro in WIQL resolves to the identity that owns the PAT. If a shared/service-account PAT is used (server fallback), all users see that account's items. Each user should save their own PAT via the widget UI to see their personal items.

### "Doing" state counted as To Do

This happens when the backend can't fetch state categories (permission issue on the process API, or unexpected URL). Check the backend logs for:

```
WARNING: Could not fetch state categories for <project>/<type> ...
```

The frontend falls back to name-based matching — add `"doing"` to the `IN_PROGRESS_NAMES` list in `AzureDevOpsWorkItems.tsx` if it's still not matching.

### Backend pod fails to start after deploy (`SyntaxError: null bytes`)

A source file was corrupted by PowerShell's UTF-16 redirection. Fix with:

```bash
python -c "
import os
for path in ['backend/app/main.py']:
    raw = open(path, 'rb').read()
    open(path, 'wb').write(raw.replace(b'\x00', b'').lstrip(b'\xff\xfe').rstrip() + b'\n')
"
git add backend/app/main.py
git commit -m 'fix: remove null bytes'
git push
```

### `kubectl rollout` times out in CI

The new pod isn't passing health checks within 300 s. Check pod events:

```bash
kubectl describe pod -n devops-control-center-dev -l app=backend
kubectl logs -n devops-control-center-dev -l app=backend --previous
```

Common causes: missing secret key referenced in deployment, DB unreachable at startup, import error in Python code.
