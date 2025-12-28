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
│                    Frontend (Next.js)                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Dashboard  │  │   Approvals  │  │ Observability│      │
│  │   Widgets    │  │   Self-Svc   │  │   Metrics    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            │ REST API
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              API Gateway / BFF (Node.js)                     │
│  - Authentication & Authorization                            │
│  - Request routing & aggregation                             │
│  - Rate limiting & caching                                   │
└────────┬────────┬────────┬────────┬────────┬────────┬───────┘
         │        │        │        │        │        │
    ┌────▼───┐┌──▼───┐┌───▼───┐┌──▼───┐┌───▼───┐┌──▼───┐
    │  Auth  ││ ADO  ││ Sonar ││ Arti ││  SNow ││  AI  │
    │  Svc   ││ Svc  ││  Svc  ││  Svc ││  Svc  ││ Svc  │
    └────┬───┘└──┬───┘└───┬───┘└──┬───┘└───┬───┘└──┬───┘
         │       │        │       │        │       │
         └───────┴────────┴───────┴────────┴───────┘
                            │
         ┌──────────────────┴──────────────────┐
         │                                      │
    ┌────▼────────┐                    ┌───────▼────┐
    │ PostgreSQL  │                    │   Redis    │
    │  - Users    │                    │  - Cache   │
    │  - Dashbds  │                    │  - Session │
    │  - Apprvls  │                    └────────────┘
    │  - Audit    │
    └─────────────┘
```

### Services Architecture

| Service | Port | Purpose |
|---------|------|---------|
| **frontend** | 3000 | Next.js web application |
| **api-gateway** | 8000 | BFF, auth, routing, aggregation |
| **auth-service** | 8001 | SSO integration, RBAC mapping |
| **azure-devops-service** | 8002 | ADO integration (PBIs, PRs, pipelines) |
| **sonarqube-service** | 8003 | Code quality metrics |
| **artifactory-service** | 8004 | Artifact management |
| **servicenow-service** | 8005 | Ticket integration |
| **ai-chatbot-service** | 8006 | AI assistant integration |
| **approval-service** | 8007 | Self-service approvals & audit |
| **postgres** | 5432 | Primary data store |
| **redis** | 6379 | Cache & session store |

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

| Capability | Platform Admin | Unit Commander | Branch Head | Head of Section | Project Manager | Team Lead | Regular User |
|------------|:--------------:|:--------------:|:-----------:|:---------------:|:---------------:|:---------:|:------------:|
| View own dashboard | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Customize widgets | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create ADO project | ✓ | ✓ | ✓ | ✓ | ✓ | - | - |
| Enable Sonar scanning | ✓ | ✓ | ✓ | ✓ | - | - | - |
| Approve requests | ✓ | ✓ | ✓ | ✓ | Partial | - | - |
| View aggregated metrics | ✓ | ✓ | ✓ | - | - | - | - |
| View observability | ✓ | - | - | ✓ | - | ✓ | - |
| Manage users | ✓ | - | - | - | - | - | - |

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

## 🔄 Self-Service & Approval System

### Approved Self-Services

| Action | Requires Approval | Approver Roles | Implementation |
|--------|:-----------------:|----------------|----------------|
| Create Azure DevOps Project | ✓ | Platform Admin, Branch Head | Approval workflow → ADO service |
| Enable SonarQube PR Scanning | ✓ | Platform Admin, Head of Section | Approval workflow → Sonar service |
| Request AI Model Access | ✓ | Platform Admin | Approval workflow → AI service |

### Redirect-Only Services (No Portal Implementation)

- **ServiceNow Ticket Creation** - Redirects to ServiceNow
- **OpenShift Namespace Creation** - Redirects to OpenShift Console
- **Artifactory Repository Creation** - Redirects to Artifactory

### Approval Workflow

```
User Request → [approval-service] → Store in DB (status: PENDING)
                                   ↓
                        Notify approvers (in-app)
                                   ↓
          Approver reviews → APPROVED / REJECTED
                                   ↓
                  Execute via adapter service (if approved)
                                   ↓
                      Update request status + audit log
                                   ↓
                         Notify requester
```

### Audit Trail

Every approval action logs:
- Requester identity (username + role)
- Request timestamp
- Request details (JSON payload)
- Approver identity
- Approval/rejection timestamp
- Approver comments
- Execution result (success/failure)

---

## 🔌 External System Integration

### Adapter Pattern

Each external system has:
1. **Service Layer** - Dedicated microservice
2. **Mock Adapter** - Returns realistic sample data for local dev
3. **Real Adapter** - Placeholder for production integration
4. **Interface Contract** - TypeScript types shared with frontend

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
export const USE_MOCK = process.env.USE_MOCK_DATA === 'true'; // Set to false for production
```

