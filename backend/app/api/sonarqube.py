from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, status

from security import AuthUser, get_current_user


router = APIRouter()


def _get_mock_projects():
    return [
        {
            "key": "backend-api",
            "name": "Backend API",
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
            "name": "Frontend Application",
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
    ]


@router.get("/projects")
def get_projects(current_user: AuthUser = Depends(get_current_user)):
    return {
        "success": True,
        "data": _get_mock_projects(),
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


