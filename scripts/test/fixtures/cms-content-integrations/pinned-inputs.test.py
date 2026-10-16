"""Safety and drift regressions for immutable A5 cross-repository inputs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class PinnedInputs(unittest.TestCase):
    def helper(self):
        file = ROOT / "pinned_inputs.py"
        self.assertTrue(file.exists(), "A5 pinned input verifier is not implemented")
        spec = importlib.util.spec_from_file_location("a5_pinned_inputs", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def repository(self, directory):
        repo = Path(directory)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        file = repo / "contracts/delivery.json"
        file.parent.mkdir()
        file.write_text('{"version":1}\n')
        subprocess.run(["git", "add", "contracts/delivery.json"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=A5 fixture", "-c", "user.email=fixture@invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], cwd=repo, check=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        return repo, file, revision


    def test_cohort_skips_only_unprovisioned_parity(self):
        script = ROOT.parents[1] / "cms-delivery-contract-parity.test.sh"
        self.assertTrue(script.exists(), "A5 parity launcher is not implemented")
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("XYNES_", "RUN_CMS_"))}
        skipped = subprocess.run(["bash", str(script)], env=environment, text=True, capture_output=True)
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertIn("SKIP: A5 parity is not provisioned", skipped.stdout)
        environment["RUN_CMS_DELIVERY_CONTRACT_PARITY"] = "true"
        required = subprocess.run(["bash", str(script)], env=environment, text=True, capture_output=True)
        self.assertNotEqual(required.returncode, 0)
        self.assertIn("ERROR: Missing pinned A5 input", required.stderr)
        self.assertNotIn("SKIP:", required.stdout)
        self.assertNotIn("Traceback", required.stderr)

    def test_reads_exact_pinned_bytes(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            repo, _, revision = self.repository(directory)
            self.assertEqual(helper.load_pinned_file(repo, revision, "contracts/delivery.json"), b'{"version":1}\n')


    def test_rejects_missing_or_symlinked_working_input(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            repo, file, revision = self.repository(directory)
            file.unlink()
            with self.assertRaisesRegex(ValueError, "absent from the checkout"):
                helper.load_pinned_file(repo, revision, "contracts/delivery.json")
            external = Path(outside) / "delivery.json"
            external.write_text('{"version":1}\n')
            file.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "escapes the repository"):
                helper.load_pinned_file(repo, revision, "contracts/delivery.json")

    def test_rejects_working_copy_drift(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            repo, file, revision = self.repository(directory)
            file.write_text('{"version":2}')
            with self.assertRaisesRegex(ValueError, "differs from pinned revision"):
                helper.load_pinned_file(repo, revision, "contracts/delivery.json")

    def test_rejects_unsafe_or_unpinned_input(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            repo, _, revision = self.repository(directory)
            for pin, path in (("develop", "contracts/delivery.json"), ("-n", "contracts/delivery.json"), (revision, "../outside"), (revision, "/etc/passwd"), ("0" * 40, "contracts/delivery.json")):
                with self.subTest(pin=pin, path=path), self.assertRaises(ValueError):
                    helper.load_pinned_file(repo, pin, path)

    def test_compares_artifact_semantics_without_ignoring_drift(self):
        helper = self.helper()
        helper.compare_contracts(b'{"version":1,"fields":["id","title"]}', b'{"fields":["id","title"],"version":1}')
        for other in (b'{"version":2,"fields":["id","title"]}', b'{"version":1,"fields":["id"]}', b'not JSON', b'[]', b'{"version":true,"fields":["id","title"]}', b'{"version":0,"version":1,"fields":["id","title"]}', b'{"version":NaN}'):
            with self.subTest(other=other), self.assertRaises(ValueError):
                helper.compare_contracts(b'{"version":1,"fields":["id","title"]}', other)


if __name__ == "__main__":
    unittest.main()
