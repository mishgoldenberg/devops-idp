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

## OpenShift Deployment

Deployment targets an OpenShift cluster and is driven by `azure-pipelines.yml`. The pipeline runs on the `devops-closed-network` agent pool and uses Artifactory as the container registry — no public internet access is required.

### Prerequisites

- `oc` CLI configured with cluster access (or use the pipeline)
- `helm` 3.x
- Access to the Artifactory Docker registry (`$(ARTIFACTORY_DOCKER_REGISTRY)`)
- Azure DevOps variable group `devops-hub-closed-network` populated (see variable list below)

### Step 1: Build & Push Container Images

In CI this happens automatically on push to `dev` or `main`. For a manual build:

```bash
# Backend image — pass closed-network pip overrides as build args
docker build \
  --build-arg PYTHON_BASE_IMAGE="<your-internal-python-image>" \
  --build-arg PIP_INDEX_URL="<your-internal-pypi-url>" \
  --build-arg PIP_TRUSTED_HOST="<your-internal-pypi-host>" \
  -f backend/app/Dockerfile \
  -t "<ARTIFACTORY_DOCKER_REGISTRY>/<ARTIFACTORY_DOCKER_REPOSITORY>/backend:<tag>" \
  .

# Frontend image
docker build \
  --build-arg NGINX_BASE_IMAGE="<your-internal-nginx-image>" \
  -f frontend/Dockerfile \
  -t "<ARTIFACTORY_DOCKER_REGISTRY>/<ARTIFACTORY_DOCKER_REPOSITORY>/frontend:<tag>" \
  .

docker push "<ARTIFACTORY_DOCKER_REGISTRY>/<ARTIFACTORY_DOCKER_REPOSITORY>/backend:<tag>"
docker push "<ARTIFACTORY_DOCKER_REGISTRY>/<ARTIFACTORY_DOCKER_REPOSITORY>/frontend:<tag>"
```

### Step 2: Secrets

All secrets are passed to Helm at deploy time via the pipeline's `devops-hub-closed-network` variable group. The Helm chart renders them into the `all-secrets` Kubernetes Secret in the target namespace. There is no manual secret creation step.

Required variables in the variable group:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `REDIS_PASSWORD` | Redis password |
| `HUB_ADMIN_USERNAME` / `HUB_ADMIN_PASSWORD` | Bootstrap admin account |
| `JWT_SECRET` | HS256 signing key |
| `AZURE_DEVOPS_BASE_URL` / `AZURE_DEVOPS_ADMIN_PAT` | Azure DevOps integration |
| `SNOW_BASE_URL` / `SNOW_API_USERNAME` / `SNOW_API_PASSWORD` | ServiceNow integration |
| `ARTIFACTORY_BASE_URL` | Artifactory integration |
| `SONARQUBE_BASE_URL` | SonarQube integration |
| `CONFLUENCE_BASE_URL` | Confluence integration |
| `ARTIFACTORY_DOCKER_REGISTRY` / `ARTIFACTORY_DOCKER_USERNAME` / `ARTIFACTORY_DOCKER_PASSWORD` | Registry auth |
| `OPENSHIFT_API_URL` / `OPENSHIFT_TOKEN` / `OPENSHIFT_NAMESPACE` | Cluster targeting |
| `HUB_HOSTNAME` | Ingress hostname |
| `POSTGRES_IMAGE` / `REDIS_IMAGE` | Internal infra images |
| `INGRESS_NGINX_IMAGE_REGISTRY` / `INGRESS_NGINX_IMAGE_NAME` / `INGRESS_NGINX_IMAGE_TAG` | Internal nginx image |

### Step 3: Deploy with Helm

The pipeline runs the following. For a manual deploy, replicate it:

