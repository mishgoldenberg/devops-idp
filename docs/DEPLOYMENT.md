# Deployment Guide

## Local Development Deployment

### Prerequisites

- Docker Desktop installed
- Docker Compose installed
- Node.js 20+ (for local development without Docker)
- 8GB RAM minimum

### Quick Start

```bash
# 1. Clone repository
git clone <repo-url>
cd devops-control-center

# 2. Create environment file
cp .env.example .env
# Edit .env with your configuration

# 3. Start all services with Docker Compose
docker-compose up -d

# 4. Wait for services to be healthy
docker-compose ps

# 5. Run database migrations
docker-compose exec api-gateway npm run migrate

# 6. Seed initial data
docker-compose exec api-gateway npm run seed

# 7. Access the application
# Frontend: http://localhost:3000
# API Gateway: http://localhost:8000
# API Docs: http://localhost:8000/api-docs
```

### Test Login

Use these seeded accounts:
- **Platform Admin**: `admin@internal`
- **Team Lead**: `lead@internal`
- **Regular User**: `user@internal`

(No passwords required in dev mode with `DEV_MODE_BYPASS_AUTH=true`)

---

## Kubernetes Deployment

### Prerequisites

- Kubernetes cluster (1.24+)
- kubectl configured
- Internal container registry
- Ingress controller (nginx recommended)

### Step 1: Build Container Images

```bash
# Build backend services
docker build \
  --build-arg SERVICE_PATH=backend/services/api-gateway \
  -f infrastructure/docker/Dockerfile.backend \
  -t your-registry/devops-control-center/api-gateway:v1.0.0 \
  .

# Repeat for other services (auth, azure-devops, sonarqube, etc.)

# Build frontend
docker build \
  --build-arg NEXT_PUBLIC_API_GATEWAY_URL=http://api-gateway:8000 \
  -f infrastructure/docker/Dockerfile.frontend \
  -t your-registry/devops-control-center/frontend:v1.0.0 \
  .

# Push to registry
docker push your-registry/devops-control-center/api-gateway:v1.0.0
docker push your-registry/devops-control-center/frontend:v1.0.0
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
kubectl logs -f deployment/frontend -n devops-control-center
```

### Step 5: Run Database Migrations

```bash
# Get API Gateway pod name
POD=$(kubectl get pods -n devops-control-center -l app=api-gateway -o jsonpath='{.items[0].metadata.name}')

# Run migrations
kubectl exec -it $POD -n devops-control-center -- npm run migrate

# Seed initial data
kubectl exec -it $POD -n devops-control-center -- npm run seed
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
# View pod logs
kubectl logs <pod-name> -n devops-control-center --previous

# Describe pod
kubectl describe pod <pod-name> -n devops-control-center
```

### Database Connection Issues

```bash
# Test database connectivity
kubectl run -it --rm debug --image=postgres:16-alpine --restart=Never -- \
  psql -h postgres -U devops_user -d devops_control_center
```

### Service Communication Issues

```bash
# Test service connectivity
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://api-gateway:8000/api/health
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

