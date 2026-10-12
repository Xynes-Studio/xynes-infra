-- WORKSPACE-INVITES-PROMOTE-1: Promote platform.workspace_invites into a proper
-- forward-only Supabase migration.
--
-- Source of truth: xynes/xynes-infra/docs/DATABASE.md §"Schema ownership map".
--
-- Why this migration exists:
--   Until 2026-05-13, the `platform.workspace_invites` table was created
--   ad-hoc by `scripts/bootstrap-local-db.sh` (via psql heredoc), not by a
--   Supabase migration. That worked because the script is idempotent
--   (CREATE TABLE IF NOT EXISTS + per-constraint NOT EXISTS guards), but
--   it had two downsides:
--     1. Fresh stacks that ran `supabase db reset` without running the
--        bootstrap afterward had no workspace_invites table.
--     2. The table was invisible to the Supabase migration history, so
--        contract tests and infra tooling could not assert it exists.
--
--   This migration moves the canonical table + constraint definitions
--   into Supabase migrations. The bootstrap script's psql heredoc for
--   workspace_invites is removed in the same change set (it would be a
--   no-op now anyway because of `IF NOT EXISTS`, but removing it keeps
--   the responsibility map clean).
--
-- Idempotency:
--   - CREATE TABLE / ALTER TABLE … ADD CONSTRAINT calls use IF NOT EXISTS
--     or `pg_constraint` lookup guards so this migration is safe to
--     replay against any environment.
--   - The conditional authz.roles FK preserves the gap-tolerant behavior
--     established in the bootstrap script (see comment block below).
--
-- DB safety: strictly additive. No DROP, no destructive DDL. Pre-existing
-- environments that already ran the bootstrap will have the table — this
-- migration's guards ensure it becomes a no-op there.

CREATE TABLE IF NOT EXISTS platform.workspace_invites (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  workspace_id uuid NOT NULL,
  email text NOT NULL,
  role_key text NOT NULL,
  invited_by uuid NOT NULL,
  token text NOT NULL,
  status text DEFAULT 'pending' NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz DEFAULT now() NOT NULL
);

COMMENT ON TABLE platform.workspace_invites IS
  'Workspace member invites issued by xynes-accounts-service. Token is the one-time secret a recipient redeems. Owned by xynes-infra (this migration); read/write by accounts-service.';

-- Unique constraint on the redemption token. The token is high-entropy
-- and one-shot; duplicates would be a security bug.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'platform.workspace_invites'::regclass
      AND conname = 'workspace_invites_token_unique'
  ) THEN
    ALTER TABLE platform.workspace_invites
      ADD CONSTRAINT workspace_invites_token_unique UNIQUE (token);
  END IF;
END $$;

-- workspace_id FK to platform.workspaces. Always safe to add here because
-- platform.workspaces is created by an earlier infra migration
-- (20251219133000_accounts_schema_1_identity_workspace_core.sql).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'platform.workspace_invites'::regclass
      AND conname = 'workspace_invites_workspace_id_workspaces_id_fk'
  ) THEN
    ALTER TABLE platform.workspace_invites
      ADD CONSTRAINT workspace_invites_workspace_id_workspaces_id_fk
      FOREIGN KEY (workspace_id) REFERENCES platform.workspaces(id);
  END IF;
END $$;

-- role_key FK to authz.roles(key). The authz.roles table is owned by
-- xynes-authz-service (Drizzle-managed, NOT a Supabase migration).
-- On a fresh stack where xynes-authz-service migrations have not run yet,
-- authz.roles does not exist and an unconditional FK would hard-fail the
-- whole reset. Skip the FK in that case with a NOTICE; re-running the
-- bootstrap (which idempotently re-applies all infra-side guards via
-- 20260513140000) once authz Drizzle migrations have applied will close
-- the gap automatically. The constraint is defense-in-depth referential
-- integrity, not a correctness requirement.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'authz' AND table_name = 'roles'
  ) THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conrelid = 'platform.workspace_invites'::regclass
        AND conname = 'workspace_invites_role_key_roles_key_fk'
    ) THEN
      ALTER TABLE platform.workspace_invites
        ADD CONSTRAINT workspace_invites_role_key_roles_key_fk
        FOREIGN KEY (role_key) REFERENCES authz.roles(key);
    END IF;
  ELSE
    RAISE NOTICE 'Skipping workspace_invites_role_key_roles_key_fk: authz.roles does not exist yet. xynes-authz-service Drizzle migrations must run before this FK can be added. Re-run bootstrap-local-db.sh after authz migrations to apply it.';
  END IF;
END $$;

-- invited_by FK to identity.users(id). Always safe: identity.users is
-- created by an earlier infra migration.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'platform.workspace_invites'::regclass
      AND conname = 'workspace_invites_invited_by_users_id_fk'
  ) THEN
    ALTER TABLE platform.workspace_invites
      ADD CONSTRAINT workspace_invites_invited_by_users_id_fk
      FOREIGN KEY (invited_by) REFERENCES identity.users(id);
  END IF;
END $$;

-- Grants: match the posture of sibling platform.* migrations
-- (20260424090000_workspace_admin_integrations.sql and
-- 20260513090000_universal_storage_platform_schema.sql both grant
-- read/write to `postgres` for consistency). Safe to replay — GRANT is
-- idempotent in Postgres.
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_invites TO postgres;
