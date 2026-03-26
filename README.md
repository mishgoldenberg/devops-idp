# DevOps Control Center

**Production-grade internal web platform for ~500 users in closed network environment**

## 🏗️ Architecture Overview

### Design Principles

- **Microservice-ready architecture** - Each external system has dedicated service
- **Frontend/Backend decoupled** - Clean API contracts via API Gateway
- **Local-first development** - Runs entirely on developer laptop
- **No internet dependency** - All services work in isolated network
- **Role-Based Access Control (RBAC)** - 7-level hierarchy with granular permissions
- **Widget-based personalization** - Users customize their own dashboards

### System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│        Frontend (HTMX templates served by FastAPI)         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Dashboard  │  │   Approvals  │  │ Observability│      │
│  │   Widgets    │  │   Self-Svc   │  │   Metrics    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            │ Server-rendered HTML / REST API
                            ▼
┌─────────────────────────────────────────────────────────────┐
│           Backend API / BFF (Python + FastAPI)             │
│  - Authentication & Authorization                          │
│  - Request routing & aggregation                           │
│  - Rate limiting & caching (via Redis)                     │
│  - Adapter modules for ADO / SonarQube / Artifactory       │
│    / ServiceNow / AI Chatbot (mocked by default)           │
└───────────────┬────────────────────────────────────────────┘
                │
   ┌────────────▼────────────┐          ┌───────────────┐
   │       PostgreSQL        │          │     Redis     │
   │  - Users                │          │  - Cache      │
   │  - Dashboards           │          │  - Session    │
   │  - Approvals            │          └───────────────┘
   │  - Audit & Metrics      │
   └─────────────────────────┘
