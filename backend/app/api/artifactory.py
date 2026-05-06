from fastapi import APIRouter, Depends

from security import AuthUser, get_current_user


router = APIRouter()


_MOCK_ARTIFACTORY_BASE = "https://mock.artifactory.example"


def _mock_repos():
    return [
        {"name": "docker-dev", "type": "docker", "description": "Development Docker images", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/docker-dev"},
        {"name": "libs-release", "type": "maven", "description": "Release binaries", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/libs-release"},
        {"name": "npm-release", "type": "npm", "description": "NPM packages", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/npm-release"},
        {"name": "generic-dev", "type": "generic", "description": "Zip and utility artifacts", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/generic-dev"},
    ]


def _mock_repo_details():
    return {
        "docker-dev": {
            "name": "docker-dev",
            "type": "docker",
            "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/docker-dev",
            "artifacts": [
                {"name": "payment-service:1.8.2", "type": "image", "last_updated": "2024-01-18T09:20:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/docker:%2F%2Fdocker-dev%2Fpayment-service/1.8.2"},
                {"name": "devops-portal:2.3.0", "type": "image", "last_updated": "2024-01-17T14:05:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/docker:%2F%2Fdocker-dev%2Fdevops-portal/2.3.0"},
                {"name": "worker-api:0.9.7", "type": "image", "last_updated": "2024-01-17T11:40:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/docker:%2F%2Fdocker-dev%2Fworker-api/0.9.7"},
                {"name": "nginx-base:1.25.4", "type": "image", "last_updated": "2024-01-16T19:15:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/docker:%2F%2Fdocker-dev%2Fnginx-base/1.25.4"},
                {"name": "terraform-runner:1.1.1", "type": "image", "last_updated": "2024-01-15T08:35:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/docker:%2F%2Fdocker-dev%2Fterraform-runner/1.1.1"},
            ],
        },
        "libs-release": {
            "name": "libs-release",
            "type": "maven",
            "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/libs-release",
            "artifacts": [
                {"name": "backend-api-1.2.5.jar", "type": "jar", "last_updated": "2024-01-15T10:00:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/libs-release/com/company/backend-api/1.2.5/backend-api-1.2.5.jar"},
                {"name": "shared-lib-0.9.2.jar", "type": "jar", "last_updated": "2024-01-14T16:45:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/libs-release/com/company/shared-lib/0.9.2/shared-lib-0.9.2.jar"},
                {"name": "billing-sdk-3.4.1.jar", "type": "jar", "last_updated": "2024-01-12T12:30:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/libs-release/com/company/billing-sdk/3.4.1/billing-sdk-3.4.1.jar"},
                {"name": "events-client-2.0.0.jar", "type": "jar", "last_updated": "2024-01-11T09:10:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/libs-release/com/company/events-client/2.0.0/events-client-2.0.0.jar"},
                {"name": "common-test-fixtures-1.1.0.jar", "type": "jar", "last_updated": "2024-01-10T08:00:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/libs-release/com/company/common-test-fixtures/1.1.0/common-test-fixtures-1.1.0.jar"},
            ],
        },
        "npm-release": {
            "name": "npm-release",
            "type": "npm",
            "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/npm-release",
            "artifacts": [
                {"name": "@company/ui-kit@4.1.0", "type": "npm", "last_updated": "2024-01-18T07:50:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/npm:%2F%2Fnpm-release%2F@company%2Fui-kit"},
                {"name": "@company/auth-client@2.6.3", "type": "npm", "last_updated": "2024-01-17T09:10:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/npm:%2F%2Fnpm-release%2F@company%2Fauth-client"},
                {"name": "@company/forms@1.8.0", "type": "npm", "last_updated": "2024-01-16T13:25:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/npm:%2F%2Fnpm-release%2F@company%2Fforms"},
                {"name": "frontend-app-2.1.3.tar.gz", "type": "zip", "last_updated": "2024-01-15T09:30:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/npm-release/frontend-app/frontend-app-2.1.3.tar.gz"},
                {"name": "@company/theme@3.0.2", "type": "npm", "last_updated": "2024-01-12T10:55:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/packages/npm:%2F%2Fnpm-release%2F@company%2Ftheme"},
            ],
        },
        "generic-dev": {
            "name": "generic-dev",
            "type": "generic",
            "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/repos/tree/General/generic-dev",
            "artifacts": [
                {"name": "terraform-plan-payment.zip", "type": "zip", "last_updated": "2024-01-18T15:05:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/generic-dev/terraform/terraform-plan-payment.zip"},
                {"name": "release-notes-2024.01.md", "type": "doc", "last_updated": "2024-01-17T17:20:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/generic-dev/docs/release-notes-2024.01.md"},
                {"name": "load-test-report.html", "type": "html", "last_updated": "2024-01-16T21:35:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/generic-dev/reports/load-test-report.html"},
                {"name": "db-migration-bundle.zip", "type": "zip", "last_updated": "2024-01-15T10:15:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/generic-dev/db/db-migration-bundle.zip"},
                {"name": "helm-values-snapshot.yaml", "type": "yaml", "last_updated": "2024-01-13T08:45:00Z", "url": f"{_MOCK_ARTIFACTORY_BASE}/ui/native/generic-dev/helm/helm-values-snapshot.yaml"},
            ],
        },
    }


@router.get("/repos")
def get_repos(current_user: AuthUser = Depends(get_current_user)):
    """Return mock Artifactory repositories for the new repos widget."""
    return {
        "success": True,
        "data": _mock_repos(),
        "timestamp": _now_iso(),
    }


@router.get("/repo-details")
def get_repo_details(name: str, current_user: AuthUser = Depends(get_current_user)):
    """Return mock latest artifacts for a single repo, loaded lazily on hover."""
    details = _mock_repo_details().get(name)
    if not details:
        details = {"name": name, "type": "", "artifacts": []}
    return {
        "success": True,
        "data": details,
        "timestamp": _now_iso(),
    }


@router.get("/artifacts")
def get_artifacts(current_user: AuthUser = Depends(get_current_user)):
    artifacts = [
        {
            "name": "backend-api-1.2.5.jar",
            "path": "/releases/com/company/backend-api/1.2.5/",
            "repo": "libs-release",
            "size": 45231872,
            "created": "2024-01-15T10:00:00Z",
            "modified": "2024-01-15T10:00:00Z",
            "created_by": "jenkins",
            "download_count": 127,
        },
        {
            "name": "frontend-app-2.1.3.tar.gz",
            "path": "/releases/com/company/frontend-app/2.1.3/",
            "repo": "npm-release",
            "size": 12458912,
            "created": "2024-01-15T09:30:00Z",
            "modified": "2024-01-15T09:30:00Z",
            "created_by": "gitlab-ci",
            "download_count": 89,
        },
        {
            "name": "shared-lib-0.9.2.jar",
            "path": "/releases/com/company/shared-lib/0.9.2/",
            "repo": "libs-release",
            "size": 2341504,
            "created": "2024-01-14T16:45:00Z",
            "modified": "2024-01-14T16:45:00Z",
            "created_by": "jenkins",
            "download_count": 234,
        },
    ]
    return {
        "success": True,
        "data": artifacts,
        "timestamp": _now_iso(),
    }


@router.get("/repositories")
def get_repositories(current_user: AuthUser = Depends(get_current_user)):
    repos = [
        {
            "key": "libs-release",
            "type": "LOCAL",
            "description": "Release binaries",
            "url": "https://artifactory.internal/libs-release",
            "package_type": "maven",
        },
        {
            "key": "npm-release",
            "type": "LOCAL",
            "description": "NPM packages",
            "url": "https://artifactory.internal/npm-release",
            "package_type": "npm",
        },
        {
            "key": "docker-local",
            "type": "LOCAL",
            "description": "Docker images",
            "url": "https://artifactory.internal/docker-local",
            "package_type": "docker",
        },
    ]
    return {
        "success": True,
        "data": repos,
        "timestamp": _now_iso(),
    }


@router.get("/storage")
def get_storage(current_user: AuthUser = Depends(get_current_user)):
    storage = {
        "used": 523487621120,
        "total": 1099511627776,
        "percentage": 47.6,
        "repositories": [
            {"name": "libs-release", "used": 256743809024, "percentage": 49.0},
            {"name": "docker-local", "used": 178956970752, "percentage": 34.2},
            {"name": "npm-release", "used": 87786841344, "percentage": 16.8},
        ],
    }
    return {
        "success": True,
        "data": storage,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


