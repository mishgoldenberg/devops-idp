from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Path, status

from security import AuthUser, get_current_user


router = APIRouter()


def _get_mock_projects():
    return [
        {
            "key": "backend-api",
            "project_key": "backend-api",
            "name": "Backend API",
            "language": "Python",
            "main_branch": "main",
            "branches": ["main", "dev", "release"],
            "quality_gate": {"status": "OK"},
            "metrics": {
                "coverage": 78.5,
                "bugs": 3,
                "vulnerabilities": 0,
                "code_smells": 12,
                "technical_debt": "2h 30m",
                "duplications": 1.2,
                "lines_of_code": 15420,
            },
            "last_analysis": "2024-01-15T08:30:00Z",
        },
        {
            "key": "frontend-app",
            "project_key": "frontend-app",
            "name": "Frontend Application",
            "language": "TypeScript",
            "main_branch": "main",
            "branches": ["main", "dev"],
            "quality_gate": {"status": "WARN"},
            "metrics": {
                "coverage": 62.3,
                "bugs": 5,
                "vulnerabilities": 1,
                "code_smells": 28,
                "technical_debt": "4h 15m",
                "duplications": 3.8,
                "lines_of_code": 22100,
            },
            "last_analysis": "2024-01-15T09:15:00Z",
        },
        {
            "key": "proj-1",
            "project_key": "proj-1",
            "name": "payment-service",
            "language": "Java",
            "main_branch": "main",
            "branches": ["main", "dev"],
            "quality_gate": {"status": "OK"},
            "metrics": {
                "coverage": 82,
                "bugs": 5,
                "vulnerabilities": 2,
                "code_smells": 18,
                "technical_debt": "3h",
                "duplications": 3,
                "lines_of_code": 12000,
            },
            "last_analysis": "2024-01-16T11:20:00Z",
        },
    ]


def _mock_project_details() -> Dict[str, Dict[str, object]]:
    return {
        str(project["project_key"]): {
            "project_key": project["project_key"],
            "name": project["name"],
            "language": project["language"],
            "branches": project["branches"],
            "main_branch": project["main_branch"],
            "bugs": project["metrics"]["bugs"],
            "vulnerabilities": project["metrics"]["vulnerabilities"],
            "coverage": project["metrics"]["coverage"],
            "duplications": project["metrics"]["duplications"],
            "lines_of_code": project["metrics"]["lines_of_code"],
        }
        for project in _get_mock_projects()
    }


@router.get("/projects")
def get_projects(current_user: AuthUser = Depends(get_current_user)):
    """Return mock SonarQube projects.

    SonarQube is intentionally mocked for now. The response includes the legacy
    fields used by the existing Code Quality widget plus the flatter
    project_key/language/main_branch shape consumed by the new hover widget.
    """
    return {
        "success": True,
        "data": _get_mock_projects(),
        "timestamp": _now_iso(),
    }


@router.get("/project-details")
def get_project_details(
    project_key: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """Return mock project metrics, loaded lazily by the frontend on hover."""
    details = _mock_project_details().get(project_key)
    if not details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return {
        "success": True,
        "data": details,
        "timestamp": _now_iso(),
    }


@router.get("/projects/{key}")
def get_project(key: str = Path(...), current_user: AuthUser = Depends(get_current_user)):
    projects = _get_mock_projects()
    project = next((p for p in projects if p["key"] == key), None)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return {
        "success": True,
        "data": project,
        "timestamp": _now_iso(),
    }


@router.post("/enable-pr-scanning")
def enable_pr_scanning(payload: dict, current_user: AuthUser = Depends(get_current_user)):
    project_key = payload.get("projectKey")
    if not project_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project key required",
        )
    return {
        "success": True,
        "message": "PR scanning enabled successfully",
        "data": {"projectKey": project_key, "enabled": True},
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


