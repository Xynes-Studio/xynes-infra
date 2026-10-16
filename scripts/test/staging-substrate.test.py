#!/usr/bin/env python3
"""Real temporary filesystem + injectable Docker response tests. No host/Docker operations."""
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / 'lib/staging_substrate.py'
LABELS = {'io.xynes.managed': 'true', 'io.xynes.owner': 'xynes-infra',
          'io.xynes.environment': 'staging', 'io.xynes.apex.substrate': '1'}
DESIRED = {'staging': 0o755, 'staging/compose': 0o750, 'staging/env': 0o750, 'staging/supabase': 0o755}


def network():
    return {'id': 'a' * 64, 'name': 'xynes_staging_net', 'driver': 'bridge', 'scope': 'local',
            'internal': False, 'attachable': False, 'ingress': False, 'ipv6': False,
            'options': {}, 'labels': copy.deepcopy(LABELS)}


class DockerFixture:
    def __init__(self):
        self.state = None
        self.calls = []
        self.before = None
        self.fail = None
        self.list_override = None
        self.inspect_override = None

    def __call__(self, argv):
        self.calls.append(argv)
        if self.before:
            self.before(argv)
        if self.fail and self.fail in argv:
            return subprocess.CompletedProcess(argv, 1, '', 'sensitive upstream value')
        if argv[3:5] == ['network', 'ls']:
            out = self.list_override if self.list_override is not None else ('' if self.state is None else json.dumps({'id': self.state['id'], 'name': self.state['name']}))
        elif argv[3:5] == ['network', 'inspect']:
            out = self.inspect_override if self.inspect_override is not None else json.dumps(self.state)
        elif argv[3:5] == ['network', 'create']:
            self.state = network();out = self.state['id'] + '\n'
        else:
            raise AssertionError('unexpected command: ' + repr(argv))
        return subprocess.CompletedProcess(argv, 0, out, '')


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-substrate-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'srv' / 'xynes';self.root.mkdir(parents=True);self.root.chmod(0o755)
        self.docker = DockerFixture()
        self.module = None

    def tool(self):
        self.assertTrue(TOOL.is_file(), 'bounded staging substrate provisioner is missing')
        if not self.module:
            spec = importlib.util.spec_from_file_location('staging_substrate', TOOL)
            self.module = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        return self.module

    def provisioner(self, **extra):
        options = {'root': self.root, 'uid': os.getuid(), 'gid': os.getgid(), 'root_gid': os.getgid(),
                   'runner': self.docker, 'euid': lambda: 0}
        options.update(extra)
        return self.tool().Substrate(**options)

    def existing(self):
        for path, mode in DESIRED.items():
            p = self.root / path;p.mkdir();p.chmod(mode)
        self.docker.state = network()

    def test_default_plan_is_deterministic_read_only_and_precisely_scoped(self):
        instance = self.provisioner();first = instance.plan();second = instance.plan()
        self.assertEqual(first, second)
        self.assertEqual([a['path'] for a in first['actions'] if a['operation'] == 'mkdir'], list(DESIRED))
        self.assertEqual(len(first['actions']), 5)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(any('create' in c for c in self.docker.calls))
        self.assertRegex(first['plan_sha256'], r'^[0-9a-f]{64}$')

    def test_apply_uses_real_fds_metadata_and_converges_idempotently(self):
        instance = self.provisioner();plan = instance.plan();result = instance.apply(plan['plan_sha256'])
        self.assertEqual(result['status'], 'converged')
        for path, mode in DESIRED.items():
            s = (self.root / path).stat();self.assertEqual((s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)), (os.getuid(), os.getgid(), mode))
        self.assertEqual(instance.plan()['actions'], [])
        self.assertEqual(instance.apply(instance.plan()['plan_sha256'])['status'], 'converged')
        self.assertEqual(sum('create' in c for c in self.docker.calls), 1)

    def test_existing_env_data_dev_qa_prod_are_byte_preserved(self):
        for stage in ('dev', 'qa', 'prod'):
            p = self.root / stage;p.mkdir();(p / '.env').write_bytes(b'do-not-read-or-touch\x00private')
        self.existing();secret = self.root / 'staging/env/.env.staging';secret.write_bytes(b'preserve-private')
        db = self.root / 'staging/supabase/db';db.mkdir();db.chmod(0o700);(db/'data').write_bytes(b'database')
        instance = self.provisioner();instance.apply(instance.plan()['plan_sha256'])
        self.assertEqual(secret.read_bytes(), b'preserve-private');self.assertEqual((db/'data').read_bytes(), b'database')
        for stage in ('dev', 'qa', 'prod'):self.assertEqual((self.root/stage/'.env').read_bytes(), b'do-not-read-or-touch\x00private')
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o700)

    def test_existing_directory_mode_mismatch_refuses_without_chmod(self):
        self.existing();p = self.root/'staging/env';p.chmod(0o777)
        with self.assertRaises(ValueError):self.provisioner().plan()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o777)

    def test_existing_directory_gid_mismatch_refuses_without_chown(self):
        self.existing()
        with self.assertRaises(ValueError):self.provisioner(gid=os.getgid()+1).plan()
        self.assertEqual((self.root/'staging').stat().st_gid, os.getgid())

    def test_non_directory_target_refuses_and_preserves_contents(self):
        p = self.root/'staging';p.write_text('private')
        with self.assertRaises(ValueError):self.provisioner().plan()
        self.assertEqual(p.read_text(), 'private')

    def test_target_symlink_refuses_without_touching_outside(self):
        outside = self.base/'outside';outside.mkdir();(outside/'keep').write_text('private')
        (self.root/'staging').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):self.provisioner().plan()
        self.assertEqual(list(outside.iterdir()), [outside/'keep'])

    def test_ancestor_symlink_refuses_even_when_final_root_is_real(self):
        link = self.base/'alias';link.symlink_to(self.base/'srv', target_is_directory=True)
        with self.assertRaises(ValueError):self.provisioner(root=link/'xynes').plan()

    def test_path_traversal_relative_missing_root_and_bad_root_metadata_refuse(self):
        for path in (Path('relative'), self.root/'..'/'xynes', self.base/'absent'):
            with self.subTest(path=str(path)), self.assertRaises(ValueError):self.provisioner(root=path).plan()
        self.root.chmod(0o775)
        with self.assertRaises(ValueError):self.provisioner().plan()

    def test_unowned_or_foreign_network_refuses_without_network_change(self):
        for labels in ({}, {**LABELS, 'io.xynes.environment': 'dev'}, {**LABELS, 'io.xynes.owner': 'unknown'}, {**LABELS, 'extra': 'unreviewed'}):
            self.docker.state = {**network(), 'labels': labels}
            with self.subTest(labels=labels), self.assertRaises(ValueError):self.provisioner().plan()
        self.assertFalse(any('create' in c for c in self.docker.calls))

    def test_existing_network_wrong_actual_attributes_refuses(self):
        for key, bad in [('driver', 'overlay'), ('scope', 'swarm'), ('internal', True), ('attachable', True),
                         ('ingress', True), ('ipv6', True), ('options', {'unreviewed': 'value'}), ('internal', 'false')]:
            self.docker.state = {**network(), key: bad}
            with self.subTest(key=key), self.assertRaises(ValueError):self.provisioner().plan()

    def test_duplicate_network_names_and_malformed_or_duplicate_json_refuse(self):
        for raw in ('{}', '{"id":"x","id":"y"}', json.dumps({'id':'a'*64,'name':'xynes_staging_net'})+'\n'+json.dumps({'id':'b'*64,'name':'xynes_staging_net'})):
            self.docker.list_override = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):self.provisioner().plan()

    def test_absence_never_inferred_from_docker_failure(self):
        self.docker.fail = 'ls'
        with self.assertRaises(ValueError):self.provisioner().plan()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_inspection_failure_and_identity_disagreement_refuse(self):
        self.docker.state = network();self.docker.fail = 'inspect'
        with self.assertRaises(ValueError):self.provisioner().plan()
        self.docker.fail = None;self.docker.inspect_override = json.dumps({**network(), 'id': 'b'*64})
        with self.assertRaises(ValueError):self.provisioner().plan()

    def test_nonroot_apply_refuses_before_lock_filesystem_or_network_mutation(self):
        instance = self.provisioner(euid=lambda: 501);plan = instance.plan();before = len(self.docker.calls)
        with self.assertRaises(ValueError):instance.apply(plan['plan_sha256'])
        self.assertEqual(list(self.root.iterdir()), []);self.assertEqual(len(self.docker.calls), before)

    def test_wrong_or_missing_plan_sha_refuses_without_substrate_creation(self):
        instance = self.provisioner();instance.plan()
        for digest in (None, 'main', '0'*64, 'A'*64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):instance.apply(digest)
        self.assertFalse((self.root/'staging').exists());self.assertIsNone(self.docker.state)

    def test_stale_plan_refuses_when_directory_identity_or_network_changed(self):
        self.existing();instance = self.provisioner();p = instance.plan()
        path = self.root/'staging/compose';path.rmdir();path.mkdir();path.chmod(0o750)
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        p = instance.plan();self.docker.state['id'] = 'b'*64
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])

    def test_owned_lock_is_exclusive_and_never_unlinked(self):
        instance = self.provisioner();p=instance.plan();lock=self.root/'.apex-staging-substrate.lock'
        fd=os.open(lock, os.O_CREAT|os.O_EXCL|os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
            self.assertFalse((self.root/'staging').exists())
        finally:os.close(fd)
        self.assertTrue(lock.is_file());instance.apply(p['plan_sha256'])

    def test_wrong_mode_nonempty_hardlinked_or_symlink_lock_refuses(self):
        instance=self.provisioner();p=instance.plan();lock=self.root/'.apex-staging-substrate.lock'
        lock.write_text('');lock.chmod(0o666)
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        lock.chmod(0o600);lock.write_text('private')
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        lock.write_text('');os.link(lock,self.root/'foreign-link')
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        (self.root/'foreign-link').unlink();lock.unlink();outside=self.base/'private';outside.write_text('keep');lock.symlink_to(outside)
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertEqual(outside.read_text(),'keep')

    def test_just_in_time_symlink_race_cannot_chown_or_modify_outside(self):
        instance=self.provisioner();p=instance.plan();outside=self.base/'outside';outside.mkdir();outside.chmod(0o700)
        calls=0
        def race(argv):
            nonlocal calls
            if argv[3:5]==['network','ls']:
                calls+=1
                if calls==1:(self.root/'staging').symlink_to(outside,target_is_directory=True)
        self.docker.before=race
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o700);self.assertEqual(list(outside.iterdir()),[])
        self.assertIsNone(self.docker.state)

    def test_root_path_replacement_after_plan_does_not_write_detached_or_replacement_tree(self):
        instance=self.provisioner();p=instance.plan();old=self.base/'original'
        triggered=False
        def race(argv):
            nonlocal triggered
            if argv[3:5]==['network','ls'] and not triggered:
                triggered=True;self.root.rename(old);self.root.mkdir();self.root.chmod(0o755)
        self.docker.before=race
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertFalse((old/'staging').exists());self.assertFalse((self.root/'staging').exists())

    def test_mkdir_eexist_race_rechecks_metadata_instead_of_repairing_it(self):
        instance=self.provisioner();p=instance.plan();original=os.mkdir
        def race(name, mode=0o777, *, dir_fd=None):
            if name=='staging':
                original(name, 0o777, dir_fd=dir_fd)
                os.chmod(name, 0o777, dir_fd=dir_fd)
                raise FileExistsError()
            return original(name, mode, dir_fd=dir_fd)
        with patch.object(self.module.os, 'mkdir', race), self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertEqual(stat.S_IMODE((self.root/'staging').stat().st_mode),0o777)
        self.assertIsNone(self.docker.state)

    def test_matching_mkdir_eexist_race_converges_without_chowning_incumbent(self):
        instance=self.provisioner();p=instance.plan();original=os.mkdir
        def race(name, mode=0o777, *, dir_fd=None):
            if name=='staging':
                original(name, 0o755, dir_fd=dir_fd);os.chmod(name, 0o755, dir_fd=dir_fd)
                raise FileExistsError()
            return original(name, mode, dir_fd=dir_fd)
        with patch.object(self.module.os, 'mkdir', race):result=instance.apply(p['plan_sha256'])
        self.assertEqual(result['status'],'converged')

    def test_lock_replacement_during_revalidation_refuses_before_substrate_write(self):
        instance=self.provisioner();p=instance.plan();lock=self.root/'.apex-staging-substrate.lock'
        replaced=False
        def race(argv):
            nonlocal replaced
            if argv[3:5]==['network','ls'] and not replaced:
                replaced=True;lock.unlink();fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);os.close(fd)
        self.docker.before=race
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertFalse((self.root/'staging').exists());self.assertIsNone(self.docker.state)

    def test_network_creation_failure_leaves_created_directories_and_retry_converges(self):
        instance=self.provisioner();p=instance.plan();self.docker.fail='create'
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        for path in DESIRED:self.assertTrue((self.root/path).is_dir())
        self.docker.fail=None;p=instance.plan();self.assertEqual(len(p['actions']),1)
        self.assertEqual(instance.apply(p['plan_sha256'])['status'],'converged')

    def test_post_create_wrong_network_attributes_refuse_and_never_delete(self):
        instance=self.provisioner();p=instance.plan()
        original=self.docker.__call__
        def runner(argv):
            r=original(argv)
            if argv[3:5]==['network','create']:self.docker.state['internal']=True
            return r
        instance.runner=runner
        with self.assertRaises(ValueError):instance.apply(p['plan_sha256'])
        self.assertTrue(self.docker.state['internal']);self.assertFalse(any('rm' in c or 'prune' in c for c in self.docker.calls))

    def test_command_catalog_uses_only_local_daemon_and_whitelisted_metadata(self):
        instance=self.provisioner();instance.apply(instance.plan()['plan_sha256'])
        for argv in self.docker.calls:
            self.assertEqual(argv[:3], ['/usr/bin/docker','--host','unix:///var/run/docker.sock'])
            self.assertIn(argv[4],('ls','inspect','create'))
            if argv[4]=='inspect':
                fmt=argv[argv.index('--format')+1]
                self.assertNotIn('Env',fmt);self.assertNotIn('Containers',fmt)
                for field in ('.Id','.Name','.Driver','.Scope','.Internal','.Attachable','.Ingress','.EnableIPv6','.Options','.Labels'):self.assertIn(field,fmt)
        create=next(c for c in self.docker.calls if 'create' in c)
        for k,v in LABELS.items():self.assertIn(k+'='+v,create)

    def test_actual_cli_rejects_custom_root_and_bad_apply_arguments_generically(self):
        self.tool()
        for args in (['--root','DO-NOT-ECHO'],['--apply'],['--apply','--plan-sha256','DO-NOT-ECHO'],['--dry-run','--apply']):
            p=subprocess.run([sys.executable,str(TOOL),*args],text=True,capture_output=True)
            self.assertNotEqual(p.returncode,0);self.assertEqual(p.stdout,'');self.assertNotIn('DO-NOT-ECHO',p.stderr)


if __name__=='__main__':unittest.main()
