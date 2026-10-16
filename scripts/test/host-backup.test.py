#!/usr/bin/env python3
"""Actual backup helper/age; inert Docker and B2 executables never connect."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_release_state
ERROR='Hosted encrypted backup refused'
AGE_PLATFORM='darwin-arm64' if sys.platform=='darwin' else 'linux-amd64'
AGE_ROOT=Path(os.environ.get('XYNES_TEST_AGE_BIN','/private/tmp/apex-backup-tools-1.3.2-5.0.0/'+AGE_PLATFORM))


class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/host_backup.py'
        self.assertTrue(path.is_file(),'root encrypted backup phase is missing')
        spec=importlib.util.spec_from_file_location('host_backup',path)
        self.module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.module;spec.loader.exec_module(self.module)
        if not all((AGE_ROOT/name).is_file() for name in ('age','age-keygen')):self.skipTest('verified native Mac age fixtures unavailable')
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-host-backup-');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.host=self.base/'host';self.host.mkdir(mode=0o700)
        self.laptop=self.base/'laptop';self.laptop.mkdir(mode=0o700)
        tools=self.host/'usr/local/lib/xynes/tools/age/1.3.2';tools.mkdir(parents=True)
        for name in ('age','age-keygen'):shutil.copyfile(AGE_ROOT/name,tools/name);(tools/name).chmod(0o555)
        chain=json.loads((ROOT/'infra/release/backup-toolchain.json').read_text())
        for name in ('age','age-keygen'):
            self.assertEqual(hashlib.sha256((tools/name).read_bytes()).hexdigest(),chain['age'][AGE_PLATFORM]['binaries'][name])
        self.identity=self.laptop/'identity.txt'
        result=subprocess.run([str(tools/'age-keygen'),'-o',str(self.identity)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)
        self.identity.chmod(0o600)
        result=subprocess.run([str(tools/'age-keygen'),'-y',str(self.identity)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0);recipient=result.stdout
        recipient_file=self.host/'etc/xynes/backup-recipient.txt';recipient_file.parent.mkdir(parents=True);recipient_file.write_bytes(recipient);recipient_file.chmod(0o644)
        config=self.host/'etc/xynes/deploy/backup.env';config.parent.mkdir();config.write_text('B2_BACKUP_BUCKET=inert-backups\nB2_KEY_ID=inert-key-id\nB2_APPLICATION_KEY=inert-application-key\n');config.chmod(0o600)
        env=self.host/'srv/xynes/qa/env/.env.qa';env.parent.mkdir(parents=True);env.write_text('XYNES_ENV=qa\nPOSTGRES_PASSWORD='+('a'*64)+'\n');env.chmod(0o640)
        self.log=self.base/'log.jsonl';self.control=self.base/'control.json';self.control.write_text('{}')
        docker=self.host/'usr/bin/docker';docker.parent.mkdir(parents=True)
        docker.write_text('''#!/usr/bin/python3 -ISB
import hashlib,json,os,pathlib,sys
args=sys.argv[1:];control=json.loads(pathlib.Path(%r).read_text())
with open(%r,'a') as log:log.write(json.dumps({'kind':'pg','args':args,'b2':any(k.startswith('B2_') for k in os.environ),'password':os.environ.get('PGPASSWORD')=='a'*64})+'\\n')
assert not any(k.startswith('B2_') for k in os.environ)
args=args[4:]
cid='a'*64
if args[0]=='ps':print(cid)
elif args[0]=='inspect':
 print(json.dumps({'id':cid,'image':%r,'labels':{'com.docker.compose.project':'xynes-qa','com.docker.compose.service':'supabase-db','com.docker.compose.oneoff':'False','com.xynes.managed':'true','com.xynes.environment':'qa','com.xynes.role':'infrastructure'},'state':'running','health':'unhealthy' if control.get('unhealthy') else 'healthy'}))
elif args[0]=='exec':
 assert os.environ['PGPASSWORD']=='a'*64
 program=args[args.index(cid)+1];flags=args[args.index(cid)+2:]
 tables=['auth.users','auth.schema_migrations']+%r
 if control.get('missing_critical'):tables.remove('platform.routes')
 if control.get('missing_auth'):tables.remove('auth.users')
 def query(sql):
  if 'pg_export_snapshot' in sql:return '00000003-0000001B-1'
  if 'json_agg(n.nspname' in sql:return json.dumps(tables)
  if 'json_agg(nspname' in sql:return json.dumps(sorted(set(t.split('.')[0] for t in tables)))
  if "'ordinal'" in sql:return json.dumps([{'table':t,'column':'id'} for t in tables])
  if "'row_md5'" in sql:return json.dumps({'rows':0,'row_md5':hashlib.md5(b'inert rows').hexdigest()})
  raise RuntimeError('not a permitted fixture query')
 if program=='psql':
  if '--command' in flags:print(query(flags[-1]))
  else:
   for sql in sys.stdin:print(query(sql),flush=True)
 elif program=='pg_dump':
  if '--version' in flags:print('pg_dump (PostgreSQL) 17.10')
  else:sys.stdout.buffer.write(b'PGDMP-inert-private-data')
 elif program=='pg_restore':
  assert flags==['--schema-only','--no-owner','--no-acl','--file=-'];sys.stdin.buffer.read();print('CREATE SCHEMA auth; -- inert schema')
 else:sys.exit(1)
else:sys.exit(1)
'''%(str(self.control),str(self.log),self.module.pg_container_bridge.DB_IMAGE,list(self.module.ACCEPTED_TABLES[1:])))
        docker.chmod(0o555)
        b2=self.host/'usr/local/lib/xynes/tools/b2/5.0.0/b2';b2.parent.mkdir(parents=True)
        b2.write_text('''#!/usr/bin/python3 -ISB
import hashlib,json,os,pathlib,sys
control=json.loads(pathlib.Path(%r).read_text());args=sys.argv[1:]
assert os.environ['B2_APPLICATION_KEY_ID']=='inert-key-id' and os.environ['B2_APPLICATION_KEY']=='inert-application-key'
assert not any(k.startswith('PG') for k in os.environ)
assert 'UNRELATED_SECRET' not in os.environ
assert 'inert-key-id' not in str(args) and 'inert-application-key' not in str(args)
cache=pathlib.Path(os.environ['B2_ACCOUNT_INFO']);cache.write_bytes(b'inert protected cache');cache.chmod(0o600)
with open(%r,'a') as log:log.write(json.dumps({'kind':'b2','args':args,'pg':any(k.startswith('PG') for k in os.environ),'record':json.loads(pathlib.Path(args[-2]).read_text()) if pathlib.Path(args[-2]).name=='record.json' else None})+'\\n')
path=pathlib.Path(args[-2]);remote=args[-1]
if control.get('fail_receipt') and path.name=='record.json':sys.exit(1)
sha=hashlib.sha1(path.read_bytes()).hexdigest()
print(json.dumps({'action':'upload','fileId':'inert-file-id','fileName':remote,'size':path.stat().st_size,'contentSha1':'bad' if control.get('bad_checksum') else sha}))
'''%(str(self.control),str(self.log)))
        b2.chmod(0o555)
        chain['b2']['linux-amd64']['binary_sha256']=hashlib.sha256(b2.read_bytes()).hexdigest()
        self.toolchain=self.host/'backup-toolchain.json';self.toolchain.write_text(json.dumps(chain));self.toolchain.chmod(0o444)
        self.config=self.module.Configuration(root=self.host,platform=AGE_PLATFORM,toolchain=self.toolchain)
        directory=self.host/'state';directory.mkdir(mode=0o700)
        self.state=host_release_state.ReleaseState(directory,stage='qa',owner_uid=os.getuid());self.state.__enter__();self.addCleanup(self.state.__exit__,None,None,None)
        self.request=dict(schema_version=1,artifact_kind='apex-deployment-request',controller=dict(repository='Xynes-Studio/xynes-infra',commit='a'*40,workflow='apex-build.yml',run_id=100,run_attempt=1),publication=dict(id=456,digest='sha256:'+'b'*64),deployment=dict(run_id=101,run_attempt=1,job_id=201))
        self.token=self.state.begin(self.request,source_sha256='sha256:'+'c'*64,started_at=int(time.time())-1)

    def create(self,**changes):
        return self.module.create(self.state,self.token,configuration=self.config,**changes)

    def refused(self,**changes):
        with self.assertRaises(self.module.BackupError) as caught:self.create(**changes)
        self.assertEqual(str(caught.exception),ERROR)

    def output(self):return self.host/'srv/xynes/qa/backups'

    def test_real_age_encryption_actual_helper_upload_and_monitor_bind_pending_release(self):
        result=self.create();self.assertTrue(result['uploaded']);self.assertFalse(result['deployable'])
        self.assertEqual(result['pending_token'],self.token);self.assertEqual(result['verification_mode'],'bootstrap')
        self.assertEqual(result['execution_scope'],'offline-fixture')
        job=self.output()/result['job'];cipher=job/'backup.tar.age'
        self.assertEqual(hashlib.sha256(cipher.read_bytes()).hexdigest(),result['ciphertext_sha256'])
        self.assertEqual(set(p.name for p in job.iterdir()),{'backup.tar.age','record.json'})
        self.assertFalse(list(self.output().glob('.backup-*')));self.assertFalse(list(self.output().glob('.receipt-*')))
        self.assertEqual((self.output()/'monitor.json').stat().st_mode&0o777,0o600)
        import re
        self.assertFalse(any(re.search(rb'(?m)^AGE-SECRET-KEY-[A-Z0-9]{58}\r?$', p.read_bytes()) for p in self.host.rglob('*') if p.is_file()))
        age=self.host/'usr/local/lib/xynes/tools/age/1.3.2/age'
        decrypted=subprocess.run([str(age),'--decrypt','--identity',str(self.identity),str(cipher)],capture_output=True,timeout=10)
        self.assertEqual(decrypted.returncode,0)
        with tarfile.open(fileobj=io.BytesIO(decrypted.stdout),mode='r:') as archive:
            self.assertEqual(set(archive.getnames()),{'database.dump','schema.sql','manifest.json','database-evidence.json'})
            evidence=json.load(archive.extractfile('database-evidence.json'))
            self.assertIn('auth.users',evidence['verified_tables']);self.assertIn('auth.schema_migrations',evidence['verified_tables'])
        rows=[json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(r['kind']=='b2' for r in rows));self.assertTrue(all(not r.get('b2',False) and not r.get('pg',False) for r in rows))

    def test_periodic_accepted_backup_uses_native_age_and_keeps_ledger_unchanged(self):
        self.state.accept(self.token,smoke=dict(stage='qa',publication=self.request['publication'],source_sha256='sha256:'+'c'*64,result='passed',observed_at=int(time.time())),accepted_at=int(time.time()))
        accepted=copy.deepcopy(self.state.read_accepted()['current']);ledger=(self.host/'state/qa.json').read_bytes()
        self.state.__exit__(None,None,None)
        directory=self.host/'var/lib/xynes/deploy/state';directory.parent.mkdir(parents=True)
        (self.host/'state').rename(directory)
        output=self.output();output.mkdir(mode=0o700)
        old=output/('qa-20000101T000000Z-'+'e'*32);old.mkdir(mode=0o700)
        ciphertext=old/'backup.tar.age';ciphertext.write_bytes(b'old-ciphertext');ciphertext.chmod(0o600)
        from datetime import datetime,timezone
        record={'version':1,'environment':'qa','uploaded':True,'bytes':len(b'old-ciphertext'),'sha256':hashlib.sha256(b'old-ciphertext').hexdigest(),'snapshot_at':datetime.fromtimestamp(time.time()-9*86400,timezone.utc).isoformat()}
        (old/'record.json').write_text(json.dumps(record));(old/'record.json').chmod(0o600)
        unrelated=output/'unrelated';unrelated.mkdir(mode=0o700)
        result=self.module.periodic('qa',configuration=self.config)
        self.assertEqual(result['status'],'backup-created');self.assertTrue(result['uploaded'])
        self.assertFalse(old.exists());self.assertTrue(unrelated.is_dir())
        self.assertEqual(result['local_retention'],{'days':7,'removed':1})
        self.assertEqual(result['accepted_token'],accepted['token']);self.assertEqual(result['accepted_source_sha256'],accepted['source_sha256'])
        self.assertNotIn('pending_token',result);self.assertEqual(result['verification_mode'],'accepted')
        saved=json.loads((self.output()/result['job']/'record.json').read_text())
        expected={'accepted_token':accepted['token'],'accepted_source_sha256':accepted['source_sha256'],'database_container_id':result['database_container_id']}
        self.assertEqual(saved['release_binding'],expected)
        uploads=[json.loads(line) for line in self.log.read_text().splitlines() if json.loads(line)['kind']=='b2']
        self.assertEqual(uploads[-1]['record']['release_binding'],expected)
        self.assertEqual(result['verification_tables'],list(self.module.ACCEPTED_TABLES))
        self.assertEqual((directory/'qa.json').read_bytes(),ledger)
        cipher=self.output()/result['job']/'backup.tar.age'
        age=self.host/'usr/local/lib/xynes/tools/age/1.3.2/age'
        decrypted=subprocess.run([str(age),'--decrypt','--identity',str(self.identity),str(cipher)],capture_output=True,timeout=10)
        self.assertEqual(decrypted.returncode,0)
        with tarfile.open(fileobj=io.BytesIO(decrypted.stdout),mode='r:') as archive:
            evidence=json.load(archive.extractfile('database-evidence.json'))
            self.assertTrue(set(self.module.ACCEPTED_TABLES)<=set(evidence['verified_tables']))
            self.assertEqual(json.load(archive.extractfile('manifest.json'))['release_binding'],expected)
        self.assertFalse(any(p.name=='identity.txt' for p in self.host.rglob('*')))

    def test_existing_accepted_release_cannot_downgrade_critical_table_evidence(self):
        self.state.accept(self.token,smoke=dict(stage='qa',publication=self.request['publication'],source_sha256='sha256:'+'c'*64,result='passed',observed_at=int(time.time())),accepted_at=int(time.time()))
        request=copy.deepcopy(self.request);request['deployment'].update(run_id=102,job_id=202);request['publication']={'id':457,'digest':'sha256:'+'d'*64}
        self.token=self.state.begin(request,source_sha256='sha256:'+'c'*64,started_at=int(time.time())-1)
        self.refused(phase='bootstrap')
        result=self.create();self.assertEqual(result['verification_mode'],'accepted')
        self.assertIn('platform.workspaces',result['verification_tables'])
        self.assertNotIn('identity.workspaces',result['verification_tables'])

    def test_missing_auth_or_critical_tables_refuse_before_upload(self):
        self.control.write_text(json.dumps({'missing_auth':True}));self.refused()
        self.control.write_text(json.dumps({'missing_critical':True}));self.refused(phase='accepted')
        if self.log.exists():self.assertFalse(any(json.loads(line)['kind']=='b2' for line in self.log.read_text().splitlines()))

    def test_partial_upload_and_checksum_mismatch_never_return_fresh_monitor(self):
        for field in ('fail_receipt','bad_checksum'):
            self.control.write_text(json.dumps({field:True}));self.refused()
        self.assertFalse((self.output()/'monitor.json').exists())
        self.assertFalse(list(self.output().glob('.backup-*')));self.assertFalse(list(self.output().glob('.receipt-*')))
        records=[json.loads((p/'record.json').read_text()) for p in self.output().iterdir() if p.is_dir() and p.name.startswith('qa-')]
        self.assertTrue(records);self.assertTrue(all(r['uploaded'] is False for r in records))

    def test_private_config_permissions_symlinks_extra_keys_and_tool_tamper_refuse(self):
        path=self.host/'etc/xynes/deploy/backup.env';original=path.read_bytes()
        path.chmod(0o644);self.refused();path.chmod(0o600)
        path.write_bytes(original+b'B2_API_URL=https://other.invalid\n');self.refused();path.write_bytes(original)
        alias=self.host/'alias';alias.write_bytes(original);alias.chmod(0o600);path.unlink();path.symlink_to(alias);self.refused();path.unlink();path.write_bytes(original);path.chmod(0o600)
        age=self.host/'usr/local/lib/xynes/tools/age/1.3.2/age';age.chmod(0o755);age.write_bytes(b'tampered native tool');age.chmod(0o555)
        self.refused();self.assertFalse(self.log.exists())

    def test_unhealthy_owned_source_and_closed_pending_state_refuse(self):
        self.control.write_text(json.dumps({'unhealthy':True}));self.refused()
        self.control.write_text('{}');self.state.fail(self.token,reason='runtime',failed_at=int(time.time()));self.refused()

    def test_native_upload_hashes_ciphertext_without_reading_entire_file(self):
        from unittest.mock import patch
        original=self.module.read
        def read(path,*args,**kwargs):
            self.assertNotEqual(Path(path).name,'backup.tar.age','ciphertext must use streaming digest')
            return original(path,*args,**kwargs)
        # Wrapper children are distinct processes; exercise the concrete digest
        # boundary directly on a private representative ciphertext file.
        file=self.host/'backup.tar.age';file.write_bytes(b'x'*(1024*1024));file.chmod(0o600)
        with patch.object(self.module,'read',read):
            self.assertEqual(self.module.file_digest(file,self.config),hashlib.sha1(file.read_bytes()).hexdigest())
    def test_ciphertext_output_streams_into_owned_file_without_memory_capture(self):
        output=self.base/'stream.bin'
        with output.open('xb') as file:
            result=self.module.bounded([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*(1024*1024))'],
                {'PATH':'/usr/bin:/bin'},limit=1024*1024,output_fd=file.fileno())
        self.assertEqual(result,b'');self.assertEqual(output.stat().st_size,1024*1024)
    def test_public_recipient_refuses_private_identity_and_caller_restore_flags(self):
        recipient=self.host/'etc/xynes/backup-recipient.txt';recipient.write_bytes(self.identity.read_bytes());self.refused()
        for program,args in [('age',['--decrypt','--identity',str(self.identity)]),('b2',['account','authorize']),('pg_restore',['--dbname','postgres'])]:
            self.assertEqual(self.module.wrapper(stage='qa',program=program,configuration=self.config,args=args),1)


if __name__=='__main__':unittest.main()
