#!/usr/bin/env python3
"""CI Docker store activation fixtures; no Docker, systemctl or /etc mutation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))

class Tests(unittest.TestCase):
    def setUp(self):
        import ci_smoke_engine
        self.module=ci_smoke_engine;self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.work=self.root/'work';self.work.mkdir(mode=0o700)
        self.etc=self.root/'docker';self.etc.mkdir(mode=0o700);self.config=self.etc/'daemon.json'
        self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'Xynes-Studio/xynes-infra','GITHUB_EVENT_NAME':'workflow_dispatch',
            'GITHUB_JOB':'historical-integration','GITHUB_REF':'refs/heads/test/apex-ci-smoke-fixture',
            'APEX_CI_RUNNER_ENVIRONMENT':'github-hosted','APEX_CI_RUNNER_OS':'Linux'}
        self.command_runner=self.module.command
        self.enabled=False;self.containers=b'';self.calls=[];self.restart_failure=False
        self.patches=[patch.object(self.module,'DAEMON_CONFIG',self.config),patch.object(self.module,'ROOT_UID',os.getuid()),
            patch.object(self.module.os,'geteuid',return_value=0),patch.object(self.module.os,'fchown'),patch.object(self.module,'command',self.command)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def command(self,argv,**kwargs):
        self.calls.append(list(argv))
        if argv[0]=='/usr/bin/systemctl':
            if self.restart_failure:raise ValueError('DO_NOT_EXPOSE')
            self.enabled=True;return b''
        if 'info' in argv:return json.dumps([['driver-type','io.containerd.snapshotter.v1']] if self.enabled else [['Backing Filesystem','extfs']]).encode()
        if 'ps' in argv:return self.containers
        raise AssertionError('unexpected fixed command')
    def prepare(self):return self.module.prepare(self.env,self.work)
    def test_classic_empty_engine_preserves_config_and_original_backup(self):
        original=b'{ "log-driver": "local", "features": {"buildkit": true}, "dns": ["127.0.0.53"] }\n'
        self.config.write_bytes(original);self.config.chmod(0o640)
        result=self.prepare();self.assertEqual(result,{'containerd_store':True,'changed':True})
        expected=json.loads(original);expected['features']['containerd-snapshotter']=True
        self.assertEqual(json.loads(self.config.read_bytes()),expected)
        self.assertEqual((self.work/'daemon.json.before').read_bytes(),original)
        self.assertEqual(self.config.stat().st_mode&0o777,0o640)
        self.assertEqual(sum(argv==['/usr/bin/systemctl','restart','docker'] for argv in self.calls),1)
        self.assertNotIn('prune',' '.join(' '.join(row) for row in self.calls))
    def test_already_containerd_is_readonly(self):
        self.enabled=True
        self.assertEqual(self.prepare(),{'containerd_store':True,'changed':False})
        self.assertFalse(self.config.exists());self.assertFalse((self.work/'daemon.json.before').exists())
        self.assertEqual(len(self.calls),1)
    def test_nonempty_containers_refuse_before_configuration_write(self):
        self.containers=b'container-id\n';self.config.write_text('{}')
        with self.assertRaises(self.module.EngineError):self.prepare()
        self.assertEqual(self.config.read_text(),'{}')
        self.assertFalse(any(argv[0]=='/usr/bin/systemctl' for argv in self.calls))
    def test_wrong_metadata_and_self_hosted_refuse_before_commands(self):
        for key,value in (('APEX_CI_RUNNER_ENVIRONMENT','self-hosted'),('APEX_CI_RUNNER_OS','macOS'),
                          ('GITHUB_REPOSITORY','other/repo'),('GITHUB_JOB','publish-images'),('GITHUB_REF','refs/heads/main')):
            with self.subTest(key=key),self.assertRaises(self.module.EngineError):self.module.prepare({**self.env,key:value},self.work)
        self.assertEqual(self.calls,[])
    def test_product_smoke_requires_protected_main(self):
        env={**self.env,'GITHUB_JOB':'product-smoke','GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true'}
        self.enabled=True;self.assertFalse(self.module.prepare(env,self.work)['changed'])
        with self.assertRaises(self.module.EngineError):self.module.prepare({**env,'GITHUB_REF_PROTECTED':'false'},self.work)
    def test_symlink_config_refused(self):
        target=self.etc/'original';target.write_text('{}');self.config.symlink_to(target)
        with self.assertRaises(self.module.EngineError):self.prepare()
        self.assertEqual(target.read_text(),'{}')
        self.assertFalse(any(argv[0]=='/usr/bin/systemctl' for argv in self.calls))
    def test_failed_restart_retains_backup_and_fails_closed(self):
        original=b'{"debug":false}';self.config.write_bytes(original);self.restart_failure=True
        with self.assertRaises(self.module.EngineError) as caught:self.prepare()
        self.assertEqual(str(caught.exception),self.module.ERROR)
        self.assertNotIn('DO_NOT_EXPOSE',str(caught.exception))
        self.assertEqual((self.work/'daemon.json.before').read_bytes(),original)
        self.assertIs(json.loads(self.config.read_bytes())['features']['containerd-snapshotter'],True)
    def test_absent_daemon_file_creates_only_feature_and_records_absence(self):
        self.assertEqual(self.prepare(),{'containerd_store':True,'changed':True})
        self.assertEqual(json.loads(self.config.read_bytes()),{'features':{'containerd-snapshotter':True}})
        self.assertEqual((self.work/'daemon.json.before').read_bytes(),b'')
        self.assertEqual(json.loads((self.work/'daemon-config-backup.json').read_bytes()),{'config_existed':False})
    def test_nonroot_refused_before_commands(self):
        with patch.object(self.module.os,'geteuid',return_value=1001),self.assertRaises(self.module.EngineError):self.prepare()
        self.assertEqual(self.calls,[])
    def test_group_writable_or_hardlinked_config_refused(self):
        for kind in ('writable','hardlinked'):
            with self.subTest(kind=kind):
                work=self.root/kind;work.mkdir(mode=0o700)
                self.config.write_text('{}')
                if kind=='writable':self.config.chmod(0o660)
                else:os.link(self.config,self.etc/'link')
                with self.assertRaises(self.module.EngineError):self.module.prepare(self.env,work)
                self.assertEqual(self.config.read_text(),'{}');self.config.chmod(0o600)
    def test_poll_is_bounded_and_never_accepts_classic_readback(self):
        original=self.command
        def still_classic(argv,**kwargs):
            result=original(argv,**kwargs)
            if 'info' in argv:return b'[["Backing Filesystem","extfs"]]'
            return result
        with patch.object(self.module,'command',side_effect=still_classic),patch.object(self.module.time,'sleep') as sleep:
            with self.assertRaises(self.module.EngineError):self.prepare()
        self.assertEqual(sum('info' in row for row in self.calls),31)
        self.assertEqual(sleep.call_count,29)
        self.assertTrue((self.work/'daemon.json.before').exists())
    def test_native_runner_constructs_clean_environment_and_redacts_error(self):
        with patch.object(self.module.subprocess,'Popen',side_effect=OSError('DO_NOT_EXPOSE')) as process:
            with self.assertRaises(self.module.EngineError) as caught:
                self.command_runner(['/usr/bin/systemctl','restart','docker'])
        self.assertEqual(str(caught.exception),self.module.ERROR)
        self.assertEqual(process.call_args.kwargs['env'],self.module.clean_environment())
    def test_conflicting_flags_refused_without_removing_them(self):
        for config in ({'userns-remap':'default'},{'storage-driver':'overlay2'},{'features':'invalid'}):
            with self.subTest(config=config):
                self.config.write_text(json.dumps(config));self.calls=[]
                with self.assertRaises(self.module.EngineError):self.prepare()
                self.assertEqual(json.loads(self.config.read_bytes()),config)
                self.assertFalse(any(argv[0]=='/usr/bin/systemctl' for argv in self.calls))
                # A refused invocation can leave its own empty client directory.
                import shutil
                shutil.rmtree(self.work/'engine-client',ignore_errors=True)

if __name__=='__main__':unittest.main()
