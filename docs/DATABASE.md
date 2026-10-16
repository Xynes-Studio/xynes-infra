# Xynes Database Configuration — Canonical Schema Ownership and Bootstrap Reference

> **Status:** Stable
> **Last revalidated:** 2026-05-13
> **Owner:** `xynes/xynes-infra`
> **Audience:** All Xynes backend services and infra contributors

This document is the **single source of truth** for:

1. Which Postgres schema each backend service owns.
2. Which migration tool runs against each schema.
3. The order in which migrations and bootstrap steps must run on a fresh local stack.
4. Cross-schema foreign-key dependencies and how to recover when they cannot be applied immediately.

If you change a schema, a migration tool, or the bootstrap order, **update this file in the same PR**. AGENTS.md and individual repo DEVELOPER.md files point here for the source of truth.

---

## 1. Schema ownership map

Every Postgres schema in the Xynes platform DB has exactly **one** owning repo. Read access across schemas is allowed via FK references; write access is reserved to the owner.

| Schema | Owner repo | Migration tool | Key tables | Notes |
|---|---|---|---|---|
| `identity` | `xynes/xynes-infra` | Supabase CLI (`supabase/migrations/`) | `identity.users` | Created by `20251219133000_accounts_schema_1_identity_workspace_core.sql`. |
| `platform` | `xynes/xynes-infra` | Supabase CLI (`supabase/migrations/`) | `routes`, `route_rate_limits`, `workspaces`, `workspace_members`, `workspace_invites`, `workspace_domains`, `workspace_api_keys`, `workspace_storage_providers`, `storage_objects`, `storage_upload_sessions`, `storage_processing_jobs`, `storage_object_variants`, `storage_usage_daily` | Shared platform-level tables. `xynes-platform-config` defines a Drizzle `pgSchema("platform")` view for type access but does **not** own migrations. |
| `authz` | `xynes/xynes-authz-service` | Drizzle (`src/db/migrations/`) | `authz.roles`, `authz.permissions`, `authz.user_roles`, etc. | Owns the RBAC catalog. `platform.workspace_invites.role_key` FK references `authz.roles(key)` — see §4 for ordering. |
| `cms` | `xynes/xynes-cms-core` | Drizzle (`drizzle/`) | `cms.content_entries`, `cms.content_directories`, `cms.content_types`, `cms.global_content_templates`, `cms.content_entry_collaborators`, `cms.content_entry_favorites`, etc. | Service owns full lifecycle. |
| `cms_migration_backups` | `xynes/xynes-cms-core` | Drizzle | internal | CMS Drizzle migration safety net; never queried by other services. |
| `docs` | `xynes/xynes-doc-service` | Drizzle (`drizzle/`) | `docs.documents` | Service-owned. Until 2026-05-13 the bootstrap also created this table via psql heredoc; that duplication was removed in favour of delegating to `drizzle-kit migrate`. |
| `telemetry` | `xynes/xynes-telemetry-service` | Drizzle (`src/db/migrations/`) | `telemetry.events`, etc. | Service-owned. |
| `comments` | (legacy) | Supabase CLI | (none active) | Created by an old infra migration; effectively dormant. Do not add new tables here without a deprecation plan. |
| `accounts` (logical) | `xynes/xynes-accounts-service` | Drizzle (`drizzle/`) | Owns no schema of its own — operates on `identity.users`, `platform.workspaces`, `platform.workspace_members`, `platform.workspace_invites`, `platform.workspace_domains`, `platform.workspace_api_keys`. | Has Drizzle migrations that mostly add indexes/columns to the `platform.*` tables co-owned with infra. Treat `xynes-infra` as the canonical schema authority for those tables. |

### Non-owners and special cases

- **`xynes-gateway`** owns no schema. Reads `platform.routes` (set by infra migrations) and `platform.workspace_api_keys` (set by infra migrations).
- **`xynes-storage-service`** owns no schema. Reads/writes `platform.storage_*` tables created by infra migrations (specifically `20260513090000_universal_storage_platform_schema.sql`). This is intentional — the storage data plane lives on platform infra so the service can be redeployed without DB-side coordination.
- **`xynes-platform-contracts`** has no DB role at all.

---

## 2. Migration tooling per repo

