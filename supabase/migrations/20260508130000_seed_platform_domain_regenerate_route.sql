-- WORKSPACE-DOMAIN-VERIFICATION-UX: regenerate-verification gateway route
--
-- Adds a gateway route for the new `platform.domains.regenerateVerification`
-- action. The action issues a fresh DNS TXT verification token for an
-- existing pending/failed domain row when the workspace owner has lost
-- the original one-time reveal. The server stores ONLY the SHA-256 hash
-- of the token (per the workspace-admin-integrations epic invariant), so
-- recopying an already-revealed value is impossible by design — this
-- route is the supported recovery path.
--
-- Idempotent: ON CONFLICT (method, path_pattern) DO UPDATE matches the
-- pattern used by 20251229100001_seed_platform_routes.sql.

INSERT INTO platform.routes
  (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  ('POST', '/workspaces/:workspaceId/domains/:domainId/regenerate-verification', 'accounts-service', '/workspaces/:workspaceId/domains/:domainId/regenerate-verification', true, 'platform.domains.regenerateVerification', false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
