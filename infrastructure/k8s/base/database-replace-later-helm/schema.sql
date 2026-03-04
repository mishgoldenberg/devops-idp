-- DevOps Control Center Database Schema
-- PostgreSQL 16+

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- CORE TABLES
-- ============================================

-- Roles table (predefined hierarchy)
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    hierarchy_level INTEGER UNIQUE NOT NULL, -- 1=highest (Platform Admin), 7=lowest (Regular User)
    permissions JSONB NOT NULL DEFAULT '[]',
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255),
    role_id INTEGER NOT NULL REFERENCES roles(id),
    domain_groups JSONB DEFAULT '[]', -- Array of AD/SSO groups
    is_active BOOLEAN DEFAULT true,
    last_login_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB DEFAULT '{}', -- Extensible user metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Dashboards table (user-specific layouts)
CREATE TABLE IF NOT EXISTS dashboards (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) DEFAULT 'My Dashboard',
    is_default BOOLEAN DEFAULT false,
    layout JSONB NOT NULL DEFAULT '[]', -- Grid layout configuration
    widgets JSONB NOT NULL DEFAULT '[]', -- Array of widget configurations
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, name)
);

-- Widget registry (available widget types)
CREATE TABLE IF NOT EXISTS widget_types (
    id SERIAL PRIMARY KEY,
    widget_key VARCHAR(100) UNIQUE NOT NULL, -- e.g., 'ado_my_work_items'
    name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(100), -- 'azure_devops', 'sonarqube', etc.
    min_role_level INTEGER NOT NULL DEFAULT 7, -- Minimum hierarchy level to access
    default_config JSONB DEFAULT '{}',
    icon VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- APPROVAL SYSTEM
-- ============================================

-- Approval request types
CREATE TYPE approval_request_type AS ENUM (
    'ADO_PROJECT_CREATE',
    'SONAR_PR_SCANNING_ENABLE',
    'AI_MODEL_ACCESS',
    'CUSTOM'
);

-- Approval status
CREATE TYPE approval_status AS ENUM (
    'PENDING',
    'APPROVED',
    'REJECTED',
    'EXECUTED',
    'FAILED'
);

-- Approval requests
CREATE TABLE IF NOT EXISTS approval_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    requester_id UUID NOT NULL REFERENCES users(id),
    request_type approval_request_type NOT NULL,
    request_title VARCHAR(255) NOT NULL,
    request_payload JSONB NOT NULL, -- Full request details
    status approval_status DEFAULT 'PENDING',
    approver_id UUID REFERENCES users(id),
    approver_comments TEXT,
    execution_result JSONB, -- Result after execution
    approved_at TIMESTAMP WITH TIME ZONE,
    executed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Approval workflow rules (which roles can approve what)
CREATE TABLE IF NOT EXISTS approval_rules (
    id SERIAL PRIMARY KEY,
    request_type approval_request_type NOT NULL,
    min_approver_role_level INTEGER NOT NULL, -- Minimum hierarchy level required to approve
    auto_approve_threshold INTEGER, -- Optional: auto-approve if requester is above this level
    requires_multiple_approvers BOOLEAN DEFAULT false,
    approver_count INTEGER DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- AUDIT & COMPLIANCE
-- ============================================

-- Audit log action types
CREATE TYPE audit_action AS ENUM (
    'USER_LOGIN',
    'USER_LOGOUT',
    'CREATE_REQUEST',
    'APPROVE_REQUEST',
    'REJECT_REQUEST',
    'EXECUTE_REQUEST',
    'UPDATE_DASHBOARD',
    'ADD_WIDGET',
    'REMOVE_WIDGET',
    'VIEW_PAGE',
    'API_CALL',
    'SYSTEM_ERROR'
);

-- Audit logs (append-only, no deletions allowed)
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id),
    action audit_action NOT NULL,
    resource_type VARCHAR(100), -- 'approval_request', 'dashboard', etc.
    resource_id UUID,
    details JSONB DEFAULT '{}',
    ip_address INET,
    user_agent TEXT,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Prevent deletion from audit_logs
CREATE RULE prevent_audit_delete AS ON DELETE TO audit_logs DO INSTEAD NOTHING;

-- ============================================
-- USAGE METRICS & ANALYTICS
-- ============================================

-- Metric types
CREATE TYPE metric_type AS ENUM (
    'WIDGET_VIEW',
    'WIDGET_INTERACTION',
    'SELF_SERVICE_USE',
    'PAGE_VIEW',
    'API_CALL',
    'SEARCH_QUERY',
    'EXTERNAL_REDIRECT'
);

-- Usage metrics
CREATE TABLE IF NOT EXISTS usage_metrics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id),
    metric_type metric_type NOT NULL,
    metric_name VARCHAR(255) NOT NULL, -- Widget name, service name, page path, etc.
    value INTEGER DEFAULT 1,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Aggregated daily metrics (for performance)
CREATE TABLE IF NOT EXISTS daily_metrics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    date DATE NOT NULL,
    metric_type metric_type NOT NULL,
    metric_name VARCHAR(255) NOT NULL,
    total_count INTEGER DEFAULT 0,
    unique_users INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, metric_type, metric_name)
);

