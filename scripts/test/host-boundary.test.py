#!/usr/bin/env python3
"""Offline namespace fixtures; coordinator tokens here are not host readiness."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
ERROR='Host deployment boundary refused'


class Tests(unittest.TestCase):
    def test_concrete_runtime_is_inside_the_exact_sealed_bundle(self):
        for name in ('host_runtime','host_supervisor','host_controls','host_smoke','host_smoke_setup',
                     'host_boundary','host_retained_dev','host_storage_control','host_quiescence'):
            self.assertIn('scripts/release/'+name+'.py',self.module.LIBRARY_FILES)

    def setUp(self):
        path=ROOT/'scripts/release/host_boundary.py'
        self.assertTrue(path.is_file(),'root-owned host boundary installer is missing')
        sys.path.insert(0,str(ROOT/'scripts/release'));self.addCleanup(sys.path.remove,str(ROOT/'scripts/release'))
        spec=importlib.util.spec_from_file_location('host_boundary',path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-boundary-test-');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'source';self.source.mkdir(mode=0o700)
        self.host=self.base/'host';self.host.mkdir(mode=0o700)
        self.hashes={}
        for name in self.module.LIBRARY_FILES:
            path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True)
            data=('# inert fixture '+name+'\ndef main():\n    return 0\n').encode() if name.endswith('.py') else b'{"fixture":true}\n'
            path.write_bytes(data);path.chmod(0o644);self.hashes[name]=hashlib.sha256(data).hexdigest()
        self.accounts={stage:dict(name='xynes_'+stage+'_deploy',uid=1100+i,gid=1100+i,
            home='/home/xynes_'+stage+'_deploy',shell='/bin/bash',groups=['xynes_'+stage+'_deploy','docker','xynes'])
            for i,stage in enumerate(('qa','staging','prod'))}
        self.current=copy.deepcopy(self.accounts)
        self.keys={stage:'ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+bytes([65+i])*32).decode()
                   for i,stage in enumerate(('qa','staging','prod'))}
        self.original=b'Port 22\nPasswordAuthentication no\n'
        main=self.host/'etc/ssh/sshd_config';main.parent.mkdir(parents=True);main.write_bytes(self.original);main.chmod(0o644)
        self.calls=[];self.token=object();self.checked=[]

    def prepare(self,**changes):
        arguments=dict(source=self.source,reviewed_hashes=self.hashes,public_keys=self.keys,
                       accounts=self.accounts,sshd_config=self.original)
        arguments.update(changes);return self.module.prepare(**arguments)

    def process(self,argv,*,timeout_seconds,max_output):
        self.calls.append(argv)
        if argv[0]=='/usr/bin/getent':
            account=next(a for a in self.current.values() if a['name']==argv[-1])
            out=f'{account["name"]}:x:{account["uid"]}:{account["gid"]}:fixture:{account["home"]}:{account["shell"]}\n'.encode()
        elif argv[:2]==['/usr/bin/id','-nG']:
            account=next(a for a in self.current.values() if a['name']==argv[-1]);out=(' '.join(account['groups'])+'\n').encode()
        elif argv[:2]==['/usr/bin/id','-gn']:
            account=next(a for a in self.current.values() if a['name']==argv[-1]);out=(account.get('primary_group',account['name'])+'\n').encode()
        elif argv[0]=='/usr/sbin/usermod':
            account=next(a for a in self.current.values() if a['name']==argv[-1]);account['shell']=argv[2];out=b''
        elif argv[0]=='/usr/bin/gpasswd':
            account=next(a for a in self.current.values() if a['name']==argv[2]);account['groups'].remove(argv[3]);out=b''
        elif argv[0]=='/usr/sbin/sshd' and '-T' in argv:
            name=argv[-1].split(',')[0].split('=')[1]
            installed=b'Include /etc/ssh/xynes-deploy-boundary.conf' in (self.host/'etc/ssh/sshd_config').read_bytes()
            selected=installed and name in {a['name'] for a in self.accounts.values()}
            profile={'forcecommand':'/usr/local/libexec/xynes-deploy-ssh' if selected else 'none',
                'authorizedkeysfile':'/etc/ssh/xynes-deploy-keys/%u' if selected else '.ssh/authorized_keys',
                'authenticationmethods':'publickey' if selected else 'any',
                'permittty':'no' if selected else 'yes','disableforwarding':'yes' if selected else 'no',
                'permituserrc':'no' if selected else 'yes','passwordauthentication':'no',
                'kbdinteractiveauthentication':'no', 'authorizedkeyscommand':'none',
                'allowusers':'xynes xynes_dev_deploy xynes_qa_deploy xynes_staging_deploy xynes_prod_deploy'}
            out=('\n'.join(k+' '+v for k,v in profile.items())+'\n').encode()
        else:
            self.assertIn(argv[0],('/usr/sbin/visudo','/usr/sbin/sshd','/usr/bin/systemctl'))
            out=b''
        return 0,out,b''

    def executor(self,token,plan):
        self.assertIs(token,self.token)
        return dict(plan_sha256=plan.plan_sha256,bundle_sha256=plan.bundle_sha256,
                    executor_sha256=self.hashes['scripts/release/root_executor.py'],executor_complete=True)

    def readiness(self,token,stage,plan):
        self.assertIs(token,self.token);self.checked.append(stage)
        return dict(stage=stage,plan_sha256=plan.plan_sha256,executor_sha256=self.hashes['scripts/release/root_executor.py'],
                    entry_smoke=True,native_admission_smoke=True,recovery_ssh_smoke=True,
                    managed_paths_sealed=True,no_active_stage_sessions=True)

    def install(self,plan=None,**changes):
        plan=plan or self.prepare()
        arguments=dict(root=self.host,run=self.process,executor_token=self.token,verify_executor=self.executor)
        arguments.update(changes);return self.module.install(plan,**arguments)

    def test_exact_reviewed_bundle_generates_fixed_isolated_bootstraps_and_hash_plan(self):
        plan=self.prepare()
        self.assertEqual(len(plan.bundle_sha256),64);self.assertEqual(len(plan.plan_sha256),64)
        self.assertEqual(set(plan.source_hashes),set(self.module.LIBRARY_FILES))
        assets={asset.path:asset for asset in plan.files}
        for name in ('xynes-deploy-ssh','xynes-deploy-root','xynes-deploy-shell'):
            script=assets['/usr/local/libexec/'+name].data.decode()
            self.assertTrue(script.startswith('#!/usr/bin/python3 -ISB\n'))
            self.assertNotIn('os.environ[',script);self.assertNotIn('os.chdir',script)
            compile(script,name,'exec')
        self.assertIn('root_executor',assets['/usr/local/libexec/xynes-deploy-root'].data.decode())
        self.assertIn('SSH_ORIGINAL_COMMAND',assets['/usr/local/libexec/xynes-deploy-shell'].data.decode())
        for stage in ('qa','staging','prod'):
            sudo=assets['/etc/sudoers.d/xynes-'+stage+'-deploy'].data.decode()
            self.assertEqual(sudo,'xynes_'+stage+'_deploy ALL=(root) NOPASSWD: /usr/local/libexec/xynes-deploy-root '+stage+'\n')
            self.assertNotIn('docker',sudo)
        self.assertFalse(any('dev_deploy' in path for path in assets))
        self.assertEqual(assets['/etc/ssh/sshd_config'].data[:len(self.original)],self.original)

    def test_backup_bootstrap_is_sealed_root_only_and_has_no_stage_sudo_grant(self):
        plan=self.prepare();assets={asset.path:asset for asset in plan.files}
        self.assertIn('/usr/local/libexec/xynes-backup',assets,'root backup bootstrap missing')
        script=assets['/usr/local/libexec/xynes-backup'].data.decode()
        self.assertIn('from scripts.release import host_backup as entry',script)
        self.assertIn('os.geteuid()!=0',script);self.assertIn('validate()',script)
        compile(script,'xynes-backup','exec')
        self.assertTrue(all('xynes-backup' not in asset.data.decode() for asset in plan.files if asset.path.startswith('/etc/sudoers.d/')))

    def test_missing_staging_account_unreviewed_hash_source_symlink_and_hardlink_refuse(self):
        accounts=copy.deepcopy(self.accounts);del accounts['staging']
        with self.assertRaises(self.module.BoundaryError):self.prepare(accounts=accounts)
        hashes=copy.deepcopy(self.hashes);hashes[next(iter(hashes))]='0'*64
        with self.assertRaises(self.module.BoundaryError):self.prepare(reviewed_hashes=hashes)
        path=self.source/self.module.LIBRARY_FILES[0];original=path.read_bytes();path.unlink();path.symlink_to(self.source/self.module.LIBRARY_FILES[1])
        with self.assertRaises(self.module.BoundaryError):self.prepare()
        path.unlink();path.write_bytes(original);os.link(path,self.source/'alias')
        with self.assertRaises(self.module.BoundaryError):self.prepare()

    def test_bad_keys_account_privileges_and_main_config_markers_refuse(self):
        for stage in ('qa','staging','prod'):
            keys=copy.deepcopy(self.keys);keys[stage]='ssh-rsa invalid'
            with self.assertRaises(self.module.BoundaryError):self.prepare(public_keys=keys)
        keys=copy.deepcopy(self.keys);keys['prod']=keys['qa']
        with self.assertRaises(self.module.BoundaryError):self.prepare(public_keys=keys)
        for key,value in [('uid',0),('name','xynes'),('groups',['sudo']),('shell','/caller/shell')]:
            accounts=copy.deepcopy(self.accounts);accounts['staging'][key]=value
            with self.assertRaises(self.module.BoundaryError):self.prepare(accounts=accounts)
        with self.assertRaises(self.module.BoundaryError):self.prepare(sshd_config=self.original+b'# BEGIN XYNES APEX DEPLOY BOUNDARY\n')

    def test_native_public_key_file_newlines_and_comments_are_normalized(self):
        keys={stage:key+' apex '+stage+' deploy\n' for stage,key in self.keys.items()}
        plan=self.prepare(public_keys=keys)
        for asset in plan.files:
            if asset.path.startswith('/etc/ssh/xynes-deploy-keys/'):
                self.assertEqual(len(asset.data.decode().splitlines()),1)
                self.assertNotIn(' apex ',asset.data.decode())

    def test_install_is_owned_convergent_and_never_revokes_legacy_privileges(self):
        plan=self.prepare();result=self.install(plan)
        self.assertTrue(result['installed']);self.assertFalse(result['privileges_revoked'])
        self.assertFalse(result['boundary_ready'])
        for asset in plan.files:
            target=self.host/asset.path.lstrip('/')
            self.assertEqual(target.read_bytes(),asset.data)
            self.assertEqual(target.stat().st_mode&0o777,asset.mode)
            self.assertEqual(target.stat().st_nlink,1)
        self.module.audit(plan,root=self.host)
        previous=len([c for c in self.calls if c[0]=='/usr/sbin/usermod'])
        self.install(plan)
        self.assertEqual(len([c for c in self.calls if c[0]=='/usr/sbin/usermod']),previous)
        self.assertFalse(any(c[0]=='/usr/bin/gpasswd' for c in self.calls))

    def test_executor_capability_is_required_before_any_install_mutation(self):
        plan=self.prepare()
        for token in (None,True,{'ready':True},'ready'):
            with self.assertRaises(self.module.BoundaryError):self.install(plan,executor_token=token)
        def invalid(token,plan):return {'executor_complete':True}
        with self.assertRaises(self.module.BoundaryError):self.install(plan,verify_executor=invalid)
        self.assertEqual((self.host/'etc/ssh/sshd_config').read_bytes(),self.original)
        self.assertFalse((self.host/'usr').exists())

    def test_mutable_ancestor_symlink_and_unowned_config_divergence_refuse(self):
        plan=self.prepare();local=self.host/'usr/local';local.mkdir(parents=True);local.chmod(0o777)
        with self.assertRaises(self.module.BoundaryError):self.install(plan)
        local.chmod(0o755);local.rmdir();outside=self.base/'outside';outside.mkdir();local.symlink_to(outside)
        with self.assertRaises(self.module.BoundaryError):self.install(plan)
        self.assertEqual(list(outside.iterdir()),[])
        local.unlink();(self.host/'etc/ssh/sshd_config').write_bytes(b'unknown operator change\n')
        with self.assertRaises(self.module.BoundaryError):self.install(plan)
        self.assertEqual((self.host/'etc/ssh/sshd_config').read_bytes(),b'unknown operator change\n')

    def test_failed_configuration_gate_restores_main_and_keeps_recovery_unchanged(self):
        def process(argv,**options):
            if argv[0]=='/usr/sbin/visudo':return 1,b'private native error',b'private native error'
            return self.process(argv,**options)
        with self.assertRaises(self.module.BoundaryError) as caught:self.install(run=process)
        self.assertEqual(str(caught.exception),ERROR)
        self.assertEqual((self.host/'etc/ssh/sshd_config').read_bytes(),self.original)
        self.assertFalse(any(c[0]=='/usr/bin/gpasswd' for c in self.calls))

    def test_staging_allowusers_assumptions_are_checked_before_mutation(self):
        def process(argv,**options):
            code,out,err=self.process(argv,**options)
            if argv[0]=='/usr/sbin/sshd' and '-T' in argv:
                out=out.replace(b' xynes_staging_deploy',b'')
            return code,out,err
        with self.assertRaises(self.module.BoundaryError):self.install(run=process)
        self.assertFalse((self.host/'usr').exists())

    def test_native_repeated_list_settings_preserve_every_value(self):
        raw=(b'forcecommand none\nallowusers xynes\nallowusers xynes_qa_deploy\n'
             b'hostkey /etc/ssh/key1\nhostkey /etc/ssh/key2\n'
             b'listenaddress [::]:22\nlistenaddress 0.0.0.0:22\n'
             b'acceptenv LANG\nacceptenv LC_*\n')
        result=self.module.profile(lambda argv,**kwargs:(0,raw,b''),self.host,'xynes')
        self.assertEqual(result['allowusers'],'xynes xynes_qa_deploy')
        self.assertEqual(result['hostkey'],'/etc/ssh/key1 /etc/ssh/key2')
        self.assertEqual(result['listenaddress'],'[::]:22 0.0.0.0:22')
        self.assertEqual(result['acceptenv'],'LANG LC_*')
        self.assertTrue(self.module.allowed(result,'xynes'))
        self.assertTrue(self.module.allowed(result,'xynes_qa_deploy'))

    def test_repeated_single_value_security_setting_refuses(self):
        raw=b'forcecommand none\nforcecommand /untrusted\n'
        with self.assertRaises(self.module.BoundaryError):
            self.module.profile(lambda argv,**kwargs:(0,raw,b''),self.host,'xynes')

    def test_denyusers_overrides_allowusers(self):
        value={'allowusers':'xynes_*','denyusers':'xynes_dev_deploy'}
        self.assertFalse(self.module.allowed(value,'xynes_dev_deploy'))
        self.assertTrue(self.module.allowed(value,'xynes_qa_deploy'))

    def test_group_and_host_scoped_access_rules_fail_closed_without_facts(self):
        for value in ({'allowgroups':'deployers'},{'denygroups':'restricted'},
                      {'denyusers':'xynes_qa_deploy@*'},{'allowusers':'xynes_qa_deploy@*'}):
            self.assertFalse(self.module.allowed(value,'xynes_qa_deploy'))

    def test_membership_revocation_requires_coordinator_smoke_for_each_stage(self):
        plan=self.prepare();self.install(plan)
        for token in (None,True,{'entry_smoke':True}):
            with self.assertRaises(self.module.BoundaryError):self.module.revoke(plan,root=self.host,run=self.process,token=token,verify=self.readiness)
        def incomplete(token,stage,plan):
            value=self.readiness(token,stage,plan);value['native_admission_smoke']=False;return value
        with self.assertRaises(self.module.BoundaryError):self.module.revoke(plan,root=self.host,run=self.process,token=self.token,verify=incomplete)
        self.assertFalse(any(c[0]=='/usr/bin/gpasswd' for c in self.calls))
        self.checked.clear()
        result=self.module.revoke(plan,root=self.host,run=self.process,token=self.token,verify=self.readiness)
        self.assertTrue(result['privileges_revoked']);self.assertFalse(result['boundary_ready'])
        self.assertEqual(set(self.checked),{'qa','staging','prod'})
        commands=[c for c in self.calls if c[0]=='/usr/bin/gpasswd']
        self.assertEqual(len(commands),6)
        self.assertFalse(any('xynes_dev_deploy' in c or c[2]=='xynes' for c in commands))
        self.assertTrue(all(a['groups']==[a['name']] for a in self.current.values()))

    def test_changed_native_staging_account_refuses_before_any_membership_removal(self):
        plan=self.prepare();self.install(plan)
        self.current['staging']['uid']=9999
        with self.assertRaises(self.module.BoundaryError):
            self.module.revoke(plan,root=self.host,run=self.process,token=self.token,verify=self.readiness)
        self.assertFalse(any(c[0]=='/usr/bin/gpasswd' for c in self.calls))

    def test_standalone_audit_detects_replaced_linked_or_mutable_installed_source(self):
        plan=self.prepare();self.install(plan)
        target=self.host/next(asset.path.lstrip('/') for asset in plan.files if asset.path.endswith('/deploy_request.py'))
        target.chmod(0o644);target.write_bytes(b'tampered');target.chmod(0o444)
        with self.assertRaises(self.module.BoundaryError):self.module.audit(plan,root=self.host)

    def test_docker_or_xynes_primary_group_refuses_before_install_mutation(self):
        for group in ('docker','xynes'):
            self.current['staging']['primary_group']=group
            with self.assertRaises(self.module.BoundaryError):self.install()
        self.assertFalse((self.host/'usr').exists())

    def test_standalone_audit_refuses_extra_symlink_and_mutable_directories(self):
        plan=self.prepare();self.install(plan)
        library=self.host/(self.module.LIBRARY+plan.bundle_sha256).lstrip('/')
        library.chmod(0o755);extra=library/'unexpected';extra.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(self.module.BoundaryError):self.module.audit(plan,root=self.host)
        extra.unlink();extra.mkdir(mode=0o777);extra.chmod(0o777)
        with self.assertRaises(self.module.BoundaryError):self.module.audit(plan,root=self.host)

    def test_default_control_process_is_bounded_and_redacts_process_output(self):
        import ssh_entry
        actual=ssh_entry.subprocess.Popen
        def popen(argv,**options):
            return actual([sys.executable,'-I','-c','import sys; sys.stdout.write("X"*70000)'],**options)
        with patch.object(ssh_entry.subprocess,'Popen',side_effect=popen):
            with self.assertRaises(self.module.BoundaryError) as caught:
                self.module.command(['/usr/bin/id','-nG','xynes_qa_deploy'],timeout_seconds=1,max_output=1024*1024)
        self.assertEqual(str(caught.exception),ERROR)

    def test_native_configuration_parsers_accept_temporary_managed_rules(self):
        plan=self.prepare();assets={asset.path:asset.data for asset in plan.files}
        visudo=Path('/usr/sbin/visudo');sshd=Path('/usr/sbin/sshd');keygen=Path('/usr/bin/ssh-keygen')
        if not all(p.is_file() for p in (visudo,sshd,keygen)):self.skipTest('native config parsers unavailable')
        tmp=self.base/'native';tmp.mkdir(mode=0o700)
        key=tmp/'hostkey'
        result=subprocess.run([str(keygen),'-q','-t','ed25519','-N','','-f',str(key)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)
        snippet=tmp/'boundary.conf';snippet.write_bytes(assets['/etc/ssh/xynes-deploy-boundary.conf'])
        main=tmp/'sshd_config';main.write_text('HostKey '+str(key)+'\nInclude '+str(snippet)+'\n')
        result=subprocess.run([str(sshd),'-T','-f',str(main),'-C','user=xynes_qa_deploy,host=localhost,addr=127.0.0.1'],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertIn(b'forcecommand /usr/local/libexec/xynes-deploy-ssh',result.stdout)
        result=subprocess.run([str(sshd),'-T','-f',str(main),'-C','user=xynes,host=localhost,addr=127.0.0.1'],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0);self.assertIn(b'forcecommand none',result.stdout)
        for stage in ('qa','staging','prod'):
            sudo=tmp/('sudo-'+stage);sudo.write_bytes(assets['/etc/sudoers.d/xynes-'+stage+'-deploy'])
            result=subprocess.run([str(visudo),'-c','-f',str(sudo)],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0)


if __name__=='__main__':unittest.main()
