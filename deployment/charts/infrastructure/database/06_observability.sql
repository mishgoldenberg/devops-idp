-- Portal observability tables (mirrors backend/app/db.ensure_tables for Helm init jobs)

CREATE TABLE IF NOT EXISTS widget_usage (
    widget_key   VARCHAR(255) PRIMARY KEY,
    widget_name  VARCHAR(255) NOT NULL,
    usage_count  BIGINT NOT NULL DEFAULT 0,
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Per-user per-widget view tracking (replaces the raw usage counter).
-- Allows COUNT(DISTINCT user_id) so refreshes don't inflate numbers.
CREATE TABLE IF NOT EXISTS widget_user_views (
    user_id      VARCHAR(255) NOT NULL,
    widget_key   VARCHAR(255) NOT NULL,
    widget_name  VARCHAR(255) NOT NULL DEFAULT '',
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, widget_key)
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
CREATE INDEX IF NOT EXISTS idx_azure_projects_completed ON azure_projects (completed_at DESC);

-- If this file was run as a superuser, ensure the portal app role (default: devops) can read/write.
-- Replace devops in your fork if DATABASE_URL uses another user.
DO $obsgrant$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'devops') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON widget_usage TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON widget_user_views TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON self_service_usage TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON azure_projects TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON servicenow_tickets TO devops;
    GRANT USAGE, SELECT ON SEQUENCE azure_projects_id_seq TO devops;
    GRANT USAGE, SELECT ON SEQUENCE servicenow_tickets_id_seq TO devops;
  END IF;
END;
$obsgrant$;
