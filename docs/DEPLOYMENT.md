# Deployment Guide

## Local Development Deployment

### Prerequisites

- Docker Desktop installed
- Docker Compose installed
- 8GB RAM minimum
- PostgreSQL client tools (optional, for direct DB access)

### Quick Start with Docker Compose

```bash
# 1. Clone repository
git clone <repo-url>
cd devops-control-center

# 2. Create environment file
cp .env.example .env
# Edit .env with your configuration (Azure DevOps PAT, etc.)

# 3. Start all services with Docker Compose
docker compose up -d

# 4. Wait for all services to be healthy (usually 30-60 seconds)
docker compose ps
# Expected: postgres (healthy), redis (healthy),
#           api-gateway (healthy)

# 5. Access the application
# UI: http://localhost:8000/ui/
# Backend API: http://localhost:8000/api/
# Health checks:
#   - Liveness:  http://localhost:8000/api/health/live
#   - Readiness: http://localhost:8000/api/health/ready
```

### Services in Docker Compose

| Service     | Port | Language         | Purpose               |
| ----------- | ---- | ---------------- | --------------------- |
| postgres    | 5432 | SQL              | Primary database      |
| redis       | 6379 | -                | Cache & session store |
| api-gateway | 8000 | Python (FastAPI) | Backend API + HTMX UI |

### Database Initialization

Database migrations and seeds are automatically applied when postgres starts. The `init/` and `migrations/` scripts execute on container startup:

```bash
# View migration logs
docker compose logs postgres
docker compose logs api-gateway

# Manually verify database (if needed)
docker compose exec postgres psql -U devops_user -d devops_control_center -c "\dt"
```

### Authentication (OIDC SSO)

The app uses admin-configured OIDC for sign-in. `HUB_ADMIN_USERNAME` and
`HUB_ADMIN_PASSWORD` create the first Platform Admin when no admin exists; that
admin configures RedHat SSO/OIDC from Platform Managing.

Use these seeded accounts:

- **Platform Admin**: `admin@internal`
- **Team Lead**: `lead@internal`
- **Regular User**: `user@internal`

(No passwords required in dev mode with `DEV_MODE_BYPASS_AUTH=true`)

### Viewing Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f api-gateway

# Stop following logs
# Press Ctrl+C
```

### Stopping Services

```bash
# Stop all containers (preserves data)
docker compose down

# Stop all containers and remove volumes (clean slate)
docker compose down -v
```

---

## Kubernetes Deployment

### Prerequisites

- Kubernetes cluster (1.24+)
- kubectl configured
- Internal container registry
- Ingress controller (nginx recommended)

### Step 1: Build & Push Container Images

```bash
# Build Python backend (FastAPI + Uvicorn)
docker build \
  -f infrastructure/docker/Dockerfile.python-backend \
  -t your-registry/devops-control-center/api-gateway:v1.0.0 \
  .

# Push to your container registry
docker push your-registry/devops-control-center/api-gateway:v1.0.0

# Update image references in K8s manifests before deployment
```

### Step 2: Create Secrets

**Option A: Using kubectl**

```bash
# Database secret
kubectl create secret generic database-secret \
  --from-literal=connection-string='postgresql://user:password@host:5432/db' \
  -n devops-control-center

# Redis secret
kubectl create secret generic redis-secret \
  --from-literal=password='your-redis-password' \
  -n devops-control-center

# Auth secrets
kubectl create secret generic auth-secret \
  --from-literal=jwt-secret='your-jwt-secret-min-64-chars' \
  --from-literal=session-secret='your-session-secret-min-32-chars' \
  -n devops-control-center

# External systems secrets
kubectl create secret generic external-systems-secret \
  --from-literal=azure-devops-pat='your-pat' \
  --from-literal=sonarqube-token='your-token' \
  --from-literal=artifactory-api-key='your-key' \
  --from-literal=servicenow-client-id='your-client-id' \
  --from-literal=servicenow-client-secret='your-client-secret' \
  --from-literal=ai-chatbot-token='your-token' \
  -n devops-control-center
```

**Option B: Using Vault (Recommended)**

Integrate with HashiCorp Vault or your organization's secret management:

```bash
# Install Vault CSI driver
helm repo add hashicorp https://helm.releases.hashicorp.com
helm install vault hashicorp/vault \
  --set "injector.enabled=true" \
  -n vault-system

