-- SEC-RATELIMIT-1: Initial rate limit configurations
-- These are sensible defaults for spam-prone routes

-- Note: This seed file assumes the routes table has been populated
-- with the initial routes. The route IDs referenced here must match
-- the IDs in the platform.routes table.

-- First, let's create a helper function to insert rate limits by path pattern
-- This makes the seed more resilient to route ID changes

DO $$
DECLARE
  v_comment_create_route_id uuid;
  v_blog_list_route_id uuid;
  v_content_list_route_id uuid;
  v_comment_list_route_id uuid;
BEGIN
  -- Get route IDs by path pattern (these are more stable than UUIDs)
  SELECT id INTO v_comment_create_route_id 
  FROM platform.routes 
  WHERE path_pattern = '/workspaces/:workspaceId/content-entries/:entryId/comments' 
    AND method = 'POST'
  LIMIT 1;

  SELECT id INTO v_blog_list_route_id 
  FROM platform.routes 
  WHERE path_pattern = '/workspaces/:workspaceId/blog' 
    AND method = 'GET'
  LIMIT 1;

  SELECT id INTO v_content_list_route_id 
  FROM platform.routes 
  WHERE path_pattern = '/workspaces/:workspaceId/content/:routeSegment' 
    AND method = 'GET'
  LIMIT 1;

  SELECT id INTO v_comment_list_route_id 
  FROM platform.routes 
  WHERE path_pattern = '/workspaces/:workspaceId/content-entries/:entryId/comments' 
    AND method = 'GET'
  LIMIT 1;

  -- Insert rate limit for comment creation (tightest limit - spam prone)
  IF v_comment_create_route_id IS NOT NULL THEN
    INSERT INTO platform.route_rate_limits (route_id, bucket_type, limit_count, window_sec, burst_factor, enabled)
    VALUES (v_comment_create_route_id, 'ip+workspace', 10, 60, 1.5, true)
    ON CONFLICT (route_id) DO UPDATE SET
      bucket_type = EXCLUDED.bucket_type,
      limit_count = EXCLUDED.limit_count,
      window_sec = EXCLUDED.window_sec,
      burst_factor = EXCLUDED.burst_factor,
      enabled = EXCLUDED.enabled,
      updated_at = now();
  END IF;

  -- Insert rate limit for blog listing (moderate limit)
  IF v_blog_list_route_id IS NOT NULL THEN
    INSERT INTO platform.route_rate_limits (route_id, bucket_type, limit_count, window_sec, burst_factor, enabled)
    VALUES (v_blog_list_route_id, 'ip', 60, 60, 1.2, true)
    ON CONFLICT (route_id) DO UPDATE SET
      bucket_type = EXCLUDED.bucket_type,
      limit_count = EXCLUDED.limit_count,
      window_sec = EXCLUDED.window_sec,
      burst_factor = EXCLUDED.burst_factor,
      enabled = EXCLUDED.enabled,
      updated_at = now();
  END IF;

  -- Insert rate limit for generic content listing (moderate limit)
  IF v_content_list_route_id IS NOT NULL THEN
    INSERT INTO platform.route_rate_limits (route_id, bucket_type, limit_count, window_sec, burst_factor, enabled)
    VALUES (v_content_list_route_id, 'ip', 60, 60, 1.2, true)
    ON CONFLICT (route_id) DO UPDATE SET
      bucket_type = EXCLUDED.bucket_type,
      limit_count = EXCLUDED.limit_count,
      window_sec = EXCLUDED.window_sec,
      burst_factor = EXCLUDED.burst_factor,
      enabled = EXCLUDED.enabled,
      updated_at = now();
  END IF;

  -- Insert rate limit for comment listing (moderate limit)
  IF v_comment_list_route_id IS NOT NULL THEN
    INSERT INTO platform.route_rate_limits (route_id, bucket_type, limit_count, window_sec, burst_factor, enabled)
    VALUES (v_comment_list_route_id, 'ip+workspace', 30, 60, 1.2, true)
    ON CONFLICT (route_id) DO UPDATE SET
      bucket_type = EXCLUDED.bucket_type,
      limit_count = EXCLUDED.limit_count,
      window_sec = EXCLUDED.window_sec,
      burst_factor = EXCLUDED.burst_factor,
      enabled = EXCLUDED.enabled,
      updated_at = now();
  END IF;

  -- Log results
  RAISE NOTICE 'Rate limit seed completed.';
  RAISE NOTICE 'Comment create route: %', v_comment_create_route_id;
  RAISE NOTICE 'Blog list route: %', v_blog_list_route_id;
  RAISE NOTICE 'Content list route: %', v_content_list_route_id;
  RAISE NOTICE 'Comment list route: %', v_comment_list_route_id;
END $$;
