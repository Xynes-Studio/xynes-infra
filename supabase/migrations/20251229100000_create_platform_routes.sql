-- PLATFORM-ROUTES-1: Create platform.routes table
--
-- platform.routes is the gateway's route policy store (auth/public/workspace scoping).
-- It is intentionally separate from application data tables.

CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE IF NOT EXISTS platform.routes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  method text NOT NULL,
  path_pattern text NOT NULL,
  service_key text NOT NULL,
  target_path text NOT NULL,
  workspace_scoped boolean NOT NULL DEFAULT true,
  action_key text,
  is_public boolean NOT NULL DEFAULT false,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT routes_method_path_unique UNIQUE (method, path_pattern)
);

CREATE INDEX IF NOT EXISTS idx_routes_service_key ON platform.routes (service_key);
CREATE INDEX IF NOT EXISTS idx_routes_enabled ON platform.routes (enabled) WHERE enabled = true;

GRANT SELECT, INSERT, UPDATE, DELETE ON platform.routes TO postgres;

COMMENT ON TABLE platform.routes IS 'Gateway route policy store (service/action mapping + scoping)';
COMMENT ON COLUMN platform.routes.path_pattern IS 'Path pattern, e.g. /workspaces/:workspaceId/documents/:id';
COMMENT ON COLUMN platform.routes.service_key IS 'Downstream service key, e.g. accounts-service';
COMMENT ON COLUMN platform.routes.action_key IS 'AuthZ action key for authorization checks (if applicable)';
COMMENT ON COLUMN platform.routes.is_public IS 'Whether route can be accessed without Authorization';