# Configure Vault integration
# See: https://www.vaultproject.io/docs/platform/k8s
```

### Step 3: Deploy with Kustomize

**Development Environment:**

```bash
kubectl apply -k infrastructure/k8s/overlays/dev
```

**Production Environment:**

```bash
# Update image tags in production/kustomization.yaml first
kubectl apply -k infrastructure/k8s/overlays/production
```

### Step 4: Verify Deployment

```bash
# Check pod status
kubectl get pods -n devops-control-center

# Check services
kubectl get svc -n devops-control-center

# Check ingress
kubectl get ingress -n devops-control-center

# View logs
kubectl logs -f deployment/api-gateway -n devops-control-center
kubectl logs -f deployment/api-gateway -n devops-control-center
```

### Step 5: Verify Health Checks & Database

```bash
# Wait for all pods to be Running and Ready
kubectl get pods -n devops-control-center -w

# Once api-gateway is ready, verify health endpoints
# This confirms the pod is healthy and Kubernetes probes are working
kubectl exec -it deployment/api-gateway -n devops-control-center -- \
  curl http://localhost:8000/api/health/live
# Should see: {"status": "alive", "timestamp": "2024-01-XX..."}

# Check readiness endpoint (used for traffic routing)
kubectl exec -it deployment/api-gateway -n devops-control-center -- \
  curl http://localhost:8000/api/health/ready
# Should see: {"status": "ready", "timestamp": "2024-01-XX..."}

# Monitor initialization logs (database migrations, data seeds)
kubectl logs -f deployment/api-gateway -n devops-control-center | grep -E "migrat|seed|health|ready"

# All database operations run automatically inside the api-gateway container
# No separate migration pods needed
```

### Step 6: Access Application

```bash
# Get ingress URL
kubectl get ingress -n devops-control-center

# Access via configured domain
# Example: https://devops.internal.company
```

---

## Scaling

### Horizontal Pod Autoscaling

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-gateway-hpa
  namespace: devops-control-center
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-gateway
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
```

Apply:

```bash
kubectl apply -f hpa.yaml
```

---

## Monitoring & Observability

### Prometheus Integration

```bash
# Install Prometheus Operator
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace

# Services expose /metrics endpoint for Prometheus scraping
```

### Logs Aggregation

```bash
# Install Loki for logs
helm repo add grafana https://grafana.github.io/helm-charts
helm install loki grafana/loki-stack -n monitoring
```

---

## Backup & Disaster Recovery

### Database Backups

```bash
# Backup PostgreSQL
kubectl exec -it postgres-0 -n devops-control-center -- \
  pg_dump -U devops_user devops_control_center > backup-$(date +%Y%m%d).sql

# Restore
kubectl exec -i postgres-0 -n devops-control-center -- \
  psql -U devops_user devops_control_center < backup-20240115.sql
```

### Configuration Backups

```bash
# Backup all Kubernetes resources
kubectl get all,configmap,secret,ingress -n devops-control-center -o yaml > k8s-backup.yaml
```

---

## Troubleshooting

### Pod Crashes

```bash
# View recent pod logs to see startup/runtime errors
kubectl logs <pod-name> -n devops-control-center

# View previous pod logs if the container restarted
kubectl logs <pod-name> -n devops-control-center --previous

# Get detailed pod information including events and resource usage
kubectl describe pod <pod-name> -n devops-control-center

# Common Python/FastAPI issues:
# - ModuleNotFoundError: Missing dependency (check requirements.txt installed)
# - psycopg2 ImportError: PostgreSQL client library issue (use psycopg2-binary)
# - Database connection timeout: Verify postgres pod is running and healthy
# - OOMKilled: Pod exceeded memory limits (increase K8s resource limits)
# - CrashLoopBackOff: Check logs for exceptions during startup

# Example: Check logs for specific error patterns
kubectl logs deployment/api-gateway -n devops-control-center | grep -i "error\|exception\|traceback"
```

### Database Connection Issues

```bash
# Test database connectivity
kubectl run -it --rm debug --image=postgres:16-alpine --restart=Never -- \
  psql -h postgres -U devops_user -d devops_control_center
```

### Service Communication Issues

```bash
# Test API gateway liveness probe (Kubernetes restart trigger)
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://api-gateway:8000/api/health/live

# Test API gateway readiness probe (Kubernetes traffic routing)
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://api-gateway:8000/api/health/ready

# Test full API connectivity
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://api-gateway:8000/api/auth/info

# Debug from inside api-gateway container
kubectl exec -it deployment/api-gateway -n devops-control-center -- sh
```

---

## Updates & Rollbacks

