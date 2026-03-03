-- Seed approval workflow rules

INSERT INTO approval_rules (request_type, min_approver_role_level, approver_count) VALUES
('ADO_PROJECT_CREATE', 3, 1),        -- Branch Head or above
('SONAR_PR_SCANNING_ENABLE', 4, 1),  -- Head of Section or above
('AI_MODEL_ACCESS', 1, 1);           -- Platform Admin only

