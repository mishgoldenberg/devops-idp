-- Seed approval workflow rules
--
-- NOTE: the running backend does NOT read approval_rules — approval permission
-- is enforced in code by _can_approve_any() (hierarchy_level <= 5) in
-- backend/app/api/approvals.py. This seed is retained only so the table is not
-- empty for anything that inspects it directly. ADO_PROJECT_CREATE is the only
-- executable request type (see SUPPORTED_REQUEST_TYPES); the removed rows for
-- SONAR_PR_SCANNING_ENABLE / AI_MODEL_ACCESS described actions that were never
-- implemented.

INSERT INTO approval_rules (request_type, min_approver_role_level, approver_count) VALUES
('ADO_PROJECT_CREATE', 3, 1);        -- Branch Head or above

