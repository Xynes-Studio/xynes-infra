# `scripts/test/` — Static `.test.sh` validators

Shell-script unit tests that validate the **shape** of the operator + smoke
scripts in `scripts/`. They never hit the network and never touch the live
database — that's the job of the smoke scripts themselves.

## What lives here

Two kinds of files:

1. **Per-script static validators** (`<script-name>.test.sh`). One per top-level script in `scripts/`. Each one typically asserts:
   - `--help` succeeds and emits a `Usage:` block with the expected flags.
   - The script fails fast on missing required args / env / token files.
   - The script source references every action key / table / endpoint the plan requires it to cover.
2. **Library coverage tests** (`db.test.sh`, `db-actions.test.sh`, `env-lib.test.sh`, `seed.test.sh`). These `source` the libs in `scripts/lib/` and exercise individual functions directly.

## Inventory

### Per-script validators

| Test | Covers | Notes |
|---|---|---|
| `bootstrap-local-db.test.sh` | `scripts/bootstrap-local-db.sh` | `--help`, route fragments, idempotency. |
| `reset-dev-db.test.sh` | `scripts/reset-dev-db.sh` | `--help`, `--plan-only`, exit code on missing input. |
| `wipe-identity.test.sh` | `scripts/wipe-identity.sh` | `--help`, `--plan-only`, fast-fail on bad UUID. |
| `supabase-local.test.sh` | `scripts/supabase-local.sh` | `--help`, every sub-command name present in source. |
| `supabase-token.test.sh` | `scripts/supabase-token.sh` | `--help`. |
| `smoke.test.sh` | `scripts/smoke.sh` | `--help`, unknown-suite rejection, suite list. |
| `smoke-auth.test.sh` | `scripts/smoke-auth.sh` | `--help`, missing-token fast-fail. |
| `smoke-cms-entry.test.sh` | `scripts/smoke-cms-entry.sh` | `--help`, missing/invalid `WORKSPACE_ID` fast-fail, lifecycle action keys. |
| `smoke-cms-api-key.test.sh` | `scripts/smoke-cms-api-key.sh` | `--help`, missing `JWT_SECRET` fast-fail, all 3 preset mints, action-key path fragments, DB invariant assertions, redaction sweep targets. |
| `smoke-db.test.sh` | `scripts/smoke-db.sh` | `--help`, `--verify-only`, bad-flag rejection. |
| `smoke-gateway-audit.test.sh` | `scripts/smoke-gateway-audit.sh` | `--help`, mode validation, method validation. |
| `smoke-security.test.sh` | `scripts/smoke-security.sh` | `--help`, mode/flag/token-file validation. |

### Migration + DB contract tests

These tests run psql against `DATABASE_URL` and assert that migrations
applied. They are pre-conditions for the smoke battery, not unit tests.

| Test | Covers |
|---|---|
| `cms-actor-columns-migration.test.sh` | `cms.content_entries.created_by` / `updated_by` columns + nullability + FK contract. |
| `cms-dashboard-gateway-routes.test.sh` | `platform.routes` has the 12 CMS dashboard route rows. |
| `workspace-admin-integrations-migration.test.sh` | `platform.workspace_domains`, `platform.workspace_api_keys`, etc. |
| `workspace-admin-integrations-authz-permissions.test.sh` | `authz.permissions` catalog contains the `platform.domains.*` + `platform.api_keys.*` action keys + correct role mapping. |
| `workspace-admin-integrations-gateway-routes.test.sh` | `platform.routes` has the 8 workspace-admin route rows. |
| `workspace-admin-integrations-domain-constraint.test.sh` | `platform.workspace_domains` unique + hostname-validation constraints. |

### Other

| Test | Covers |
|---|---|
| `feature-flags.test.sh` | Feature-flag plumbing (env-file → runtime → service behaviour). |
| `supabase-email-templates.test.sh` | Local Supabase email templates exist + render. |

### Library coverage

| Test | Lib under test |
|---|---|
| `env-lib.test.sh` | `scripts/lib/env.sh` |
| `db.test.sh` | `scripts/lib/db.sh` |
| `db-actions.test.sh` | `scripts/lib/db-actions.sh` (+ `db.sh`) |
| `seed.test.sh` | `scripts/lib/seed.sh` (+ `db-actions.sh`, `db.sh`) |

## The runner

```bash
bash ./scripts/test/run.sh
```

`run.sh` is intentionally a one-line glob: `for t in ./scripts/test/*.test.sh; do bash "$t"; done`. **New `.test.sh` files are auto-discovered** — no manual registration in `run.sh` is needed.

## Convention for adding a new test

1. Name it `<script-or-feature-name>.test.sh` matching the script it covers.
2. Source no libs unless you're testing a lib (just call the script under test directly via `./scripts/<name>.sh ...`).
3. Define `fail()` and `assert_contains()` helpers at the top. Copy from `smoke-cms-api-key.test.sh` for the canonical macOS-compatible pattern (uses `grep -q -- "$needle"` so needles starting with `-` aren't parsed as grep flags).
4. End with `echo "PASS: <name>.test.sh"` and let `set -euo pipefail` bail on the first failure.
5. Run `bash ./scripts/test/run.sh` before commit.

## What these tests deliberately do NOT cover

- **Live behaviour** — that's the smoke scripts themselves. Run them against a real stack (`docker compose ... up -d`) to validate end-to-end paths.
- **TypeScript / bun unit coverage** — service repos own their own `bun run test`. Infra owns the shell smoke layer.
- **Production readiness** — these scripts are local dev tooling only.

## See also

- `scripts/README.md` — top-level script catalog.
- `scripts/lib/README.md` — shared library contracts.
- `SMOKE-TEST.md` — long-form runbook for the live smoke flows.
