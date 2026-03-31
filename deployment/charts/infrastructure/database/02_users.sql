-- Bootstrap platform admin (idempotent). Other users are created on first SSO login.

INSERT INTO users (username, email, full_name, role_id)
VALUES (
    'golden.mihel@gmail.com',
    'golden.mihel@gmail.com',
    'Golden Mihel',
    1
)
ON CONFLICT (email) DO UPDATE SET
    role_id = EXCLUDED.role_id,
    is_active = true,
    updated_at = CURRENT_TIMESTAMP;
