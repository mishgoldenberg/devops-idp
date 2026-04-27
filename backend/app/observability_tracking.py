"""
Portal observability: persist usage counters and integration events to Postgres.

Call sites should treat failures as non-fatal (logging only).
"""

from __future__ import annotations

import logging
from typing import Optional

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


def _widget_label(widget_key: str) -> str:
    return WIDGET_LABELS.get(widget_key, widget_key.replace("_", " ").title())


def record_self_service_execution(service_key: str, service_name: Optional[str] = None) -> None:
    """
    One row per logical self-service product, keyed by stable identifier e.g.
    'azure_devops.create_project'. New flows register a new key + display name.
    """
    if not service_key or not str(service_key).strip():
        return
    sk = str(service_key).strip()[:255]
    name = (service_name or sk.replace(".", " — ").replace("_", " ")).strip()[:255]
    try:
        import db

        db.execute(
            """
            INSERT INTO self_service_usage (service_key, service_name, execution_count, last_executed_at)
            VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (service_key) DO UPDATE SET
              execution_count = self_service_usage.execution_count + 1,
              service_name = EXCLUDED.service_name,
              last_executed_at = CURRENT_TIMESTAMP
            """,
            [sk, name],
        )
    except Exception as exc:
        logger.debug("record_self_service_execution failed: %s", exc)


def register_azure_provision_job(
    job_id: str,
    project_name: str,
    created_by: str,
    process_type: str,
) -> None:
    if not job_id:
        return
    try:
        import db

        db.execute(
            """
            INSERT INTO azure_projects (job_id, project_name, created_by, process_type, created_at, completed_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, NULL)
            ON CONFLICT (job_id) DO UPDATE SET
              project_name = EXCLUDED.project_name,
              created_by = EXCLUDED.created_by,
              process_type = EXCLUDED.process_type
            """,
            [job_id[:128], project_name[:255], created_by[:255], process_type[:64]],
        )
    except Exception as exc:
        logger.debug("register_azure_provision_job failed: %s", exc)


def finalize_azure_provision_job(job_id: str) -> None:
    """Mark provision as completed (Terraform succeeded)."""
    if not job_id:
        return
    try:
        import db

        db.execute(
            """
            UPDATE azure_projects
            SET completed_at = CURRENT_TIMESTAMP
            WHERE job_id = %s AND completed_at IS NULL
            """,
            [job_id[:128]],
        )
    except Exception as exc:
        logger.debug("finalize_azure_provision_job failed: %s", exc)


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


def priority_to_severity_band(priority_code: str) -> str:
    """Map ServiceNow priority (1–5) to High / Medium / Low for dashboards."""
    p = str(priority_code).strip()
    if p in ("1", "2"):
        return "High"
    if p == "3":
        return "Medium"
    return "Low"
