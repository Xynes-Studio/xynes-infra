#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'scripts/security/secret_gate.py'
REAL = Path(os.environ.get('XYNES_TEST_GITLEAKS_BIN', '/private/tmp/apex-gitleaks-8.30.1/gitleaks'))
# Inert runtime fixture; never issued by GitHub or placed in the public source as a token.
SECRET = 'ghp_' + '0Xa7JpfKmw3Z2BvQcY6L9DNsH1gR5Ut8EeA4'

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp = tempfile.TemporaryDirectory(prefix='apex-secret-gate-')
  self.addCleanup(self.tmp.cleanup)
  self.base = Path(self.tmp.name); self.repo = self.base/'repo'; self.repo.mkdir()
  self.git('init', '-q'); self.blob = f'api_key = "{SECRET}" # gitleaks:allow\n'.encode()
  (self.repo/'configuration.env').write_bytes(self.blob); self.git('add', '.')
  self.git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')
  self.commit = self.git('rev-parse','HEAD').strip()
  self.finding = dict(RuleID='github-pat', File='configuration.env', Commit=self.commit, Secret=SECRET)
  self.scanner = self.base/'scanner'
  self.fake([self.finding], 1)
 def git(self, *args):
  return subprocess.run(['git','-C',str(self.repo),*args],check=True,text=True,capture_output=True).stdout
 def fake(self, report, status, version='8.30.1', raw=None, commits=1):
  payload = json.dumps(report) if raw is None else raw
  self.scanner.write_text('#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n'
   + f'if sys.argv[1:] == ["version"]: print({version!r}); sys.exit(0)\n'
   + 'args=sys.argv\nassert args[args.index("--report-path")+1] == "-"\n'
   + 'assert os.fstat(1).st_mode & 0o777 == 0o600\n'
   + 'assert "--ignore-gitleaks-allow" in args\nassert "--all --no-ext-diff --no-textconv" in args\nassert "--redact" not in args\n'
   + f'sys.stdout.write({payload!r});print({SECRET!r},file=sys.stderr)\nprint("{commits} commits scanned.",file=sys.stderr)\nsys.exit({status})\n')
  self.scanner.chmod(0o700)
 def run_gate(self, exceptions=None, scanner=None):
  args=['python3',str(TOOL),'--repo',str(self.repo),'--scanner',str(scanner or self.scanner)]
  if exceptions is not None:
   path=self.base/'exceptions.json';path.write_text(json.dumps(exceptions));args+=['--exceptions',str(path)]
  p=subprocess.run(args,text=True,capture_output=True,timeout=15)
  self.assertNotIn(SECRET,p.stdout+p.stderr)
  return p
 def exception(self):
  return dict(rule='github-pat',file='configuration.env',blob_sha256=hashlib.sha256(self.blob).hexdigest(),candidate_sha256=hashlib.sha256(SECRET.encode()).hexdigest(),reason='Reviewed inert fixture, no credential.')
 def test_unreviewed_candidate_blocks_without_raw_diagnostics(self):
  self.assertEqual(self.run_gate().returncode,1)
 def test_exact_exception_passes_but_changed_hashes_do_not(self):
  row=self.exception();self.assertEqual(self.run_gate([row]).returncode,0)
  for key in ['blob_sha256','candidate_sha256']:
   changed={**row,key:'0'*64};self.assertEqual(self.run_gate([changed]).returncode,1)
 def test_optional_scanner_link_never_changes_exact_exception_authority(self):
  self.fake([{**self.finding,'Link':'https://github.com/example/public/blob/'+self.commit+'/configuration.env#L1'}],1)
  self.assertEqual(self.run_gate().returncode,1)
  self.assertEqual(self.run_gate([self.exception()]).returncode,0)
  changed={**self.exception(),'candidate_sha256':'0'*64}
  self.assertEqual(self.run_gate([changed]).returncode,1)
 def test_optional_scanner_link_rejects_malformed_metadata(self):
  for link in (None,17,{},[],"https://example.invalid/\nprivate",'x'*8193):
   with self.subTest(link_type=type(link).__name__):
    self.fake([{**self.finding,'Link':link}],1)
    self.assertEqual(self.run_gate([self.exception()]).returncode,2)
 def test_empty_clean_report_passes(self):
  self.fake([],0);self.assertEqual(self.run_gate().returncode,0)
 def test_zero_scanned_commits_are_refused_even_with_clean_exit(self):
  self.fake([],0,commits=0);self.assertEqual(self.run_gate().returncode,2)
 def test_invalid_process_report_metadata_and_exceptions_fail_closed(self):
  for report,status,version in [([],1,'8.30.1'),([self.finding],0,'8.30.1'),([],2,'8.30.1'),([],0,'8.29.0'),({},0,'8.30.1'),([{**self.finding,'Commit':'HEAD'}],1,'8.30.1'),([{**self.finding,'File':'../fixture.txt'}],1,'8.30.1'),([{**self.finding,'Secret':'absent-in-blob'}],1,'8.30.1'),([{**self.finding,'Unknown':'unsupported'}],1,'8.30.1')]:
   with self.subTest(report=type(report).__name__,status=status,version=version):
    self.fake(report,status,version);self.assertEqual(self.run_gate().returncode,2)
  self.fake([self.finding],1)
  self.assertEqual(self.run_gate([{'rule':'generic-api-key','file':'configuration.env'}]).returncode,2)
 def test_oversized_report_and_shallow_repository_fail_closed(self):
  self.fake([],0,raw=' '* (4*1024*1024+1));self.assertEqual(self.run_gate().returncode,2)
  (self.repo/'.git/shallow').write_text(self.commit+'\n');self.fake([],0)
  self.assertEqual(self.run_gate().returncode,2)
 @unittest.skipUnless(REAL.is_file(), 'Pinned local Gitleaks not available')
 def test_real_scanner_ignores_inline_allow_annotation_and_exact_exception(self):
  self.assertEqual(self.run_gate(scanner=REAL).returncode,1)
  self.assertEqual(self.run_gate([self.exception()],scanner=REAL).returncode,0)
  config=self.base/'scan.toml';config.write_text('[extend]\nuseDefault=true\n')
  ignore=self.base/'empty-ignore';ignore.touch()
  check=subprocess.run([str(REAL),'dir',str(self.base/'exceptions.json'),'--config',str(config),'--gitleaks-ignore-path',str(ignore),'--ignore-gitleaks-allow','--redact=100','--no-banner','--log-level','error','--timeout','10'],capture_output=True,text=True,timeout=15)
  self.assertEqual(check.returncode,0,'exception digest metadata itself triggered a secret finding')

if __name__=='__main__': unittest.main()