```

### Services Architecture

| Service         | Port | Purpose                                                                           |
| --------------- | ---- | --------------------------------------------------------------------------------- |
| **api-gateway** | 8000 | Python FastAPI backend (BFF, auth, routing, aggregation, approvals, integrations) |
| **postgres**    | 5432 | Primary data store                                                                |
| **redis**       | 6379 | Cache & session store                                                             |

---

## 🧩 HTMX Prototype Frontend (Optional)

This repository now includes a minimal HTMX-powered frontend served from the Python backend. It is intended as a starting point for rebuilding the UI from scratch while leveraging the existing FastAPI APIs.

- The HTMX UI lives under: **`/ui/`**
- Static assets (CSS, images, etc.) are served from **`/static/`**
- Use HTMX to call existing API routes under **`/api/`** (e.g., `/api/health/ready`)

### Running the HTMX prototype

1. Start the backend (via Docker):
   ```sh
   docker compose up -d postgres redis api-gateway
   ```
2. Open the prototype in your browser:
   ```
   http://localhost:8000/ui/
   ```

---

## 🔐 RBAC Model

### Role Hierarchy (Highest → Lowest)

1. **Platform Admin** - Full system access, approval authority, observability
2. **Unit Commander** - Cross-branch visibility, strategic metrics
3. **Branch Head** - Branch-level aggregation, team oversight
4. **Head of Section** - Section metrics, observability access
5. **Project Manager** - Project-level visibility, some approvals
6. **Team Lead** - Team metrics, observability access
7. **Regular User** - Personal dashboard, basic self-service

### Authorization Matrix

| Capability              | Platform Admin | Unit Commander | Branch Head | Head of Section | Project Manager | Team Lead | Regular User |
| ----------------------- | :------------: | :------------: | :---------: | :-------------: | :-------------: | :-------: | :----------: |
| View own dashboard      |       ✓        |       ✓        |      ✓      |        ✓        |        ✓        |     ✓     |      ✓       |
| Customize widgets       |       ✓        |       ✓        |      ✓      |        ✓        |        ✓        |     ✓     |      ✓       |
| Create ADO project      |       ✓        |       ✓        |      ✓      |        ✓        |        ✓        |     -     |      -       |
| Enable Sonar scanning   |       ✓        |       ✓        |      ✓      |        ✓        |        -        |     -     |      -       |
| Approve requests        |       ✓        |       ✓        |      ✓      |        ✓        |     Partial     |     -     |      -       |
| View aggregated metrics |       ✓        |       ✓        |      ✓      |        -        |        -        |     -     |      -       |
| View observability      |       ✓        |       -        |      -      |        ✓        |        -        |     ✓     |      -       |
| Manage users            |       ✓        |       -        |      -      |        -        |        -        |     -     |      -       |

---

## 📊 Widget System

### Widget Architecture

Widgets are self-contained React components that:

- Fetch their own data from API Gateway
- Handle loading/error states independently
- Respect RBAC (backend filters data by role)
- Support drag-and-drop reordering
- Persist layout per user in PostgreSQL

### Available Widgets

#### Azure DevOps

- **My Work Items** - PBIs assigned to user (To Do / In Progress counts)
- **My Pull Requests** - Active PRs with review status
- **Pipeline Status** - Last 5 pipeline runs
- **Sprint Progress** - Current sprint burn-down (Team Lead+)

#### SonarQube

- **Quality Gate** - Current project status
- **Code Coverage** - Trend indicator
- **Technical Debt** - Hours estimation
- **Security Hotspots** - Critical issues count

#### Artifactory

- **Latest Artifacts** - Recent builds per repository
- **Storage Usage** - Quota consumption (Team Lead+)
- **Download Stats** - Most downloaded artifacts (PM+)

#### ServiceNow

- **My Tickets** - Open incident/request count
- **Team Tickets** - Team workload (Team Lead+)

#### AI Assistant

- **Chatbot Widget** - Embedded conversational UI
- **Recent Conversations** - Quick access to history

#### Executive Widgets (Commander/Head roles)

- **Branch Dashboard** - Aggregated metrics per branch
- **Deployment Frequency** - Release velocity
- **Quality Trends** - Multi-project quality overview
- **Resource Utilization** - Cross-team capacity

---

## 🔄 Self-Service Features

### Self-Service Project Creation

**Status**: ✅ Direct provisioning (no approval required)

Users with appropriate roles can directly create Azure DevOps projects with:

- **Custom process templates** - Creates dedicated inherited process (e.g., `project-name-Scrum`)
- **Process types** - Scrum, Agile, CMMI, or Basic
- **Admin user assignment** - Automatically grants Project Administrator permissions
- **Instant provisioning** - Project available immediately via "Open in Azure DevOps" button

**Access**: Project Manager+  
**Endpoint**: `POST /api/azure-devops/projects/create`  
**Request**:

```json
{
	"project_name": "my-new-project",
	"process_type": "Scrum",
	"admin_username": "user@company.com"
}
```

**Response**:

```json
{
  "success": true,
  "data": {
    "project": {...},
    "added_admin": true,
    "project_url": "https://dev.azure.com/org/my-new-project"
  }
}
```

### Future Approval-Based Self-Services

These services are planned for future phases with approval workflows:

| Action                       |   Status   | Approver Roles                  | Implementation                        |
| ---------------------------- | :--------: | ------------------------------- | ------------------------------------- |
| Enable SonarQube PR Scanning | 🔄 Planned | Platform Admin, Head of Section | Approval workflow → Sonar integration |
| Request AI Model Access      | 🔄 Planned | Platform Admin                  | Approval workflow → AI service        |

### Redirect-Only Services (No Portal Implementation)

- **ServiceNow Ticket Creation** - Redirects to ServiceNow
- **OpenShift Namespace Creation** - Redirects to OpenShift Console
- **Artifactory Repository Creation** - Redirects to Artifactory
- Request timestamp
- Request details (JSON payload)
- Approver identity
- Approval/rejection timestamp
- Approver comments
- Execution result (success/failure)

---

## 🔌 External System Integration

### Adapter Pattern

Each external system is integrated via an **adapter module inside the Python backend**:

1. **Adapter Module** - Python code that knows how to talk to the external system
2. **Mock Implementation** - Returns realistic sample data for local dev (current default)
3. **Real Implementation** - Placeholder for production integration
4. **Stable API Contract** - JSON shapes shared with the frontend via the REST API

### Integration Points (Currently Mocked)

#### Azure DevOps

- **Endpoint**: `https://dev.azure.com/{org}`
- **Auth**: PAT (Personal Access Token)
- **APIs Used**:
  - Work Items (GET /wit/workitems)
  - Pull Requests (GET /git/pullrequests)
  - Pipelines (GET /pipelines/runs)

#### SonarQube

- **Endpoint**: `https://sonarqube.internal`
- **Auth**: Token-based
- **APIs Used**:
  - Project Status (GET /api/qualitygates/project_status)
  - Measures (GET /api/measures/component)

#### Artifactory

- **Endpoint**: `https://artifactory.internal`
- **Auth**: API Key
- **APIs Used**:
  - Artifacts (GET /api/storage)
  - Stats (GET /api/storageinfo)

#### ServiceNow

- **Endpoint**: `https://servicenow.internal`
- **Auth**: OAuth2
- **APIs Used**:
  - Incidents (GET /api/now/table/incident)
  - Requests (GET /api/now/table/sc_request)

#### AI Chatbot

- **Endpoint**: Internal AI model endpoint
- **Auth**: Internal token
- **Integration**: iframe embed + postMessage API

### Adding Real Integrations

To replace mocks with real integrations:

1. Navigate to `backend/services/{service-name}/src/adapters/`
2. Update `real-adapter.ts` with actual API calls
3. Configure credentials in `backend/.env` or Vault
4. Switch adapter in `backend/services/{service-name}/src/config/index.ts`