| Repo | Tool | Command (host-side) |
|---|---|---|
| `xynes-infra` | Supabase CLI | `cd xynes-infra/supabase && supabase db reset --yes` (replays everything under `supabase/migrations/`) |
| `xynes-authz-service` | Drizzle | `cd xynes-authz-service && bun run migrate` |
| `xynes-cms-core` | Drizzle (via wrapper) | `cd xynes-cms-core && bun --env-file=<env> run src/infra/db/migrate.ts` |
| `xynes-doc-service` | Drizzle | `cd xynes-doc-service && bun x drizzle-kit migrate` |
| `xynes-telemetry-service` | Drizzle | `cd xynes-telemetry-service && bun run migrate` |
| `xynes-accounts-service` | Drizzle | `cd xynes-accounts-service && bun run migrate` |
| `xynes-platform-config` | Drizzle (type-only, read paths) | typically no host-side migration step — the service reads `platform.*` from infra |

**Rule:** Do not invoke another service's migration tool directly from a foreign repo unless the call is documented in `scripts/bootstrap-local-db.sh`. The bootstrap is the single coordinator for cross-tool ordering.

---

## 3. Canonical local bootstrap path

Fresh local stack from zero to fully usable:

```bash
# 1. Start the local Supabase stack (Postgres + Studio + auth + storage)
cd xynes/xynes-infra
bash ./scripts/supabase-local.sh start

# 2. Reset + bootstrap (replays ALL migrations, runs the canonical coordinator)
bash ./scripts/supabase-local.sh reset
```

The `reset` step internally runs:

1. **`supabase db reset --yes`** — replays every `supabase/migrations/*.sql` file in timestamp order. This creates:
   - `identity` schema + `identity.users`
   - `platform` schema + all infra-owned tables (routes, workspaces, workspace_invites, domains, api_keys, storage_*, etc.)
   - `docs`, `cms`, `cms_migration_backups`, `telemetry`, `comments` **as empty schemas** (the corresponding tables are owned by service repos and applied next)

