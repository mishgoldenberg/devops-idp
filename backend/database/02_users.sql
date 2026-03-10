-- Seed test users (for development only)

INSERT INTO users (id, username, email, full_name, role_id, domain_groups) VALUES
('a0000000-0000-0000-0000-000000000001', 'admin@internal', 'admin@internal.company', 'Platform Administrator', 1, '["DOMAIN\\DevOps-Admins"]'::jsonb),
('a0000000-0000-0000-0000-000000000002', 'commander@internal', 'commander@internal.company', 'John Commander', 2, '["DOMAIN\\Unit-Commanders"]'::jsonb),
('a0000000-0000-0000-0000-000000000003', 'branch.head@internal', 'branch.head@internal.company', 'Sarah Branch', 3, '["DOMAIN\\Branch-Heads"]'::jsonb),
('a0000000-0000-0000-0000-000000000004', 'section.head@internal', 'section.head@internal.company', 'Mike Section', 4, '["DOMAIN\\Section-Heads"]'::jsonb),
('a0000000-0000-0000-0000-000000000005', 'pm@internal', 'pm@internal.company', 'Alice Manager', 5, '["DOMAIN\\Project-Managers"]'::jsonb),
('a0000000-0000-0000-0000-000000000006', 'lead@internal', 'lead@internal.company', 'Bob Lead', 6, '["DOMAIN\\Team-Leads"]'::jsonb),
('a0000000-0000-0000-0000-000000000007', 'user@internal', 'user@internal.company', 'Jane Developer', 7, '["DOMAIN\\Engineers"]'::jsonb);

