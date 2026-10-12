-- WORKSPACE-DOMAIN-VERIFICATION-UX: dns_records_found column
--
-- Adds a count-only diagnostic column to platform.workspace_domains so
-- the verify handler can surface a categorized failure breakdown to the
-- frontend without echoing raw TXT record values back. The frontend
-- renders a 3-step diagnostic strip ("DNS lookup → ✓/✗", "TXT records:
-- N", "Value match → ✓/✗") driven by `failure_code` + `dns_records_found`.
--
-- Security invariant: this column stores ONLY a count. The raw TXT
-- record values returned by the resolver could be attacker-supplied
-- content from a hostile DNS zone (XSS surface, log noise, audit
-- confusion when reading someone else's records). Counts are
-- sufficient to drive the diagnostic UI; raw values are not needed.
--
-- Forward-only: the column is nullable with no default, so existing rows
-- (where verification has never been attempted) read as NULL → the FE
-- renders the strip in an "unknown" state. New verify attempts populate
-- it. Idempotent via IF NOT EXISTS.

ALTER TABLE platform.workspace_domains
  ADD COLUMN IF NOT EXISTS dns_records_found integer;

COMMENT ON COLUMN platform.workspace_domains.dns_records_found IS
  'Count of TXT records returned by the resolver in the most recent verify attempt. NULL when no attempt has been made or DNS lookup itself errored. NEVER stores raw TXT record values.';
