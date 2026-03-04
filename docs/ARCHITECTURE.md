# Architecture Deep Dive

## Overview

DevOps Control Center is built using a **microservice architecture** with clear separation between frontend and backend, designed for scalability, maintainability, and future Kubernetes deployment.

---

## Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│                      Presentation Layer                      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │           Next.js Frontend (React + TypeScript)         │ │
│  │  • App Router for routing                               │ │
│  │  • Tailwind CSS + shadcn/ui for UI                      │ │
│  │  • React Grid Layout for drag-and-drop                  │ │
│  │  • Client-side RBAC checks                              │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                             │
                          REST API
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                     API Gateway Layer                        │
│  ┌────────────────────────────────────────────────────────┐ │
│  │         API Gateway (Python 3.11 + FastAPI + Uvicorn)   │ │
│  │  • Authentication middleware (JWT)                      │ │
│  │  • Authorization middleware (RBAC)                      │ │
│  │  • Rate limiting                                        │ │
│  │  • Request routing & aggregation                        │ │
│  │  • Audit logging                                        │ │
│  │  • Session management (Redis)                           │ │
│  │  • Health check endpoints (/api/health/live, ready)    │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
              ▼                             ▼
┌─────────────────────────┐    ┌─────────────────────────┐
│    Business Logic Layer │    │    Authentication Layer │
│  ┌───────────────────┐  │    │  ┌───────────────────┐  │
│  │  Adapter Services │  │    │  │  Auth Service     │  │
│  │  • Azure DevOps   │  │    │  │  • SSO Integration│  │
│  │  • SonarQube      │  │    │  │  • Role Mapping   │  │
│  │  • Artifactory    │  │    │  │  • JWT Generation │  │
│  │  • ServiceNow     │  │    │  └───────────────────┘  │
│  │  • AI Chatbot     │  │    └─────────────────────────┘
│  └───────────────────┘  │
│  ┌───────────────────┐  │
│  │ Approval Service  │  │
│  │  • Workflow       │  │
│  │  • Execution      │  │
│  │  • Audit Log      │  │
│  └───────────────────┘  │
└─────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│                        Data Layer                            │
│  ┌──────────────────┐              ┌──────────────────┐     │
│  │   PostgreSQL     │              │      Redis       │     │
│  │  • Users         │              │  • Sessions      │     │
│  │  • Dashboards    │              │  • Cache         │     │
│  │  • Approvals     │              │  • Rate Limits   │     │
│  │  • Audit Logs    │              └──────────────────┘     │
│  │  • Metrics       │                                        │
│  └──────────────────┘                                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Design Patterns

### 1. Adapter Pattern (External Systems)

Each external system integration is accessible via the FastAPI API Gateway:

```
backend/python_backend/app/
├── api/
│   ├── __init__.py
│   ├── azure_devops.py      # Azure DevOps endpoints
│   ├── auth.py              # Authentication endpoints
│   ├── approvals.py         # Approval workflow endpoints
│   ├── artifactory.py       # Artifactory integration
│   ├── dashboards.py        # Dashboard endpoints
│   ├── health.py            # Health check endpoints
│   ├── metrics.py           # Metrics/observability
│   ├── servicenow.py        # ServiceNow integration
│   ├── sonarqube.py         # SonarQube integration
│   └── ai_chatbot.py        # AI Chatbot integration
├── db.py                    # Database connection pool
├── redis_client.py          # Redis cache management
├── security.py              # RBAC authorization
├── config.py                # Environment configuration
└── main.py                  # FastAPI application factory
```

**benefits:**
- Clean API route organization by domain
- Environment-based URL and authentication
- Mock implementations for testing
- Async request handling for better concurrency

### 4. Middleware Chain (FastAPI)

Request processing pipeline:
```python
# In main.py
app.add_middleware(TrustedHostMiddleware)    # Security headers
app.add_middleware(CORSMiddleware)           # CORS handling
app.add_middleware(RequestIdMiddleware)      # Correlation ID
app.add_middleware(RateLimitMiddleware)      # Rate limiting
# Also applied via decorators:
@app.get("/api/...")
@require_auth                                # JWT validation
@require_permission(["read:projects"])      # RBAC check
@audit_log                                   # Audit logging
def endpoint(...)...
```

### 5. Layered Architecture (Frontend)