2. **`bootstrap-local-db.sh --env-file .env.localhost.local --restart-gateway`** — the canonical coordinator. In order:
   1. Apply `xynes-doc-service` Drizzle migrations → `docs.documents`
   2. Apply `xynes-cms-core` Drizzle migrations → all `cms.*` tables
   2a. Apply `xynes-authz-service` Drizzle migrations → all `authz.*` tables AND seed the RBAC catalog (5 roles, 62 permissions, 198 role-permission mappings) via `src/scripts/seed.ts`
   3. Seed CMS defaults for every existing `platform.workspaces` row (`xynes-cms-core/src/infra/db/seed.ts`)
   3b. Re-apply the STORAGE-LIVE-2 R2 dev `platform.workspace_storage_providers` seed (idempotent UPSERT — no-op when there are no workspaces yet)
   4. Apply the `workspace_invites_role_key_roles_key_fk` to `platform.workspace_invites` (now unconditional in the happy path because step 2a guarantees `authz.roles` exists; the `IF EXISTS` guard is retained as defense in depth for recovery flows — see §4)
   5. Apply `platform.routes.max_body_bytes` column + non-negative check (for the gateway's body limit feature)
   6. Verify the authz catalog is seeded (exactly 5 roles, ≥1 permission, ≥1 role-permission mapping) — defense-in-depth gate so a regression in step 2a fails the bootstrap loudly instead of producing a stack where `POST /workspaces` silently 502s on role assignment
   7. Verify reset-critical CMS tables and dashboard routes are present
   8. Verify the R2 dev `platform.workspace_storage_providers` row landed for each workspace
   9. Restart the gateway container so it reloads DB-backed routes

The bootstrap script is **idempotent**: every CREATE uses `IF NOT EXISTS`, every constraint addition is guarded by a `pg_constraint` NOT EXISTS lookup, every seed checks for presence before writing.

### What runs WHERE (data plane)

`bootstrap-local-db.sh` runs against the **host-side Postgres** (port 5432) using `DATABASE_URL` from the env-file. Service containers (gateway, cms-core, accounts-service, etc.) connect to the same Postgres via the container-internal hostname `db.local` / `host.docker.internal`, which the script's `normalize_database_url_for_host` rewrites to `127.0.0.1` for host tooling.

### What the bootstrap does NOT do

- Does NOT run `xynes-telemetry-service` Drizzle migrations. The telemetry service applies its own on first boot.
- Does NOT run `xynes-accounts-service` Drizzle migrations. The accounts service applies its own; the infra Supabase migrations already create every table the accounts service writes to.

> **Note (2026-05-27):** Until 2026-05-27 this list also called out that the bootstrap did NOT run `xynes-authz-service` Drizzle migrations and that operators had to invoke `bun run migrate` + `bun run seed` from the authz repo by hand. That gap caused fresh local resets to silently break `POST /workspaces` (creating a workspace 502'd inside `accounts.workspaces.create` when the role-assignment call hit a non-existent `authz.roles` table). The bootstrap now owns step 2a — running authz migrations and the catalog seed — and verifies the catalog at step 6. The cross-tool FK ordering described in §4 still holds, but the happy-path branch is now always taken.

---

## 4. Cross-schema FK ordering — the `authz.roles` gap

`platform.workspace_invites.role_key` references `authz.roles(key)`. The `platform.workspace_invites` table is owned by infra Supabase migrations; `authz.roles` is owned by `xynes-authz-service` Drizzle migrations. These run in two different tools with no implicit ordering between them.

**Without coordination, a fresh `supabase db reset` followed by bootstrap would hard-fail when adding the FK** because `authz.roles` doesn't exist yet.

The system handles this with three layers of defense:

1. **The infra migration that creates `workspace_invites` (`20260513140000_promote_workspace_invites_to_infra.sql`) makes the `authz.roles` FK conditional** on `authz.roles` already existing. If it does not, a `RAISE NOTICE` is emitted and the FK is skipped.

2. **As of 2026-05-27 the bootstrap script runs `xynes-authz-service` Drizzle migrations + seed at step 2a**, so by the time the FK guard at step 4 executes, `authz.roles` always exists in the happy path and the FK is added on every fresh reset. The `IF EXISTS … ELSE NOTICE` branch in the FK guard is retained as defense in depth for rare recovery flows (e.g. an operator drops the `authz` schema by hand). Step 6 then verifies the catalog row counts (exactly 5 roles, ≥1 permission, ≥1 role-permission mapping) so any regression in step 2a fails the bootstrap loudly. So:
   - On a fresh `bash ./scripts/supabase-local.sh reset` → step 2a creates `authz.roles` and seeds the catalog → step 4 adds the FK unconditionally → step 6 confirms the catalog is populated.
   - In a recovery flow where `authz.roles` is missing AND step 2a is bypassed (operator ran `--skip-cms-seed` or hand-deleted the schema) → the FK guard's `ELSE` branch emits a `NOTICE`, then step 6 fails the bootstrap with a clear `authz catalog verification failed` error.

3. **Every other infra FK that crosses schemas references only infra-owned tables** (`platform.workspaces`, `identity.users`). There is no other gap of this kind today.

### When to re-run the bootstrap

After any of these, re-run `bash ./scripts/bootstrap-local-db.sh --env-file .env.localhost.local --restart-gateway`:

- After creating a new `platform.workspaces` row in local dev, to seed CMS defaults for it AND seed its R2 dev provider row.
- After applying a new infra Supabase migration that needs the gateway to reload `platform.routes`.
- After a recovery operation that dropped the `authz` schema by hand — the bootstrap will re-apply step 2a's migrations + seed.

The bootstrap is fully idempotent — re-running it costs nothing and only adds what's missing.

---

## 5. Adding a new platform-level table

If you need a new table in the `platform` schema:

1. Add the migration under `xynes-infra/supabase/migrations/<timestamp>_<feature>.sql`.
2. Use `CREATE TABLE IF NOT EXISTS` and `CREATE EXTENSION IF NOT EXISTS` everywhere.
3. Use `pg_constraint` NOT EXISTS guards for any FK / CHECK / UNIQUE.
4. If the FK references a schema owned by a different service (e.g. `authz.*`), wrap it in `IF EXISTS (… information_schema.tables WHERE table_schema = '…')` and emit a `RAISE NOTICE` when skipped. See `20260513140000_promote_workspace_invites_to_infra.sql` for the template.
5. Add a contract test under `xynes-infra/scripts/test/<feature>-migration.test.sh` mirroring the workspace-admin / universal-storage pattern.
6. Update this document's §1 schema map.
7. Do NOT edit pre-existing migration files (e.g. `20251229100001_seed_platform_routes.sql`) after they've been merged. Already-applied environments will not replay them. Add a new forward-only `<timestamp>_seed_<feature>_route.sql` migration instead (see `20260513120000_seed_platform_storage_routes.sql` for the template).

---

## 6. Adding a new schema owned by a new service

If a new service needs its own schema:

1. The service applies its own Drizzle (or equivalent) migrations to create the schema and its tables. The service repo owns the migration tool config (`drizzle.config.ts`) and the migration files.
2. The bootstrap script (`xynes-infra/scripts/bootstrap-local-db.sh`) is updated to invoke the new service's migrations at the right point in the ordered sequence.
3. Any cross-schema FK from `platform.*` to the new schema MUST follow the conditional pattern from §4 — wrap the `ALTER TABLE … ADD CONSTRAINT` in an `IF EXISTS (information_schema.tables …)` guard.
4. Update this document's §1 and §3.

---

## 7. Verification checklist

Run after any DB-touching change. All must pass for a fresh local stack:

```bash
cd xynes/xynes-infra

# 1) Canonical reset
bash ./scripts/supabase-local.sh reset

# 2) Schemas + key tables present
PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "
  SELECT to_regclass('identity.users')                       AS identity_users,
         to_regclass('platform.routes')                      AS platform_routes,
         to_regclass('platform.workspaces')                  AS platform_workspaces,
         to_regclass('platform.workspace_invites')           AS platform_workspace_invites,
         to_regclass('platform.workspace_domains')           AS platform_workspace_domains,
         to_regclass('platform.workspace_api_keys')          AS platform_workspace_api_keys,
         to_regclass('platform.storage_objects')             AS platform_storage_objects,
         to_regclass('docs.documents')                       AS docs_documents,
         to_regclass('cms.content_entries')                  AS cms_content_entries;
"

# 3) Route seeds count
PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "
  SELECT
    (SELECT count(*) FROM platform.routes)                                          AS total_routes,
    (SELECT count(*) FROM platform.routes WHERE path_pattern LIKE '%/storage/%')   AS storage_routes,
    (SELECT count(*) FROM platform.routes WHERE path_pattern LIKE '%/domains%')    AS domain_routes,
    (SELECT count(*) FROM platform.routes WHERE path_pattern LIKE '%/api-keys%')   AS api_key_routes;
"

# 4) Contract tests (file-based, no live services required)
bash scripts/test/universal-storage-migration.test.sh
bash scripts/test/universal-storage-gateway-routes.test.sh
bash scripts/test/workspace-admin-integrations-migration.test.sh
bash scripts/test/workspace-admin-integrations-gateway-routes.test.sh
bash scripts/test/canonical-reset-smoke.test.sh    # end-to-end validation, see §8
```

Every table in §2 of the verification SQL should return its own `<schema>.<table>` name (not `NULL`). All counts in §3 should match the per-feature contract tests' expectations.

---

## 8. End-to-end canonical reset smoke

The script `xynes-infra/scripts/test/canonical-reset-smoke.test.sh` validates the full canonical reset path against a running local Supabase. It's a higher-tier test than the file-based contract tests — it actually executes `supabase db reset` and the bootstrap, then asserts the resulting DB state.

Run it after any change to:

- `scripts/supabase-local.sh`
- `scripts/bootstrap-local-db.sh`
- Any `supabase/migrations/*.sql` file
- Any service repo's Drizzle migrations that the bootstrap delegates to

Skips automatically if local Supabase is not running (per the existing `feature-flags.test.sh` pattern).

---

## 9. Related docs

- `xynes-infra/ENV_GUIDE.md` — env files, secrets, and `.env.localhost.local` vs `.env.dev` semantics.
- `xynes-infra/DEVELOPER.md` — broader infra developer guide.
- `xynes-infra/infra/SUPABASE_DEV.md` — Supabase CLI specifics.
- AGENTS.md (workspace root) → "Supabase Local Setup" + "Agent Operating Rules" §10 (Local DB reset rule).
- Per-repo `DEVELOPER.md` files for each service should link here for the schema ownership map instead of duplicating it.

---

## 10. Change log for this document

| Date | Change | PR / Branch |
|---|---|---|
| 2026-05-13 | Initial canonical version. Schema ownership map, bootstrap order, `authz.roles` FK gap doc, §5/§6 procedures, §7 verification checklist, §8 smoke smoke. Companion to `fix/infra-local-reset-bootstrap-cleanup`. | `fix/infra-local-reset-bootstrap-cleanup` |
