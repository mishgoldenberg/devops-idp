"""
Portal observability: persist usage counters and integration events to Postgres.

Call sites should treat failures as non-fatal (logging only).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Human-readable labels for dashboard widget keys (extend when adding widgets).
WIDGET_LABELS: dict[str, str] = {
    "quick_links": "Quick Links",
    "ado_my_work_items": "Azure DevOps — My work items",
    "snow_my_tickets": "ServiceNow — My tickets",
    "ado_my_pull_requests": "Azure DevOps — My pull requests",
    "ado_prs_for_review": "Azure DevOps — PRs to review",
    "ado_pipeline_status": "Azure DevOps — Pipeline status",
    "sonar_projects": "SonarQube — Projects",
    "artifactory_repos": "Artifactory — Repos",
    "artifactory_storage": "Artifactory — Storage",
    "recent_activity": "Recent Activity",
}


def record_servicenow_portal_ticket(
    ticket_id: str,
    created_by: str,
    severity: str,
    short_description: str = "",
) -> None:
    if not ticket_id:
        return
    try:
        import db

        db.execute(
            """
            INSERT INTO servicenow_tickets (ticket_id, created_by, severity, short_description, created_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            """,
            [
                str(ticket_id)[:128],
                (created_by or "")[:255],
                (severity or "Medium")[:32],
                (short_description or "")[:512],
            ],
        )
    except Exception as exc:
        logger.debug("record_servicenow_portal_ticket failed: %s", exc)


def urgency_to_severity_band(urgency: str) -> str:
    """The support wizard's urgency word (or a 1-5 priority code) as High/Medium/Low."""
    u = str(urgency or "").strip().lower()
    if u in ("urgent", "critical", "high", "1", "2"):
        return "High"
    if u in ("medium", "moderate", "normal", "3"):
        return "Medium"
    return "Low"  # low, planning, blank, or anything unrecognised
