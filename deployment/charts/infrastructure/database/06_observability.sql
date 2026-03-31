-- Portal observability tables (mirrors backend/app/db.ensure_tables for Helm init jobs)

CREATE TABLE IF NOT EXISTS widget_usage (
    widget_key   VARCHAR(255) PRIMARY KEY,
    widget_name  VARCHAR(255) NOT NULL,
    usage_count  BIGINT NOT NULL DEFAULT 0,
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS self_service_usage (
    service_key      VARCHAR(255) PRIMARY KEY,
    service_name     VARCHAR(255) NOT NULL,
    execution_count  BIGINT NOT NULL DEFAULT 0,
    last_executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS azure_projects (
    id            SERIAL PRIMARY KEY,
    job_id        VARCHAR(128) UNIQUE NOT NULL,
    project_name  VARCHAR(255) NOT NULL,
    created_by    VARCHAR(255) NOT NULL,
    process_type  VARCHAR(64) NOT NULL,
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at  TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS servicenow_tickets (
    id                 SERIAL PRIMARY KEY,
    ticket_id          VARCHAR(128) NOT NULL,
    created_by         VARCHAR(255) NOT NULL,
    severity           VARCHAR(32) NOT NULL,
    short_description  VARCHAR(512) DEFAULT '',
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_servicenow_tickets_created ON servicenow_tickets (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_azure_projects_completed ON azure_projects (completed_at DESC NULLS LAST);
