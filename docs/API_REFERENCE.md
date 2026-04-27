# API Reference

Base URL: `http://localhost:8000/api`

## Authentication

All API requests (except `/auth/*` and `/health`) require authentication via JWT token in the `Authorization` header:

```
Authorization: Bearer <token>
```

---

## Auth API

### POST /auth/login

Login with username (mock SSO).

**Request:**
```json
{
  "username": "admin@internal"
}
```

**Response:**
```json
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "user": {
      "id": "uuid",
      "username": "admin@internal",
      "email": "admin@internal.company",
      "role": "Platform Admin",
      "hierarchy_level": 1,
      "permissions": ["*"]
    }
  }
}
```

---

## Dashboard API

### GET /dashboards

Get all dashboards for current user.

**Response:**
```json
{
  "success": true,
  "data": [
    {
      "id": "uuid",
      "user_id": "uuid",
      "name": "My Dashboard",
      "is_default": true,
      "layout": [...],
      "widgets": [...]
    }
  ]
}
```

### GET /dashboards/default

Get user's default dashboard.

### PUT /dashboards/:id

Update dashboard layout or widgets.

**Request:**
```json
{
  "name": "Updated Dashboard",
  "layout": [...],
  "widgets": [...]
}
```

### GET /dashboards/widget-types

Get available widget types for user's role.

---

## Azure DevOps API

### GET /azure-devops/work-items

Get work items for user.

**Query Parameters:**
- `username` - Username (optional, uses authenticated user)

**Response:**
```json
{
  "success": true,
  "data": [
    {
      "id": 1001,
      "title": "Implement feature",
      "state": "In Progress",
      "type": "User Story",
      "url": "https://..."
    }
  ]
}
```

### GET /azure-devops/pull-requests

Get pull requests for user.

### GET /azure-devops/pipelines

Get recent pipeline runs.

---

### POST /azure-devops/projects/create

Submit a Terraform job to provision a new Azure DevOps project. Returns immediately with a `job_id`; poll the status endpoint to track progress.

> See [SELF_SERVICE_TERRAFORM.md](./SELF_SERVICE_TERRAFORM.md) for full architecture details.

**Request:**
```json
{
  "project_name": "my-platform",
  "process_type": "Scrum",
  "admin_username": "user@company.com"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `project_name` | string | ✓ | Name for the new ADO project. Sanitised to ADO naming rules (max 64 chars). |
| `process_type` | string | ✓ | One of: `Scrum`, `Agile`, `CMMI`, `Basic`. A custom inherited process `<project_name>-<process_type>` is created automatically. |
| `admin_username` | string | ✓ | Azure DevOps principal name (e-mail) of the user to assign as project administrator. |

**Response `200`:**
```json
{
  "success": true,
  "data": {
    "job_id": "tf-my-platform-2412101430",
    "status": "pending"
  },
  "timestamp": "2024-12-10T14:30:00Z"
}
```

**Error responses:**

| Status | Condition |
|---|---|
| `400` | `process_type` not in allowed list, or blank fields after sanitisation |
| `409` | A project with `project_name` already exists in the Azure DevOps organisation |
| `422` | `admin_username` not found in the Azure DevOps organisation |
| `500` | `AZURE_DEVOPS_BASE_URL` not configured on the server |

---

### GET /azure-devops/projects/create/status/{job_id}

Poll the status of a Terraform provisioning job. Call every 3 seconds until `status` is `succeeded` or `failed`.

**Path parameter:** `job_id` — the value returned by `POST /projects/create`.

**Response (while running):**
```json
{
  "success": true,
  "data": {
    "status": "running"
  },
  "timestamp": "2024-12-10T14:30:15Z"
}
```

**Response (succeeded):**
```json
{
  "success": true,
  "data": {
    "status": "succeeded",
    "project_url": "https://dev.azure.com/DevCollection-Inheritance/my-platform"
  },
  "timestamp": "2024-12-10T14:31:45Z"
}
```

**Response (failed):**
```json
{
  "success": true,
  "data": {
    "status": "failed",
    "error": "A project with this name already exists in Azure DevOps."
  },
  "timestamp": "2024-12-10T14:31:50Z"
}
```

**Possible `status` values:**

| Value | Meaning |
|---|---|
| `pending` | K8s Job created, pod not yet scheduled |
| `running` | Terraform `init` / `apply` in progress |
| `succeeded` | Project created; `project_url` is populated |
| `failed` | Terraform apply failed; `error` contains a user-facing message |

On the first `succeeded` poll the backend also:
- Assigns `admin_username` to the project's *Project Administrators* group.
- Saves Terraform pod logs to `gs://devops-control-center-tfstate/terraform/logs/`.

---

## SonarQube API

### GET /sonarqube/projects

Get all SonarQube projects.

**Response:**
```json
{
  "success": true,
  "data": [
    {
      "key": "backend-api",
      "name": "Backend API",
      "quality_gate": {
        "status": "OK"
      },
      "metrics": {
        "coverage": 78.5,
        "bugs": 3,
        "vulnerabilities": 0
      }
    }
  ]
}
```

### GET /sonarqube/projects/:key

Get specific project details.

---

## Artifactory API

### GET /artifactory/artifacts

Get recent artifacts.

### GET /artifactory/repositories

Get all repositories.

### GET /artifactory/storage

Get storage usage information.

---

## ServiceNow API

### GET /servicenow/tickets

Get tickets for user.

### GET /servicenow/stats

Get incident statistics.

---

## Approval API

### GET /approvals/requests

Get approval requests.

**Query Parameters:**
- `status` - Filter by status (PENDING, APPROVED, REJECTED)

**Response:**
```json
{
  "success": true,
  "data": [
    {
      "id": "uuid",
      "requester_username": "user@internal",
      "request_type": "ADO_PROJECT_CREATE",
      "request_title": "Create new project",
      "status": "PENDING",
      "created_at": "2024-01-15T10:00:00Z"
    }
  ]
}
```

### POST /approvals/requests

Create new approval request.

**Request:**
```json
{
  "request_type": "ADO_PROJECT_CREATE",
  "request_title": "Create DevOps project for Team X",
  "request_payload": {
    "name": "team-x-project",
    "description": "Project for Team X"
  }
}
```

### POST /approvals/requests/:id/approve

Approve a request.

**Request:**
```json
{
  "comments": "Approved - looks good"
}
```

### POST /approvals/requests/:id/reject

Reject a request.

**Request:**
```json
{
  "comments": "Rejected - needs more details"
}
```

---

## Metrics API

### GET /metrics/usage

Get usage metrics (requires observability permission).

**Query Parameters:**
- `period` - Time period (7d, 30d)

**Response:**
```json
{
  "success": true,
  "data": {
    "widgetUsage": [...],
    "selfServiceUsage": [...],
    "dailyActiveUsers": [...]
  }
}
```

### GET /metrics/services

Get service health metrics.

### GET /metrics/approvals

Get approval metrics and statistics.

---

## Health Check

### GET /health

Basic health check.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:00:00Z",
  "services": {
    "database": "up",
    "redis": "up"
  }
}
```

---

## Error Responses

All errors follow this format:

```json
{
  "success": false,
  "error": "Error message",
  "timestamp": "2024-01-15T10:00:00Z"
}
```

### HTTP Status Codes

- `200` - Success
- `201` - Created
- `400` - Bad Request
- `401` - Unauthorized
- `403` - Forbidden
- `404` - Not Found
- `500` - Internal Server Error
- `503` - Service Unavailable

