-- Hosted gateway body-limit contract, formerly only in the local bootstrap.
-- Additive and nullable: existing routes retain the gateway default.
ALTER TABLE platform.routes ADD COLUMN IF NOT EXISTS max_body_bytes integer;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'platform.routes'::regclass
      AND conname = 'max_body_bytes_non_negative'
  ) THEN
    ALTER TABLE platform.routes
      ADD CONSTRAINT max_body_bytes_non_negative
      CHECK (max_body_bytes IS NULL OR max_body_bytes >= 0);
  END IF;
END $$;

COMMENT ON COLUMN platform.routes.max_body_bytes IS
  'SEC-BODYLIMIT-1: Maximum allowed request body size in bytes. NULL = use default (1MB), 0 = reject bodies.';
