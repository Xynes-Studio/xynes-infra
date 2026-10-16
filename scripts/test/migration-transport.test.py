#!/usr/bin/env python3
"""Data-only migration transport fixtures: never execute source/SQL or use a DB."""
import copy
import hashlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module

def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()

class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/migration_transport.py'
        self.assertTrue(path.is_file(),'source-bound migration transport is missing')
        self.module=load('migration_transport',path)
        self.fixture=load('transport_plan_fixture',ROOT/'scripts/test/migration-plan.test.py').MigrationPlanTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.manifest=copy.deepcopy(self.fixture.manifest)
        self.controller={'repository':'Xynes-Studio/xynes-infra','commit':'a'*40,'workflow':'apex-build.yml','run_id':123,'run_attempt':1}
        for owner,repo in self.fixture.roots.items():
            git(repo,'init','-q');git(repo,'config','user.name','Inert Fixture');git(repo,'config','user.email','fixture@example.invalid')
            git(repo,'add','.');git(repo,'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','inert migration source')
            commit=git(repo,'rev-parse','HEAD')
            if owner=='xynes-infra':self.controller['commit']=commit
            else:self.manifest['sources'][owner]['commit']=commit
        self.manifest.update(status='approved',approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1')
        self.manifest_bytes=(json.dumps(self.manifest,indent=2)+'\n').encode()
        self.temp=tempfile.TemporaryDirectory(prefix='apex-transport-');self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name).resolve();self.work.chmod(0o700)
        self.bundle=self.work/'migration-bundle.tar'
        self.owner=patch.object(self.module,'EXTRACT_UID',os.getuid());self.owner.start();self.addCleanup(self.owner.stop)

    def create(self,**overrides):
        kwargs=dict(manifest_bytes=self.manifest_bytes,controller=self.controller,repositories=self.fixture.roots,output=self.bundle)
        kwargs.update(overrides);return self.module.create(**kwargs)

    def extract(self,receipt=None,**overrides):
        kwargs=dict(bundle=self.bundle,expected_sha256=(receipt or self.receipt)['bundle_sha256'],
                    manifest_bytes=self.manifest_bytes,controller=self.controller,work=self.work/'receiver')
        kwargs['work'].mkdir(mode=0o700,exist_ok=True)
        kwargs.update(overrides);return self.module.extract(**kwargs)

    def refused(self,callback):
        with self.assertRaises(self.module.TransportError) as caught:callback()
        self.assertEqual(str(caught.exception),'Migration transport refused')

    def rewrite(self,change):
        with tarfile.open(self.bundle,'r:') as source:
            members=[(copy.copy(member),source.extractfile(member).read()) for member in source.getmembers()]
        members=change(members)
        with tarfile.open(self.bundle,'w',format=tarfile.USTAR_FORMAT) as output:
            for info,raw in members:output.addfile(info,io.BytesIO(raw) if info.isreg() else None)
        return {'bundle_sha256':hashlib.sha256(self.bundle.read_bytes()).hexdigest()}

    def test_complete_deterministic_bundle_and_exact_manifest_source_units(self):
        self.receipt=self.create();first=self.bundle.read_bytes()
        another=self.work/'second.tar';second=self.create(output=another)
        self.assertEqual(first,another.read_bytes());self.assertEqual(self.receipt,second)
        verified=self.extract()
        self.assertEqual(len(verified.plan['units']),40)
        self.assertEqual(len(verified.inventory['migrations']['xynes-infra']),25)
        self.assertEqual(len(verified.files),92)
        self.assertEqual((verified.directory/'source-manifest.json').read_bytes(),self.manifest_bytes)
        units,payloads=self.module.application_inputs(verified)
        self.assertEqual(units,verified.plan['units']);self.assertEqual(len(payloads),40)
        self.assertNotIn(('xynes-infra',self.module.migration_plan.DEV_PROVIDER),payloads)
        self.assertFalse(verified.plan['approved']);self.assertFalse(verified.plan['deployable'])
        for file in verified.directory.rglob('*'):
            self.assertEqual(file.stat().st_mode&0o777,0o700 if file.is_dir() else 0o600)

    def test_writer_uses_exact_git_blobs_and_refuses_wrong_checkout_or_planning(self):
        self.receipt=self.create();original=self.bundle.read_bytes()
        path=self.fixture.roots['xynes-doc-service']/'drizzle/0000_complete_war_machine.sql'
        path.write_text('DROP DATABASE unsafe;')
        other=self.work/'dirty.tar';self.create(output=other);self.assertEqual(original,other.read_bytes())
        bad=copy.deepcopy(self.controller);bad['commit']='b'*40
        self.refused(lambda:self.create(controller=bad,output=self.work/'bad.tar'))
        planning=copy.deepcopy(self.manifest);planning.update(status='planning',approval_reference=None)
        self.refused(lambda:self.create(manifest_bytes=json.dumps(planning).encode(),output=self.work/'bad.tar'))
        self.assertFalse((self.work/'bad.tar').exists())

    def test_archive_native_hash_manifest_byte_and_controller_binding(self):
        self.receipt=self.create()
        self.refused(lambda:self.extract(expected_sha256='0'*64))
        self.refused(lambda:self.extract(manifest_bytes=json.dumps(self.manifest,separators=(',',':')).encode()))
        changed=copy.deepcopy(self.controller);changed['run_id']+=1
        self.refused(lambda:self.extract(controller=changed))
        self.assertFalse((self.work/'receiver'/'migration').exists())

    def test_missing_duplicate_extra_and_reordered_sql_refused(self):
        self.receipt=self.create();original=self.bundle.read_bytes()
        for mode in ('missing','duplicate','extra','plan-order'):
            self.bundle.write_bytes(original)
            def change(members):
                if mode=='missing':return members[:-1]
                if mode=='duplicate':return members+[members[-1]]
                if mode=='extra':
                    info=tarfile.TarInfo('private.txt');info.mode=0o600;info.size=6
                    return members+[(info,b'secret')]
                for i,(info,raw) in enumerate(members):
                    if info.name=='migration-plan.json':
                        value=json.loads(raw);value['units'].reverse();raw=json.dumps(value).encode();info.size=len(raw);members[i]=(info,raw)
                return members
            receipt=self.rewrite(change);self.refused(lambda:self.extract(receipt))
            self.assertFalse((self.work/'receiver'/'migration').exists())

    def test_sql_hash_path_type_and_archive_metadata_refused_before_extraction(self):
        self.receipt=self.create();original=self.bundle.read_bytes()
        for mode in ('tamper','traversal','symlink','hardlink','directory','exec-mode','uid','mtime'):
            self.bundle.write_bytes(original)
            def change(members):
                i=next(i for i,(m,_) in enumerate(members) if m.name.endswith('.sql'))
                info,raw=members[i]
                if mode=='tamper':raw=b'SELECT private;';info.size=len(raw)
                elif mode=='traversal':info.name='../private.sql'
                elif mode in ('symlink','hardlink'):info.type=tarfile.SYMTYPE if mode=='symlink' else tarfile.LNKTYPE;info.linkname='/private/secret';info.size=0
                elif mode=='directory':info.type=tarfile.DIRTYPE;info.size=0
                elif mode=='exec-mode':info.mode=0o700
                elif mode=='uid':info.uid=1000
                elif mode=='mtime':info.mtime=1
                members[i]=(info,raw);return members
            receipt=self.rewrite(change);self.refused(lambda:self.extract(receipt))
            self.assertFalse((self.work/'receiver'/'migration').exists())

    def test_private_single_link_owned_bounds_and_existing_output_preserved(self):
        self.receipt=self.create();self.bundle.chmod(0o644)
        self.refused(self.extract);self.bundle.chmod(0o600)
        link=self.work/'link.tar';link.symlink_to(self.bundle)
        self.refused(lambda:self.extract(bundle=link));link.unlink()
        os.link(self.bundle,link);self.refused(self.extract);link.unlink()
        receiver=self.work/'receiver';receiver.mkdir(exist_ok=True,mode=0o700)
        (receiver/'migration').mkdir();sentinel=receiver/'migration'/'keep';sentinel.write_text('preserve')
        self.refused(self.extract);self.assertEqual(sentinel.read_text(),'preserve')
        before=self.bundle.read_bytes();self.refused(self.create);self.assertEqual(before,self.bundle.read_bytes())

    def test_parent_symlink_and_nonprivate_receiver_refused(self):
        self.receipt=self.create()
        link=self.work/'parent-link';link.symlink_to(self.work,target_is_directory=True)
        self.refused(lambda:self.extract(bundle=link/self.bundle.name))
        receiver=self.work/'receiver';receiver.mkdir(mode=0o700,exist_ok=True);receiver.chmod(0o755)
        self.refused(self.extract);receiver.chmod(0o700)
        with patch.object(self.module,'EXTRACT_UID',os.getuid()+1):self.refused(self.extract)
        self.assertFalse((receiver/'migration').exists())

    def test_compression_trailing_bytes_json_duplicates_and_size_bounds_refused(self):
        self.receipt=self.create();original=self.bundle.read_bytes()
        for raw in (gzip.compress(original),original+b'private-trailing-bytes'):
            self.bundle.write_bytes(raw)
            receipt={'bundle_sha256':hashlib.sha256(raw).hexdigest()}
            self.refused(lambda:self.extract(receipt))
        self.bundle.write_bytes(original)
        def duplicate(members):
            for i,(info,raw) in enumerate(members):
                if info.name=='controller.json':
                    raw=b'{"run_id":123,"run_id":456}';info.size=len(raw);members[i]=(info,raw)
            return members
        changed=self.rewrite(duplicate);self.refused(lambda:self.extract(changed))
        self.bundle.write_bytes(original)
        with patch.object(self.module,'MAX_BUNDLE',len(original)-1):self.refused(self.extract)
        self.assertFalse((self.work/'receiver'/'migration').exists())

    def test_extracted_sql_mutation_refused_before_application_inputs(self):
        self.receipt=self.create();verified=self.extract()
        first=verified.plan['units'][0]
        file=verified.roots[first['owner']]/first['path'];file.write_text('DROP DATABASE unsafe;')
        self.refused(lambda:self.module.application_inputs(verified))

if __name__=='__main__':unittest.main()
