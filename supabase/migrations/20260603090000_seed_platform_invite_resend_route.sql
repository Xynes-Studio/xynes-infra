-- MAIL-5: workspace-invite resend gateway route (forward-only seed)
--
-- Adds a gateway route for the new `accounts.invites.resend` action.
-- The action re-dispatches a pending workspace invite email and rotates
-- the invite token in a single SQL UPDATE (see
-- xynes-accounts-service/src/actions/handlers/invites/resend.ts).
--
-- Why this is a separate timestamped migration (and NOT an edit to the
-- baseline seed 20251229100001_seed_platform_routes.sql):
--
--   Supabase tracks applied migrations by filename in
--   supabase_migrations.schema_migrations. Once 20251229100001 has run
--   on an environment, the CLI never replays it — even if its content
--   changes. Editing the baseline only seeds fresh databases. Every
--   environment that has previously bootstrapped (dev / staging / prod
--   and every developer machine) would silently miss the new row, and
--   the gateway (fail-closed + DB-backed per AGENTS.md "Critical
--   invariants") would 404 every POST to
--   /workspaces/:workspaceId/invites/:inviteId/resend.
--
--   The repo precedent for this exact pattern is
--   20260508130000_seed_platform_domain_regenerate_route.sql, which
--   added a single platform.routes row for the
--   platform.domains.regenerateVerification action via a forward-only
--   migration after the same Codex review feedback on the
--   workspace-domain-verification UX work.
--
-- Idempotent: ON CONFLICT (method, path_pattern) DO UPDATE matches the
-- pattern used by 20251229100001_seed_platform_routes.sql so a fresh
-- DB reset (which applies BOTH this file and the baseline seed in
-- timestamp order) ends in the same state regardless of order, and a
-- replay against an already-seeded environment UPSERTs only the
-- value-columns (service_key, target_path, workspace_scoped,
-- action_key, is_public, enabled, updated_at) without touching the
-- conflict key (method, path_pattern).

INSERT INTO platform.routes
  (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  ('POST', '/workspaces/:workspaceId/invites/:inviteId/resend', 'accounts-service', '/workspaces/:workspaceId/invites/:inviteId/resend', true, 'accounts.invites.resend', false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
