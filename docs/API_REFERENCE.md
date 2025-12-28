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

