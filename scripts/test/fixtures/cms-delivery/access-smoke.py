"""Back up and prepare an explicitly disposable A4 DB, then run real source/HTTP tests.

The caller owns database creation/teardown. No automatic existing-volume cleanup.
"""
import os
import secrets
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse, unquote

url = os.environ.get("DATABASE_URL", "")
parsed = urlparse(url)
if parsed.scheme not in ("postgres", "postgresql") or parsed.query or parsed.fragment or parsed.hostname not in ("localhost", "127.0.0.1") or not re.fullmatch(r"/cms_int_a4_[a-z0-9_]+", parsed.path):
    raise SystemExit("Requires an explicitly disposable cms_int_a4_* loopback database")
repos = {key: Path(os.environ[key]).resolve() for key in (
    "XYNES_INFRA_REPO", "XYNES_CMS_REPO", "XYNES_GATEWAY_REPO", "XYNES_ACCOUNTS_REPO")}
test_file = "test/integration/content-delivery-access.test.ts"
if os.environ.get("XYNES_CMS_DELIVERY_TEST_FILE"):
    supplied = Path(os.environ["XYNES_CMS_DELIVERY_TEST_FILE"])
    resolved = supplied.resolve()
    owned = repos["XYNES_CMS_REPO"] / ".tmp"
    if (supplied.is_symlink() or not resolved.is_file()
            or resolved.parent.parent != owned
            or not resolved.parent.name.startswith("cms-int-a5-")
            or resolved.name != "copied-requests.test.ts"):
        raise SystemExit("Unsupported owned fixture entrypoint")
    test_file = str(resolved)
pg_dump = os.environ.get("PG_DUMP_BIN", "pg_dump")
psql = os.environ.get("PSQL_BIN", "psql")
# libpq hostaddr/service/options can override a guarded URL. Keep only explicit PG settings.
env = {**{key: value for key, value in os.environ.items() if not key.startswith("PG")}, "PGDATABASE": parsed.path[1:], "PGHOST": parsed.hostname, "PGPORT": str(parsed.port or 5432), "PGUSER": unquote(parsed.username or ""), "PGPASSWORD": unquote(parsed.password or ""), "NODE_ENV": "test"}
# Keep the backup outside the disposable database. Its path contains no credentials.
backup_dir = Path(tempfile.mkdtemp(prefix="cms-a4-before-platform-"))
backup_dir.chmod(0o700)
backup = backup_dir / "database.dump"
subprocess.run([pg_dump, "-Fc", "-f", str(backup)], env=env, check=True, capture_output=True)
backup.chmod(0o600)
print(f"Disposable fixture backup: {backup}", flush=True)
infra = repos["XYNES_INFRA_REPO"]
# Minimal workspace identity scaffolding; route/key tables use their canonical migrations.
# GRANTs for hosted postgres are irrelevant to the isolated owner and omitted only here.
setup = "CREATE SCHEMA IF NOT EXISTS platform; CREATE TABLE IF NOT EXISTS platform.workspaces (id uuid primary key);\n"
for name in ("20251229100000_create_platform_routes.sql", "20260424090000_workspace_admin_integrations.sql"):
    sql = (infra / "supabase/migrations" / name).read_text()
    setup += re.sub(r"GRANT\s+[^;]+;", "", sql, flags=re.I) + "\n"
subprocess.run([psql, "-X", "-v", "ON_ERROR_STOP=1"], input=setup, env=env,
               text=True, check=True, capture_output=True)
# Generate process-only secrets; no filled env file or committed test key.
# Prefer the established legacy/hybrid internal auth fixture for this CMS consumer.
# Preserve the resolved test-tool selector for the second fixture backup. It is not a libpq option.
env["PG_DUMP_BIN"] = pg_dump
env.update(INTERNAL_SERVICE_TOKEN=secrets.token_hex(32), INTERNAL_AUTH_MODE="hybrid", INTERNAL_JWT_SIGNING_KEY="")
test_command = ["bun", "test", test_file]
if os.environ.get("XYNES_CMS_DELIVERY_TEST_FILE"):
    test_command += ["--coverage", "--coverage-reporter=lcov", "--coverage-dir=" + str(resolved.parent / "coverage")]
subprocess.run(test_command,
               cwd=repos["XYNES_CMS_REPO"], env=env, check=True)
