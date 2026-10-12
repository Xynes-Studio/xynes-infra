-- PLATFORM-ROUTES-2: Seed initial routes
--
-- Source of truth: xynes-platform-config/src/seeds/routes.ts
-- Keep this seed idempotent and stable (use method+path_pattern as natural key).

INSERT INTO platform.routes (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  -- Accounts
  ('GET',  '/me', 'accounts-service', '/me', false, 'accounts.me.getOrCreate', false, true),

  -- Workspaces
  ('GET',  '/workspaces', 'accounts-service', '/workspaces', false, 'accounts.workspaces.listForUser', false, true),
  ('POST', '/workspaces', 'accounts-service', '/workspaces', false, 'accounts.workspaces.create', false, true),

  -- Invites
  ('POST', '/workspaces/:workspaceId/invites', 'accounts-service', '/workspaces/:workspaceId/invites', true, 'accounts.invites.create', false, true),
  ('GET',  '/workspace-invites/:token', 'accounts-service', '/workspace-invites/:token', false, 'accounts.invites.resolve', true, true),
  ('POST', '/workspace-invites/:token/accept', 'accounts-service', '/workspace-invites/:token/accept', false, 'accounts.invites.accept', false, true),

  -- Documents
  ('POST', '/workspaces/:workspaceId/documents', 'doc-service', '/workspaces/:workspaceId/documents', true, 'docs.document.create', false, true),
  ('GET',  '/workspaces/:workspaceId/documents/:id', 'doc-service', '/workspaces/:workspaceId/documents/:id', true, 'docs.document.read', false, true),

  -- Blog
  ('GET',  '/workspaces/:workspaceId/blog', 'cms-core', '/workspaces/:workspaceId/blog', true, 'cms.blog_entry.listPublished', true, true),
  ('GET',  '/workspaces/:workspaceId/blog/:slug', 'cms-core', '/workspaces/:workspaceId/blog/:slug', true, 'cms.blog_entry.getPublishedBySlug', true, true),

  -- Generic content
  ('GET',  '/workspaces/:workspaceId/content/:routeSegment', 'cms-core', '/workspaces/:workspaceId/content/:routeSegment', true, 'cms.content.listPublished', true, true),
  ('GET',  '/workspaces/:workspaceId/content/:routeSegment/:slug', 'cms-core', '/workspaces/:workspaceId/content/:routeSegment/:slug', true, 'cms.content.getPublishedBySlug', true, true),

  -- Comments
  ('POST', '/workspaces/:workspaceId/content-entries/:entryId/comments', 'cms-core', '/workspaces/:workspaceId/content-entries/:entryId/comments', true, 'cms.comments.create', true, true),
  ('GET',  '/workspaces/:workspaceId/content-entries/:entryId/comments', 'cms-core', '/workspaces/:workspaceId/content-entries/:entryId/comments', true, 'cms.comments.listForEntry', true, true),

  -- Telemetry
  ('GET',  '/workspaces/:workspaceId/telemetry/events', 'telemetry-service', '/workspaces/:workspaceId/telemetry/events', true, 'telemetry.events.listRecentForWorkspace', false, true),
  ('GET',  '/workspaces/:workspaceId/telemetry/stats/routes', 'telemetry-service', '/workspaces/:workspaceId/telemetry/stats/routes', true, 'telemetry.stats.summaryByRoute', false, true),

  -- Workspace Admin: Domains
  ('GET',    '/workspaces/:workspaceId/domains', 'accounts-service', '/workspaces/:workspaceId/domains', true, 'platform.domains.list', false, true),
  ('POST',   '/workspaces/:workspaceId/domains', 'accounts-service', '/workspaces/:workspaceId/domains', true, 'platform.domains.create', false, true),
  ('POST',   '/workspaces/:workspaceId/domains/:domainId/verify', 'accounts-service', '/workspaces/:workspaceId/domains/:domainId/verify', true, 'platform.domains.verify', false, true),
  ('POST',   '/workspaces/:workspaceId/domains/:domainId/regenerate-verification', 'accounts-service', '/workspaces/:workspaceId/domains/:domainId/regenerate-verification', true, 'platform.domains.regenerateVerification', false, true),
  ('DELETE', '/workspaces/:workspaceId/domains/:domainId', 'accounts-service', '/workspaces/:workspaceId/domains/:domainId', true, 'platform.domains.delete', false, true),

  -- Workspace Admin: API Keys
  ('GET',  '/workspaces/:workspaceId/api-keys', 'accounts-service', '/workspaces/:workspaceId/api-keys', true, 'platform.api_keys.list', false, true),
  ('POST', '/workspaces/:workspaceId/api-keys', 'accounts-service', '/workspaces/:workspaceId/api-keys', true, 'platform.api_keys.create', false, true),
  ('POST', '/workspaces/:workspaceId/api-keys/:keyId/revoke', 'accounts-service', '/workspaces/:workspaceId/api-keys/:keyId/revoke', true, 'platform.api_keys.revoke', false, true),
  ('GET',  '/workspaces/:workspaceId/api-keys/:keyId/usage', 'accounts-service', '/workspaces/:workspaceId/api-keys/:keyId/usage', true, 'platform.api_keys.usage.read', false, true),

  -- Universal Object Storage (STORAGE-3)
  --
  -- Source of truth:
  --   xynes/xynes-infra/docs/plans/2026-05-10-universal-object-storage-file-upload-api.md §7
  --   xynes/xynes-storage-service/docs/api-contract.md
  --
  -- Routes target storage-service. All workspace-scoped, all non-public.
  -- Gateway env STORAGE_SERVICE_URL must be set before these routes
  -- can actually proxy; until then the gateway returns 502 BAD_GATEWAY
  -- when matched (fail closed).
  ('POST',   '/workspaces/:workspaceId/storage/uploads', 'storage-service', '/workspaces/:workspaceId/storage/uploads', true, 'platform.storage.objects.upload', false, true),
  ('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/complete', 'storage-service', '/workspaces/:workspaceId/storage/uploads/:uploadId/complete', true, 'platform.storage.objects.upload', false, true),
  ('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/abort', 'storage-service', '/workspaces/:workspaceId/storage/uploads/:uploadId/abort', true, 'platform.storage.objects.upload', false, true),
  ('GET',    '/workspaces/:workspaceId/storage/objects', 'storage-service', '/workspaces/:workspaceId/storage/objects', true, 'platform.storage.objects.read', false, true),
  ('GET',    '/workspaces/:workspaceId/storage/objects/:objectId', 'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId', true, 'platform.storage.objects.read', false, true),
  ('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/download-url', 'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId/download-url', true, 'platform.storage.objects.read', false, true),
  ('DELETE', '/workspaces/:workspaceId/storage/objects/:objectId', 'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId', true, 'platform.storage.objects.delete', false, true),
  ('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/process/retry', 'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId/process/retry', true, 'platform.storage.objects.process.retry', false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();

