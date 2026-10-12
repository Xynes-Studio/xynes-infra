-- Create app-specific schemas
CREATE SCHEMA IF NOT EXISTS docs;
CREATE SCHEMA IF NOT EXISTS cms;
CREATE SCHEMA IF NOT EXISTS comments;
CREATE SCHEMA IF NOT EXISTS telemetry;

-- Grant usage and create permissions to the postgres role
-- (used by app services)
GRANT USAGE ON SCHEMA docs TO postgres;
GRANT USAGE ON SCHEMA cms TO postgres;
GRANT USAGE ON SCHEMA comments TO postgres;
GRANT USAGE ON SCHEMA telemetry TO postgres;

GRANT CREATE ON SCHEMA docs TO postgres;
GRANT CREATE ON SCHEMA cms TO postgres;
GRANT CREATE ON SCHEMA comments TO postgres;
GRANT CREATE ON SCHEMA telemetry TO postgres;
