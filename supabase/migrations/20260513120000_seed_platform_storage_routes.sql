-- STORAGE-3: Universal Object Storage gateway routes (forward-only)
--
-- Source of truth:
--   xynes/xynes-infra/docs/plans/2026-05-10-universal-object-storage-file-upload-api.md §7
--   xynes/xynes-storage-service/docs/api-contract.md
--
-- Forward-only migration. Supabase migration tooling tracks applied
-- migrations by filename, so any environment that already ran the prior
-- version of `20251229100001_seed_platform_routes.sql` will NOT replay
-- it after the storage rows are added there. This new timestamped
-- migration guarantees the eight storage routes land in every
-- environment that runs `supabase db reset` / `supabase db push`
-- from now on, matching the precedent set by
-- `20260508130000_seed_platform_domain_regenerate_route.sql`
-- (workspace-domain-verification-ux).
--
-- The companion edit to `20251229100001_seed_platform_routes.sql` is
-- preserved so that fresh-environment bootstraps from a clean slate
-- still see the storage routes inline with their workspace-admin
-- siblings (both files use ON CONFLICT (method, path_pattern) DO UPDATE,
-- so running both is idempotent and order-independent).
--
-- Routes target storage-service. All workspace-scoped, all non-public,
-- all enabled. Gateway env STORAGE_SERVICE_URL must be set before these
-- routes can actually proxy; until STORAGE-4 ships the storage-service
-- container the gateway returns 502 BAD_GATEWAY when matched (fail
-- closed posture).

INSERT INTO platform.routes
  (method, path_pattern, service_key, target_path, workspace_scoped, action_key, is_public, enabled)
VALUES
  ('POST',   '/workspaces/:workspaceId/storage/uploads',                          'storage-service', '/workspaces/:workspaceId/storage/uploads',                          true, 'platform.storage.objects.upload',         false, true),
  ('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/complete',       'storage-service', '/workspaces/:workspaceId/storage/uploads/:uploadId/complete',       true, 'platform.storage.objects.upload',         false, true),
  ('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/abort',          'storage-service', '/workspaces/:workspaceId/storage/uploads/:uploadId/abort',          true, 'platform.storage.objects.upload',         false, true),
  ('GET',    '/workspaces/:workspaceId/storage/objects',                          'storage-service', '/workspaces/:workspaceId/storage/objects',                          true, 'platform.storage.objects.read',           false, true),
  ('GET',    '/workspaces/:workspaceId/storage/objects/:objectId',                'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId',                true, 'platform.storage.objects.read',           false, true),
  ('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/download-url',   'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId/download-url',   true, 'platform.storage.objects.read',           false, true),
  ('DELETE', '/workspaces/:workspaceId/storage/objects/:objectId',                'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId',                true, 'platform.storage.objects.delete',         false, true),
  ('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/process/retry',  'storage-service', '/workspaces/:workspaceId/storage/objects/:objectId/process/retry',  true, 'platform.storage.objects.process.retry',  false, true)
ON CONFLICT (method, path_pattern) DO UPDATE SET
  service_key = EXCLUDED.service_key,
  target_path = EXCLUDED.target_path,
  workspace_scoped = EXCLUDED.workspace_scoped,
  action_key = EXCLUDED.action_key,
  is_public = EXCLUDED.is_public,
  enabled = EXCLUDED.enabled,
  updated_at = now();