```bash
oc login "$OPENSHIFT_API_URL" --token="$OPENSHIFT_TOKEN"
oc project "$OPENSHIFT_NAMESPACE" || oc new-project "$OPENSHIFT_NAMESPACE"

helm dependency build ./deployment

helm upgrade --install devops-hub ./deployment \
  --namespace "$OPENSHIFT_NAMESPACE" \
  --create-namespace \
  --atomic \
  --timeout 15m \
  --set-string global.customImage.repository="$IMAGE_REPO" \
  --set-string backend.image.tag="$IMAGE_TAG" \
  --set-string frontend.image.tag="$IMAGE_TAG" \
  --set-string backend.ingress.host="$HUB_HOSTNAME" \
  --set-string frontend.ingress.host="$HUB_HOSTNAME" \
  --set-string infrastructure.postgres.image="$POSTGRES_IMAGE" \
  --set-string infrastructure.redis.image="$REDIS_IMAGE" \
  --set-string ingress-prod.controller.image.registry="$INGRESS_NGINX_IMAGE_REGISTRY" \
  --set-string ingress-prod.controller.image.image="$INGRESS_NGINX_IMAGE_NAME" \
  --set-string ingress-prod.controller.image.tag="$INGRESS_NGINX_IMAGE_TAG" \
  --set-string ingress-prod.controller.image.digest="" \
  --set-string ingress-prod.controller.scope.namespace="$OPENSHIFT_NAMESPACE"
```

All infra dependencies (Postgres, Redis, ingress-nginx) are vendored in the Helm umbrella chart — `helm dependency build` does not reach the internet.

### Step 4: Verify Deployment

```bash
# Check pod status
oc get pods -n <OPENSHIFT_NAMESPACE>

# Check services
oc get svc -n <OPENSHIFT_NAMESPACE>

# Check ingress / routes
oc get ingress -n <OPENSHIFT_NAMESPACE>

# View logs
oc logs -f deployment/backend -n <OPENSHIFT_NAMESPACE>
```

### Step 5: Verify Health Checks & Database

```bash
# Wait for all pods to be Running and Ready
oc get pods -n <OPENSHIFT_NAMESPACE> -w

# Verify health endpoints from inside the pod
oc exec -it deployment/backend -n <OPENSHIFT_NAMESPACE> -- \
  curl http://localhost:8000/api/health/live
# Should see: {"status": "alive", "timestamp": "..."}

oc exec -it deployment/backend -n <OPENSHIFT_NAMESPACE> -- \
  curl http://localhost:8000/api/health/ready
# Should see: {"status": "ready", "timestamp": "..."}

# Monitor initialization logs (database migrations run automatically on startup)
oc logs -f deployment/backend -n <OPENSHIFT_NAMESPACE> | grep -E "migrat|seed|health|ready"
```

### Step 6: Access Application

```bash
# Get the configured ingress hostname
oc get ingress -n <OPENSHIFT_NAMESPACE>

# Access via the HUB_HOSTNAME configured in the variable group
# Example: https://devops-hub.your-domain.example.com
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

The self-service project creation feature requires the backend pod to create Kubernetes Jobs in the cluster. The required `terraform-job-manager` Role and RoleBinding are deployed automatically by the Helm chart (`deployment/charts/backend/templates/terraform-rbac.yaml`) — no manual RBAC setup is needed.

The Helm chart grants `backend-sa` (the backend pod's service account) permission to:
- Create, get, list, and watch `batch/jobs`
- Create, get, and delete `configmaps`
- Get, list, and watch `pods` and `pods/log`

### Verify

After deploying, confirm the RBAC is in place:

```bash
oc auth can-i create jobs \
  --as=system:serviceaccount:<OPENSHIFT_NAMESPACE>:backend-sa \
  -n <OPENSHIFT_NAMESPACE>
# Expected: yes
```

---

## Production Checklist

- [ ] All secrets present in the `devops-hub-closed-network` variable group
- [ ] Database migrations completed (run automatically on pod startup)
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
- [ ] `terraform-job-manager` Role and RoleBinding deployed by Helm (verify with `oc auth can-i`)
- [ ] `AZURE_DEVOPS_ADMIN_PAT` present in `all-secrets` K8s Secret (required by Terraform runner)
- [ ] Self-service project creation tested end-to-end (mock mode off)
