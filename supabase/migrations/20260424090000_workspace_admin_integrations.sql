-- WORKSPACE-ADMIN-INTEGRATIONS-1: Platform-level verified domains and API keys
--
-- Stores global Workspace Admin integration primitives in the platform schema.
-- Secrets are represented only by hashes; raw API keys and DNS verification
-- values must never be persisted or logged by application code.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS platform;

-- -----------------------------------------------------------------------------
-- platform.workspace_domains
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform.workspace_domains (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  hostname text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'failed', 'disabled')),
  verification_method text NOT NULL DEFAULT 'dns_txt' CHECK (verification_method IN ('dns_txt')),
  verification_name text NOT NULL,
  verification_value_hash text NOT NULL,
  last_checked_at timestamptz,
  verified_at timestamptz,
  created_by uuid NOT NULL REFERENCES identity.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  failure_code text,
  failure_message text,

  CONSTRAINT workspace_domains_id_workspace_unique UNIQUE (id, workspace_id),
  CONSTRAINT workspace_domains_hostname_not_blank CHECK (btrim(hostname) <> ''),
  CONSTRAINT workspace_domains_verification_name_not_blank CHECK (btrim(verification_name) <> ''),
  CONSTRAINT workspace_domains_verification_value_hash_not_blank CHECK (btrim(verification_value_hash) <> ''),
  CONSTRAINT workspace_domains_hostname_lower CHECK (hostname = lower(hostname)),
  -- Pattern-literal rejection of URL/path/port/wildcard fragments. Uses
  -- `position(<frag> in hostname) = 0` (a character-substring test) instead
  -- of POSIX regex matching to avoid escape ambiguity around `*` — under
  -- `standard_conforming_strings = on`, the literal `'\\*'` is the regex
  -- pattern `\\*` ("zero or more backslashes"), which matches every input
  -- and was rejecting every hostname including `example.com`. See
  -- migration `20260508120000_fix_workspace_domains_hostname_shape.sql`.
  CONSTRAINT workspace_domains_hostname_shape CHECK (
    position('://' in hostname) = 0
    AND position('/' in hostname) = 0
    AND position(':' in hostname) = 0
    AND position('*' in hostname) = 0
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_domains_active_hostname_uidx
  ON platform.workspace_domains (hostname)
  WHERE status <> 'disabled';

CREATE UNIQUE INDEX IF NOT EXISTS workspace_domains_workspace_hostname_uidx
  ON platform.workspace_domains (workspace_id, hostname)
  WHERE status <> 'disabled';

CREATE INDEX IF NOT EXISTS workspace_domains_workspace_status_idx
  ON platform.workspace_domains (workspace_id, status, created_at DESC);

-- -----------------------------------------------------------------------------
-- platform.workspace_domain_bindings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform.workspace_domain_bindings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id uuid NOT NULL REFERENCES platform.workspace_domains(id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  app_key text NOT NULL,
  binding_type text NOT NULL,
  target_ref text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'pending')),
  created_by uuid NOT NULL REFERENCES identity.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT workspace_domain_bindings_domain_workspace_fkey
    FOREIGN KEY (domain_id, workspace_id)
    REFERENCES platform.workspace_domains(id, workspace_id)
    ON DELETE CASCADE,
  CONSTRAINT workspace_domain_bindings_app_key_not_blank CHECK (btrim(app_key) <> ''),
  CONSTRAINT workspace_domain_bindings_type_not_blank CHECK (btrim(binding_type) <> '')
);

CREATE INDEX IF NOT EXISTS workspace_domain_bindings_workspace_idx
  ON platform.workspace_domain_bindings (workspace_id, app_key, binding_type);

CREATE INDEX IF NOT EXISTS workspace_domain_bindings_domain_idx
  ON platform.workspace_domain_bindings (domain_id, status);

-- -----------------------------------------------------------------------------
-- platform.workspace_api_keys
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform.workspace_api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  name text NOT NULL,
  key_prefix text NOT NULL,
  key_hash text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired')),
  preset_key text,
  created_by uuid NOT NULL REFERENCES identity.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz,
  revoked_by uuid REFERENCES identity.users(id),
  last_used_at timestamptz,

  CONSTRAINT workspace_api_keys_name_not_blank CHECK (btrim(name) <> ''),
  CONSTRAINT workspace_api_keys_prefix_not_blank CHECK (btrim(key_prefix) <> ''),
  CONSTRAINT workspace_api_keys_hash_not_blank CHECK (btrim(key_hash) <> ''),
  CONSTRAINT workspace_api_keys_revoked_consistency CHECK (
    (status = 'revoked' AND revoked_at IS NOT NULL) OR status <> 'revoked'
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_api_keys_prefix_uidx
  ON platform.workspace_api_keys (key_prefix);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_api_keys_hash_uidx
  ON platform.workspace_api_keys (key_hash);

CREATE INDEX IF NOT EXISTS workspace_api_keys_workspace_idx
  ON platform.workspace_api_keys (workspace_id, status, created_at DESC);

-- -----------------------------------------------------------------------------
-- platform.workspace_api_key_scopes
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform.workspace_api_key_scopes (
  api_key_id uuid NOT NULL REFERENCES platform.workspace_api_keys(id) ON DELETE CASCADE,
  action_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (api_key_id, action_key),
  CONSTRAINT workspace_api_key_scopes_action_key_not_blank CHECK (btrim(action_key) <> '')
);

CREATE INDEX IF NOT EXISTS workspace_api_key_scopes_action_key_idx
  ON platform.workspace_api_key_scopes (action_key);

-- -----------------------------------------------------------------------------
-- Grants
-- -----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_domains TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_domain_bindings TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_api_keys TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_api_key_scopes TO postgres;

-- -----------------------------------------------------------------------------
-- Documentation
-- -----------------------------------------------------------------------------
COMMENT ON TABLE platform.workspace_domains IS 'Workspace-owned verified domains for global Workspace Admin integrations';
COMMENT ON COLUMN platform.workspace_domains.verification_value_hash IS 'Hash of the DNS verification value; raw verification tokens are not stored';
COMMENT ON TABLE platform.workspace_domain_bindings IS 'Contextual app bindings for verified workspace domains';
COMMENT ON TABLE platform.workspace_api_keys IS 'Workspace-scoped global API keys; only prefixes and key hashes are stored';
COMMENT ON COLUMN platform.workspace_api_keys.key_prefix IS 'Non-secret lookup/display prefix for an API key';
COMMENT ON COLUMN platform.workspace_api_keys.key_hash IS 'One-way hash of the full API key; raw keys are shown once by application code and never stored';
COMMENT ON TABLE platform.workspace_api_key_scopes IS 'Action-key based scopes granted to workspace API keys';
