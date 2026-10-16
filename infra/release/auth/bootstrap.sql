-- Fresh DB only. GoTrue owns the Auth schema, tables and migration history.
-- Psql reads the scoped credential from its environment; never supply it with -v.
\set ON_ERROR_STOP on
SET log_statement = none;
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SET password_encryption = 'scram-sha-256';
\getenv auth_password AUTH_DATABASE_PASSWORD
BEGIN;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth')
     OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('supabase_auth_admin','anon','authenticated','service_role')) THEN
    RAISE EXCEPTION 'Database already has Auth state; explicit migration required';
  END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION pgcrypto WITH SCHEMA public;
SELECT format('CREATE ROLE supabase_auth_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'auth_password') \gexec
CREATE SCHEMA auth AUTHORIZATION supabase_auth_admin;
GRANT CONNECT ON DATABASE postgres TO supabase_auth_admin;
GRANT USAGE ON SCHEMA public TO supabase_auth_admin;
ALTER ROLE supabase_auth_admin SET search_path = auth, public;
-- GoTrue supplies auth.uid(), auth.role(), auth.email() and auth.jwt().
COMMIT;
\unset auth_password
