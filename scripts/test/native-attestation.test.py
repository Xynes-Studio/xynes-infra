#!/usr/bin/env python3
"""Native process boundary fixtures; fake process output is not a signature."""
import importlib.util,json,os
from pathlib import Path
import sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/native_attestation.py';self.assertTrue(path.is_file(),'pinned native verifier adapter is missing')
  spec=importlib.util.spec_from_file_location('native_attestation',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  spec=importlib.util.spec_from_file_location('binding_fixture',ROOT/'scripts/test/attestation-binding.test.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
  self.fixture=f.Tests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.token='ghp_INERT_FIXTURE_API_TOKEN_ONLY_123456';self.registry='ghp_INERT_FIXTURE_PULL_TOKEN_ONLY_123456';self.calls=[]
 def process(self,args,**options):
  self.calls.append((args,options));self.assertNotIn(self.token,str(args));self.assertNotIn(self.registry,str(args))
  env=options['env'];self.assertEqual(env['GH_TOKEN'],self.token)
  self.assertEqual(env['GH_HOST'],'github.com');self.assertNotIn('LD_PRELOAD',env);self.assertNotIn('GH_ENTERPRISE_TOKEN',env)
  root=Path(env['HOME']);self.assertEqual(root.stat().st_mode&0o777,0o700)
  config=Path(env['DOCKER_CONFIG'])/'config.json';self.assertEqual(config.stat().st_mode&0o777,0o600)
  self.assertEqual(set(json.loads(config.read_text())['auths']),{'ghcr.io'})
  options['stdout'].write(json.dumps([self.fixture.result]).encode());options['stdout'].flush()
  class Process:
   returncode=0
   def wait(self,timeout):return 0
   def kill(self):raise AssertionError('successful process must not be killed')
  return Process()
 def verify(self,process=None):
  with patch.object(self.module,'validate_binary',return_value=Path('/owned/gh')),patch.object(self.module.subprocess,'Popen',side_effect=process or self.process):
   return self.module.verify(image=self.fixture.image,controller=self.fixture.controller,binary=Path('/owned/gh'),api_token=self.token,registry_token=self.registry)
 def test_owned_binary_exact_argv_clean_environment_private_credentials_and_bound_result(self):
  with patch.dict(os.environ,{'LD_PRELOAD':'hostile','GH_ENTERPRISE_TOKEN':'hostile'}):proof=self.verify()
  self.assertTrue(proof['native_signature_verified']);self.assertFalse(proof['deployable'])
  self.assertEqual(len(self.calls),1);args,options=self.calls[0]
  self.assertEqual(args[0],'/owned/gh');self.assertIn('--source-digest',args)
  self.assertEqual(proof['verifier_version'],'2.102.0')
  self.assertFalse(Path(options['env']['HOME']).exists())
 def test_failed_native_exit_never_accepts_plausible_output(self):
  def process(args,**options):
   p=self.process(args,**options);p.returncode=1;p.wait=lambda timeout:1;return p
  with self.assertRaises(self.module.NativeError):self.verify(process)
 def test_timeout_and_oversized_or_wrong_result_refuse_without_secret_diagnostics(self):
  for mode in ('timeout','oversized','wrong'):
   def process(args,**options):
    p=self.process(args,**options)
    if mode=='timeout':
     p.wait=lambda timeout:(_ for _ in ()).throw(self.module.subprocess.TimeoutExpired('private-value',timeout));p.kill=lambda:None
    else:
     options['stdout'].seek(0);options['stdout'].truncate();options['stdout'].write(b'X'*262145 if mode=='oversized' else b'{"verified":true}');options['stdout'].flush()
    return p
   with self.assertRaises(self.module.NativeError) as c:self.verify(process)
   self.assertEqual(str(c.exception),'Native attestation verification refused')
   self.assertNotIn(self.token,str(c.exception));self.assertNotIn(self.registry,str(c.exception))
 def test_untrusted_binary_or_credentials_refuse_before_process_launch(self):
  with patch.object(self.module,'validate_binary',side_effect=ValueError('private-value')),patch.object(self.module.subprocess,'Popen') as process:
   with self.assertRaises(self.module.NativeError):self.module.verify(image=self.fixture.image,controller=self.fixture.controller,binary=Path('/wrong/gh'),api_token=self.token,registry_token=self.registry)
   process.assert_not_called()
  for token in ('','bad\ncredential','short'):
   with patch.object(self.module,'validate_binary',return_value=Path('/owned/gh')),patch.object(self.module.subprocess,'Popen') as process:
    with self.assertRaises(self.module.NativeError):self.module.verify(image=self.fixture.image,controller=self.fixture.controller,binary=Path('/owned/gh'),api_token=token,registry_token=self.registry)
    process.assert_not_called()

if __name__=='__main__':unittest.main()