```
frontend/src/
├── app/                    # Next.js App Router (routing layer)
├── components/
│   ├── widgets/           # Business logic components
│   ├── layout/            # Layout components
│   └── common/            # Reusable UI components
├── lib/
│   ├── api-client.ts      # Data access layer
│   ├── auth.ts            # Authentication utilities
│   └── utils.ts           # Helper functions
```

---

## Data Flow

### Authentication Flow

```
1. User → Frontend: Enter username
2. Frontend → API Gateway: POST /api/auth/login
3. API Gateway → Auth Service: Forward request
4. Auth Service → PostgreSQL: Lookup user + role
5. Auth Service → API Gateway: Return JWT + user data
6. API Gateway → Frontend: Return JWT + user data
7. Frontend: Store JWT in localStorage
8. Frontend: Include JWT in all subsequent requests
```

### Widget Data Flow

```
1. Dashboard loads → Frontend requests dashboard layout
2. API Gateway: Fetch dashboard from PostgreSQL
3. Frontend: Render widgets based on layout
4. Each widget: Fetches own data from API Gateway
5. API Gateway: Route to appropriate adapter service
6. Adapter Service: Return data (mock or real)
7. Widget: Display data
```

### Approval Flow

```
1. User: Request self-service action
2. Frontend → API Gateway: POST /api/approvals/requests
3. API Gateway → Approval Service: Create request
4. Approval Service → PostgreSQL: Store request (PENDING)
5. Approval Service → PostgreSQL: Log audit trail
6. Notification: Alert approvers
7. Approver: Review request
8. Approver: Approve/Reject
9. Approval Service: Execute if approved
10. Approval Service → Adapter Service: Perform action
11. Approval Service → PostgreSQL: Update status (EXECUTED)
```

---

## Security Architecture

### Authentication & Authorization

**Multi-layer security:**

1. **Network Layer**: Internal network only (no internet)
2. **API Gateway Layer**: JWT validation on every request
3. **Application Layer**: RBAC checks before data access
4. **Data Layer**: Row-level security in queries

### Session Management

- JWT tokens for stateless authentication
- Redis-backed sessions for stateful data
- 8-hour token expiry (configurable)
- Automatic refresh on activity

### Audit Logging

All sensitive operations are logged:
```sql
INSERT INTO audit_logs (
    user_id,
    action,
    resource_type,
    resource_id,
    details,
    ip_address,
    user_agent
) VALUES (...)
```

Audit logs are:
- Append-only (no deletions)
- Immutable
- Timestamped
- Include full context

---

## Scalability Considerations

### Horizontal Scaling

All services are stateless and can scale horizontally:

```yaml
# Kubernetes example
replicas: 3  # Run 3 instances of API Gateway
```

**Load balancing:**
- Kubernetes Service provides round-robin load balancing
- Ingress controller distributes external traffic
- No sticky sessions required (JWT-based auth)

### Caching Strategy

**Three-tier caching:**

1. **Browser Cache**: Static assets (CSS, JS, images)
2. **Redis Cache**: API responses (5-minute TTL)
3. **Database Query Cache**: PostgreSQL query results

**Cache invalidation:**
- Time-based expiry (TTL)
- Event-based invalidation on data changes
- Cache keys include user ID for user-specific data

### Database Optimization

**Indexing strategy:**
```sql
-- Frequently queried fields
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_dashboards_user_id ON dashboards(user_id);
CREATE INDEX idx_approval_requests_status ON approval_requests(status);

-- Composite indexes for common queries
CREATE INDEX idx_audit_logs_user_created ON audit_logs(user_id, created_at DESC);
```

**Connection pooling:**
```typescript
const pool = new Pool({
  max: 20,                    // Max connections
  idleTimeoutMillis: 30000,   // Close idle connections
  connectionTimeoutMillis: 2000
});
```

---

## Observability Architecture

### Structured Logging

All logs follow JSON format:
```json
{
  "timestamp": "2024-01-15T10:00:00.000Z",
  "level": "info",
  "service": "api-gateway",
  "message": "User logged in",
  "userId": "uuid",
  "requestId": "correlation-id"
}
```

### Health Checks

**Three types:**

1. **Liveness** (`/health/live`): Is service running?
2. **Readiness** (`/health/ready`): Is service ready for traffic?
3. **Health** (`/health`): Detailed health including dependencies

### Metrics

Services expose Prometheus metrics:
- Request count
- Response times (P50, P95, P99)
- Error rates
- Active connections
- Cache hit ratio

### Distributed Tracing

