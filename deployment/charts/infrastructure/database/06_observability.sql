-- Portal observability tables (mirrors backend/app/db.ensure_tables for Helm init jobs)

-- Drop legacy widget tables from earlier iterations. These two are genuinely dead
-- schema and dropping them is the point of this block.
DROP TABLE IF EXISTS widget_user_views;
DROP TABLE IF EXISTS home_widget_prefs;

-- widget_usage is DELIBERATELY NOT DROPPED ANY MORE.
--
-- This script is run by a Job on every single deploy, so dropping widget_usage here
-- emptied it on every release — and the CREATE TABLE IF NOT EXISTS below could never
-- preserve anything, because the line above had just deleted the table it was
-- guarding. The effect was that "Suggested for you", "Recently used" and every
-- Observability count silently reset to zero each time the pipeline ran, which reads
-- exactly like the database having been wiped.
--
-- It is the current schema, not legacy. If it ever genuinely needs replacing, that
-- is a migration written for that change - not a drop on every deploy forever.

-- Event-based widget usage: one row per widget render.
CREATE TABLE IF NOT EXISTS widget_usage (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    widget_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    session_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_widget_usage_key ON widget_usage (widget_key);
CREATE INDEX IF NOT EXISTS idx_widget_usage_session ON widget_usage (session_id);

-- Current dashboard state: one row per user/widget the user has active.
-- Observability page reads this for live "users with widget" counts.
CREATE TABLE IF NOT EXISTS user_widgets (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    widget_key TEXT NOT NULL,
    session_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (user_id, widget_key)
);
CREATE INDEX IF NOT EXISTS idx_user_widgets_key ON user_widgets (widget_key);

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
    GRANT SELECT, INSERT, UPDATE, DELETE ON user_widgets TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON self_service_usage TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON azure_projects TO devops;
    GRANT SELECT, INSERT, UPDATE, DELETE ON servicenow_tickets TO devops;
    GRANT USAGE, SELECT ON SEQUENCE widget_usage_id_seq TO devops;
    GRANT USAGE, SELECT ON SEQUENCE user_widgets_id_seq TO devops;
    GRANT USAGE, SELECT ON SEQUENCE azure_projects_id_seq TO devops;
    GRANT USAGE, SELECT ON SEQUENCE servicenow_tickets_id_seq TO devops;
  END IF;
END;
$obsgrant$;