### Rolling Update

```bash
# Update image
kubectl set image deployment/api-gateway \
  api-gateway=your-registry/api-gateway:v1.1.0 \
  -n devops-control-center

# Check rollout status
kubectl rollout status deployment/api-gateway -n devops-control-center
```

### Rollback

```bash
# View rollout history
kubectl rollout history deployment/api-gateway -n devops-control-center

# Rollback to previous version
kubectl rollout undo deployment/api-gateway -n devops-control-center

# Rollback to specific revision
kubectl rollout undo deployment/api-gateway --to-revision=2 -n devops-control-center
```

---

## Security Considerations

1. **Network Policies**: Implement network policies to restrict pod-to-pod communication
2. **RBAC**: Configure proper Kubernetes RBAC for service accounts
3. **Pod Security**: Use Pod Security Standards (PSS) to enforce security policies
4. **Image Scanning**: Scan container images for vulnerabilities before deployment
5. **Secrets Rotation**: Regularly rotate all secrets and credentials
6. **TLS**: Enable TLS for all external communication

---

## Terraform Self-Service Prerequisites

The self-service project creation feature requires several one-time cluster and GCP resources. Apply these **before** deploying the application to a new cluster.

### 1. Kubernetes ServiceAccount & Workload Identity

```bash
# Create the K8s SA in the correct namespace
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: devops-terraform-sa
  namespace: devops-control-center
  annotations:
    iam.gke.io/gcp-service-account: devops-terraform-sa@devops-idp-489012.iam.gserviceaccount.com
EOF

# Bind the K8s SA to the GCP SA via Workload Identity
gcloud iam service-accounts add-iam-policy-binding \
  devops-terraform-sa@devops-idp-489012.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:devops-idp-489012.svc.id.goog[devops-control-center/devops-terraform-sa]"
```

### 2. GCS Bucket Access

```bash
# Grant the Terraform SA write access to the tfstate bucket
gcloud storage buckets add-iam-policy-binding \
  gs://devops-control-center-tfstate \
  --member="serviceAccount:devops-terraform-sa@devops-idp-489012.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### 3. Backend Pod RBAC (Job/ConfigMap/Pod management)

The API pod needs permission to create Kubernetes Jobs and ConfigMaps (for the Terraform workspace) and to read pod logs (for status polling and GCS log upload).

```bash
kubectl apply -f - <<'EOF'
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
    resources: ["configmaps"]
    verbs: ["create", "get", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: terraform-job-manager
  namespace: devops-control-center
subjects:
  - kind: ServiceAccount
    name: api-gateway
    namespace: devops-control-center
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: terraform-job-manager
EOF
```

### 4. Verify

```bash
# K8s SA exists and has WI annotation
kubectl get sa devops-terraform-sa -n devops-control-center -o yaml

# Backend RBAC is in place
kubectl auth can-i create jobs \
  --as=system:serviceaccount:devops-control-center:api-gateway \
  -n devops-control-center
# Expected: yes

# GCS bucket accessible via WI (run from a test pod using the devops-terraform-sa SA)
kubectl run -it --rm tf-test \
  --image=gcr.io/google.com/cloudsdktool/cloud-sdk:alpine \
  --serviceaccount=devops-terraform-sa \
  --restart=Never \
  -n devops-control-center \
  -- gsutil ls gs://devops-control-center-tfstate
```

---

## Production Checklist

- [ ] All secrets created and stored securely
- [ ] Database migrations completed
- [ ] Ingress configured with valid TLS certificates
- [ ] Monitoring and alerting configured
- [ ] Backup strategy implemented
- [ ] Disaster recovery plan documented
- [ ] Resource limits and requests properly set
- [ ] Horizontal Pod Autoscaling configured
- [ ] Health checks configured and tested
- [ ] Logging aggregation configured
- [ ] Network policies applied
- [ ] RBAC configured
- [ ] External system integrations tested
- [ ] Performance testing completed
- [ ] Security scan passed
- [ ] `devops-terraform-sa` K8s ServiceAccount created with Workload Identity annotation
- [ ] GCP Workload Identity binding configured for `devops-terraform-sa`
- [ ] `devops-control-center-tfstate` GCS bucket exists and SA has `objectAdmin`
- [ ] `terraform-job-manager` Role and RoleBinding applied
- [ ] `AZURE_DEVOPS_PAT` present in `all-secrets` K8s Secret (required by Terraform runner)
- [ ] Self-service project creation tested end-to-end (mock mode off)

=======
