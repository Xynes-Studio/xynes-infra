#!/usr/bin/env python3
"""Real isolated PostgreSQL17 smoke-provider transitions, no hosted targets.

Reuse the existing pinned local-Docker fixture: no pulls, networks, ports,
provider credentials, host database selection, or dependency installation.
ROOT-relative identity/workspace and provider DDL are hash-qualified. Auth and
Authz use minimal queried-column fixtures, not complete service bootstrap.
These tests prove local DB behavior, never R2 or hosted product acceptance.
"""
import hashlib,importlib.util,json,os,pathlib,selectors,subprocess,sys,time,unittest,uuid
from unittest.mock import Mock,patch
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_smoke_setup as m
spec=importlib.util.spec_from_file_location('provider_postgres_fixture',ROOT/'scripts/test/migration-sql-runtime.test.py')
postgres_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(postgres_fixture)
BASE=ROOT
class Store:
 def __init__(self,j):self.j=j
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self,n):return self.j
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.fixture=postgres_fixture.PostgresFixture()
  # Registered before launch; the shared fixture owns exact nonce/image cleanup
  # even when Docker startup or readiness fails before returning its CID.
  cls.addClassCleanup(cls.fixture.close)
  cls.fixture.start()
  cls.args=cls.fixture.docker+cls.fixture.psql_args('postgres')+['-q']
 @classmethod
 def query(cls,sql,ok=True):
  text=sql if isinstance(sql,str) else sql.decode()
  result=cls.fixture.command(cls.fixture.psql_args('postgres')+['-q'],input=text,timeout=25)
  if ok and result.returncode:raise RuntimeError('Fixture PostgreSQL statement refused')
  return subprocess.CompletedProcess(result.args,result.returncode,result.stdout.encode(),result.stderr.encode())
 def interactive(self,application=None):
  return self.fixture.docker+self.fixture.psql_args('postgres',application=application)+['-q']
 def setUp(self):
  self.stage='staging';self.j=m.new_journal(self.stage)
  for role in m.ROLES:self.j['accounts'][role].update(user_id=str(uuid.uuid4()),workspace_id=str(uuid.uuid4()),user_intent=True,workspace_intent=True)
  self.setup={'stage':self.stage,'configured':True,'user_id':self.j['accounts']['primary']['user_id'],'workspace_id':self.j['accounts']['primary']['workspace_id'],'forbidden_workspace_id':self.j['accounts']['secondary']['workspace_id']}
  self.values={'R2_ACCOUNT_ID':'a'*32,'R2_BUCKET':'stage-test-bucket','R2_REGION':'auto'}
  self.reset()
 def reset(self):
  ownership_raw=(BASE/'supabase/migrations/20251219133000_accounts_schema_1_identity_workspace_core.sql').read_bytes()
  self.assertEqual(hashlib.sha256(ownership_raw).hexdigest(),'6ce10e0f40968fe16baa79f6c9566774e6c8ada0435c6a92262f4e24b7b6fb98')
  ownership=ownership_raw.decode()
  provider=(BASE/'supabase/migrations/20260513090000_universal_storage_platform_schema.sql').read_bytes()
  self.assertEqual(hashlib.sha256(provider).hexdigest(),'7b3f2827728bc7ab98be4589d5bf6e903a18fe4e25bba3048420395756c4aa4e')
  p=provider.decode();p=p[p.index('CREATE TABLE IF NOT EXISTS platform.workspace_storage_providers'):p.index('-- platform.storage_objects')]
  self.query('DROP SCHEMA IF EXISTS platform CASCADE; DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS auth CASCADE; DROP SCHEMA IF EXISTS authz CASCADE;'+ownership+"CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,email_confirmed_at timestamptz,raw_app_meta_data jsonb,role text NOT NULL DEFAULT 'authenticated'); CREATE SCHEMA authz; CREATE TABLE authz.user_roles(user_id uuid NOT NULL,workspace_id uuid NOT NULL,role_key text NOT NULL,PRIMARY KEY(user_id,workspace_id,role_key));"+p)
  for role in m.ROLES:
   a=self.j['accounts'][role];slug='apex-smoke-'+self.stage+'-'+self.j['nonce']+'-'+role[0];marker=json.dumps({'apex_smoke_fixture':{'version':1,'stage':self.stage,'nonce':self.j['nonce'],'identity':role}})
   self.query("INSERT INTO identity.users(id,email) VALUES ('%s','%s'); INSERT INTO auth.users(id,email,email_confirmed_at,raw_app_meta_data) VALUES ('%s','%s',now(),'%s'::jsonb); INSERT INTO platform.workspaces(id,name,slug,created_by) VALUES ('%s','%s','%s','%s'); INSERT INTO platform.workspace_members(workspace_id,user_id) VALUES ('%s','%s'); INSERT INTO authz.user_roles VALUES ('%s','%s','workspace_owner');"%(a['user_id'],a['email'],a['user_id'],a['email'],marker,a['workspace_id'],slug,slug,a['user_id'],a['workspace_id'],a['user_id'],a['user_id'],a['workspace_id']))
 def execute(self,stage,sql):return self.query(sql).stdout
 def ensure(self):return m.ensure_storage_provider(self.stage,self.setup,store=Store(self.j),values_loader=lambda s:self.values,executor=self.execute)
 def snapshot(self):return self.query("SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY id),'[]') FROM platform.workspace_storage_providers p;").stdout
 def insert(self,workspace,*,name='other',bucket='foreign-bucket'):
  return "INSERT INTO platform.workspace_storage_providers(workspace_id,provider_kind,display_name,bucket,region,endpoint,credential_ref,status,is_default,created_by) VALUES ('%s','r2','%s','%s','auto','https://%s.r2.cloudflarestorage.com','secret://xynes/storage/r2-staging','active',false,'%s');"%(workspace,name,bucket,'a'*32,self.j['accounts']['secondary']['user_id'])
 def test_absent_insert_and_exact_idempotence(self):
  for stage in ('staging','prod'):
   self.stage=stage;self.j['stage']=stage
   for role in m.ROLES:self.j['accounts'][role]['email']='apex-smoke-'+stage+'-'+self.j['nonce']+'-'+role+'@example.invalid'
   self.setup['stage']=stage;self.reset();self.assertEqual(self.ensure(),{'stage':stage,'storage_provider_ready':True});before=self.snapshot();self.ensure();self.assertEqual(self.snapshot(),before)
   row=json.loads(before)[0];self.assertEqual(row['credential_ref'],'secret://xynes/storage/r2-'+stage);self.assertTrue(row['is_default']);self.assertEqual(row['region'],'auto')
 def test_conflicting_or_disabled_rows_never_overwritten(self):
  for column,value in [('bucket','different'),('status','disabled'),('is_default',False),('endpoint','https://foreign.invalid'),('region','other'),('credential_ref','secret://foreign/key'),('display_name','other'),('provider_kind','minio'),('created_by',self.j['accounts']['secondary']['user_id'])]:
   self.reset();self.ensure();literal='false' if value is False else "'"+value+"'";self.query('UPDATE platform.workspace_storage_providers SET '+column+'='+literal+';');before=self.snapshot()
   with self.assertRaises(m.SetupError):self.ensure()
   self.assertEqual(self.snapshot(),before,column)
 def test_multiple_rows_refuse_without_replacing_anything(self):
  self.ensure();self.query(self.insert(self.setup['workspace_id']));before=self.snapshot()
  with self.assertRaises(m.SetupError):self.ensure()
  self.assertEqual(self.snapshot(),before)
 def test_foreign_workspace_row_preserved(self):
  self.query(self.insert(self.setup['forbidden_workspace_id']));before=json.loads(self.snapshot())[0];self.ensure();after=json.loads(self.snapshot());self.assertIn(before,after);self.assertEqual(len(after),2)
 def test_database_ownership_change_rolls_back_before_insert(self):
  for mutation in ["UPDATE auth.users SET raw_app_meta_data='{}'", "UPDATE auth.users SET email_confirmed_at=NULL", "UPDATE platform.workspace_members SET status='inactive'", "DELETE FROM authz.user_roles", "UPDATE platform.workspaces SET name='foreign'", "UPDATE identity.users SET email='foreign@example.invalid'"]:
   self.reset();self.query(mutation)
   with self.assertRaises(m.SetupError):self.ensure()
   self.assertEqual(json.loads(self.snapshot()),[])
 def hold(self,sql):
  p=subprocess.Popen(self.args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.fixture.environment)
  p.stdin.write((sql+"\nSELECT 'LOCKED';\n").encode());p.stdin.flush();selector=selectors.DefaultSelector();selector.register(p.stdout,selectors.EVENT_READ);deadline=time.monotonic()+10
  try:
   while time.monotonic()<deadline:
    if selector.select(.2):
     line=os.read(p.stdout.fileno(),65536)
     if b'LOCKED\n' in line:return p
     if not line:break
   raise RuntimeError('Owned fixture lock not obtained')
  except BaseException:p.kill();p.communicate();raise
  finally:selector.close()
 def release(self,p):
  try:p.communicate(b'ROLLBACK;\n',timeout=5)
  finally:
   if p.poll() is None:p.kill();p.communicate()
 def test_actual_fk_blocks_insert_and_move_in(self):
  foreign=self.setup['forbidden_workspace_id'];target=self.setup['workspace_id'];self.query(self.insert(foreign))
  holder=self.hold("BEGIN; SELECT id FROM platform.workspaces WHERE id='%s' FOR UPDATE;"%target);before=self.snapshot()
  try:
   for sql in [self.insert(target,bucket='other-bucket'),"UPDATE platform.workspace_storage_providers SET workspace_id='%s';"%target]:
    r=self.query("SET lock_timeout='200ms';"+sql,ok=False);self.assertNotEqual(r.returncode,0);self.assertIn(b'lock timeout',r.stderr);self.assertEqual(self.snapshot(),before)
  finally:self.release(holder)
  self.ensure();self.assertEqual(len(json.loads(self.snapshot())),2)
 def test_existing_provider_row_lock_blocks_nonkey_update(self):
  self.ensure();target=self.setup['workspace_id'];before=self.snapshot()
  holder=self.hold("BEGIN; SELECT id FROM platform.workspaces WHERE id='%s' FOR UPDATE; SELECT id FROM platform.workspace_storage_providers WHERE workspace_id='%s' FOR UPDATE;"%(target,target))
  try:
   r=self.query("SET lock_timeout='200ms'; UPDATE platform.workspace_storage_providers SET bucket='changed';",ok=False);self.assertNotEqual(r.returncode,0);self.assertIn(b'lock timeout',r.stderr);self.assertEqual(self.snapshot(),before)
  finally:self.release(holder)
  self.ensure();self.assertEqual(self.snapshot(),before)
 def test_exact_hook_blocks_insert_movein_and_owner_revocation(self):
  # A fixture-only BEFORE INSERT trigger pauses the UNMODIFIED candidate SQL
  # after its parent/ownership locks; no barrier exists in production source.
  foreign=self.setup['forbidden_workspace_id'];target=self.setup['workspace_id'];self.query(self.insert(foreign))
  self.query("CREATE FUNCTION platform.fixture_barrier() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.display_name='Apex deployment smoke staging' THEN PERFORM pg_advisory_xact_lock(604022); END IF; RETURN NEW; END $$; CREATE TRIGGER fixture_barrier BEFORE INSERT ON platform.workspace_storage_providers FOR EACH ROW EXECUTE FUNCTION platform.fixture_barrier();")
  blocker=self.hold('BEGIN; SELECT pg_advisory_xact_lock(604022);');hook=None
  try:
   payload=m.storage_provider_payload(self.stage,self.setup,self.j,self.values);sql=m.storage_provider_sql(payload)
   application='apex_lock_'+self.j['nonce'][:16]
   hook=subprocess.Popen(self.interactive(application),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.fixture.environment)
   hook.stdin.write(sql);hook.stdin.close();hook.stdin=None
   deadline=time.monotonic()+8
   while time.monotonic()<deadline:
    waiting=self.query("SELECT count(*) FROM pg_stat_activity WHERE application_name='"+application+"' AND wait_event='advisory';").stdout.strip()
    if waiting==b'1':break
    time.sleep(.02)
   else:self.fail('Exact hook did not reach owned fixture barrier')
   before=self.snapshot()
   for query in [self.insert(target,bucket='concurrent-bucket'),"UPDATE platform.workspace_storage_providers SET workspace_id='%s';"%target,"DELETE FROM authz.user_roles WHERE workspace_id='%s';"%target,"UPDATE platform.workspace_members SET status='inactive' WHERE workspace_id='%s';"%target]:
    r=self.query("SET lock_timeout='200ms';"+query,ok=False);self.assertNotEqual(r.returncode,0);self.assertIn(b'lock timeout',r.stderr);self.assertEqual(self.snapshot(),before)
   self.release(blocker);blocker=None
   out,err=hook.communicate(timeout=10);self.assertEqual(hook.returncode,0);self.assertEqual(json.loads(out),{'stage':'staging','storage_provider_ready':True});self.assertEqual(len(json.loads(self.snapshot())),2)
  finally:
   if blocker:self.release(blocker)
   if hook and hook.poll() is None:hook.kill();hook.communicate()
 def test_exact_hook_rereads_after_waiting_for_parent_lock(self):
  target=self.setup['workspace_id']
  # Sequence increments survive rollback, exposing any attempted insert after
  # a stale snapshot. Only the candidate's exact display name increments it.
  self.query("CREATE SEQUENCE platform.fixture_insert_attempts; CREATE FUNCTION platform.fixture_insert_counter() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.display_name='Apex deployment smoke staging' THEN PERFORM nextval('platform.fixture_insert_attempts'); END IF; RETURN NEW; END $$; CREATE TRIGGER fixture_counter BEFORE INSERT ON platform.workspace_storage_providers FOR EACH ROW EXECUTE FUNCTION platform.fixture_insert_counter();")
  holder=self.hold("BEGIN; SELECT id FROM platform.workspaces WHERE id='%s' FOR UPDATE;"%target+self.insert(target,bucket='new-conflict-bucket'));hook=None
  try:
   sql=m.storage_provider_sql(m.storage_provider_payload(self.stage,self.setup,self.j,self.values))
   application='apex_lock_'+self.j['nonce'][:16]
   hook=subprocess.Popen(self.interactive(application)+['-c',"SET default_transaction_isolation='repeatable read';",'-f','-'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.fixture.environment)
   hook.stdin.write(sql);hook.stdin.close();hook.stdin=None
   deadline=time.monotonic()+8
   while time.monotonic()<deadline:
    waiting=self.query("SELECT count(*) FROM pg_stat_activity WHERE application_name='"+application+"' AND wait_event_type='Lock';").stdout.strip()
    if waiting==b'1':break
    time.sleep(.02)
   else:self.fail('Exact hook did not wait for parent row')
   holder.communicate(b'COMMIT;\n',timeout=5);self.assertEqual(holder.returncode,0);holder=None
   out,err=hook.communicate(timeout=10);self.assertNotEqual(hook.returncode,0);self.assertIn(b'Smoke provider conflict refused',err)
   self.assertEqual(self.query('SELECT is_called FROM platform.fixture_insert_attempts;').stdout.strip(),b'f')
   rows=json.loads(self.snapshot());self.assertEqual(len(rows),1);self.assertEqual(rows[0]['bucket'],'new-conflict-bucket');self.assertFalse(rows[0]['is_default'])
   before=self.snapshot()
   with self.assertRaises(m.SetupError):self.ensure()
   self.assertEqual(self.snapshot(),before)
  finally:
   if holder:self.release(holder)
   if hook and hook.poll() is None:hook.kill();hook.communicate()
class LifecycleTests(unittest.TestCase):
 def test_launch_failure_still_closes_only_the_owned_fixture(self):
  fixture=Mock();fixture.start.side_effect=postgres_fixture.FixtureError('fixture startup refused')
  with patch.object(postgres_fixture,'PostgresFixture',return_value=fixture):
   with self.assertRaises(postgres_fixture.FixtureError):Tests.setUpClass()
   Tests.doClassCleanups();self.assertEqual(Tests.tearDown_exceptions,[]);fixture.close.assert_called_once_with()
 def test_cleanup_failure_is_reported(self):
  fixture=Mock();fixture.start.side_effect=postgres_fixture.FixtureError('fixture startup refused');fixture.close.side_effect=postgres_fixture.FixtureError('fixture cleanup refused')
  with patch.object(postgres_fixture,'PostgresFixture',return_value=fixture):
   with self.assertRaises(postgres_fixture.FixtureError):Tests.setUpClass()
   Tests.doClassCleanups();self.assertEqual(len(Tests.tearDown_exceptions),1);fixture.close.assert_called_once_with()
if __name__=='__main__':unittest.main()
