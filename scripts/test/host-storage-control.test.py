#!/usr/bin/env python3
"""Actual private files/Unix socket fixtures, inert native Docker inventory only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
CID='a'*64
IMAGE='ghcr.io/xynes-studio/xynes-storage-service@sha256:'+'b'*64


class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/host_storage_control.py'
        self.assertTrue(path.is_file(),'Storage control provisioning helper missing')
        sys.path.insert(0,str(path.parent));self.addCleanup(sys.path.remove,str(path.parent))
        spec=importlib.util.spec_from_file_location('host_storage_control',path)
        self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
        base='/private/tmp' if sys.platform=='darwin' else '/tmp'
        self.temp=tempfile.TemporaryDirectory(prefix='sc-',dir=base);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.calls=[];self.native=None;self.gone=False
        self.control=self.m.StorageControl('qa',root=self.root,runner=self.runner)

    def runner(self,argv,*,env,timeout,limit):
        self.calls.append(argv)
        self.assertEqual(env,self.m.host_compose.CLEAN_ENV)
        self.assertEqual(timeout,15);self.assertEqual(limit,65536)
        self.assertEqual(argv[:5],self.m.host_compose.BASE)
        if argv[5:8]==['inspect','--format',self.m.INSPECT]:
            self.assertEqual(argv[-1],CID);return json.dumps(self.native).encode()
        self.assertEqual(argv[5:],['ps','--all','--no-trunc','--filter','id='+CID,'--format','{{.ID}}'])
        return b'' if self.gone else (CID+'\n').encode()

    def generation(self,name='c'*64):
        self.control.provision_token()
        return self.control.provision_generation(name)

    def socket(self,generation):
        path=self.control.directory(generation)/'control.sock'
        service=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(service.close)
        service.bind(str(path));path.chmod(0o600);os.chown(path,os.getuid(),os.getgid())
        return path

    def observation(self,generation):
        return dict(id=CID,image=IMAGE,user='1001:1001',state='running',health='healthy',
                    started_at='2026-10-14T01:00:00.000000000Z',pid=2345,
                    labels={'com.xynes.managed':'true','com.xynes.environment':'qa',
                            'com.docker.compose.project':'xynes-qa','com.docker.compose.service':'storage-service',
                            'com.docker.compose.oneoff':'False','com.xynes.storage-generation':generation.name},
                    mounts=[dict(Type='bind',Source='/run/xynes/storage/qa/'+generation.name[:32],
                                 Destination='/run/xynes/storage-control',RW=True),
                            dict(Type='bind',Source='/etc/xynes/storage-control/qa/control.token',
                                 Destination='/run/secrets/storage-quiesce-token',RW=False)])

    def bound(self):
        generation=self.generation();path=self.socket(generation)
        self.native=self.observation(generation)
        self.control.bind_generation(generation,CID,image=IMAGE)
        return generation,path

    def test_token_is_private_immutable_and_never_in_generation_record(self):
        self.control.provision_token();token=self.control.read_token()
        self.assertRegex(token,r'^[a-f0-9]{64}$')
        path=self.root/'etc/xynes/storage-control/qa/control.token'
        self.assertEqual(path.stat().st_mode&0o777,0o400)
        for parent in (path.parent,path.parent.parent):self.assertEqual(parent.stat().st_mode&0o777,0o700)
        before=path.stat();self.control.provision_token()
        self.assertEqual(path.stat().st_ino,before.st_ino);self.assertEqual(self.control.read_token(),token)
        generation=self.control.provision_generation('c'*64)
        self.assertNotIn(token,(self.control.record(generation)).read_text())
        self.assertEqual(generation.token_sha256,hashlib.sha256(token.encode()).hexdigest())

    def test_existing_safe_system_ancestors_need_no_permission_rewrite(self):
        for name in ('etc','etc/xynes','run','run/xynes','var','var/lib','var/lib/xynes'):
            path=self.root/name;path.mkdir(exist_ok=True);path.chmod(0o755)
        self.control.provision_token();self.control.provision_generation('c'*64)
        for name in ('etc','etc/xynes','run','run/xynes','var','var/lib','var/lib/xynes'):
            self.assertEqual((self.root/name).stat().st_mode&0o777,0o755)

    def test_generation_is_unique_bounded_and_has_correct_directory_identity(self):
        generation=self.generation();directory=self.control.directory(generation)
        self.assertEqual(directory.stat().st_mode&0o777,0o700)
        self.control.verify_generation(generation)
        with self.assertRaises(self.m.StorageControlError):self.control.provision_generation(generation.name)
        for value in ('../prod','c'*63,'C'*64,True,None,'c'*64+'\n'):
            with self.assertRaises(self.m.StorageControlError):self.control.provision_generation(value)
        self.assertEqual(list(directory.iterdir()),[])
        self.assertEqual(directory.name,generation.name[:32])
        collision=generation.name[:32]+'e'*32
        with self.assertRaises(self.m.StorageControlError):self.control.provision_generation(collision)

    def test_existing_invalid_token_symlink_hardlink_or_mutable_ancestor_refuses(self):
        self.control.provision_token();path=self.control.token_path
        original=path.read_bytes();path.chmod(0o600)
        with self.assertRaises(self.m.StorageControlError):self.control.read_token()
        path.chmod(0o400);outside=self.root/'alias';os.link(path,outside)
        with self.assertRaises(self.m.StorageControlError):self.control.provision_token()
        outside.unlink();path.unlink();path.symlink_to(outside)
        with self.assertRaises(self.m.StorageControlError):self.control.provision_token()
        path.unlink();path.write_bytes(original);path.chmod(0o400);os.chown(path,os.getuid(),os.getgid())
        path.parent.chmod(0o755)
        with self.assertRaises(self.m.StorageControlError):self.control.read_token()

    def test_cross_stage_handle_and_replaced_directory_refuse(self):
        generation=self.generation();other=self.m.StorageControl('prod',root=self.root,runner=self.runner)
        with self.assertRaises(self.m.StorageControlError):other.verify_generation(generation)
        directory=self.control.directory(generation);directory.rename(directory.with_name('preserved-old'))
        directory.mkdir(mode=0o700)
        with self.assertRaises(self.m.StorageControlError):self.control.verify_generation(generation)
        self.assertTrue(directory.with_name('preserved-old').exists())

    def test_native_bind_requires_exact_image_labels_mounts_process_and_socket(self):
        generation=self.generation();self.socket(generation);valid=self.observation(generation)
        attacks=[('image','ghcr.io/other/image@sha256:'+'d'*64),('user','0:0'),('id','f'*64),
                 ('pid',0),('state','exited'),('health','starting')]
        for key,value in attacks:
            self.native=copy.deepcopy(valid);self.native[key]=value
            with self.assertRaises(self.m.StorageControlError):self.control.bind_generation(generation,CID,image=IMAGE)
        for mutate in (lambda row:row['labels'].update({'com.xynes.environment':'prod'}),
                       lambda row:row['labels'].update({'com.xynes.storage-generation':'e'*64}),
                       lambda row:row['mounts'][0].update(RW=False),
                       lambda row:row['mounts'][1].update(RW=True),
                       lambda row:row['mounts'].append(copy.deepcopy(row['mounts'][0]))):
            self.native=copy.deepcopy(valid);mutate(self.native)
            with self.assertRaises(self.m.StorageControlError):self.control.bind_generation(generation,CID,image=IMAGE)
        self.native=valid;self.control.bind_generation(generation,CID,image=IMAGE)

    def test_unbound_or_still_existing_exact_container_never_cleans_socket(self):
        generation=self.generation();path=self.socket(generation)
        self.gone=True
        with self.assertRaises(self.m.StorageControlError):self.control.retire(generation)
        self.native=self.observation(generation);self.control.bind_generation(generation,CID,image=IMAGE)
        self.gone=False
        with self.assertRaises(self.m.StorageControlError):self.control.retire(generation)
        self.assertTrue(path.exists())

    def test_exact_gone_original_socket_cleanup_keeps_tombstone_and_cannot_reuse(self):
        generation,path=self.bound();self.gone=True
        result=self.control.retire(generation)
        self.assertTrue(result['retired']);self.assertFalse(result['deployment_accepted'])
        self.assertFalse(path.parent.exists());self.assertTrue(self.control.record(generation).exists())
        self.assertEqual(json.loads(self.control.record(generation).read_text())['state'],'retired')
        with self.assertRaises(self.m.StorageControlError):self.control.provision_generation(generation.name)
        fresh=self.control.provision_generation('d'*64)
        self.assertNotEqual(fresh.name,generation.name)

    def test_unknown_file_and_replaced_socket_are_preserved(self):
        generation,path=self.bound();self.gone=True
        unknown=path.parent/'unknown';unknown.write_text('preserve')
        with self.assertRaises(self.m.StorageControlError):self.control.retire(generation)
        self.assertEqual(unknown.read_text(),'preserve');self.assertTrue(path.exists())
        unknown.unlink();path.unlink();path.write_bytes(b'replaced-unknown')
        with self.assertRaises(self.m.StorageControlError):self.control.retire(generation)
        self.assertEqual(path.read_bytes(),b'replaced-unknown')

    def test_native_failure_and_process_drift_preserve_everything(self):
        generation,path=self.bound();self.gone=True
        def fail(*args,**kwargs):raise RuntimeError('private native payload')
        self.control.runner=fail
        with self.assertRaises(self.m.StorageControlError) as caught:self.control.retire(generation)
        self.assertEqual(str(caught.exception),'Storage control provisioning refused');self.assertTrue(path.exists())
        self.control.runner=self.runner;self.native['started_at']='2026-10-14T02:00:00.000000000Z'
        with self.assertRaises(self.m.StorageControlError):self.control.bind_generation(generation,CID,image=IMAGE)


if __name__=='__main__':unittest.main()
