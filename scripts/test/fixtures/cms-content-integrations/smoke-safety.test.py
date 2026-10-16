"""Fail before tooling for unsafe/missing provisioned copied-request fixtures."""
import contextlib
import io
import importlib.util
import os
from pathlib import Path
import unittest
import tempfile
import subprocess
from unittest.mock import patch

SOURCE = Path(__file__).with_name("smoke.py")

class SmokeSafety(unittest.TestCase):
    def launcher(self):
        spec = importlib.util.spec_from_file_location("a5_smoke", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_and_nonisolated_database_fail_before_subprocess(self):
        for url in ("", "postgres://u@remote.test/cms_int_a4_a5_x", "postgres://u@127.0.0.1/live", "postgres://u@127.0.0.1/cms_int_a4_a5_x?host=remote", "postgres://u@127.0.0.1/cms_int_a4_a5_x#remote"):
            with self.subTest(url=url), patch.dict(os.environ,{"DATABASE_URL":url},clear=True), patch("subprocess.run",side_effect=AssertionError("Tooling forbidden")):
                with self.assertRaisesRegex(ValueError,"explicitly disposable"):
                    self.launcher().main()

    def test_backend_only_cannot_be_reported_as_full_smoke(self):
        with patch.dict(os.environ,{"DATABASE_URL":"postgres://u@127.0.0.1/cms_int_a4_a5_x","CMS_DELIVERY_PARITY_MODE":"backend-only"},clear=True),patch("subprocess.run",side_effect=AssertionError("Tooling forbidden")):
            with self.assertRaisesRegex(ValueError,"Full frontend"):
                self.launcher().main()


    def test_missing_runtime_pin_fails_before_tools(self):
        with patch.dict(os.environ,{"DATABASE_URL":"postgres://u@127.0.0.1/cms_int_a4_a5_x"},clear=True),patch("subprocess.run",side_effect=AssertionError("Tooling forbidden")):
            with self.assertRaisesRegex(ValueError,"Missing immutable"):
                self.launcher().main()

    def test_dirty_or_wrong_runtime_revision_fails_before_parity_and_database(self):
        for head, dirty in (("b"*40,0),("a"*40,1)):
            env={"DATABASE_URL":"postgres://u@127.0.0.1/cms_int_a4_a5_x","XYNES_CMS_REPO":"/fixture","XYNES_CMS_REVISION":"a"*40}
            with patch.dict(os.environ,env,clear=True),patch("subprocess.run",side_effect=[subprocess.CompletedProcess([],0,head),subprocess.CompletedProcess([],dirty)]) as run:
                with self.assertRaisesRegex(ValueError,"differs from pinned"):
                    self.launcher().main()
                self.assertEqual(run.call_count,2)

    def test_owned_temporary_sources_removed_on_compiler_failure_and_success(self):
        for failing in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                env={"DATABASE_URL":"postgres://u@127.0.0.1/cms_int_a4_a5_x","XYNES_INFRA_REPO":str(root)}
                for name in ("CMS","FRONTEND","GATEWAY","ACCOUNTS"):
                    env["XYNES_"+name+"_REPO"]=str(root)
                    env["XYNES_"+name+"_REVISION"]="a"*40
                module=self.launcher()
                real_mkdtemp=tempfile.mkdtemp
                calls=[]
                def run(command,**options):
                    calls.append(command)
                    if command[:2]==["git","rev-parse"]:
                        return subprocess.CompletedProcess(command,0,"a"*40)
                    if command[:2]==["bun","x"] and failing:
                        raise subprocess.CalledProcessError(1,command)
                    if command[-1].endswith("access-smoke.py"):
                        Path(options["env"]["XYNES_A5_EVIDENCE_DIR"],"copied-requests.json").write_text('{}')
                    return subprocess.CompletedProcess(command,0)
                def owned(**options):
                    return real_mkdtemp(**{**options,"dir":options.get("dir",root)})
                with contextlib.redirect_stdout(io.StringIO()),patch.dict(os.environ,env,clear=True),patch("subprocess.run",run),patch.object(module,"load_pinned_file",return_value=b"    let sql: ReturnType<typeof postgres>;"),patch("tempfile.mkdtemp",owned):
                    if failing:
                        with self.assertRaises(subprocess.CalledProcessError): module.main()
                    else: module.main()
                compiler=next(command for command in calls if command[:2]==["bun","x"])
                self.assertIn("--lib",compiler)
                self.assertEqual(compiler[compiler.index("--lib")+1],"esnext")
                self.assertEqual(list((root/".tmp").iterdir()),[])
                self.assertEqual(any(command[-1].endswith("access-smoke.py") for command in calls),not failing)

if __name__ == "__main__":
    unittest.main()