---

## 🚀 Local Development Setup

### Prerequisites

Before you begin, ensure you have the following installed:

- **Node.js** 20+ ([Download](https://nodejs.org/))
- **Docker Desktop** ([Download](https://www.docker.com/products/docker-desktop))
- **Git** ([Download](https://git-scm.com/))
- **8GB RAM minimum** (recommended: 16GB)

**Verify installations:**
```bash
node --version    # Should be v20.x.x or higher
docker --version  # Should be 20.10+
docker compose version  # Should be 2.0+
git --version
```

### Quick Start (Automated Setup) ⚡

The easiest way to get started is using our automated setup scripts. They handle all the configuration, building, and database setup for you.

**Windows (PowerShell):**
```powershell
# Run the setup script (right-click -> Run with PowerShell, or in terminal)
.\setup.ps1

# After setup, use restart script for code changes:
.\restart.ps1                # Quick restart (backend changes)
.\restart.ps1 -RebuildFrontend  # Frontend changes
```

**Linux/Mac (Bash):**
```bash
# Make scripts executable (first time only)
chmod +x setup.sh restart.sh

# Run the setup script
./setup.sh
```

The setup script will:
1. ✅ Check prerequisites (Docker, Node.js)
2. ✅ Copy `env.example` to `.env` if it doesn't exist
3. ✅ Build all Docker images
4. ✅ Start all services (database, Redis, backend services, frontend)
5. ✅ Wait for services to be healthy
6. ✅ Run database migrations
7. ✅ Seed initial data (roles, users, widgets)
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
- Build application images (frontend, backend services)
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
docker compose logs frontend

# View all logs
docker compose logs -f
```

#### Step 5: Run Database Migrations

Create database tables and schema:

```bash
npm run migrate
```

**Note:** This script uses the `DATABASE_URL` from your `.env` file. Ensure `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` are set correctly.

If you see connection errors:
- Verify PostgreSQL container is running: `docker compose ps postgres`
- Check database credentials in `.env` match `docker-compose.yml`
- Wait a few seconds for PostgreSQL to fully initialize

#### Step 6: Seed Initial Data

Populate the database with initial data (roles, users, widget types, etc.):

```bash
npm run seed
```

This creates:
- 7 role definitions (Platform Admin through Regular User)
- Sample users for each role (login with any `@internal` domain user)
- Widget type definitions
- Approval rules
- Service health entries

#### Step 7: Access the Application

**Frontend:** http://localhost:3000

**API Gateway:** http://localhost:8000

**Health Check:** http://localhost:8000/api/health

**Database:** `localhost:5432` (user: `devops`, password: from `.env`)

### Test Users

After seeding, you can log in with any of these demo users:

| Username | Role | Description |
|----------|------|-------------|
| `admin@internal` | Platform Admin | Full system access, can approve requests, view observability |
| `commander@internal` | Unit Commander | Cross-branch visibility, strategic metrics |
| `branch.head@internal` | Branch Head | Branch-level aggregation |
| `section.head@internal` | Head of Section | Section metrics, observability access |
| `pm@internal` | Project Manager | Project-level visibility |
| `lead@internal` | Team Lead | Team metrics, observability access |
| `user@internal` | Regular User | Personal dashboard, basic self-service |

**Note:** In local development, authentication uses mock SSO. Any username ending in `@internal` will be accepted and mapped to a role based on the seeded users.

### Common Commands

**Quick Restart (After Code Changes):**

The easiest way to restart services after making changes:

```bash
# Windows (PowerShell)
.\restart.ps1                    # Quick restart (backend changes)
.\restart.ps1 -RebuildFrontend   # Rebuild frontend (frontend changes)
.\restart.ps1 -Rebuild           # Rebuild all services (dependency changes)
.\restart.ps1 -Full              # Clean rebuild (full reset)
.\restart.ps1 -Service api-gateway  # Restart specific service

# Linux/Mac (Bash)
./restart.sh                     # Quick restart (backend changes)
./restart.sh --rebuild-frontend  # Rebuild frontend (frontend changes)
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
docker compose logs -f frontend

# Rebuild a specific service
docker compose build frontend
docker compose up -d frontend

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

1. **Frontend Changes:**
   - Edit files in `frontend/src/`
   - **Important:** Frontend runs in production mode, so rebuild after changes:
     ```bash
     # Windows
     .\restart.ps1 -RebuildFrontend
     
     # Linux/Mac
     ./restart.sh --rebuild-frontend
     ```

2. **Backend Changes:**
   - Edit files in `backend/services/{service-name}/src/`
   - Changes are reflected immediately (services run with volume mounts)
   - Quick restart:
     ```bash
     # Windows
     .\restart.ps1
     
     # Linux/Mac
     ./restart.sh
     ```
   - Or restart specific service:
     ```bash
     # Windows
     .\restart.ps1 -Service api-gateway
     
     # Linux/Mac
     ./restart.sh --service api-gateway
     ```

3. **Database Changes:**
   - Edit `backend/database/schema.sql`
   - Rebuild and migrate:
     ```bash
     docker compose down -v
     docker compose up -d postgres redis
     npm run migrate
     npm run seed
     docker compose up -d
     ```

4. **After Dependency Changes (package.json):**
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
netstat -ano | findstr :3000

# Linux/Mac: Find process using port
lsof -i :3000

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

**Frontend Shows Old Changes:**
- Frontend is built in production mode, rebuild after changes:
  ```bash
  # Windows
  .\restart.ps1 -RebuildFrontend
  
  # Linux/Mac
  ./restart.sh --rebuild-frontend
  ```

### Development Workflow

```bash
# Start individual service
cd backend/services/api-gateway
npm run dev

# Start frontend with hot reload
cd frontend
npm run dev

# Run all services in watch mode
npm run dev:all

# View logs
docker-compose logs -f [service-name]

# Reset database
npm run db:reset
```

### Test Users (Seeded)

| Username | Role | Password |
|----------|------|----------|
| admin@internal | Platform Admin | admin123 |
| commander@internal | Unit Commander | test123 |
| branch.head@internal | Branch Head | test123 |
| section.head@internal | Head of Section | test123 |
| pm@internal | Project Manager | test123 |
| lead@internal | Team Lead | test123 |
| user@internal | Regular User | test123 |

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

```
devops-control-center/
├── frontend/                      # Next.js application
│   ├── src/
│   │   ├── app/                  # App Router pages
│   │   │   ├── dashboard/
│   │   │   ├── approvals/
│   │   │   ├── observability/
│   │   │   └── layout.tsx
│   │   ├── components/           # React components
│   │   │   ├── widgets/         # Dashboard widgets
│   │   │   ├── layout/          # Layout components
│   │   │   └── common/          # Shared components
│   │   ├── lib/                 # Utilities
│   │   │   ├── api-client.ts
│   │   │   ├── auth.ts
│   │   │   └── rbac.ts
│   │   ├── styles/              # Global styles
│   │   └── types/               # TypeScript types
│   ├── public/
│   ├── package.json
│   └── next.config.js
│
├── backend/
│   ├── services/
│   │   ├── api-gateway/         # Main API Gateway + BFF
│   │   │   ├── src/
│   │   │   │   ├── routes/
│   │   │   │   ├── middleware/
│   │   │   │   ├── services/
│   │   │   │   └── index.ts
│   │   │   └── package.json
│   │   │
│   │   ├── auth-service/        # SSO + RBAC
│   │   ├── azure-devops-service/
│   │   ├── sonarqube-service/
│   │   ├── artifactory-service/
│   │   ├── servicenow-service/
│   │   ├── ai-chatbot-service/
│   │   └── approval-service/
│   │
│   ├── shared/                   # Shared libraries
│   │   ├── types/
│   │   ├── utils/
│   │   └── rbac/
│   │
│   └── database/
│       ├── migrations/
│       ├── seeds/
│       └── schema.sql
│
├── infrastructure/
│   ├── docker/
│   │   ├── Dockerfile.frontend
│   │   ├── Dockerfile.backend
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
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEPLOYMENT.md
│   ├── API_REFERENCE.md
│   └── INTEGRATION_GUIDE.md
│
├── docker-compose.yml
├── .env.example
├── package.json
└── README.md
```

---

## 🐳 Docker Compose (Local Development)

The `docker-compose.yml` orchestrates all services:

- Frontend (port 3000)
- API Gateway (port 8000)
- 7 Backend services (ports 8001-8007)
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
    primary: '#0066CC',      // Adjustable
    secondary: '#6B7280',
    success: '#10B981',
    warning: '#F59E0B',
    error: '#EF4444',
    // ... more colors
  },
  spacing: { /* grid system */ },
  typography: { /* font scales */ },
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
- Sensitive data (tokens, secrets) stored in Vault (not in DB)
- Audit logs are append-only (no deletions)
- Session tokens expire after 8 hours

---

## 🧪 Testing Strategy (To Be Implemented)

```bash
# Unit tests
npm run test:unit

# Integration tests
npm run test:integration

# E2E tests
npm run test:e2e

# Load tests
npm run test:load
```

---

## 📚 Additional Documentation

- [Architecture Deep Dive](docs/ARCHITECTURE.md)
- [API Reference](docs/API_REFERENCE.md)
- [Integration Guide](docs/INTEGRATION_GUIDE.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## 🤝 Contributing

1. Create feature branch from `main`
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

