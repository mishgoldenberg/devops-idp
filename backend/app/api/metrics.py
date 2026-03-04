from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from db import query_all
from security import AuthUser, can_view_observability, get_current_user


router = APIRouter()


@router.get("/usage")
def get_usage_metrics(
    period: str = Query("7d", pattern="^(7d|30d)$"),
    current_user: AuthUser = Depends(get_current_user),
):
    if not can_view_observability(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    days = 30 if period == "30d" else 7

    widget_usage = query_all(
        f"""
        SELECT metric_name, COUNT(*) as count, COUNT(DISTINCT user_id) as unique_users
        FROM usage_metrics
        WHERE metric_type = 'WIDGET_VIEW'
          AND created_at > NOW() - INTERVAL '{days} days'
        GROUP BY metric_name
        ORDER BY count DESC
        LIMIT 10
        """
    )

    self_service_usage = query_all(
        f"""
        SELECT metric_name, COUNT(*) as count
        FROM usage_metrics
        WHERE metric_type = 'SELF_SERVICE_USE'
          AND created_at > NOW() - INTERVAL '{days} days'
        GROUP BY metric_name
        ORDER BY count DESC
        """
    )

    daily_active_users = query_all(
        f"""
        SELECT DATE(created_at) as date, COUNT(DISTINCT user_id) as active_users
        FROM usage_metrics
        WHERE created_at > NOW() - INTERVAL '{days} days'
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        """
    )

    return {
        "success": True,
        "data": {
            "widgetUsage": widget_usage,
            "selfServiceUsage": self_service_usage,
            "dailyActiveUsers": daily_active_users,
        },
        "timestamp": _now_iso(),
    }


@router.get("/services")
def get_service_metrics(current_user: AuthUser = Depends(get_current_user)):
    if not can_view_observability(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    services = query_all(
        """
        SELECT service_name, status, last_check_at, last_success_at, last_error_at,
               error_message, response_time_ms, updated_at
        FROM service_health
        ORDER BY service_name
        """
    )
    return {
        "success": True,
        "data": services,
        "timestamp": _now_iso(),
    }


@router.get("/approvals")
def get_approval_metrics(current_user: AuthUser = Depends(get_current_user)):
    if not can_view_observability(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    stats = query_all(
        """
        SELECT 
          request_type,
          COUNT(*) as total,
          COUNT(*) FILTER (WHERE status = 'PENDING') as pending,
          COUNT(*) FILTER (WHERE status = 'APPROVED') as approved,
          COUNT(*) FILTER (WHERE status = 'REJECTED') as rejected,
          AVG(EXTRACT(EPOCH FROM (approved_at - created_at))) 
             FILTER (WHERE approved_at IS NOT NULL) as avg_approval_time_seconds
        FROM approval_requests
        WHERE created_at > NOW() - INTERVAL '30 days'
        GROUP BY request_type
        """
    )
    return {
        "success": True,
        "data": stats,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


