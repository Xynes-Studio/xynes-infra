-- CMS-DASHBOARD-ROUTES-1: Seed directory-first CMS dashboard routes.
--
-- Dashboard authoring flows use cms.entry.* and cms.content_directories.*
-- actions. Keep these rows idempotent so local Supabase resets restore the
-- CMS console route registry without manual DB edits.

INSERT INTO platform.routes (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  -- CMS content directories
  ('GET',    '/workspaces/:workspaceId/content-directories', 'cms-core', '/workspaces/:workspaceId/content-directories', true, 'cms.content_directories.listForWorkspace', false, true),
  ('POST',   '/workspaces/:workspaceId/content-directories', 'cms-core', '/workspaces/:workspaceId/content-directories', true, 'cms.content_directories.create', false, true),
  ('PATCH',  '/workspaces/:workspaceId/content-directories/:directoryId', 'cms-core', '/workspaces/:workspaceId/content-directories/:directoryId', true, 'cms.content_directories.update', false, true),
  ('DELETE', '/workspaces/:workspaceId/content-directories/:directoryId', 'cms-core', '/workspaces/:workspaceId/content-directories/:directoryId', true, 'cms.content_directories.delete', false, true),

  -- CMS directory-first entries
  ('GET',    '/workspaces/:workspaceId/content/entries', 'cms-core', '/workspaces/:workspaceId/content/entries', true, 'cms.entry.listByDirectory', false, true),
  ('POST',   '/workspaces/:workspaceId/content/entries', 'cms-core', '/workspaces/:workspaceId/content/entries', true, 'cms.entry.create', false, true),
  ('GET',    '/workspaces/:workspaceId/content/entries/:entryId', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId', true, 'cms.entry.getById', false, true),
  ('PATCH',  '/workspaces/:workspaceId/content/entries/:entryId', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId', true, 'cms.entry.update', false, true),
  ('DELETE', '/workspaces/:workspaceId/content/entries/:entryId', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId', true, 'cms.entry.delete', false, true),
  ('POST',   '/workspaces/:workspaceId/content/entries/:entryId/publish', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId/publish', true, 'cms.entry.publish', false, true),
  ('POST',   '/workspaces/:workspaceId/content/entries/:entryId/status', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId/status', true, 'cms.entry.status.set', false, true),
  ('PUT',    '/workspaces/:workspaceId/content/entries/:entryId/collaborators', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId/collaborators', true, 'cms.entry.collaborators.set', false, true),
  ('POST',   '/workspaces/:workspaceId/content/entries/:entryId/favorite', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId/favorite', true, 'cms.entry.favorite.toggle', false, true),
  ('GET',    '/workspaces/:workspaceId/content/entries/favorites', 'cms-core', '/workspaces/:workspaceId/content/entries/favorites', true, 'cms.entry.favorite.list', false, true),
  ('POST',   '/workspaces/:workspaceId/content/entries/:entryId/share-link', 'cms-core', '/workspaces/:workspaceId/content/entries/:entryId/share-link', true, 'cms.entry.share.generateInternalLink', false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