Request correlation:
```
Request ID: uuid-123
  ├─ API Gateway (10ms)
  ├─ Auth Service (5ms)
  └─ Azure DevOps Service (50ms)
Total: 65ms
```

---

## Deployment Architecture

### Local Development

```
Docker Compose
├─ frontend (port 3000)
├─ api-gateway (port 8000)
├─ auth-service (port 8001)
├─ azure-devops-service (port 8002)
├─ sonarqube-service (port 8003)
├─ artifactory-service (port 8004)
├─ servicenow-service (port 8005)
├─ ai-chatbot-service (port 8006)
├─ approval-service (port 8007)
├─ postgres (port 5432)
└─ redis (port 6379)
```

### Kubernetes Production

```
Namespace: devops-control-center
├─ Deployments
│  ├─ frontend (2 replicas)
│  ├─ api-gateway (3 replicas)
│  └─ backend services (1-2 replicas each)
├─ Services (ClusterIP)
├─ ConfigMaps (application config)
├─ Secrets (credentials)
├─ Ingress (internal routing)
└─ HorizontalPodAutoscaler
```

---

## Technology Decisions

### Why Python (FastAPI) for Backend?

- **FastAPI framework**: Modern async web framework with automatic OpenAPI docs
- **Async/await**: Native async I/O for handling concurrent requests efficiently
- **Type hints**: Built-in Python type annotations with Pydantic for validation
- **Performance**: Comparable to Node.js/Express for I/O-bound operations
- **Rich ecosystem**: Extensive libraries for DevOps integrations (Azure SDK, requests, etc.)
- **Data science ready**: Natural evolution path if analytics/ML features needed
- **Production ready**: Used by major tech companies, well-documented

### Why Next.js for Frontend?

- **React framework**: Industry standard
- **App Router**: Modern routing with layouts
- **TypeScript**: Type safety out of the box
- **SSR capability**: Future performance optimization
- **Developer experience**: Excellent tooling

### Why PostgreSQL?

- **ACID compliance**: Critical for audit logs and approvals
- **JSON support**: Flexible for widget configurations
- **Mature**: Battle-tested, stable, well-documented
- **Performance**: Excellent for read-heavy workloads
- **Extensions**: uuid-ossp, full-text search, etc.

### Why Redis?

- **Fast**: In-memory data store
- **Simple**: Key-value model perfect for sessions and cache
- **Persistence**: Can be configured for durability
- **Pub/Sub**: Future real-time notifications

### Why Tailwind CSS?

- **Utility-first**: Rapid development
- **Customization**: Easy to adjust colors/spacing
- **Tree-shaking**: Small bundle size
- **No CSS conflicts**: Scoped by design
- **Executive-friendly**: Clean, modern aesthetics

---

## Future Enhancements

### Phase 2 Features

1. **Real-time notifications** using WebSockets
2. **Advanced analytics** with time-series database
3. **Multi-tenancy** for different organizations
4. **Mobile app** with React Native
5. **GraphQL API** for flexible queries
6. **Service mesh** (Istio) for advanced traffic management

### Performance Optimizations

1. **CDN** for static assets
2. **Database read replicas** for scalability
3. **Redis Cluster** for distributed caching
4. **Edge caching** with Cloudflare/internal CDN

### Security Enhancements

1. **OAuth2/OIDC** for SSO
2. **MFA** for Platform Admins
3. **Vault integration** for secrets management
4. **Network policies** in Kubernetes
5. **Pod Security Admission** enforcement

---

## Maintenance & Operations

### Regular Tasks

- **Daily**: Monitor health checks, review error logs
- **Weekly**: Review usage metrics, check disk space
- **Monthly**: Rotate secrets, update dependencies
- **Quarterly**: Performance testing, capacity planning

### Backup Strategy

- **Database**: Daily automated backups, 30-day retention
- **Configuration**: GitOps approach, version controlled
- **Secrets**: Backed up in Vault

### Disaster Recovery

- **RTO**: 4 hours (Recovery Time Objective)
- **RPO**: 1 hour (Recovery Point Objective)
- **Failover**: Manual process documented in runbooks

---

## Conclusion

This architecture balances:
- **Simplicity**: Easy to understand and maintain
- **Scalability**: Can grow with user base
- **Security**: Multiple layers of protection
- **Maintainability**: Clear structure, good documentation
- **Performance**: Efficient use of resources

The modular design allows teams to work independently on different services, and the clear separation of concerns makes testing and deployment straightforward.

