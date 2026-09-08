-- Seed roles (RBAC)
--
-- Two roles, because there are two levels of access. There used to be seven,
-- each carrying a `permissions` array such as '["approve:ado_projects"]', but no
-- authorization decision ever read that array: every real gate tested
-- hierarchy_level, and only level 1 (and, for approvals, <= 5) was ever tested.
-- The five intermediate roles therefore granted nothing that Regular User did
-- not, while reading like a hierarchy that meant something.
--
-- hierarchy_level 1 and 7 are kept rather than renumbered to 1 and 2: the value
-- is stored on every existing users row and read by widget-catalogue filtering,
-- so renumbering would silently reinterpret live data.
--
-- This file only runs when Postgres initialises an EMPTY data directory. An
-- existing database is migrated at startup by db.collapse_non_admin_roles(),
-- which moves every non-admin account onto Regular User and deliberately leaves
-- the old role rows in place.

INSERT INTO roles (id, name, hierarchy_level, permissions, description) VALUES
(1, 'Platform Admin', 1,
 '["*"]'::jsonb,
 'Full system access: user management, approvals, observability, SSO, safe mode'),

(7, 'Regular User', 7,
 '[]'::jsonb,
 'Personal dashboard, self-service requests, support tickets');

-- Update sequence
SELECT setval('roles_id_seq', (SELECT MAX(id) FROM roles));
