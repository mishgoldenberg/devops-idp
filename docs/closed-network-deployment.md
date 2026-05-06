# Closed-Network Deployment Runbook

This runbook starts after `offline-deps/README.md` is complete: Python wheels,
npm package tarballs (`offline-deps/npm/packages`), and Docker images have been transferred into the closed
network and uploaded to internal Artifactory repositories.

## 1. Files Used By The Deployment

Use these project files:

| Purpose | File / folder |
| --- | --- |
| Azure DevOps pipeline YAML | `azure-pipelines.yml` |
| Helm umbrella chart | `deployment/` |
| Backend Dockerfile | `backend/app/Dockerfile` |
| Frontend/Nginx Dockerfile | `frontend/Dockerfile` |
| Offline dependency bundle instructions | `offline-deps/README.md` |
| Environment variable reference | `docs/env.md` |

The pipeline has three stages:

1. `Build` - installs dependencies from internal repositories, builds CSS, and
   builds backend/frontend container images.
2. `Push` - pushes those images into internal Artifactory Docker registry.
3. `Deploy` - logs into OpenShift and deploys the Helm chart.

## 2. Required Internal Repositories

Create or confirm these Artifactory repositories exist:

| Repository | Used for | Example URL / value |
| --- | --- | --- |
| PyPI repository | Backend Python packages | `https://artifactory.company.local/artifactory/api/pypi/devops-pypi/simple` |
| npm repository | Tailwind / DaisyUI frontend packages | `https://artifactory.company.local/artifactory/api/npm/devops-npm/` |
| Docker registry | App images and mirrored base/runtime images | `artifactory.company.local` |
| Docker repository path | Image namespace | `devops-hub` |

Seed the Python, npm, and Docker packages using `offline-deps/README.md`.

Important Docker images to mirror:

```text
python:3.11-slim
nginx:1.29-alpine
node:22-alpine
postgres:16-alpine
redis:7-alpine
registry.k8s.io/ingress-nginx/controller:v1.15.0
```

The first two are app build base images. `postgres`, `redis`, and
`ingress-nginx/controller` are runtime images used by the Helm chart.

## 3. Create The Azure DevOps Variable Group

In Azure DevOps:

1. Open the target project.
2. Go to `Pipelines` -> `Library`.
3. Create a variable group named exactly:

```text
devops-hub-closed-network
```

4. Add the variables below.
5. Mark credentials and tokens as secret.
6. Enable pipeline access for the YAML pipeline.

### Variable Group Values

| Variable | Secret | Description |
| --- | --- | --- |
| `ARTIFACTORY_DOCKER_REGISTRY` | No | Internal Docker registry host, for example `artifactory.company.local`. |
| `ARTIFACTORY_DOCKER_REPOSITORY` | No | Docker repository/path for app images, for example `devops-hub`. |
| `ARTIFACTORY_DOCKER_USERNAME` | Yes | User allowed to push/pull images. |
| `ARTIFACTORY_DOCKER_PASSWORD` | Yes | Password/API key for the Docker registry user. |
| `NPM_REGISTRY_URL` | No | Internal npm registry URL, ending with `/`, for example `https://artifactory.company.local/artifactory/api/npm/devops-npm/`. |
| `PIP_INDEX_URL` | No | Internal PyPI simple index URL. |
| `PIP_TRUSTED_HOST` | No | Hostname only if your internal PyPI uses an internal CA, for example `artifactory.company.local`. |
| `PYTHON_BASE_IMAGE` | No | Mirrored Python image, for example `artifactory.company.local/docker/python:3.11-slim`. |
| `NGINX_BASE_IMAGE` | No | Mirrored Nginx image, for example `artifactory.company.local/docker/nginx:1.29-alpine`. |
| `POSTGRES_IMAGE` | No | Mirrored Postgres image, for example `artifactory.company.local/docker/postgres:16-alpine`. |
| `REDIS_IMAGE` | No | Mirrored Redis image, for example `artifactory.company.local/docker/redis:7-alpine`. |
| `INGRESS_NGINX_IMAGE_REGISTRY` | No | Registry part for mirrored ingress controller, for example `artifactory.company.local`. |
| `INGRESS_NGINX_IMAGE_NAME` | No | Image path, for example `docker/ingress-nginx/controller`. |
| `INGRESS_NGINX_IMAGE_TAG` | No | Ingress controller tag, for example `v1.15.0`. |
| `OPENSHIFT_API_URL` | No | OpenShift API URL, for example `https://api.cluster.company.local:6443`. |
| `OPENSHIFT_TOKEN` | Yes | Token for a service account that can deploy into the namespace. |
| `OPENSHIFT_NAMESPACE` | No | Namespace/project, for example `devops-hub-prod`. |
| `OPENSHIFT_INSECURE_SKIP_TLS_VERIFY` | No | Usually `false`. Use `true` only if your internal CA is not trusted on the agent. |
| `HUB_HOSTNAME` | No | Browser hostname, for example `devops.company.local`. |
| `POSTGRES_PASSWORD` | Yes | Database password used by the in-cluster Postgres. |
| `REDIS_PASSWORD` | Yes | Redis password stored in the shared secret. |
| `HUB_ADMIN_USERNAME` | Yes | Bootstrap admin username/email. |
| `HUB_ADMIN_PASSWORD` | Yes | Bootstrap admin password. |
| `JWT_SECRET` | Yes | Strong random string for session JWT signing. |
| `AZURE_DEVOPS_BASE_URL` | No | Azure DevOps base URL. For multiple collections use the shared base, for example `https://dev.azure.com/` or internal server root. |
| `AZURE_DEVOPS_ADMIN_PAT` | Yes | Admin PAT used only for project discovery/support flows. |
| `SNOW_BASE_URL` | No | ServiceNow instance URL. |
| `SNOW_API_USERNAME` | Yes | ServiceNow service account username. |
| `SNOW_API_PASSWORD` | Yes | ServiceNow service account password. |
| `ARTIFACTORY_BASE_URL` | No | User-facing Artifactory URL used by widgets and Open buttons. |
| `SONARQUBE_BASE_URL` | No | User-facing SonarQube URL used by widgets and Open buttons. |
| `CONFLUENCE_BASE_URL` | No | User-facing Confluence URL used by widgets and Open buttons. |

