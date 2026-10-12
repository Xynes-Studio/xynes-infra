-- ACCOUNTS-SCHEMA-1 – Identity & Workspace Core Tables
--
-- Creates shared schemas/tables keyed off Supabase auth.users.id (uuid).
-- No RLS is enabled here; least-privilege grants should be managed separately.

-- -----------------------------------------------------------------------------
-- identity.users
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS identity;

CREATE TABLE IF NOT EXISTS identity.users (
  id           uuid PRIMARY KEY,
  email        text NOT NULL,
  display_name text,
  avatar_url   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- platform.workspaces + platform.workspace_members
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE IF NOT EXISTS platform.workspaces (
  id          uuid PRIMARY KEY,
  name        text NOT NULL,
  slug        text UNIQUE,
  created_by  uuid NOT NULL REFERENCES identity.users(id),
  plan_type   text NOT NULL DEFAULT 'free',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.workspace_members (
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id),
  user_id      uuid NOT NULL REFERENCES identity.users(id),
  status       text NOT NULL DEFAULT 'active',
  joined_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

-- -----------------------------------------------------------------------------
-- Indexes (FK performance)
-- -----------------------------------------------------------------------------
-- Note: Postgres does not automatically index FK columns.
CREATE INDEX IF NOT EXISTS workspaces_created_by_idx ON platform.workspaces (created_by);
CREATE INDEX IF NOT EXISTS workspace_members_user_id_idx ON platform.workspace_members (user_id);

-- -----------------------------------------------------------------------------
-- Grants (matches existing infra pattern; required for services running migrations)
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA identity TO postgres;
GRANT USAGE ON SCHEMA platform TO postgres;

GRANT CREATE ON SCHEMA identity TO postgres;
GRANT CREATE ON SCHEMA platform TO postgres;
