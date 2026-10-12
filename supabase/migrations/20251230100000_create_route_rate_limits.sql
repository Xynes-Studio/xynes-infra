-- SEC-RATELIMIT-1: Create route_rate_limits table for dynamic rate limiting
-- This table stores rate limit configurations per route

-- Ensure the platform schema exists (should already exist from previous migrations)
CREATE SCHEMA IF NOT EXISTS platform;

-- Create the route_rate_limits table
CREATE TABLE IF NOT EXISTS platform.route_rate_limits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id uuid NOT NULL REFERENCES platform.routes(id) ON DELETE CASCADE,
  bucket_type text NOT NULL CHECK (bucket_type IN ('ip', 'workspace', 'user', 'ip+workspace', 'ip+user')),
  limit_count integer NOT NULL CHECK (limit_count > 0),
  window_sec integer NOT NULL CHECK (window_sec > 0),
  burst_factor numeric NOT NULL DEFAULT 1.0 CHECK (burst_factor >= 1.0),
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  
  -- For v1, we allow one rate limit config per route (can be extended to multiple later)
  CONSTRAINT route_rate_limits_route_id_unique UNIQUE (route_id)
);

-- Create index for faster lookups by route_id
CREATE INDEX IF NOT EXISTS idx_route_rate_limits_route_id ON platform.route_rate_limits(route_id);

-- Create index for enabled lookups (common query pattern)
CREATE INDEX IF NOT EXISTS idx_route_rate_limits_enabled ON platform.route_rate_limits(enabled) WHERE enabled = true;

-- Grant permissions to postgres role
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.route_rate_limits TO postgres;

-- Add comment for documentation
COMMENT ON TABLE platform.route_rate_limits IS 'Stores rate limit configurations per route for dynamic rate limiting in gateway';
COMMENT ON COLUMN platform.route_rate_limits.bucket_type IS 'Rate limit key type: ip, workspace, user, ip+workspace, ip+user';
COMMENT ON COLUMN platform.route_rate_limits.limit_count IS 'Maximum number of requests allowed in the time window';
COMMENT ON COLUMN platform.route_rate_limits.window_sec IS 'Time window in seconds for the rate limit';
COMMENT ON COLUMN platform.route_rate_limits.burst_factor IS 'Multiplier for allowing burst traffic (1.0 = no burst)';
COMMENT ON COLUMN platform.route_rate_limits.enabled IS 'Whether this rate limit is active';