```typescript
// Example: azure-devops-service/src/config/index.ts
export const USE_MOCK = process.env.USE_MOCK_DATA === "true"; // Set to false for production
```

---

## 🚀 Local Development Setup

### Prerequisites

Before you begin, ensure you have the following installed:

- **Docker Desktop** with Docker Compose ([Download](https://www.docker.com/products/docker-desktop)) - **Required**
- **Git** ([Download](https://git-scm.com/)) - **Required**
- **8GB RAM minimum** (recommended: 16GB)

**Note:** Node.js and Python are not required locally — both UI and backend run inside Docker containers.

**Verify Docker installations:**

```bash
docker --version      # Should be 20.10+
docker compose version # Should be 2.0+
git --version
```

### Quick Start (Automated Setup) ⚡

The easiest way to get started is using our automated setup scripts. They handle all the configuration, building, and database setup for you.

# <<<<<<< HEAD

**Windows (PowerShell):**

```powershell
# Run the setup script (right-click -> Run with PowerShell, or in terminal)
.\setup.ps1

# After setup, use restart script for code changes:
.\restart.ps1                # Quick restart (backend changes)
```

**Linux/Mac (Bash):**

```bash
# Make scripts executable (first time only)
chmod +x setup.sh restart.sh

# Run the setup script
./setup.sh
```

> > > > > > > 035ffb8 (Remove old frontend code and replace it with a starter htmx template)
> > > > > > > The setup script will:

1. ✅ Check prerequisites (Docker)
2. ✅ Copy `env.example` to `.env` if it doesn't exist
3. ✅ Build all Docker images
4. ✅ Start all services (database, Redis, Python backend)
5. ✅ Wait for services to be healthy
6. ✅ Run database migrations
7. ✅ Seed initial data (roles, users, widgets)

### Run the backend locally (recommended)

If you need to run the FastAPI backend directly (for debugging or development), prefer running it as a module so Python resolves relative imports correctly. Running the file directly (for example `python backend/python_backend/app/main.py`) can trigger "attempted relative import with no known parent package" errors.

Recommended commands:

```bash
# Run using uvicorn with the package module path (when running from repository root)
uvicorn backend.python_backend.app.main:app --reload --host 0.0.0.0 --port 8000

# Alternate (matches Docker image layout where code is copied to `backend_python`):
uvicorn backend_python.app.main:app --reload --host 0.0.0.0 --port 8000

# Run via python module (ensures package context)
python -m backend.python_backend.app.main
```

Notes:

- The `uvicorn` forms above launch the FastAPI app in a way that preserves package context, so relative imports like `from .api import api_router` work as expected.
- Prefer `docker compose up -d` for local development to keep the environment consistent with other services (Postgres, Redis).
- If a contributor still runs the file directly and sees import errors, run the `uvicorn` command instead; a small fallback exists in `backend/python_backend/app/main.py` to help but module-based invocation is the correct long-term approach.

8. ✅ Open the application in your browser

**Manual Setup (Step-by-Step)**

If you prefer to set up manually or the script fails:

#### Step 1: Clone the Repository

```bash
git clone <repo-url>
cd devops-control-center
```

#### Step 2: Configure Environment Variables

Copy the example environment file and review/edit if needed:

```bash
# Windows (PowerShell)
Copy-Item env.example .env

# Linux/Mac
cp env.example .env
```

**Important:** The default `.env` values work out-of-the-box for local development. You only need to edit `.env` if you want to:

- Change database passwords
- Configure real external system integrations (currently using mocks)
- Adjust ports or service URLs

Key environment variables you might want to check:

- `POSTGRES_PASSWORD` - Database password (default: `Devops4ever`)
- `REDIS_PASSWORD` - Redis password (default: `Devops4ever`)
- `JWT_SECRET` - JWT signing secret (auto-generated, change for production)
- `SESSION_SECRET` - Session encryption secret (auto-generated, change for production)

#### Step 3: Start Docker Services

Start all services using Docker Compose:

```bash
# Windows (PowerShell)
docker compose up -d

# Linux/Mac
docker compose up -d
```

This will:

- Pull required Docker images (PostgreSQL, Redis)
- Build application images (Python backend)
- Start all containers
- Set up Docker networks and volumes

**Note:** The first build may take 5-10 minutes depending on your internet connection and system performance.

#### Step 4: Verify Services Are Running

Check that all containers are up and healthy:

```bash
docker compose ps
```

You should see all services with status "Up" or "running". Wait for `postgres` and `redis` to show "healthy" status before proceeding.

**Troubleshooting:** If any service fails to start:

```bash
# View logs for a specific service
docker compose logs postgres
docker compose logs api-gateway

# View all logs
docker compose logs -f
```

#### Step 5: Database Migrations & Seeding

**Database migrations and initial data seeding happen automatically when containers start.**

The Python backend runs migrations from `backend/database/migrations/` and seeds from `backend/database/seeds/` during first startup. You can verify this:

```bash
# Check backend logs for initialization
docker compose logs api-gateway | grep -i "migrat\|seed\|ready"
```

**If migrations fail:** Check the logs. If you need to manually debug:

```bash
# Connect to the database directly
docker compose exec postgres psql -U devops_user -d devops_control_center

# View tables
\dt

# Exit
\q
```

Once the database is ready:

- 7 role definitions (Platform Admin through Regular User)
- Widget type definitions
- Approval rules
- Service health entries

#### Step 7: Access the Application

**UI (HTMX):** http://localhost:8000/ui/

**API Gateway:** http://localhost:8000/api/

**Health Check:** http://localhost:8000/api/health

**Database:** `localhost:5432` (user: `devops`, password: from `.env`)

### Authentication (Google SSO)

Authentication uses **Google OAuth 2.0 / OpenID Connect**. Users sign in with their Google (Gmail) accounts; the backend creates or looks up users by email and issues an internal JWT (8-hour session by default).

<<<<<<< HEAD

- **Login:** Frontend “Continue with Google” → backend redirects to Google consent → callback at `/api/auth/callback` → backend issues JWT and returns token/user to frontend (popup or redirect).
- **Config:** Backend needs `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` (e.g. `https://devops.internal.company/api/auth/callback`), plus `JWT_SECRET` and `CORS_ORIGINS`. Frontend needs `NEXT_PUBLIC_API_BASE_URL` pointing at the same host.
- # **Full setup, env vars, GCP checklist, and troubleshooting:** [docs/SSO_GOOGLE_OAUTH.md](docs/SSO_GOOGLE_OAUTH.md).
  | Username                | Role            | Description                                                  |
  | ----------------------- | --------------- | ------------------------------------------------------------ |
  | `admin@internal`        | Platform Admin  | Full system access, can approve requests, view observability |
  | `commander@internal`    | Unit Commander  | Cross-branch visibility, strategic metrics                   |
  | `branch.head@internal`  | Branch Head     | Branch-level aggregation                                     |
  | `section.head@internal` | Head of Section | Section metrics, observability access                        |
  | `pm@internal`           | Project Manager | Project-level visibility                                     |
  | `lead@internal`         | Team Lead       | Team metrics, observability access                           |
  | `user@internal`         | Regular User    | Personal dashboard, basic self-service                       |

**Note:** In local development, authentication uses mock SSO. Any username ending in `@internal` will be accepted and mapped to a role based on the seeded users.

> > > > > > > 035ffb8 (Remove old frontend code and replace it with a starter htmx template)

### Common Commands

**Quick Restart (After Code Changes):**

The easiest way to restart services after making changes:

```bash
# Windows (PowerShell)
.\restart.ps1                    # Quick restart (backend changes)
.\restart.ps1 -Rebuild           # Rebuild all services (dependency changes)
.\restart.ps1 -Full              # Clean rebuild (full reset)
.\restart.ps1 -Service api-gateway  # Restart specific service

# Linux/Mac (Bash)
./restart.sh                     # Quick restart (backend changes)
./restart.sh --rebuild           # Rebuild all services (dependency changes)
./restart.sh --full              # Clean rebuild (full reset)
./restart.sh --service api-gateway  # Restart specific service
```

**Docker Compose Commands:**

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Stop and remove volumes (clears database)
docker compose down -v

# View logs for all services
docker compose logs -f

# View logs for specific service
docker compose logs -f api-gateway

# Rebuild a specific service
docker compose build api-gateway
docker compose up -d api-gateway

# Rebuild all services
docker compose build
docker compose up -d

# Check service status
docker compose ps

# Restart a specific service
docker compose restart api-gateway
```

**Database Commands:**

```bash
# Reset database (drop and recreate)
npm run db:reset

# Run migrations only
npm run migrate

# Run seeds only
npm run seed

# Access PostgreSQL CLI
docker compose exec postgres psql -U devops -d devops_control_center

# Access Redis CLI
docker compose exec redis redis-cli -a Devops4ever
```

### Development Workflow

**Making Code Changes:**

1. **Backend Changes (Python):**
   - Edit files in `backend/python_backend/app/`
   - Restart the backend container:

     ```bash
     # Windows
     .\restart.ps1 -Service api-gateway

     # Linux/Mac
     ./restart.sh --service api-gateway
     ```

2. **Database Changes:**
   - Edit `backend/database/schema.sql`
   - Rebuild and migrate:
     ```bash
     docker compose down -v
     docker compose up -d postgres redis
     npm run migrate
     npm run seed
     docker compose up -d
     ```

3. **After Dependency Changes (package.json):**
   - Rebuild affected services:

     ```bash
     # Windows
     .\restart.ps1 -Rebuild

     # Linux/Mac
     ./restart.sh --rebuild
     ```

### Troubleshooting

**Port Already in Use:**

```bash
# Windows: Find process using port
netstat -ano | findstr :8000

# Linux/Mac: Find process using port
lsof -i :8000

# Kill the process or change port in .env and docker-compose.yml
```

**Docker Build Fails:**

```bash
# Clear Docker cache and rebuild
docker compose build --no-cache

# Check Docker daemon is running
docker ps
```

**Database Connection Errors:**

- Verify PostgreSQL is healthy: `docker compose ps postgres`
- Check DATABASE_URL in `.env` matches docker-compose.yml values
- Ensure migrations ran successfully: Check logs with `docker compose logs postgres`

**Services Won't Start:**

- Check Docker has enough resources (memory, CPU)
- View service logs: `docker compose logs [service-name]`
- Verify all required environment variables are set in `.env`

### Development Workflow

- Make backend changes in `backend/python_backend/app/`.
- Restart the backend container when code changes:

  ```bash
  # Windows
  .\restart.ps1 -Service api-gateway

  # Linux/Mac
  ./restart.sh --service api-gateway
  ```

- Rebuild containers after dependency changes:

  ```bash
  # Windows
  .\restart.ps1 -Rebuild

  # Linux/Mac
  ./restart.sh --rebuild
  ```

- View backend logs:

  ```bash
  docker compose logs -f api-gateway
  ```

### Test Users (Seeded)

| Username              | Role            | Password |
| --------------------- | --------------- | -------- |
| admin@internal        | Platform Admin  | admin123 |
| commander@internal    | Unit Commander  | test123  |
| branch.head@internal  | Branch Head     | test123  |
| section.head@internal | Head of Section | test123  |
| pm@internal           | Project Manager | test123  |
| lead@internal         | Team Lead       | test123  |
| user@internal         | Regular User    | test123  |

---

Here is the consolidated Helm deployment and CI/CD documentation, optimized for your project's `README.md`.

---

## 🚀 Helm Deployment & CI/CD Workflow

This project utilizes **Helm** for declarative infrastructure management. By using Helm templates, we eliminate manual `kubectl` patches, ensuring the cluster state is always defined by the code in your repository.

### 1. The Deployment Pattern (`deployment/templates/deployment.yaml`)

To ensure pods restart automatically when secrets are updated, we use a checksum annotation. This forces a rolling update the moment the secret content is modified.

```yaml
spec:
  template:
    metadata:
      annotations:
        # Forces a fresh pod restart when the secret contents change
        checksum/secrets:
          {
            {
              include (print $.Template.BasePath "/secrets.yaml") . | sha256sum,
            },
          }
    spec:
      containers:
        - name: backend
          envFrom:
            - secretRef:
                name: all-secrets
```

### 2. The Secrets Pattern (`deployment/templates/secrets.yaml`)

Helm manages the `all-secrets` object directly, removing the need for error-prone `kubectl create secret` commands in your CI pipeline.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: all-secrets
type: Opaque
stringData:
  POSTGRES_PASSWORD: { { .Values.secrets.postgresPassword | quote } }
  OAUTH_CLIENT_ID: { { .Values.secrets.oauthClientId | quote } }
  OAUTH_CLIENT_SECRET: { { .Values.secrets.oauthClientSecret | quote } }
```

### 3. CI/CD Workflow (`template-workflow.yml`)

The workflow acts as an atomic delivery vehicle. It never patches the cluster manually; it simply updates the Helm release, waiting for the deployment to reach a healthy state.

```yaml
- name: Deploy Helm Chart
  run: |
    # 1. Clean up any stuck locks before attempting an upgrade
    helm rollback devops-stack 0 -n devops-control-center-prod --force || true

    # 2. Deploy using Helm (injecting secrets via --set)
    helm upgrade --install devops-stack ./deployment \
      -n devops-control-center-prod \
      --wait --timeout 10m \
      --set secrets.postgresPassword="${{ secrets.POSTGRES_PASSWORD }}" \
      --set secrets.oauthClientId="${{ secrets.OAUTH_CLIENT_ID }}" \
      --set secrets.oauthClientSecret="${{ secrets.OAUTH_CLIENT_SECRET }}"
```

### 4. Adding New Secrets

Whenever you introduce a new dependency, follow this process to maintain a clean cluster state:

1. **Add to GitHub Actions:** Add the new secret to **Settings > Secrets and variables > Actions**.
2. **Pass to Workflow:** Update your caller workflow (`deploy.yml`) to pass the variable:

```yaml
secrets:
  NEW_SECRET: ${{ secrets.NEW_SECRET }}
```

3. **Update Deployment:**

- Add `--set secrets.newSecret="${{ secrets.NEW_SECRET }}"` to your `template-workflow.yml`.
- Add `NEW_SECRET: {{ .Values.secrets.newSecret | quote }}` to your `secrets.yaml` template.

To ensure your team can access the platform at `devops.internal.company` immediately after deployment, you need to account for local DNS resolution, as this is an internal-only environment.

Add this section to your `README.md` to guide users through the initial configuration:

---

### 🌐 Accessing the Platform (Initial Setup)

Since `devops.internal.company` is an internal domain, your workstation will not automatically know how to resolve it to your Kubernetes Ingress controller's IP address.

#### 1. Identify the Ingress IP

First, find the external IP assigned to your NGINX Ingress controller:

```bash
kubectl get svc -n ingress-nginx
# Look for the 'EXTERNAL-IP' column

```

_(If you are running in a local environment like Minikube or Kind, run `minikube tunnel` or `kind load` to expose the IP.)_

#### 2. Configure Local DNS (Hosts File)

You must point the domain to your Ingress controller's IP by editing your local hosts file.

**For Linux / macOS:**

1. Open the file: `sudo nano /etc/hosts`
2. Add the following line:

```text
<EXTERNAL-IP-FROM-STEP-1>  devops.internal.company

```

3. Save and exit (Ctrl+O, Enter, Ctrl+X).

**For Windows (PowerShell as Administrator):**

1. Run: `notepad C:\Windows\System32\drivers\etc\hosts`
2. Add the same line:

```text
<EXTERNAL-IP-FROM-STEP-1>  devops.internal.company

```

3. Save the file.

#### 3. Verify Connectivity

Once saved, you should be able to reach the platform via your browser:
`https://devops.internal.company`

_Note: If you are using a self-signed certificate, your browser will show a "Not Secure" warning on the first visit. You can click "Advanced" and "Proceed" to continue to the site._

---

## 🗄️ Database Schema

### Core Tables

#### users

- `id` - UUID primary key
- `username` - Unique username from SSO
- `email` - User email
- `role_id` - Foreign key to roles
- `domain_groups` - JSON array of AD groups
- `created_at`, `updated_at`

#### roles

- `id` - Integer primary key
- `name` - Role name (Platform Admin, Unit Commander, etc.)
- `hierarchy_level` - Integer (1=highest, 7=lowest)
- `permissions` - JSON array of permission strings

#### dashboards

- `id` - UUID primary key
- `user_id` - Foreign key to users
- `layout` - JSON (widget positions, sizes)
- `widgets` - JSON array (widget IDs and configs)
- `updated_at`

#### approval_requests

- `id` - UUID primary key
- `requester_id` - Foreign key to users
- `request_type` - Enum (ADO_PROJECT, SONAR_SCAN, etc.)
- `request_payload` - JSON
- `status` - Enum (PENDING, APPROVED, REJECTED, EXECUTED)
- `approver_id` - Foreign key to users (nullable)
- `approver_comments` - Text
- `approved_at` - Timestamp
- `created_at`

#### audit_logs

- `id` - UUID primary key
- `user_id` - Foreign key to users
- `action` - String (CREATE_REQUEST, APPROVE, REJECT, etc.)
- `resource_type` - String
- `resource_id` - UUID
- `details` - JSON
- `ip_address` - String
- `user_agent` - String
- `created_at`

#### usage_metrics

- `id` - UUID primary key
- `user_id` - Foreign key to users
- `metric_type` - Enum (WIDGET_VIEW, SELF_SERVICE_USE, etc.)
- `metric_name` - String (widget name, service name, etc.)
- `value` - Integer
- `metadata` - JSON
- `created_at`

Full schema: `backend/database/schema.sql`

---

## 📦 Project Structure

<<<<<<< HEAD

```
devops-idp/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   ├── redis_client.py
│   │   ├── requirements.txt
│   │   ├── secrets_manager.py
│   │   ├── security.py
│   │   ├── terraform_runner.py
│   │   ├── test_rbac_and_pat.py
│   │   └── api/
│   │       ├── __init__.py
│   │       ├── ai_chatbot.py
│   │       ├── approvals.py
│   │       ├── artifactory.py
│   │       ├── auth.py
│   │       ├── azure_devops.py
│   │       ├── dashboards.py
│   │       ├── health.py
│   │       ├── metrics.py
│   │       ├── servicenow.py
│   │       └── sonarqube.py
├── deployment/
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── charts/
│   │   ├── backend/
│   │   │   ├── Chart.yaml
│   │   │   ├── values.yaml
│   │   │   └── templates/
│   │   │       ├── azure-devops-config.yaml
│   │   │       ├── deployment.yaml
│   │   │       ├── infra-config.yaml
│   │   │       ├── ingress.yaml
│   │   │       ├── secrets.yaml
│   │   │       ├── service-accounts.yaml
│   │   │       ├── service.yaml
│   │   │       ├── sso-config.yaml
│   │   │       └── terraform-rbac.yaml
│   │   ├── frontend/
│   │   │   ├── Chart.yaml
│   │   │   ├── values.yaml
│   │   │   └── templates/
│   │   │       ├── deployment.yaml
│   │   │       ├── ingress.yaml
│   │   │       └── service.yaml
│   │   └── infrastructure/
│   │       ├── Chart.yaml
│   │       ├── values.yaml
│   │       ├── database/
│   │       │   ├── 00_schema.sql
│   │       │   ├── 01_roles.sql
│   │       │   ├── 02_users.sql
│   │       │   ├── 03_widget_types.sql
│   │       │   ├── 04_approval_rules.sql
│   │       │   └── 05_service_health.sql
│   │       └── templates/
│   │           ├── app-config.yaml
│   │           ├── infra-secrets.yaml
│   │           ├── migration-job.yaml
│   │           ├── postgres-config.yaml
│   │           ├── postgres-statefulset.yaml
│   │           ├── redis-statefulset.yaml
│   │           └── services.yaml
│   └── ingress-nginx/
│       ├── Chart.yaml
│       ├── cloudbuild.yaml
│       ├── OWNERS
│       ├── README.md
│       ├── README.md.gotmpl
│       ├── values.yaml
│       ├── changelog/
│       ├── ci/
│       ├── templates/
│       └── tests/
=======
```

devops-control-center/
├── backend/
│ ├── python_backend/ # Python FastAPI backend (serves API + HTMX UI)
│ │ ├── app/ # FastAPI application code
│ │ ├── database/ # DB schema, migrations, seeds
│ │ └── requirements.txt
├── infrastructure/ # Docker & Kubernetes manifests
│ ├── docker/
│ │ └── Dockerfile
│ └── k8s/
├── docker-compose.yml # Local dev environment
├── setup.sh # Setup helper (build + migrate + seed)
├── restart.sh # Restart helper script
├── README.md
└── env.example

```│ │   ├── app/
│   │   │   ├── api/             # Routers (auth, dashboards, metrics, integrations, approvals)
│   │   │   ├── db.py            # Postgres helper
│   │   │   ├── redis_client.py  # Redis helper
│   │   │   ├── security.py      # JWT + RBAC helpers
│   │   │   └── main.py          # FastAPI entrypoint
│   │   └── requirements.txt
│   │
│   └── database/
│       ├── migrations/
│       ├── seeds/
│       └── schema.sql
│
├── infrastructure/
│   ├── docker/
│   │   ├── Dockerfile.frontend
│   │   ├── Dockerfile
│   │   └── nginx.conf
│   │
│   ├── k8s/                      # Kubernetes manifests
│   │   ├── base/
│   │   │   ├── deployments/
│   │   │   ├── services/
│   │   │   ├── configmaps/
│   │   │   └── secrets/
│   │   └── overlays/
│   │       ├── dev/
│   │       └── production/
│   │
│   └── helm/                     # Helm charts (alternative to k8s/)
│       └── devops-control-center/
│           ├── Chart.yaml
│           ├── values.yaml
│           └── templates/
│
>>>>>>> 035ffb8 (Remove old frontend code and replace it with a starter htmx template)
├── docs/
│   ├── API_REFERENCE.md
<<<<<<< HEAD
│   ├── ARCHITECTURE.md
│   ├── AZURE_DEVOPS.md
│   ├── DEPLOYMENT.md
│   ├── INTEGRATION_GUIDE.md
│   ├── SELF_SERVICE_TERRAFORM.md
│   ├── SSO_GOOGLE_OAUTH.md
│   └── TROUBLESHOOTING.md
├── frontend/
│   ├── Dockerfile
│   ├── next-env.d.ts
│   ├── next.config.js
│   ├── package.json
│   ├── postcss.config.js
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   ├── public/
│   │   └── robots.txt
│   └── src/
│       ├── app/
│       │   ├── globals.css
│       │   ├── layout.tsx
│       │   ├── page.tsx
│       │   ├── approvals/
│       │   ├── automation/
│       │   ├── dashboard/
│       │   ├── how-to/
│       │   ├── login/
│       │   ├── observability/
│       │   ├── requests/
│       │   ├── self-service/
│       │   └── support/
│       ├── components/
│       │   ├── chat/
│       │   ├── common/
│       │   ├── dashboard/
│       │   ├── layout/
│       │   ├── providers/
│       │   ├── self-service/
│       │   └── widgets/
│       ├── contexts/
│       │   └── ThemeContext.tsx
│       ├── hooks/
│       │   └── useAzureDevOpsConnection.ts
│       └── lib/
│           ├── api-client.ts
│           ├── auth.ts
│           ├── utils.ts
│           └── widget-library.ts
└── .github/
    └── workflows/
        ├── backend-workflow.yml
        ├── frontend-workflow.yml
        └── template-workflow.yml
```

=======
│ └── INTEGRATION_GUIDE.md
│
├── docker-compose.yml
├── .env.example
├── package.json
└── README.md

````
>>>>>>> 035ffb8 (Remove old frontend code and replace it with a starter htmx template)

---

## 🐳 Docker Compose (Local Development)

The `docker-compose.yml` orchestrates all services:

- Python backend API (`api-gateway` service, port 8000)
- PostgreSQL (port 5432)
- Redis (port 6379)

All services run in `devops-network` bridge network.

Volumes:

- `postgres-data` - Persists database
- `redis-data` - Persists cache

---

## ☸️ Kubernetes Deployment

### Structure

The project is Kubernetes-ready with:

- **Kustomize overlays** for dev/production environments
- **Helm chart** as alternative approach
- **ConfigMaps** for configuration
- **Secrets** for credentials (integrate with Vault)
- **Ingress** for internal routing
- **ServiceMonitor** for Prometheus

### Deployment Steps (Future)

```bash
# Using Kustomize
kubectl apply -k infrastructure/k8s/overlays/production

# Using Helm
helm install devops-control-center infrastructure/helm/devops-control-center \
  -f infrastructure/helm/devops-control-center/values-production.yaml
```

### Service Mesh (Optional)

Platform is ready for Istio/Linkerd integration:

- Services use standard Kubernetes service discovery
- Health checks at `/health` and `/ready`
- Metrics at `/metrics` (Prometheus format)

---

## 📈 Observability

### Metrics (Internal Pages)

Accessible to Platform Admin, Head of Section, Team Lead:

- **Service Health** - Status of all backend services
- **Sync Status** - Last successful sync timestamp per external system
- **Error Rates** - 4xx/5xx counts per service
- **Response Times** - P50/P95/P99 latencies
- **Cache Hit Ratio** - Redis performance

### Usage Tracking

Automatically tracked:

- Widget view counts per user
- Self-service action counts
- Most-used features
- Login frequency per role

Metrics stored in `usage_metrics` table and exposed via `/api/metrics/usage` endpoint.

### Application Logs

- Structured JSON logging
- Log levels: DEBUG, INFO, WARN, ERROR
- Correlation IDs for request tracing
- All logs include: timestamp, service, user_id, action

---

## 🎨 UI/UX Design System

### Component Library Choice: **Tailwind CSS + shadcn/ui**

**Rationale:**

- Tailwind: Maximum flexibility, easy theme customization
- shadcn/ui: Copy-paste components, full control, no bloat
- Executive-friendly clean aesthetic
- Easy to adjust colors later from brand guidelines

### Theme Structure

```typescript
// theme.config.ts
export const theme = {
	colors: {
		primary: "#0066CC", // Adjustable
		secondary: "#6B7280",
		success: "#10B981",
		warning: "#F59E0B",
		error: "#EF4444",
		// ... more colors
	},
	spacing: {
		/* grid system */
	},
	typography: {
		/* font scales */
	},
};
```

### Design Principles

- **Clean & Uncluttered** - White space is intentional
- **Role-appropriate density** - Executives see summaries, engineers see details
- **Smooth transitions** - 200ms for interactions, 300ms for page changes
- **Skeleton loaders** - Never show blank screens
- **Responsive** - Works on laptop screens (primary) and tablets

---

## 🔒 Security Considerations

### Authentication Flow

1. User accesses portal → Redirected to SSO
2. SSO validates → Returns JWT with username + domain groups
3. API Gateway validates JWT
4. Auth service maps domain groups → role
5. Frontend receives session token + role

### Authorization

- Every API request includes session token
- API Gateway validates token + checks RBAC
- Backend services trust API Gateway (internal network)
- No service-to-service authentication required (same network)

### Data Protection

- All passwords hashed with bcrypt (if storing local accounts)
- Sensitive data (tokens, secrets) stored in Github Secrets (not in DB)
- Audit logs are append-only (no deletions)
- Session tokens expire after 8 hours


---

## 📚 Additional Documentation

- [Architecture Deep Dive](docs/ARCHITECTURE.md)
- [API Reference](docs/API_REFERENCE.md)
- [Integration Guide](docs/INTEGRATION_GUIDE.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Google SSO (OAuth / OIDC)](docs/SSO_GOOGLE_OAUTH.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## 🤝 Contributing

1. Create feature branch from `dev`
2. Follow code style (Prettier + ESLint)
3. Add tests for new features
4. Update documentation
5. Submit pull request

---

## 📞 Support

Internal team: devops-platform@internal
Slack: #devops-control-center

---

**This platform is designed for years of maintenance and evolution. Every decision prioritizes clean architecture over quick hacks.**
````
