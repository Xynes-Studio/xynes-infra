-- CMS-INT-A4: frozen v1 snapshot delivery routes (forward-only).
-- Apply after CMS publication migrations and deployed delivery handlers.
-- Restart the gateway to load the registry afterwards. This never changes existing key scopes.
INSERT INTO platform.routes
  (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  ('GET', '/workspaces/:workspaceId/delivery/entries', 'cms-core', '/internal/cms-actions', true, 'cms.delivery.listByDirectory', false, true),
  ('GET', '/workspaces/:workspaceId/delivery/entries/:entryId', 'cms-core', '/internal/cms-actions', true, 'cms.delivery.getById', false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
