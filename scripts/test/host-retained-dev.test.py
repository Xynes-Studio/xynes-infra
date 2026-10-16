#!/usr/bin/env python3
"""Offline coordinator fixtures; their capabilities establish no host readiness."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
NAME='xynes_dev_deploy'
ERROR='Retained Dev parking refused'


class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/host_retained_dev.py'
        self.assertTrue(path.is_file(),'retained Dev parking coordinator is missing')
        sys.path.insert(0,str(path.parent));self.addCleanup(sys.path.remove,str(path.parent))
        spec=importlib.util.spec_from_file_location('host_retained_dev',path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory(prefix='apex-retained-dev-');self.addCleanup(self.temp.cleanup)
        self.host=Path(self.temp.name).resolve();self.host.chmod(0o700)
        self.main=self.host/'etc/ssh/sshd_config';self.main.parent.mkdir(parents=True)
        self.original=b'Port 22\nDenyUsers old_disabled_user\nMatch User unrelated\n    PermitTTY no\n'
        self.main.write_bytes(self.original);self.main.chmod(0o644)
        self.account=dict(name=NAME,uid=1004,gid=1004,home='/home/'+NAME,
                          shell='/bin/bash',groups=[NAME,'docker','xynes'])
        self.current=copy.deepcopy(self.account)
        self.files={
            self.host/'home'/NAME/'.ssh/authorized_keys':b'public-key-fixture\n',
            self.host/'srv/xynes/dev/env/.env.dev':b'private-fixture-not-for-logs',
            self.host/'srv/xynes/dev/compose/retained.yml':b'retained fixture',
        }
        for path,data in self.files.items():
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o600)
        self.preservation=self.inventory()
        self.calls=[];self.token=object();self.checked=0

    def inventory(self):
        return hashlib.sha256(b''.join(str(p.relative_to(self.host)).encode()+b'\0'+p.read_bytes()
                                       for p in sorted(self.files))).hexdigest()

    def prepare(self,**changes):
        values=dict(sshd_config=self.original,account=self.account,preservation_sha256=self.preservation)
        values.update(changes);return self.module.prepare(**values)

    def process(self,argv,*,timeout_seconds,max_output):
        self.assertEqual(timeout_seconds,15);self.assertLessEqual(max_output,65536)
        self.calls.append(argv)
        if argv==['/usr/bin/getent','passwd',NAME]:
            a=self.current;out=f'{NAME}:x:{a["uid"]}:{a["gid"]}:fixture:{a["home"]}:{a["shell"]}\n'.encode()
        elif argv==['/usr/bin/id','-gn',NAME]:out=(self.current.get('primary_group',NAME)+'\n').encode()
        elif argv==['/usr/bin/id','-nG',NAME]:out=(' '.join(self.current['groups'])+'\n').encode()
        elif argv==['/usr/sbin/usermod','--shell','/usr/sbin/nologin',NAME]:
            self.current['shell']='/usr/sbin/nologin';out=b''
        elif argv[:3]==['/usr/bin/gpasswd','--delete',NAME] and argv[3] in ('docker','xynes'):
            self.current['groups'].remove(argv[3]);out=b''
        elif argv[0]=='/usr/sbin/sshd' and '-T' in argv:
            parked=b'DenyUsers '+NAME.encode()+b'\n' in self.main.read_bytes()
            out=b'passwordauthentication no\npermittty yes\nforcecommand none\ndenyusers old_disabled_user\n'
            if parked:out+=b'denyusers '+NAME.encode()+b'\n'
        elif argv==['/usr/sbin/sshd','-t','-f',str(self.main)]:out=b''
        elif argv==['/usr/bin/systemctl','reload','ssh']:out=b''
        else:self.fail('unexpected operation '+repr(argv))
        return 0,out,b''

    def proof(self,token,plan):
        self.assertIs(token,self.token);self.checked+=1
        return dict(plan_sha256=plan.plan_sha256,dev_preservation_sha256=self.inventory(),
                    dev_files_preserved=True,no_active_uid_processes=True,no_active_uid_sessions=True,
                    no_other_sudo_authority=True,protected_stage_paths=True,recovery_ssh_smoke=True)

    def park(self,plan=None,**changes):
        arguments=dict(root=self.host,run=self.process,token=self.token,verify=self.proof)
        arguments.update(changes);return self.module.park(plan or self.prepare(),**arguments)

    def test_fixed_global_deny_precedes_match_and_preserves_original_bytes(self):
        plan=self.prepare()
        self.assertEqual(plan.updated.replace(self.module.BLOCK,b''),self.original)
        self.assertLess(plan.updated.index(b'DenyUsers '+NAME.encode()),plan.updated.index(b'Match '))
        self.assertEqual(len(plan.plan_sha256),64)

    def test_parking_preserves_dev_and_only_removes_two_supplemental_groups(self):
        plan=self.prepare();result=self.park(plan)
        self.assertTrue(result['parked']);self.assertFalse(result['isolation_ready'])
        self.assertEqual(result['execution_scope'],'offline-fixture')
        self.assertEqual(self.current['groups'],[NAME]);self.assertEqual(self.current['shell'],'/usr/sbin/nologin')
        self.assertEqual(self.inventory(),self.preservation);self.assertGreaterEqual(self.checked,2)
        evidence=self.host/'etc/xynes/retained-dev'/plan.plan_sha256
        self.assertEqual(evidence.stat().st_mode&0o777,0o700)
        backup=evidence/'sshd_config.before'
        self.assertEqual(backup.read_bytes(),self.original);self.assertEqual(backup.stat().st_mode&0o777,0o600)
        mutations=[a for a in self.calls if a[0] in ('/usr/sbin/usermod','/usr/bin/gpasswd')]
        self.assertEqual(mutations,[['/usr/sbin/usermod','--shell','/usr/sbin/nologin',NAME],
                                    ['/usr/bin/gpasswd','--delete',NAME,'docker'],
                                    ['/usr/bin/gpasswd','--delete',NAME,'xynes']])
        self.park(plan)
        self.assertEqual(len([a for a in self.calls if a in mutations]),3)

    def test_missing_or_forged_capability_cannot_mutate(self):
        for token in (None,True,'ready',{},[]):
            with self.assertRaises(self.module.ParkingError):self.park(token=token)
        self.assertEqual(self.main.read_bytes(),self.original)
        self.assertFalse(any(a[0]=='/usr/sbin/usermod' for a in self.calls))

    def test_every_independent_proof_must_be_true_and_exactly_bound(self):
        for key in ('no_active_uid_processes','no_active_uid_sessions','no_other_sudo_authority',
                    'protected_stage_paths','dev_files_preserved','recovery_ssh_smoke',
                    'dev_preservation_sha256','plan_sha256'):
            def invalid(token,plan):
                value=self.proof(token,plan);value[key]=False;return value
            with self.assertRaises(self.module.ParkingError):self.park(verify=invalid)
        self.assertEqual(self.main.read_bytes(),self.original)

    def test_account_plan_and_native_drift_refuse(self):
        for key,value in [('name','xynes'),('uid',1005),('gid',1005),('home','/tmp/home'),
                          ('groups',[NAME,'sudo']),('shell','/caller/shell')]:
            account=copy.deepcopy(self.account);account[key]=value
            with self.assertRaises(self.module.ParkingError):self.prepare(account=account)
        for key,value in [('uid',9999),('primary_group','docker'),('groups',[NAME,'sudo'])]:
            self.current=copy.deepcopy(self.account);self.current[key]=value
            with self.assertRaises(self.module.ParkingError):self.park()
        self.assertEqual(self.main.read_bytes(),self.original)

    def test_symlink_mutable_ancestor_and_unknown_config_refuse(self):
        self.main.unlink();self.main.symlink_to(self.host/'outside')
        with self.assertRaises(self.module.ParkingError):self.park()
        self.main.unlink();self.main.write_bytes(self.original);self.main.chmod(0o644)
        self.main.parent.chmod(0o777)
        with self.assertRaises(self.module.ParkingError):self.park()
        self.main.parent.chmod(0o755);self.main.write_bytes(b'operator drift\n')
        with self.assertRaises(self.module.ParkingError):self.park()
        self.assertEqual(self.main.read_bytes(),b'operator drift\n')

    def test_malformed_markers_and_duplicate_account_keys_refuse(self):
        with self.assertRaises(self.module.ParkingError):
            self.prepare(sshd_config=self.original+self.module.BEGIN)
        with self.assertRaises(self.module.ParkingError):
            self.prepare(sshd_config=self.original+self.module.BLOCK*2)
        account=copy.deepcopy(self.account);account['extra']='ignored'
        with self.assertRaises(self.module.ParkingError):self.prepare(account=account)

    def test_native_config_failure_rolls_back_and_redacts(self):
        def fail(argv,**options):
            if argv[0]=='/usr/sbin/sshd' and '-t' in argv:return 1,b'private-canary',b'private-canary'
            return self.process(argv,**options)
        with self.assertRaises(self.module.ParkingError) as caught:self.park(run=fail)
        self.assertEqual(str(caught.exception),ERROR)
        self.assertEqual(self.main.read_bytes(),self.original);self.assertEqual(self.current,self.account)

    def test_recovery_profile_drift_rolls_back(self):
        def fail(argv,**options):
            result=self.process(argv,**options)
            if '-T' in argv and b'DenyUsers '+NAME.encode() in self.main.read_bytes():
                return 0,result[1]+b'allowusers caller\n',b''
            return result
        with self.assertRaises(self.module.ParkingError):self.park(run=fail)
        self.assertEqual(self.main.read_bytes(),self.original)

    def test_withdrawn_proof_after_reload_prevents_shell_and_group_revocation(self):
        def withdrawn(token,plan):
            value=self.proof(token,plan)
            if self.checked>1:value['no_active_uid_sessions']=False
            return value
        with self.assertRaises(self.module.ParkingError):self.park(verify=withdrawn)
        self.assertEqual(self.current,self.account);self.assertEqual(self.main.read_bytes(),self.original)
        self.assertGreaterEqual(len([a for a in self.calls if a==['/usr/bin/systemctl','reload','ssh']]),2)

    def test_partial_group_failure_keeps_deny_and_never_regrants_authority(self):
        def fail(argv,**options):
            if argv==['/usr/bin/gpasswd','--delete',NAME,'xynes']:return 1,b'',b'private-canary'
            return self.process(argv,**options)
        with self.assertRaises(self.module.ParkingError):self.park(run=fail)
        self.assertIn(self.module.BLOCK,self.main.read_bytes())
        self.assertEqual(self.current['shell'],'/usr/sbin/nologin');self.assertNotIn('docker',self.current['groups'])
        self.assertFalse(any('--add' in a or '-aG' in a for a in self.calls))

    def test_uid_drift_after_shell_change_refuses_before_group_removal(self):
        def drift(argv,**options):
            result=self.process(argv,**options)
            if argv[0]=='/usr/sbin/usermod':self.current['uid']=9999
            return result
        with self.assertRaises(self.module.ParkingError):self.park(run=drift)
        self.assertFalse(any(a[0]=='/usr/bin/gpasswd' for a in self.calls))
        self.assertIn(self.module.BLOCK,self.main.read_bytes())

    def test_native_parser_accepts_global_deny_with_existing_match(self):
        sshd=Path('/usr/sbin/sshd');keygen=Path('/usr/bin/ssh-keygen')
        if not sshd.is_file() or not keygen.is_file():self.skipTest('native SSH parsers unavailable')
        key=self.host/'native-host-key'
        result=subprocess.run([str(keygen),'-q','-t','ed25519','-N','','-f',str(key)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)
        original=('HostKey '+str(key)+'\n').encode()+self.original
        plan=self.prepare(sshd_config=original);native=self.host/'native-config';native.write_bytes(plan.updated)
        result=subprocess.run([str(sshd),'-T','-f',str(native),'-C','user=xynes,host=localhost,addr=127.0.0.1'],
                              capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'denyusers '+NAME.encode(),result.stdout)
        self.assertIn(b'denyusers old_disabled_user',result.stdout)


if __name__=='__main__':unittest.main()
