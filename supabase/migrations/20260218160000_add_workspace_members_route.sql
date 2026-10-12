-- PLATFORM-ROUTES-4: Ensure workspace members route exists for directory UI
--
-- Adds GET /workspaces/:workspaceId/members ->
-- accounts.workspace_members.listForWorkspace
-- Idempotent via method+path_pattern upsert.

INSERT INTO platform.routes (
  method,
  path_pattern,
  service_key,
  target_path,
  workspace_scoped,
  action_key,
  is_public,
  enabled
)
VALUES (
  'GET',
  '/workspaces/:workspaceId/members',
  'accounts-service',
  '/workspaces/:workspaceId/members',
  true,
  'accounts.workspace_members.listForWorkspace',
  false,
  true
)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
