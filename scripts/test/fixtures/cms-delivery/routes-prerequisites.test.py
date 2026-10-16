"""Exercise the actual parity launcher in a standalone checkout, without a DB."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RoutePrerequisites(unittest.TestCase):
    def test_standalone_checkout_skips_missing_default_siblings(self):
        root = Path(__file__).resolve().parents[4]
        for available in (None, "xynes-platform-contracts", "xynes-platform-config"):
            with self.subTest(available=available), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                infra = base / "infra"
                for name in ("scripts/test/cms-content-delivery-routes.test.sh", "scripts/test/fixtures/cms-delivery/routes-contract.py"):
                    target = infra / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / name, target)
                if available:
                    relative = "contracts/cms-delivery.v1.json" if available == "xynes-platform-contracts" else "src/seeds/routes.ts"
                    file = base / available / relative
                    file.parent.mkdir(parents=True)
                    file.write_text("prerequisite exists but must not be evaluated")
                env = {key: value for key, value in os.environ.items() if not key.startswith("XYNES_")}
                result = subprocess.run(["bash", str(infra / "scripts/test/cms-content-delivery-routes.test.sh")], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("SKIP: CMS delivery route parity requires", result.stdout)
                self.assertNotIn("PASS:", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_explicit_missing_checkout_fails_instead_of_skipping(self):
        root = Path(__file__).resolve().parents[4]
        with tempfile.TemporaryDirectory() as directory:
            for key in ("XYNES_CONTRACTS_REPO", "XYNES_PLATFORM_CONFIG_REPO"):
                with self.subTest(key=key):
                    env = {**os.environ, key: directory}
                    result = subprocess.run(["bash", str(root / "scripts/test/cms-content-delivery-routes.test.sh")], env=env, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ERROR: Missing explicit CMS delivery parity prerequisite", result.stderr)
                    self.assertNotIn("SKIP:", result.stdout)
                    self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
