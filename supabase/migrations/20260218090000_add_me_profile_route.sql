-- PLATFORM-ROUTES-3: Add self profile update route for auth users
--
-- Adds PATCH /me/profile -> accounts.user.updateSelf
-- Keeps migration idempotent via method+path_pattern upsert.

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
  'PATCH',
  '/me/profile',
  'accounts-service',
  '/me/profile',
  false,
  'accounts.user.updateSelf',
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
