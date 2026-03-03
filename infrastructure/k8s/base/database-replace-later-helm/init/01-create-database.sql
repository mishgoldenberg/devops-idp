-- Initialize database for DevOps Control Center
-- This runs automatically when PostgreSQL container starts for the first time

-- The database and user are created by Docker environment variables
-- This file can contain additional initialization SQL if needed

-- Ensure extensions are available
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Set timezone
SET timezone = 'UTC';

