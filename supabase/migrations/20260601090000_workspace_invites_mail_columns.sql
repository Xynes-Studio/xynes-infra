-- MAIL-3: Add `email_sent_at`, `email_attempts`, `last_email_error_code`
-- columns to `platform.workspace_invites`.
--
-- Plan: docs/plans/2026-05-30-invite-mail-delivery.md §7
-- Predecessors:
--   - 20260513140000_promote_workspace_invites_to_infra.sql
--     (WORKSPACE-INVITES-PROMOTE-1 — canonical table + constraints)
--
-- WHY THIS MIGRATION
--   MAIL-2 (xynes-accounts-service) shipped the `MailerClient` port
--   + `StubMailerClient` + DI hook but deliberately did NOT wire
--   `mailer.sendInvite(...)` into `accounts.invites.create`. MAIL-5
--   will wire the runtime call and needs three new columns on
--   `platform.workspace_invites` to record dispatch outcomes:
--
--     1. `email_sent_at`        — when the most recent successful
--                                  send happened. NULL means "no mail
--                                  has been successfully sent yet".
--     2. `email_attempts`       — total dispatch attempts (success
--                                  AND failure). MAIL-5's resend
--                                  handler reads this for the
--                                  rate-limit cap.
--     3. `last_email_error_code` — closed-set `MailerErrorCode` from
--                                  MAIL-2 (`RECIPIENT_INVALID`,
--                                  `PROVIDER_UNAVAILABLE`,
--                                  `RATE_LIMITED`,
--                                  `TEMPLATE_RENDER_FAILED`,
--                                  `PROVIDER_REJECTED`) or NULL if
--                                  the most recent attempt
--                                  succeeded.
--
--   These columns are purely observability + rate-limit state. The
--   raw token is unchanged; the redemption contract is unchanged;
--   the canonical row shape (`id`, `workspace_id`, `email`,
--   `role_key`, `invited_by`, `token`, `status`, `expires_at`,
--   `created_at`) is preserved byte-for-byte.
--
-- BACKWARD-COMPATIBILITY CONTRACT
--   - `email_sent_at` is nullable BY DESIGN — NULL is the documented
--     "no successful send yet" state for both pre-MAIL-3 rows AND
--     post-MAIL-3 rows whose dispatch errored or hasn't run yet.
--   - `email_attempts` is NOT NULL with `DEFAULT 0` — pre-MAIL-3 rows
--     auto-populate to 0 without violating the NOT NULL constraint
--     AND match the semantic "no attempts made for this invite".
--   - `last_email_error_code` is nullable BY DESIGN — NULL means "no
--     error on the most recent attempt" (or no attempt at all).
--   - Old-replica compatibility during a rolling deploy: pre-MAIL-3
--     code never references these columns, so it INSERTs without
--     them and the NOT NULL DEFAULT / nullable settings auto-populate.
--     Post-MAIL-3 code reads + writes all three. Safe in both
--     directions.
--   - The `accounts.invites.create` handler return shape is preserved
--     byte-for-byte (MAIL-5 will record dispatch state via a separate
--     UPDATE inside the handler, not by extending the return DTO).
--
-- SECURITY INVARIANTS
--   - `last_email_error_code` carries ONLY closed-set `MailerErrorCode`
--     values. The COMMENT documents the closed set so future
--     maintainers don't accidentally relax it into an open string
--     column. MAIL-5's handler MUST write only closed-set codes —
--     raw provider error text NEVER reaches this column.
--   - The migration adds NO indexes on these columns — operators
--     cannot accidentally enable a query path that turns
--     `last_email_error_code` into a side-channel for provider
--     diagnostics.
--   - NO RLS — auth is enforced by the gateway action-key system +
--     accounts-service handler workspace-ownership checks (same
--     posture as the rest of `platform.workspace_invites`).
--
-- DB SAFETY
--   - Strictly additive. NO destructive DDL (no table removal, no
--     column removal, no constraint removal, no table truncation,
--     no row deletion).
--   - `ADD COLUMN IF NOT EXISTS` → reset-safe + replay-safe.
--   - The two NOT NULL DEFAULTs use immutable scalar defaults so
--     Postgres 11+ rewrites them as a fast metadata-only operation
--     (no full table rewrite).
--   - Rollback: drop the three columns by name. The
--     xynes-accounts-service code change would need to roll back in
--     lockstep (Drizzle schema + MAIL-5 wiring). For an emergency
--     rollback of just the DB layer, pre-MAIL-3 code still works
--     because it never references these columns.

