# Troubleshooting Guide

Common issues and solutions for DevOps Control Center.

---

## Docker Compose Issues

### Services Won't Start

**Problem**: Services fail to start or keep restarting

**Solutions**:

1. Check logs:
```bash
docker-compose logs -f [service-name]
```

2. Verify environment variables:
```bash
docker-compose config
```

3. Check port conflicts:
```bash
netstat -an | grep LISTEN | grep "3000\|8000\|5432\|6379"
```

4. Clean and rebuild:
```bash
docker-compose down -v
docker-compose build --no-cache
docker-compose up -d
```

### Database Connection Failed

**Problem**: Services can't connect to PostgreSQL

**Solutions**:

1. Check if PostgreSQL is running:
```bash
docker-compose ps postgres
```

2. Verify connection string:
```bash
docker-compose exec api-gateway env | grep DATABASE_URL
```

3. Test connection manually:
```bash
docker-compose exec postgres psql -U devops_user -d devops_control_center
```

4. Reset database:
```bash
docker-compose down -v
docker-compose up -d postgres
sleep 10
docker-compose up -d
```

### Redis Connection Issues

**Problem**: Can't connect to Redis

**Solutions**:

1. Check Redis status:
```bash
docker-compose exec redis redis-cli ping
```

2. Verify password:
```bash
docker-compose exec redis redis-cli -a $REDIS_PASSWORD ping
```

---

## Backend Issues

### 401 Unauthorized Errors

**Problem**: API requests return 401

**Solutions**:

1. Check JWT token:
```bash
# Token should be present in localStorage
console.log(localStorage.getItem('auth_token'))
```

2. Verify token expiration:
```bash
# Decode JWT at https://jwt.io or:
node -e "console.log(JSON.parse(Buffer.from('YOUR_TOKEN'.split('.')[1], 'base64')))"
```

3. Re-login:
```bash
# Clear storage and login again
localStorage.clear()
```

### 500 Internal Server Error

**Problem**: API returns 500 errors

**Solutions**:

1. Check backend logs:
```bash
docker-compose logs -f api-gateway
```

2. Common causes:
   - Database connection lost
   - Redis connection lost
   - Missing environment variables
   - Uncaught exceptions

3. Restart service:
```bash
docker-compose restart api-gateway
```

### Service Communication Errors

**Problem**: Gateway can't reach backend services

**Solutions**:

1. Verify network:
```bash
docker network ls
docker network inspect devops-network
```

2. Test service connectivity:
```bash
docker-compose exec api-gateway curl http://auth-service:8001/health
```

3. Check service names in docker-compose.yml match environment variables

---

## Frontend Issues

### Page Won't Load / Blank Screen

**Problem**: Frontend shows blank screen

**Solutions**:

1. Check browser console for errors (F12)

2. Verify API Gateway URL:
```javascript
console.log(process.env.NEXT_PUBLIC_API_GATEWAY_URL)
```

3. Check network tab in DevTools for failed requests

4. Clear cache and reload:
```bash
Ctrl+Shift+R (hard reload)
```

5. Rebuild frontend:
```bash
docker-compose down frontend
docker-compose build frontend --no-cache
docker-compose up -d frontend
```

### Widgets Not Loading

**Problem**: Dashboard widgets show loading state indefinitely

**Solutions**:

1. Check API responses in Network tab

2. Verify authentication token:
```javascript
console.log(localStorage.getItem('auth_token'))
```

3. Check widget adapter configuration:
```bash
docker-compose logs -f azure-devops-service
```

4. Verify mock data flags:
```bash
echo $USE_MOCK_AZURE_DEVOPS
```

### Drag-and-Drop Not Working

**Problem**: Can't rearrange widgets

**Solutions**:

1. Check if `react-grid-layout` CSS is loaded:
```javascript
// In browser console
document.querySelector('.react-grid-layout')
```

2. Verify no JavaScript errors in console

3. Try refreshing the page

---

## Database Issues

### Migration Failed

**Problem**: Database migrations don't run successfully

**Solutions**:

1. Check migration logs:
```bash
docker-compose exec api-gateway npm run migrate
```

2. Manually run schema:
```bash
docker-compose exec postgres psql -U devops_user -d devops_control_center < backend/database/schema.sql
```

3. Reset database completely:
```bash
docker-compose down -v
docker-compose up -d postgres
sleep 10
docker-compose exec api-gateway npm run migrate
docker-compose exec api-gateway npm run seed
```

