# `xynes-infra/scripts/` — Operator + smoke test scripts

Shell scripts that operate the local stack, reset/bootstrap the DB, mint dev
credentials, and exercise live smoke matrices against the running services.

> **Scope.** These scripts target the **local dev stack** described in
> `xynes/xynes-infra/docker-compose.dev.yml` and the **local Supabase** stack
> in `xynes/xynes-infra/supabase/`. They are NOT production tooling. None of
> them mutate production data; the ones that touch the DB use `DATABASE_URL`
> from local env files (`.env.localhost*`).

---

## Folder layout

```
scripts/
├── README.md                ← this file
├── lib/                     ← shared sourced libraries (see lib/README.md)
│   ├── colors.sh
│   ├── db.sh
│   ├── db-actions.sh
│   ├── env.sh
│   ├── http.sh
│   └── seed.sh
├── test/                    ← static `.test.sh` validators (see test/README.md)
│   ├── run.sh               ← runs every *.test.sh in this dir
│   └── *.test.sh
└── *.sh                     ← operator + smoke entrypoints (this README)
```

Conventions for the top level:

- **Filename prefix categorises the script.** No `.sh` outside the categories
  below should be added without updating this README first.
- **Every script supports `--help`** that prints a `Usage:` block.
- **Every script honours `--env-file <path>`** when it needs DB or service URLs.
- **Defaults are localhost.** Most scripts default `MODE=localhost`; pass
  `--mode env` to use the gateway/service URLs from the env file directly.
- **Exit codes:** `0` success, `1` user-fixable failure (bad args, missing
  env, downstream HTTP error), `2` infra precondition failure (Bun/psql/
  shellcheck missing).
- **No script writes the raw text of a secret to a file or to stdout.** Keys,
  JWTs, and Argon2 hashes stay in shell variables; only 8-char `keyPrefix`
  values and UUIDs are echoed.

---

## Categories

### A. Local DB lifecycle

Scripts that own the local Postgres state. Use these whenever a developer
needs a clean DB or the live route registry has drifted.

| Script | Purpose | When to use |
|---|---|---|
| `supabase-local.sh` | Wrapper around `supabase` CLI (`start`/`stop`/`status`/`reset`). The default `reset` path also runs `bootstrap-local-db.sh` afterward. | Daily local dev. **Preferred reset entrypoint.** |
| `bootstrap-local-db.sh` | After a raw `supabase db reset`, applies CMS-Core service migrations, seeds CMS defaults for existing `platform.workspaces`, verifies directory-first dashboard routes, and (with `--restart-gateway`) reloads the gateway route registry. | Always runs after a raw Supabase reset. Invoked transitively by `supabase-local.sh reset`. |
| `reset-dev-db.sh` | Targeted dev DB reset (NOT the Supabase config reset). Drops + recreates specific schemas. Supports `--plan-only` and `--dry-run`. | Schema-level reset without restarting the Supabase containers. |
| `wipe-identity.sh` | Targeted teardown of a single user / email across `identity.users`, `auth.users`, and related rows. Supports `--plan-only`, `--dry-run`, `--yes`. | Removing a test account that's blocking signup/invite flows. |

> **Local DB reset rule (from `AGENTS.md`):** prefer `bash ./scripts/supabase-local.sh reset`. If you must run a raw `supabase db reset`, IMMEDIATELY run `bash ./scripts/bootstrap-local-db.sh --env-file .env.localhost.local --restart-gateway` before debugging app failures.

### B. Smoke tests — live battery

Live HTTP smoke harnesses that exercise a running stack on `localhost`.
Each one is single-purpose; the master runners (Section C) compose them.

| Script | Surface tested | Notes |
|---|---|---|
| `smoke-health.sh` | `/health` + `/ready` for all 6 services | Liveness/readiness probe. No auth needed. |
| `smoke-auth.sh` | Supabase auth → `/me` → workspace listing | Requires Supabase access token (`--token-file`). |
| `smoke-content.sh` | Public CMS read paths (`/content/*`, `/blog`) | Requires `WORKSPACE_ID`. |
| `smoke-cms-entry.sh` | Full lifecycle of `cms.entry.*` actions through cms-core | Requires `WORKSPACE_ID` + internal service token. |
| `smoke-gateway-audit.sh` | Gateway canonical access log emission for dynamic + DB routes | Requires DB access (asserts `gateway_audit.events` rows). |
| `smoke-security.sh` | Rate limits, body limits, JWT validation, telemetry redaction | Requires Supabase access token. |
| `smoke-db.sh` | DB connectivity + schema-shape verification | `--verify-only`, `--seed`, `--cleanup` modes. |
| `smoke-api-key-enforcement.sh` | Workspace API key auth + scope enforcement at the gateway. Mints a `workspace_admin` preset key, runs 11 assertions, revokes on exit. | Closes the loop for the Workspace Admin integrations gateway plan (Task 7). |
| `smoke-cms-api-key.sh` | Workspace API key end-to-end through cms-core. Mints 3 CMS preset keys (`cms_readonly`, `cms_authoring`, `cms_publisher`), runs 36 assertions including DB audit-column invariants, revokes on exit. | Closes the loop for CMS-API-KEY-ACTOR-1 (Story E). |

#### Standard invocation pattern

Every smoke script reads `--env-file` (defaults vary; see `--help`). Typical
local run:

