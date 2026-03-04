from fastapi import APIRouter, Depends

from ..security import AuthUser, get_current_user


router = APIRouter()


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


