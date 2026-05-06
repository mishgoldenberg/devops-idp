-- Seed available widget types
DELETE FROM widget_types
WHERE widget_key IN (
    'sonar_code_coverage',
    'sonar_technical_debt',
    'sonar_security_hotspots',
    'artifactory_latest_artifacts',
    'artifactory_storage_usage',
    'artifactory_download_stats',
    'ai_chatbot',
    'ai_recent_conversations'
);

INSERT INTO widget_types (widget_key, name, description, category, min_role_level, icon) VALUES
-- Azure DevOps widgets
('ado_my_work_items', 'My Work Items', 'Personal backlog items (To Do / In Progress)', 'azure_devops', 7, 'CheckSquare'),
('ado_my_pull_requests', 'My Pull Requests', 'Active PRs with review status', 'azure_devops', 7, 'GitPullRequest'),
('ado_pipeline_status', 'Pipeline Status', 'Recent pipeline runs', 'azure_devops', 7, 'Activity'),
('ado_sprint_progress', 'Sprint Progress', 'Current sprint burn-down', 'azure_devops', 6, 'TrendingUp'),

-- SonarQube widgets
('sonar_projects', 'SonarQube Projects', 'Project metrics and status', 'sonarqube', 7, 'Shield'),

-- Artifactory widgets
('artifactory_repos', 'Artifactory Repos', 'Repository browser and latest artifacts', 'artifactory', 7, 'Package'),
('artifactory_storage', 'Artifactory Storage', 'Quota consumption and biggest repositories', 'artifactory', 7, 'HardDrive'),

-- ServiceNow widgets
('snow_my_tickets', 'My Tickets', 'Open incidents and requests', 'servicenow', 7, 'Ticket'),
('snow_team_tickets', 'Team Tickets', 'Team workload overview', 'servicenow', 6, 'Users'),

-- Dashboard widgets
('recent_activity', 'Recent Activity', 'Latest user activity in the portal', 'dashboard', 7, 'Clock'),

-- Executive widgets
('executive_branch_dashboard', 'Branch Dashboard', 'Aggregated metrics per branch', 'executive', 3, 'BarChart'),
('executive_deployment_frequency', 'Deployment Frequency', 'Release velocity metrics', 'executive', 3, 'Zap'),
('executive_quality_trends', 'Quality Trends', 'Multi-project quality overview', 'executive', 2, 'TrendingUp'),
('executive_resource_utilization', 'Resource Utilization', 'Cross-team capacity', 'executive', 2, 'PieChart')
ON CONFLICT (widget_key) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    min_role_level = EXCLUDED.min_role_level,
    icon = EXCLUDED.icon;