```bash
cd xynes/xynes-infra
docker compose --env-file .env.dev.local -f docker-compose.dev.yml up -d
bash ./scripts/smoke-health.sh                              # quick liveness
bash ./scripts/smoke-cms-api-key.sh                         # CMS API key matrix
bash ./scripts/smoke-api-key-enforcement.sh --keep-key      # gateway matrix
```

### C. Smoke test runners (composite entrypoints)

| Script | What it composes |
|---|---|
| `smoke.sh` | Modern master runner. Takes suite names: `health`, `setup`, `all`, `security`, `gateway-audit`, `auth`, `content`, `cms-entry`, `db`. Supports `--all`, `--quick`, `--full`. |
| `smoke-all.sh` | Legacy master runner. Health → auth → invites → docs → CMS → authz → telemetry. Supports `--health-only` for a quick liveness pass. Kept because it's referenced from many archived plans and the canonical `DEVELOPER.md`. |
| `smoke-setup.sh` | Not a test — seeds the minimum data a smoke run needs (workspace, content type, sample doc). Idempotent. Used by `smoke.sh setup` and as a prereq for `smoke-content.sh` / `smoke-cms-entry.sh`. |

> **Which runner do I use?** Prefer `smoke.sh` for new work; it's the modern,
> composable entrypoint. `smoke-all.sh` is still maintained because plan
> archives and `DEVELOPER.md` reference it; do not remove without updating
> those callers.

### D. Local Supabase / auth utilities

| Script | Purpose |
|---|---|
| `supabase-local.sh` | (also in §A) `start`/`stop`/`status`/`reset` of the local Supabase containers via the `supabase` CLI. |
| `supabase-token.sh` | Mints a Supabase access token by calling `/auth/v1/token` with `password` grant. Output goes to stdout; pipe to `.tmp/supabase_access_token` for the smoke scripts. |

### E. Env + lint plumbing

| Script | Purpose |
|---|---|
| `link-env-files.sh` | Creates symlinks from per-repo `.env.dev` / `.env.localhost` to the centralised files in `xynes-infra/`. Backs up existing regular files as `.env.*.bak.<timestamp>`. Supports `--dry-run`. |
| `lint.sh` | Runs `shellcheck -x -S warning` against every `.sh` under `scripts/`, `scripts/lib/`, `scripts/test/`. Falls back to `bash -n` (syntax-only) if shellcheck isn't installed. Used by the dev pre-commit pass. |

---

## Adding a new script

1. **Pick a category.** If none fits, propose a new category in this README before adding the file.
2. **Use a category prefix:** `smoke-*` (live tests), `supabase-*` (local Supabase ops), `bootstrap-/reset-/wipe-` (DB lifecycle). Other names are for unique top-level tools (`lint.sh`, `link-env-files.sh`).
3. **Source the shared libs** from `scripts/lib/` rather than reinventing them. The standard preamble (copy from `smoke-health.sh` or `smoke-cms-api-key.sh`):

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
   ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
   cd "${ROOT_DIR}"

   # shellcheck source=./scripts/lib/colors.sh
   source "${SCRIPT_DIR}/lib/colors.sh"
   # shellcheck source=./scripts/lib/env.sh
   source "${SCRIPT_DIR}/lib/env.sh"
   # shellcheck source=./scripts/lib/http.sh
   source "${SCRIPT_DIR}/lib/http.sh"
   ```

4. **Add `--help` and a `Usage:` block.** Mirror `smoke-cms-api-key.sh`.
5. **Add a sibling `scripts/test/<name>.test.sh` static validator.** It must at minimum assert `--help` works and that the script fails fast on missing required args. See `scripts/test/README.md`.
6. **Document the script in this README** in the right category table.
7. **Run** `bash ./scripts/lint.sh` and `bash ./scripts/test/run.sh` — both must pass before commit.

---

## Why no subdirectories?

This folder has 19 top-level scripts. Subdividing into `db/`, `smoke/`,
`supabase/`, `dev/` would require editing:

- 80+ doc references in `README.md`, `DEVELOPER.md`, `SMOKE-TEST.md`, `ENV_GUIDE.md`, every plan in `docs/plans/archive/*.md`, and `AGENTS.md` in the workspace root.
- 24+ test files in `scripts/test/*.test.sh` that hard-code `./scripts/<name>.sh`.
- 13 scripts with `# shellcheck source=./scripts/lib/...` annotations.
- 19 `Usage: ./scripts/<name>.sh` help-text strings.
- Cross-script invocations (e.g. `supabase-local.sh` calls `bootstrap-local-db.sh`).

The filename prefixes already do the categorisation work the folders would do.
The current shape was kept deliberately; this README documents the convention.

If a future refactor decides the cost is worth paying, the cleanest split would
be `scripts/db/`, `scripts/smoke/`, `scripts/supabase/`, `scripts/dev/`. Do that
in one atomic PR that also rewrites every reference.

---

## See also

- `xynes/xynes-infra/README.md` — high-level local dev entrypoints.
- `xynes/xynes-infra/DEVELOPER.md` — full local dev runbook.
- `xynes/xynes-infra/ENV_GUIDE.md` — env file layout + which mode to use.
- `xynes/xynes-infra/SMOKE-TEST.md` — long-form smoke test reference (the runbook these scripts implement).
- `xynes/xynes-infra/scripts/lib/README.md` — sourced library contracts.
- `xynes/xynes-infra/scripts/test/README.md` — `.test.sh` validator convention.
