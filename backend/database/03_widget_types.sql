-- Seed available widget types

INSERT INTO widget_types (widget_key, name, description, category, min_role_level, icon) VALUES
-- Azure DevOps widgets
('ado_my_work_items', 'My Work Items', 'Personal backlog items (To Do / In Progress)', 'azure_devops', 7, 'CheckSquare'),
('ado_my_pull_requests', 'My Pull Requests', 'Active PRs with review status', 'azure_devops', 7, 'GitPullRequest'),
('ado_pipeline_status', 'Pipeline Status', 'Recent pipeline runs', 'azure_devops', 7, 'Activity'),
('ado_sprint_progress', 'Sprint Progress', 'Current sprint burn-down', 'azure_devops', 6, 'TrendingUp'),

-- SonarQube widgets
('sonar_quality_gate', 'Quality Gate', 'Current project quality gate status', 'sonarqube', 7, 'Shield'),
('sonar_code_coverage', 'Code Coverage', 'Test coverage percentage and trend', 'sonarqube', 7, 'Target'),
('sonar_technical_debt', 'Technical Debt', 'Estimated hours to fix issues', 'sonarqube', 6, 'AlertTriangle'),
('sonar_security_hotspots', 'Security Hotspots', 'Critical security issues', 'sonarqube', 6, 'Lock'),

-- Artifactory widgets
('artifactory_latest_artifacts', 'Latest Artifacts', 'Recent builds per repository', 'artifactory', 7, 'Package'),
('artifactory_storage_usage', 'Storage Usage', 'Quota consumption', 'artifactory', 6, 'HardDrive'),
('artifactory_download_stats', 'Download Stats', 'Most downloaded artifacts', 'artifactory', 5, 'Download'),

-- ServiceNow widgets
('snow_my_tickets', 'My Tickets', 'Open incidents and requests', 'servicenow', 7, 'Ticket'),
('snow_team_tickets', 'Team Tickets', 'Team workload overview', 'servicenow', 6, 'Users'),

-- AI Chatbot widgets
('ai_chatbot', 'AI Assistant', 'Embedded conversational interface', 'ai_chatbot', 7, 'MessageSquare'),
('ai_recent_conversations', 'Recent Conversations', 'Quick access to chat history', 'ai_chatbot', 7, 'Clock'),

-- Executive widgets
('executive_branch_dashboard', 'Branch Dashboard', 'Aggregated metrics per branch', 'executive', 3, 'BarChart'),
('executive_deployment_frequency', 'Deployment Frequency', 'Release velocity metrics', 'executive', 3, 'Zap'),
('executive_quality_trends', 'Quality Trends', 'Multi-project quality overview', 'executive', 2, 'TrendingUp'),
('executive_resource_utilization', 'Resource Utilization', 'Cross-team capacity', 'executive', 2, 'PieChart');

