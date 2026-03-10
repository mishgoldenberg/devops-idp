-- Seed roles (RBAC hierarchy)

INSERT INTO roles (id, name, hierarchy_level, permissions, description) VALUES
(1, 'Platform Admin', 1, 
 '["*"]'::jsonb,
 'Full system access, user management, all approvals, observability'),

(2, 'Unit Commander', 2,
 '["view:all_dashboards", "view:aggregated_metrics", "approve:all_requests", "view:reports"]'::jsonb,
 'Cross-branch visibility, strategic metrics, approval authority'),

(3, 'Branch Head', 3,
 '["view:branch_dashboards", "view:branch_metrics", "approve:ado_projects", "approve:sonar_scans"]'::jsonb,
 'Branch-level aggregation, team oversight, approval authority'),

(4, 'Head of Section', 4,
 '["view:section_metrics", "view:observability", "approve:section_requests"]'::jsonb,
 'Section metrics, observability access, section approvals'),

(5, 'Project Manager', 5,
 '["view:project_metrics", "create:ado_projects", "approve:team_requests"]'::jsonb,
 'Project-level visibility, some approvals'),

(6, 'Team Lead', 6,
 '["view:team_metrics", "view:observability", "create:self_service"]'::jsonb,
 'Team metrics, observability access'),

(7, 'Regular User', 7,
 '["view:own_dashboard", "create:tickets"]'::jsonb,
 'Personal dashboard, basic self-service');

-- Update sequence
SELECT setval('roles_id_seq', (SELECT MAX(id) FROM roles));

