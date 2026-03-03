from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..security import AuthUser, get_current_user


router = APIRouter()


@router.get("/work-items")
def get_work_items(
    username: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    effective_username = username or current_user.get("username")
    if not effective_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username required",
        )

    # Mock data, ported from Node service semantics
    work_items = [
        {
            "id": 1001,
            "title": "Implement feature",
            "state": "In Progress",
            "type": "User Story",
            "assigned_to": effective_username,
            "created_date": "2024-01-14T09:00:00Z",
            "changed_date": "2024-01-15T10:00:00Z",
            "url": "https://dev.azure.com/org/project/_workitems/edit/1001",
        }
    ]
    return {
        "success": True,
        "data": work_items,
        "timestamp": _now_iso(),
    }


@router.get("/pull-requests")
def get_pull_requests(
    username: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    effective_username = username or current_user.get("username")
    if not effective_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username required",
        )

    pull_requests = [
        {
            "id": 2001,
            "title": "Refactor authentication flow",
            "status": "active",
            "created_by": effective_username,
            "created_date": "2024-01-15T08:30:00Z",
            "repository": "backend-api",
            "source_branch": "feature/auth-refactor",
            "target_branch": "main",
            "url": "https://dev.azure.com/org/project/_git/backend-api/pullrequest/2001",
        }
    ]
    return {
        "success": True,
        "data": pull_requests,
        "timestamp": _now_iso(),
    }


@router.get("/pipelines")
def get_pipelines(
    project: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    pipelines = [
        {
            "id": 3001,
            "name": project or "backend-api CI",
            "run_id": 987,
            "status": "completed",
            "result": "succeeded",
            "created_date": "2024-01-15T07:00:00Z",
            "finished_date": "2024-01-15T07:05:00Z",
            "url": "https://dev.azure.com/org/project/_build/results?buildId=987",
        }
    ]
    return {
        "success": True,
        "data": pipelines,
        "timestamp": _now_iso(),
    }


@router.post("/projects")
def create_project(payload: Dict[str, Any], current_user: AuthUser = Depends(get_current_user)):
    # Mock project creation, used by approval flow
    name = payload.get("name")
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project name required",
        )
    project = {
        "id": 4001,
        "name": name,
        "description": payload.get("description") or "",
        "url": f"https://dev.azure.com/org/{name}",
    }
    return {
        "success": True,
        "data": project,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