All system connection URLs are changed in this variable group. Do not hardcode
them in templates, Dockerfiles, or Helm templates.

## 4. Create The Azure DevOps Pipeline

1. Open Azure DevOps project.
2. Go to `Pipelines` -> `New pipeline`.
3. Select the repository that contains this hub.
4. Choose `Existing Azure Pipelines YAML file`.
5. Select:

```text
/azure-pipelines.yml
```

6. Save the pipeline.
7. Confirm the pipeline can access the `devops-hub-closed-network` variable
   group.
8. Run the pipeline from `dev` or `main`.

The pipeline expects a self-hosted closed-network agent pool named:

```text
devops-closed-network
```

If your pool uses a different name, change `pool.name` in
`azure-pipelines.yml`.

The build agent must have:

- Docker CLI and daemon access.
- `python` and `pip`.
- `node` and `npm`.
- `helm`.
- `oc`.
- Network access to internal Artifactory.
- Network access to OpenShift API.

## 5. Prepare OpenShift

### Namespace / Project

Create the project if the pipeline service account cannot create it:

```bash
oc new-project devops-hub-prod
```

Set the same value in `OPENSHIFT_NAMESPACE`.

### Pipeline Service Account

Create a deployer service account:

```bash
oc project devops-hub-prod
oc create serviceaccount azure-pipeline-deployer
oc adm policy add-role-to-user edit -z azure-pipeline-deployer -n devops-hub-prod
```

Get its token and put it in `OPENSHIFT_TOKEN`:

```bash
oc create token azure-pipeline-deployer -n devops-hub-prod
```

If your cluster version does not support `oc create token`, create a token
secret for the service account using your cluster's approved method.

### Image Pull Secret

If OpenShift needs credentials to pull from Artifactory:

```bash
oc create secret docker-registry artifactory-pull-secret \
  --docker-server=artifactory.company.local \
  --docker-username='<user>' \
  --docker-password='<token>' \
  --docker-email='devops@example.local' \
  -n devops-hub-prod

oc secrets link default artifactory-pull-secret --for=pull -n devops-hub-prod
oc secrets link backend-sa artifactory-pull-secret --for=pull -n devops-hub-prod
```

The `backend-sa` service account is created by Helm. If it does not exist yet,
link the secret after the first failed/pending deploy or create the service
account before linking:

```bash
oc create serviceaccount backend-sa -n devops-hub-prod
oc secrets link backend-sa artifactory-pull-secret --for=pull -n devops-hub-prod
```

### Storage

The chart deploys Postgres and Redis with persistent storage. Confirm a default
StorageClass exists:

```bash
oc get storageclass
```

If there is no default StorageClass, add one through cluster administration or
extend the chart values before deployment.

### Security Context Constraints

The app containers are designed to run as non-root. If your OpenShift cluster
blocks the ingress controller or infrastructure images because of SCC policies,
grant only the minimum SCC approved by your platform team. Start by checking
pod events:

```bash
oc get pods -n devops-hub-prod
oc describe pod <pod-name> -n devops-hub-prod
```

Do not grant broad SCC permissions unless your security team approves it.

## 6. DNS, Ingress, And Browser Access

The Helm chart creates:

- `frontend-service` on port `3000`.
- `backend-service` on port `8000`.
- Frontend ingress at `/`.
- Backend ingress at `/api`.

Both ingresses use the host from `HUB_HOSTNAME`.

After deployment, check ingress:

```bash
oc get ingress -n devops-hub-prod
oc describe ingress frontend-ingress -n devops-hub-prod
oc describe ingress backend-ingress -n devops-hub-prod
```

Point DNS for `HUB_HOSTNAME` to the OpenShift ingress/router address. Get the
router address with one of:

```bash
oc get svc -A | grep -i router
oc get ingresscontroller -n openshift-ingress-operator
oc get route -n openshift-console
```