-- ============================================
-- OBSERVABILITY & HEALTH
-- ============================================

-- Service health status
CREATE TYPE service_status AS ENUM (
    'HEALTHY',
    'DEGRADED',
    'DOWN',
    'UNKNOWN'
);

-- External service health tracking
CREATE TABLE IF NOT EXISTS service_health (
    id SERIAL PRIMARY KEY,
    service_name VARCHAR(100) UNIQUE NOT NULL, -- 'azure_devops', 'sonarqube', etc.
    status service_status DEFAULT 'UNKNOWN',
    last_check_at TIMESTAMP WITH TIME ZONE,
    last_success_at TIMESTAMP WITH TIME ZONE,
    last_error_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    response_time_ms INTEGER,
    metadata JSONB DEFAULT '{}',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- SYSTEM CONFIGURATION
-- ============================================

-- System-wide settings
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(100) PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    is_sensitive BOOLEAN DEFAULT false, -- If true, value should be encrypted
    updated_by UUID REFERENCES users(id),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- INDEXES
-- ============================================

-- Users
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_role_id ON users(role_id);
CREATE INDEX idx_users_active ON users(is_active) WHERE is_active = true;

-- Dashboards
CREATE INDEX idx_dashboards_user_id ON dashboards(user_id);
CREATE INDEX idx_dashboards_default ON dashboards(user_id, is_default) WHERE is_default = true;

-- Approval requests
CREATE INDEX idx_approval_requests_requester ON approval_requests(requester_id);
CREATE INDEX idx_approval_requests_approver ON approval_requests(approver_id);
CREATE INDEX idx_approval_requests_status ON approval_requests(status);
CREATE INDEX idx_approval_requests_type ON approval_requests(request_type);
CREATE INDEX idx_approval_requests_created ON approval_requests(created_at DESC);

-- Audit logs
CREATE INDEX idx_audit_logs_user ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_action ON audit_logs(action);
CREATE INDEX idx_audit_logs_created ON audit_logs(created_at DESC);
CREATE INDEX idx_audit_logs_resource ON audit_logs(resource_type, resource_id);

-- Usage metrics
CREATE INDEX idx_usage_metrics_user ON usage_metrics(user_id);
CREATE INDEX idx_usage_metrics_type ON usage_metrics(metric_type);
CREATE INDEX idx_usage_metrics_name ON usage_metrics(metric_name);
CREATE INDEX idx_usage_metrics_created ON usage_metrics(created_at DESC);

-- Daily metrics
CREATE INDEX idx_daily_metrics_date ON daily_metrics(date DESC);
CREATE INDEX idx_daily_metrics_type_name ON daily_metrics(metric_type, metric_name);

-- Service health
CREATE INDEX idx_service_health_name ON service_health(service_name);
CREATE INDEX idx_service_health_status ON service_health(status);

-- ============================================
-- TRIGGERS
-- ============================================

-- Update updated_at timestamp automatically
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_roles_updated_at BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_dashboards_updated_at BEFORE UPDATE ON dashboards
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_approval_requests_updated_at BEFORE UPDATE ON approval_requests
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_service_health_updated_at BEFORE UPDATE ON service_health
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- VIEWS
-- ============================================

-- Pending approvals with requester details
CREATE OR REPLACE VIEW pending_approvals AS
SELECT 
    ar.id,
    ar.request_type,
    ar.request_title,
    ar.request_payload,
    ar.created_at,
    u.username as requester_username,
    u.email as requester_email,
    u.full_name as requester_name,
    r.name as requester_role,
    r.hierarchy_level as requester_role_level
FROM approval_requests ar
JOIN users u ON ar.requester_id = u.id
JOIN roles r ON u.role_id = r.id
WHERE ar.status = 'PENDING'
ORDER BY ar.created_at ASC;

-- User dashboard summary
CREATE OR REPLACE VIEW user_dashboard_summary AS
SELECT 
    u.id as user_id,
    u.username,
    u.email,
    r.name as role,
    r.hierarchy_level,
    COUNT(DISTINCT d.id) as dashboard_count,
    COUNT(DISTINCT ar.id) FILTER (WHERE ar.status = 'PENDING') as pending_requests
FROM users u
JOIN roles r ON u.role_id = r.id
LEFT JOIN dashboards d ON u.id = d.user_id
LEFT JOIN approval_requests ar ON u.id = ar.requester_id
WHERE u.is_active = true
GROUP BY u.id, u.username, u.email, r.name, r.hierarchy_level;

-- ============================================
-- COMMENTS
-- ============================================

COMMENT ON TABLE users IS 'Platform users with SSO integration';
COMMENT ON TABLE roles IS 'RBAC hierarchy (7 levels)';
COMMENT ON TABLE dashboards IS 'User-specific widget layouts';
COMMENT ON TABLE approval_requests IS 'Self-service approval workflow';
COMMENT ON TABLE audit_logs IS 'Immutable audit trail';
COMMENT ON TABLE usage_metrics IS 'User behavior tracking';
COMMENT ON TABLE service_health IS 'External system health monitoring';

