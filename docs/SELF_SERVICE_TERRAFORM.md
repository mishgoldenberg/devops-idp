# Self-Service: Terraform Project Provisioning

This document covers the redesigned Azure DevOps self-service feature. Instead of calling the ADO REST API directly from the backend and blocking until the project is ready, every project is now provisioned by a **Terraform container** running as a Kubernetes Job. The backend submits the job and returns immediately; the frontend polls a status endpoint and shows live progress.

---

## Table of Contents

1. [Overview](#1-overview)
2. [User Flow](#2-user-flow)
3. [Backend Architecture](#3-backend-architecture)
   - [Input Validation](#31-input-validation)
   - [Pre-flight: Custom Process Creation](#32-pre-flight-custom-process-creation)
   - [Terraform Module Generation](#33-terraform-module-generation)
   - [Kubernetes Job Execution](#34-kubernetes-job-execution)
   - [Status Polling & Post-job Tasks](#35-status-polling--post-job-tasks)
4. [Frontend Architecture](#4-frontend-architecture)
5. [GCP Storage Layout](#5-gcp-storage-layout)
6. [Security & RBAC](#6-security--rbac)
7. [Infrastructure Prerequisites](#7-infrastructure-prerequisites)
8. [Mock Mode (Development)](#8-mock-mode-development)
9. [Error Reference](#9-error-reference)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

```
Browser  →  POST /projects/create  →  Backend validates + submits K8s Job  →  returns job_id
           ←  { job_id, status: "pending" }

Browser  →  GET /projects/create/status/{job_id}  (every 3 s)
           ←  { status: "pending|running|succeeded|failed", project_url?, error? }

                   K8s Job (hashicorp/terraform:1.6)
                   ├── terraform init   (GCS backend, Workload Identity)
                   ├── terraform apply  (creates ADO project)
                   └── outputs project_url → parsed from pod logs by backend

           On first "succeeded" poll:
             • Backend assigns admin_username to Project Administrators group
             • Backend saves pod logs → GCS bucket
```

**Key design decisions:**

| Decision | Rationale |
|---|---|
| Async job instead of blocking HTTP | ADO project creation takes 30–90 s; a synchronous response would time out at the gateway/ingress layer |
| Terraform for project creation | State is tracked in GCS; re-runs are idempotent; infrastructure drift is detectable |
| Custom process pre-created via REST | The `microsoft/azuredevops` provider accepts a process by name; pre-creating it via REST before Terraform runs avoids a dependency on an unmerged provider feature |
| Admin assignment post-Terraform | Keeps the Terraform module simple; assignment is a lightweight REST call that does not need state |

---

## 2. User Flow

```
┌─────────────────────────────────────────────────────────┐
│  Self-Service page  →  click "Create"                   │
│                                                          │
│  ┌──── Modal: Form step ─────────────────────────────┐  │
│  │  Project Name    [my-platform      ] [?]           │  │
│  │  Process Type    [Scrum ▾          ] [?]           │  │
│  │  Admin User      [user@company.com ] [?]           │  │
│  │                                                    │  │
│  │  [ Review & Confirm → ]   [ Cancel ]               │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──── Modal: Confirm step ───────────────────────────┐  │
│  │  Project name   my-platform                        │  │
│  │  Process        my-platform-Scrum                  │  │
│  │  Admin          user@company.com                   │  │
│  │                                                    │  │
│  │  [ Confirm & Create → ]   [ ← Back ]               │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  (modal closes, page shows loading banner)               │
│                                                          │
│  ┌──── Provisioning banner ───────────────────────────┐  │
│  │  ⟳  Provisioning my-platform…                      │  │
│  │      ✓  Initialising Terraform workspace           │  │
│  │      ●  Downloading provider microsoft/azuredevops │  │
│  │      ○  Creating custom inherited process          │  │
│  │      ○  Provisioning Azure DevOps project          │  │
│  │      ○  Applying final configuration               │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──── Success banner ────────────────────────────────┐  │
│  │  ✓  Project Created Successfully!                  │  │
│  │     Project Name   my-platform                     │  │
│  │     Process        my-platform-Scrum               │  │
│  │                                                    │  │
│  │     [ Go to Project ↗ ]                            │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Hoverable hints** (`?` icon) are shown next to each form field:

| Field | Hint |
|---|---|
| Project Name | Lowercase letters, digits, hyphens and spaces only. Max 64 chars. |
| Process Type | A custom inherited process `<project>-<process>` is created and used as the work-item template. |
| Admin User | The Azure DevOps principal name (usually the user's e-mail). The account must already exist in the organisation. |

---

## 3. Backend Architecture

### 3.1 Input Validation

`POST /api/azure-devops/projects/create`

Before any ADO call the backend:

1. Sanitises `project_name` — strips characters not allowed by ADO naming rules, trims to 64 chars.
2. Checks `process_type` is one of `Scrum | Agile | CMMI | Basic` (case-insensitive).
3. Rejects blank `admin_username`.

In real mode (non-mock), two additional checks are made against the live ADO API:

| Check | HTTP status on failure |
|---|---|
| Project name already exists in the organisation | `409 Conflict` |
| `admin_username` not found in the organisation | `422 Unprocessable Entity` |

These are the **only two error conditions surfaced to the end user**. All other failures (network errors, Terraform apply failures) are logged and, if the job has already started, the user sees a generic "provisioning failed — check GCS logs" message.

### 3.2 Pre-flight: Custom Process Creation

Because the `microsoft/azuredevops` Terraform provider accepts a process template by **name**, the custom inherited process must exist in ADO before `terraform apply` runs.

The backend:
1. Fetches all existing processes from `GET /_apis/process/processes`.
2. Looks for a non-system process named `<project_name>-<process_type>`.
3. If absent, calls `POST /_apis/process/processes` (`api-version=7.1-preview.1`) with:
   ```json
   {
     "name": "my-platform-Scrum",
     "description": "Custom Scrum process for my-platform",
     "parentProcessTypeId": "<id of built-in Scrum>"
   }
   ```
4. If creation fails (e.g. insufficient PAT scope), falls back to the built-in process name.

### 3.3 Terraform Module Generation

`terraform_runner.generate_main_tf()` produces a `main.tf` with:

```hcl
terraform {
  required_providers {
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 1.1.0"
    }
  }
  backend "gcs" {
    bucket = "devops-control-center-tfstate"
    prefix = "terraform/state/<sanitised-project-name>"
  }
}

# AZDO_PERSONAL_ACCESS_TOKEN is injected via environment variable
provider "azuredevops" {
  org_service_url = "https://dev.azure.com/<org>"
}

resource "azuredevops_project" "project" {
  name               = "<project_name>"
  visibility         = "private"
  version_control    = "Git"
  work_item_template = "<project_name>-<process_type>"
}

output "project_url" {
  value = "https://dev.azure.com/<org>/<project_name>"
}
```

**No secrets are embedded in the Terraform source.** The PAT is injected as `AZDO_PERSONAL_ACCESS_TOKEN` from the `all-secrets` Kubernetes Secret (see §3.4). GCS backend credentials come from Workload Identity.

The generated file is stored in a Kubernetes ConfigMap named `tf-<project>-<timestamp>` in the `devops-control-center` namespace.

### 3.4 Kubernetes Job Execution

A `batch/v1` Job is created with:

| Field | Value |
|---|---|
| Image | `hashicorp/terraform:1.6` |
| Working directory | `/workspace` (ConfigMap mounted here) |
| Command | `terraform init -no-color && terraform apply -auto-approve -no-color` |
| Service account | `devops-terraform-sa` |
| GCP Workload Identity | `iam.gke.io/gcp-service-account: terraform-sa@your-project.iam.gserviceaccount.com` |
| `AZDO_PERSONAL_ACCESS_TOKEN` | Injected from `all-secrets` K8s Secret key `AZURE_DEVOPS_ADMIN_PAT` |
| `TF_LOG` | `INFO` |
| `ttlSecondsAfterFinished` | `86400` (24 h auto-cleanup) |
| `backoffLimit` | `0` (no retries — ADO project creation is not idempotent) |

Job name and ConfigMap name follow the pattern `tf-<sanitised-name>-<YYMMDDHHmm>`.

Job annotations carry metadata used by the status endpoint:

```
devops-portal/project-name   → original project name
devops-portal/admin-username → admin e-mail for post-job assignment
devops-portal/log-path       → GCS path for log upload
devops-portal/configmap      → ConfigMap to clean up after TTL
```

### 3.5 Status Polling & Post-job Tasks

`GET /api/azure-devops/projects/create/status/{job_id}`

The backend reads the K8s Job `.status` fields:

| K8s condition | Returned status |
|---|---|
| `.status.active > 0` | `running` |
| `.status.succeeded > 0` | `succeeded` |
| `.status.failed > 0` | `failed` |
| Neither (just created) | `pending` |

**On the first `succeeded` poll** (tracked via `_admin_assigned_jobs` in-memory set):

1. `project_url` is extracted by parsing the pod logs for a line containing `project_url =`.
2. `_assign_admin_post_terraform()` is called — looks up the user descriptor and group descriptor via ADO Graph API and creates the group membership.
3. `save_logs_to_gcs()` is called — reads all pod logs via the K8s API and uploads them to `gs://<TF_GCS_BUCKET>/terraform/logs/<project>-YYYYMMDD-HHMM.log`.

**On the first `failed` poll**: logs are saved to GCS; the error message is extracted from pod logs (see §9).

---

## 4. Frontend Architecture

### Files changed

| File | Change |
|---|---|
| `frontend/src/components/self-service/CreateProjectForm.tsx` | Full rewrite — form + confirm steps, tooltips, submit |
| `frontend/src/app/self-service/page.tsx` | Full rewrite — provisioning, polling, success/error banners |
| `frontend/src/lib/api-client.ts` | Two new typed methods |

### `CreateProjectForm` state machine

```
form  ──(submit)──▶  confirming  ──(confirm)──▶  submitting
                        │                              │
                     (back)                       onSuccess({job_id, …})
                        │
                     (form)
```

`submitting` shows a spinner and immediately calls `apiClient.createProject()`. On success the `onSuccess` callback fires with `{job_id, project_name, process_type, admin_username}` — the modal closes and control passes to the page.

On API error the state returns to `form` with an inline field error:
- HTTP 409 → red border + message on the **Project Name** field
- HTTP 422 → red border + message on the **Admin User** field

### `SelfServicePage` state machine

```
idle  ──(onSuccess)──▶  provisioning  ──(succeeded)──▶  success
                              │
                          (failed)
                              │
                           error  ──(Try again)──▶  idle
```

`provisioning` runs `setInterval(poll, 3000)` and advances a `loadingStep` index every 3.5 s to cycle through the five progress labels in the banner.

---

## 5. GCP Storage Layout

All Terraform artefacts are stored in `gs://<TF_GCS_BUCKET>`:

```
<TF_GCS_BUCKET>/
├── terraform/
│   ├── state/
│   │   └── <sanitised-project-name>/     ← tfstate managed by GCS backend
│   │       └── default.tfstate
│   └── logs/
│       └── <sanitised-project-name>-YYYYMMDD-HHMM.log  ← pod logs
```

The GCP service account `terraform-sa@your-project.iam.gserviceaccount.com` needs `roles/storage.objectAdmin` on this bucket.

---

## 6. Security & RBAC

- The self-service endpoint is **accessible to all authenticated users** (no additional RBAC gate).
- The Azure DevOps PAT (`AZURE_DEVOPS_ADMIN_PAT` / `all-secrets`) must have: **Project and Team (Read & Write)**, **Process (Read & Write)**, **Graph (Read & Write)**.
- The Terraform container **never receives the GCP SA key file** — authentication is via GKE Workload Identity only.
- The generated `main.tf` is stored in a Kubernetes ConfigMap (not a Secret); no credentials are written into it.
- Job TTL is 24 hours; ConfigMaps are removed when the Job is garbage-collected.

---

## 7. Infrastructure Prerequisites

The following Kubernetes resources must be present before deploying this feature. They are **not** created by the application — provision them once per cluster.

### 7.1 Kubernetes ServiceAccount

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: devops-terraform-sa
  namespace: devops-control-center
  annotations:
    iam.gke.io/gcp-service-account: terraform-sa@your-project.iam.gserviceaccount.com
```

### 7.2 GCP Workload Identity binding

```bash
gcloud iam service-accounts add-iam-policy-binding \
  terraform-sa@your-project.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:your-project.svc.id.goog[devops-control-center/devops-terraform-sa]"
```

### 7.3 GCS bucket permissions

```bash
gcloud storage buckets add-iam-policy-binding \
  gs://<TF_GCS_BUCKET> \
  --member="serviceAccount:terraform-sa@your-project.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### 7.4 Backend pod RBAC (allows the API pod to manage Jobs / ConfigMaps)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: terraform-job-manager
  namespace: devops-control-center
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch"]
  - apiGroups: [""]
    resources: ["configmaps", "pods", "pods/log"]
    verbs: ["create", "get", "list", "watch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: terraform-job-manager
  namespace: devops-control-center
subjects:
  - kind: ServiceAccount
    name: api-gateway          # the SA used by the backend pod
    namespace: devops-control-center
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: terraform-job-manager
```

Apply both:
```bash
kubectl apply -f infrastructure/k8s/base/rbac/terraform-job-manager.yaml
```

---

## 8. Error Reference

| Scenario | HTTP status | User-facing message |
|---|---|---|
| `project_name` blank after sanitisation | 400 | `project_name is required.` |
| `process_type` not in allowed list | 400 | `process_type must be one of: Scrum, Agile, CMMI, Basic.` |
| `admin_username` blank | 400 | `admin_username is required.` |
| Project name already taken in ADO | 409 | `A project named '<name>' already exists in Azure DevOps.` |
| Admin user not found in ADO org | 422 | `Azure DevOps user '<email>' was not found in the organisation.` |
| Terraform apply fails with "already exists" in pod logs | Status `failed` | `A project with this name already exists in Azure DevOps.` |
| Terraform apply fails with "not found" in pod logs | Status `failed` | `The specified admin user could not be found in Azure DevOps.` |
| Any other Terraform failure | Status `failed` | `Terraform execution failed. Check logs in the GCS bucket for details.` |

---

## 10. Troubleshooting

### Job stuck in `pending` for more than 2 minutes

The K8s scheduler may not be able to place the pod. Check:
```bash
kubectl describe job <job-id> -n devops-control-center
kubectl get pods -n devops-control-center -l app=terraform-runner
kubectl describe pod <tf-pod> -n devops-control-center
```

Common causes: `devops-terraform-sa` ServiceAccount missing, image pull failure, resource quota exceeded.

### Terraform fails with authentication error

`Error: The argument "personal_access_token" is required` — the `AZDO_PERSONAL_ACCESS_TOKEN` env var was not injected. Verify the `all-secrets` K8s Secret contains the key `AZURE_DEVOPS_ADMIN_PAT`:
```bash
kubectl get secret all-secrets -n devops-control-center -o jsonpath='{.data.AZURE_DEVOPS_ADMIN_PAT}' | base64 -d | wc -c
# Should be > 0
```

### GCS backend initialisation fails

`Error: Failed to get existing workspaces: storage: bucket doesn't exist` — the bucket name is wrong or the SA lacks permissions. Check:
```bash
gcloud storage ls gs://<TF_GCS_BUCKET> --impersonate-service-account=terraform-sa@your-project.iam.gserviceaccount.com
```

### Workload Identity not working

```bash
# Verify the K8s SA annotation
kubectl get sa devops-terraform-sa -n devops-control-center -o yaml | grep gcp-service-account

# Verify the IAM binding
gcloud iam service-accounts get-iam-policy terraform-sa@your-project.iam.gserviceaccount.com
```

### View Terraform logs after a run

```bash
# From the K8s pod (while TTL has not expired)
kubectl logs -l job-name=<job-id> -n devops-control-center

# From GCS (persisted after job deletion)
gcloud storage cat gs://<TF_GCS_BUCKET>/terraform/logs/<project>-YYYYMMDD-HHMM.log
```

### Backend cannot create Jobs (403 Forbidden from K8s API)

The backend's service account is missing the `terraform-job-manager` RoleBinding. Apply the RBAC manifest from §7.4.