### Data Not Persisting

**Problem**: Data disappears after restart

**Solutions**:

1. Check if volume is mounted:
```bash
docker volume ls | grep devops
docker volume inspect devops-postgres-data
```

2. Verify data directory:
```bash
docker-compose exec postgres ls -la /var/lib/postgresql/data
```

3. Don't use `-v` flag when stopping:
```bash
docker-compose down  # Good
docker-compose down -v  # BAD - deletes volumes!
```

---

## Authentication Issues

### Can't Login

**Problem**: Login fails with invalid credentials

**Solutions**:

1. Verify user exists:
```bash
docker-compose exec postgres psql -U devops_user -d devops_control_center -c "SELECT username, email, role_id FROM users;"
```

2. Check if seeding ran:
```bash
docker-compose exec api-gateway npm run seed
```

3. Try default users:
   - admin@internal
   - lead@internal
   - user@internal

### Session Expires Too Quickly

**Problem**: User logged out frequently

**Solutions**:

1. Check session expiry setting:
```bash
echo $SESSION_EXPIRY_HOURS  # Should be 8
```

2. Verify Redis is working:
```bash
docker-compose exec redis redis-cli -a $REDIS_PASSWORD keys '*session*'
```

---

## Performance Issues

### Slow API Responses

**Problem**: API calls take too long

**Solutions**:

1. Check database query performance:
```sql
-- Enable query logging
ALTER DATABASE devops_control_center SET log_statement = 'all';
```

2. Check Redis cache hit ratio:
```bash
docker-compose exec redis redis-cli -a $REDIS_PASSWORD info stats
```

3. Review backend logs for slow queries

4. Add database indexes if needed

### High Memory Usage

**Problem**: Services consuming too much memory

**Solutions**:

1. Check container stats:
```bash
docker stats
```

2. Adjust memory limits in docker-compose.yml:
```yaml
services:
  api-gateway:
    deploy:
      resources:
        limits:
          memory: 512M
```

3. Restart services:
```bash
docker-compose restart
```

---

## Kubernetes Issues

### Pods CrashLoopBackOff

**Problem**: Pods keep restarting

**Solutions**:

1. Check pod logs:
```bash
kubectl logs <pod-name> -n devops-control-center
kubectl logs <pod-name> -n devops-control-center --previous
```

2. Describe pod:
```bash
kubectl describe pod <pod-name> -n devops-control-center
```

3. Common causes:
   - Missing secrets
   - Wrong image tag
   - Database not ready
   - Configuration errors

### ImagePullBackOff

**Problem**: Can't pull container image

**Solutions**:

1. Verify image exists:
```bash
docker images | grep devops-control-center
```

2. Check image registry:
```bash
kubectl get pods <pod-name> -n devops-control-center -o yaml | grep image:
```

3. Verify image pull secrets:
```bash
kubectl get secrets -n devops-control-center
```

### Service Unreachable

**Problem**: Can't access services through ingress

**Solutions**:

1. Check ingress:
```bash
kubectl get ingress -n devops-control-center
kubectl describe ingress devops-control-center-ingress -n devops-control-center
```

2. Verify services:
```bash
kubectl get svc -n devops-control-center
```

3. Test service directly:
```bash
kubectl port-forward svc/frontend 3000:3000 -n devops-control-center
```

4. Check ingress controller logs:
```bash
kubectl logs -n ingress-nginx <ingress-controller-pod>
```

---

## Development Issues

### Hot Reload Not Working

**Problem**: Changes don't reflect in running application

**Solutions**:

1. For backend:
```bash
# Make sure nodemon is configured
cd backend/services/api-gateway
npm run dev
```

2. For frontend:
```bash
cd frontend
npm run dev
```

3. If using Docker, mount source as volume:
```yaml
volumes:
  - ./backend/services/api-gateway/src:/app/src
```

### TypeScript Errors

**Problem**: Type errors prevent compilation

**Solutions**:

1. Install dependencies:
```bash
npm install
```

2. Check tsconfig.json

3. Clear build cache:
```bash
rm -rf dist/ .next/
npm run build
```

---

## Getting Help

If you can't resolve an issue:

1. Check logs thoroughly
2. Search this documentation
3. Review API reference docs
4. Contact internal DevOps team: #devops-control-center
5. Create detailed bug report with:
   - Steps to reproduce
   - Error messages
   - Logs
   - Environment details