CREATE SCHEMA IF NOT EXISTS platform;

-- ============================================================================
-- Add `email_sent_at` — nullable timestamptz
-- ----------------------------------------------------------------------------
-- NULL is the documented "no mail successfully sent yet" state. MAIL-5
-- writes `now()` to this column ONLY on a successful `mailer.sendInvite`
-- return (no thrown `MailerError`). A failed attempt bumps
-- `email_attempts` and writes `last_email_error_code` but does NOT
-- touch `email_sent_at` — so the column captures the timestamp of the
-- most recent successful send.
-- ============================================================================
ALTER TABLE platform.workspace_invites
  ADD COLUMN IF NOT EXISTS email_sent_at timestamptz;

-- ============================================================================
-- Add `email_attempts` — non-null integer with fail-closed default 0
-- ----------------------------------------------------------------------------
-- Counts every dispatch attempt (success AND failure). MAIL-5 bumps
-- this atomically via `SET email_attempts = email_attempts + 1`.
-- Pre-MAIL-3 rows auto-populate to 0 — matching "no attempts made"
-- without violating NOT NULL.
--
-- MAIL-5's `accounts.invites.resend` action reads this column to
-- enforce the rate-limit cap (`MAIL_RESEND_MAX_ATTEMPTS`, default 5
-- per invite). The cap is per-invite and bounded by `expires_at`, so
-- a malicious caller cannot weaponise this column for storage growth.
-- ============================================================================
ALTER TABLE platform.workspace_invites
  ADD COLUMN IF NOT EXISTS email_attempts integer NOT NULL DEFAULT 0;

-- ============================================================================
-- Add `last_email_error_code` — nullable text, closed-set surface
-- ----------------------------------------------------------------------------
-- Carries ONLY the most recent `MailerError.code` value from MAIL-2's
-- closed set:
--   - `RECIPIENT_INVALID`     (non-retryable, recipient validation failed)
--   - `PROVIDER_UNAVAILABLE`  (retryable, transient transport failure)
--   - `RATE_LIMITED`          (retryable, provider quota exhausted)
--   - `TEMPLATE_RENDER_FAILED` (non-retryable, template field missing)
--   - `PROVIDER_REJECTED`     (non-retryable, permanent provider rejection)
--
-- Rationale for `text` (not a CHECK CONSTRAINT or enum):
--   The MAIL-2 closed set is a TS-only contract, and tightening the DB
--   to a CHECK would force every future code change adding a new
--   `MailerErrorCode` through a migration. The COMMENT below documents
--   the closed-set contract; MAIL-5's handler is the gate that
--   enforces it. A future story may tighten this column if the
--   closed-set churn calms down — explicitly deferred per plan §13 Q3.
--
-- NULL is the documented "no error on the most recent attempt" state
-- (which includes "no attempt at all"). On success, MAIL-5 clears the
-- column back to NULL.
-- ============================================================================
ALTER TABLE platform.workspace_invites
  ADD COLUMN IF NOT EXISTS last_email_error_code text;

-- ============================================================================
-- Documentation comments
-- ============================================================================
COMMENT ON COLUMN platform.workspace_invites.email_sent_at IS
  'MAIL-3: timestamp of the most recent SUCCESSFUL mailer.sendInvite return. NULL means "no mail has been successfully sent for this invite yet" (either pre-MAIL-3, or never attempted, or the most recent attempt failed). MAIL-5 writes now() on successful dispatch only.';

COMMENT ON COLUMN platform.workspace_invites.email_attempts IS
  'MAIL-3: total mailer dispatch attempts (success + failure). Incremented atomically by MAIL-5 on every send. MAIL-5 accounts.invites.resend reads this for the rate-limit cap (MAIL_RESEND_MAX_ATTEMPTS, default 5).';

COMMENT ON COLUMN platform.workspace_invites.last_email_error_code IS
  'MAIL-3: closed-set MailerError code from MAIL-2 (RECIPIENT_INVALID, PROVIDER_UNAVAILABLE, RATE_LIMITED, TEMPLATE_RENDER_FAILED, PROVIDER_REJECTED) or NULL if the most recent attempt succeeded (or no attempt at all). Raw provider error text MUST NEVER reach this column — MAIL-5 writes only the closed-set code from the caught MailerError.code field.';
