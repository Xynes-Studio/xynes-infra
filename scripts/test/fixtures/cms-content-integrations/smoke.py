"""Join pinned B requests to the existing real-service A4 disposable fixture."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from pinned_inputs import load_pinned_file


def main():
    parsed = urlparse(os.environ.get("DATABASE_URL", ""))
    if (parsed.scheme not in ("postgres", "postgresql") or parsed.hostname not in ("localhost", "127.0.0.1")
            or parsed.query or parsed.fragment or not re.fullmatch(r"/cms_int_a4_a5_[a-z0-9_]+", parsed.path)):
        raise ValueError("Requires an explicitly disposable cms_int_a4_a5_* loopback database")
    if os.environ.get("CMS_DELIVERY_PARITY_MODE", "full") != "full":
        raise ValueError("Full frontend contract parity is required")
    repos = {}
    for name in ("CMS", "FRONTEND", "GATEWAY", "ACCOUNTS"):
        path = os.environ.get("XYNES_" + name + "_REPO")
        revision = os.environ.get("XYNES_" + name + "_REVISION", "")
        if not path or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Missing immutable runtime input: " + name)
        root = Path(path).resolve()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, capture_output=True).returncode
        if head != revision or dirty:
            raise ValueError("Runtime checkout differs from pinned input: " + name)
        repos[name] = root
    fixture_source = load_pinned_file(repos["CMS"], os.environ["XYNES_CMS_REVISION"], "test/integration/content-delivery-access.test.ts").decode()
    infra = Path(os.environ["XYNES_INFRA_REPO"]).resolve()
    source = Path(__file__).parent
    # All pin/parity failures occur before backups, migrations, or database access.
    subprocess.run([sys.executable, "-B", str(source / "parity.py")], check=True)
    parent = repos["CMS"] / ".tmp"
    if parent.is_symlink():
        raise ValueError("Owned fixture parent must not be a symlink")
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="cms-int-a5-", dir=parent))
    evidence = Path(tempfile.mkdtemp(prefix="cms-int-a5-evidence-"))
    evidence.chmod(0o700)
    try:
        for name in ("copied-request.ts", "copied-request-hooks.ts"):
            shutil.copyfile(source / name, temporary / name)
        # Reuse the pinned A4 suite verbatim except its owned hook seam and relative helper import.
        marker = "    let sql: ReturnType<typeof postgres>;"
        if fixture_source.count(marker) != 1:
            raise ValueError("Pinned access fixture hook seam changed")
        combined = 'import { installCopiedRequestHooks } from "./copied-request-hooks";\n' + fixture_source.replace(marker, "    installCopiedRequestHooks();\n" + marker).replace('from "./libpq-env"', 'from "../../test/integration/libpq-env"')
        (temporary / "copied-requests.test.ts").write_text(combined)
        env = {**os.environ, "RUN_CMS_DELIVERY_ACCESS_TESTS": "true", "XYNES_CMS_DELIVERY_TEST_FILE": str(temporary / "copied-requests.test.ts"), "XYNES_A5_EVIDENCE_DIR": str(evidence)}
        subprocess.run(["bun", "x", "tsc", "--noEmit", "--strict", "--target", "esnext", "--lib", "esnext", "--module", "esnext", "--moduleResolution", "bundler", "--allowImportingTsExtensions", "--types", "bun-types", "--skipLibCheck", str(temporary / "copied-requests.test.ts")], cwd=repos["CMS"], env=env, check=True)
        subprocess.run([sys.executable, "-B", str(infra / "scripts/test/fixtures/cms-delivery/access-smoke.py")], env=env, check=True)
        if (temporary / "coverage").is_dir():
            shutil.copytree(temporary / "coverage", evidence / "coverage")
        if not (evidence / "copied-requests.json").is_file():
            raise ValueError("Copied-request smoke produced no evidence")
        print("PASS: A5 actual copied-request smoke; evidence: " + str(evidence), flush=True)
    finally:
        shutil.rmtree(temporary)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("ERROR: " + (str(error) if isinstance(error, ValueError) else "A5 smoke prerequisite or execution failed"), file=sys.stderr)
        raise SystemExit(1)
