-- WORKSPACE-ADMIN-INTEGRATIONS-FIX: workspace_domains_hostname_shape regex bug
--
-- The original migration `20260424090000_workspace_admin_integrations.sql`
-- defined the `workspace_domains_hostname_shape` CHECK constraint as:
--
--   CONSTRAINT workspace_domains_hostname_shape CHECK (
--     hostname !~ '://' AND hostname !~ '/' AND hostname !~ ':' AND hostname !~ '\\*'
--   )
--
-- The intent was to reject any hostname containing `://`, `/`, `:`, or `*`.
-- The first three patterns work as intended (no regex metachars), but the
-- fourth pattern is broken: with `standard_conforming_strings = on` (the
-- PostgreSQL default since 9.1), the SQL string literal `'\\*'` is a
-- 3-character string `\\*` whose POSIX-regex meaning is "zero or more
-- backslashes". That pattern matches the empty string anywhere in any
-- input, so `'example.com' ~ '\\*'` is TRUE → `!~` is FALSE → the
-- constraint rejects EVERY hostname, including the obviously valid ones.
--
-- This migration drops the broken constraint and re-adds it using
-- `position(<frag> in hostname) = 0` (pattern-literal substring tests).
-- These are character-literal matches, no regex engine, no escape
-- ambiguity, identical behaviour regardless of
-- `standard_conforming_strings`.
--
-- Forward-only: the original migration file has also been updated so a
-- fresh `supabase db reset` produces the correct constraint directly.
-- This migration is for environments where the original migration has
-- already been applied with the broken constraint.
--
-- DB safety: no data rebuild required. The broken constraint rejected
-- every insert, so `platform.workspace_domains` is empty in every
-- affected environment. The `DROP … IF EXISTS` form is idempotent and
-- safe if a previous attempt to fix this already ran.

ALTER TABLE platform.workspace_domains
  DROP CONSTRAINT IF EXISTS workspace_domains_hostname_shape;

ALTER TABLE platform.workspace_domains
  ADD CONSTRAINT workspace_domains_hostname_shape CHECK (
    position('://' in hostname) = 0
    AND position('/' in hostname) = 0
    AND position(':' in hostname) = 0
    AND position('*' in hostname) = 0
  );