Your network/DNS team should create an `A` or `CNAME` record like:

```text
devops.company.local -> <OpenShift router/load-balancer address>
```

Browser URL:

```text
https://devops.company.local/ui/
```

If TLS is terminated by the OpenShift router or a corporate load balancer,
configure certificates according to your platform standard. The included
ingress annotations currently do not force SSL redirect.

## 7. Deploy With Helm Manually

The pipeline deploys with Helm automatically, but a manual deploy is useful for
first-time troubleshooting.

Login:

```bash
oc login https://api.cluster.company.local:6443 --token='<token>'
oc project devops-hub-prod
```

Create a temporary values file for secrets:

```yaml
backend:
  secrets:
    postgresPassword: "<postgres-password>"
    redisPassword: "<redis-password>"
    hubAdminUsername: "<admin-user>"
    hubAdminPassword: "<admin-password>"
    jwtSecret: "<long-random-secret>"
    azureDevopsBaseUrl: "https://dev.azure.com/"
    azureDevopsAdminPat: "<admin-pat>"
    snowBaseUrl: "https://service-now.company.local"
    snowApiUsername: "<snow-user>"
    snowApiPassword: "<snow-password>"
    artifactoryBaseUrl: "https://artifactory.company.local"
    sonarqubeBaseUrl: "https://sonarqube.company.local"
    confluenceBaseUrl: "https://confluence.company.local"
```

Deploy:

```bash
helm dependency build ./deployment

helm upgrade --install devops-hub ./deployment \
  --namespace devops-hub-prod \
  --create-namespace \
  --atomic \
  --timeout 15m \
  -f ./closed-network-secrets-values.yaml \
  --set-string global.customImage.repository="artifactory.company.local/devops-hub" \
  --set-string backend.image.tag="<image-tag>" \
  --set-string frontend.image.tag="<image-tag>" \
  --set-string backend.ingress.host="devops.company.local" \
  --set-string frontend.ingress.host="devops.company.local" \
  --set-string infrastructure.postgres.image="artifactory.company.local/docker/postgres:16-alpine" \
  --set-string infrastructure.redis.image="artifactory.company.local/docker/redis:7-alpine" \
  --set-string ingress-prod.controller.image.registry="artifactory.company.local" \
  --set-string ingress-prod.controller.image.image="docker/ingress-nginx/controller" \
  --set-string ingress-prod.controller.image.tag="v1.15.0" \
  --set-string ingress-prod.controller.image.digest="" \
  --set-string ingress-prod.controller.scope.namespace="devops-hub-prod"
```

Do not run `oc apply` directly on files under `deployment/charts/**/templates`.
Those files contain Helm template expressions and must be rendered by Helm.

## 8. Post-Deployment Checks

Run:

```bash
oc get pods -n devops-hub-prod
oc get svc -n devops-hub-prod
oc get ingress -n devops-hub-prod
helm status devops-hub -n devops-hub-prod
```

Expected pods include:

- `backend`
- `frontend`
- `postgres`
- `redis`
- ingress-nginx controller pods if the bundled ingress controller is enabled

Health checks:

```bash
oc port-forward svc/backend-service 8000:8000 -n devops-hub-prod
curl http://localhost:8000/api/health/ready
```

Frontend browser check:

```text
https://<HUB_HOSTNAME>/ui/
```

Login with `HUB_ADMIN_USERNAME` / `HUB_ADMIN_PASSWORD`.

## 9. Common Fixes

| Symptom | Check |
| --- | --- |
| `ImagePullBackOff` | Image names in variable group, Artifactory pull secret, and image mirror paths. |
| `CreateContainerConfigError` | Missing key in `all-secrets`; confirm every variable group value exists. |
| Frontend ingress returns 503 | `frontend` pod readiness, `frontend-service`, ingress host DNS, and `/healthz`. |
| Backend 500 on login | Backend logs, `JWT_SECRET`, database connectivity, and dependency image version. |
| Widgets ask for tokens | User-level PATs must be entered in the widget prompt; system base URLs come from variable group. |
| Azure DevOps widgets show no projects | PAT permissions and `AZURE_DEVOPS_BASE_URL`; the app supports multiple collections based on PAT access. |
| ServiceNow ticket creation fails | `SNOW_*` values and service account permissions to `sys_user`, `sys_user_group`, `incident`, and attachment APIs. |

## 10. Things To Confirm Before Final Transfer

- `offline-deps/security/sha256sums.txt` verifies after transfer.
- Internal Artifactory contains all Python wheels and npm tarballs.
- Internal Docker registry contains app base images and runtime images.
- The closed-network agent can run `docker`, `python`, `npm`, `helm`, and `oc`.
- DNS points `HUB_HOSTNAME` to the OpenShift router/load balancer.
- The OpenShift namespace has storage available for Postgres and Redis PVCs.
- The Azure DevOps variable group is linked to the pipeline.
- The app can reach Azure DevOps, ServiceNow, Artifactory, SonarQube, and Confluence from inside OpenShift.
