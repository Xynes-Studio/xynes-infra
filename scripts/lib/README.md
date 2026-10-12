# `scripts/lib/` — Shared sourced libraries

Helper functions sourced by scripts in `scripts/` and `scripts/test/`. None of
these files are intended to be executed directly — they expose functions and
constants for callers via `source` / `# shellcheck source=...` annotations.

## Files

| File | Exports | Used by |
|---|---|---|
| `colors.sh` | ANSI color constants: `GREEN`, `RED`, `YELLOW`, `BLUE`, `CYAN`, `BOLD`, `NC`. All variables `export`-ed for sub-shells. | Every smoke script. |
| `env.sh` | `strip_quotes()`, env-file loading helpers, `require_bin()`-style guards (NB: `require_bin` actually lives in `http.sh`). | Every smoke + DB script. |
| `http.sh` | `require_bin()`, curl wrappers, response-body capture helpers. Generic smoke-test plumbing. | Every smoke script (`smoke-*.sh`). |
| `db.sh` | `extract_db_host()` and other low-level Postgres URL parsers / connection helpers. | `smoke-db.sh`, `reset-dev-db.sh`, `wipe-identity.sh`, plus `scripts/test/db.test.sh`. |
| `db-actions.sh` | Higher-level psql actions (table existence, row counts, FK probes). Builds on `db.sh`. | `smoke-db.sh`, plus `scripts/test/db-actions.test.sh`. |
| `seed.sh` | Idempotent dev seed-data helpers (sample workspace, content type, blog entry). Builds on `db.sh` + `db-actions.sh`. | `smoke-db.sh`, `smoke-setup.sh`, plus `scripts/test/seed.test.sh`. |

## How libraries are sourced

The canonical pattern, used by every caller in `scripts/`:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"
```

The `# shellcheck source=...` annotation is required so the lint pass
(`scripts/lint.sh` → `shellcheck -x`) can resolve cross-file references and
catch broken function names.

## Stability contract

These libraries form the **internal stable API** of the smoke harness. When
modifying any function here:

- **Do not change** a function's name, parameter order, or return convention
  without auditing every caller listed in the "Used by" column above.
- **Do add** new helpers freely; the sourced-library pattern means callers
  pick up new functions automatically.
- **Do not** `set -euo pipefail` at the top of a library file. The caller owns
  the shell mode. Only `db-actions.sh` and `seed.sh` set strict mode because
  they document their own contract.
- **Do not** `cd` inside a library function. Use absolute paths via
  `${SCRIPT_DIR}` / `${ROOT_DIR}` passed in from the caller.

## Tests

The library files themselves are exercised by static `.test.sh` validators:

- `scripts/test/env-lib.test.sh` → `env.sh`
- `scripts/test/db.test.sh` → `db.sh`
- `scripts/test/db-actions.test.sh` → `db-actions.sh` (and transitively `db.sh`)
- `scripts/test/seed.test.sh` → `seed.sh` (and transitively `db-actions.sh`, `db.sh`)

Run all of them via `bash ./scripts/test/run.sh` from the repo root.

## Why no per-library README?

Each file is short (under 100 lines) and self-documenting via inline comments
above each function. A separate README per file would be more noise than
signal. If a library grows past ~200 lines, split it before documenting it.
