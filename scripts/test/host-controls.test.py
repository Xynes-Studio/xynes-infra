#!/usr/bin/env python3
"""Fixed host operations with private filesystem and inert command boundaries."""
import importlib.util,json,os,socket,sys,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/release'))
class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/host_controls.py';self.assertTrue(path.is_file(),'concrete host controls missing')
  spec=importlib.util.spec_from_file_location('host_controls',path);self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
  self.temp=tempfile.TemporaryDirectory(prefix='hc-',dir='/private/tmp' if Path('/private/tmp').is_dir() else '/tmp');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
  self.calls=[];self.cid='a'*64;self.counts={'authz_permissions':200,'authz_roles':5,'authz_mappings':700,'cms_templates':3,'workspaces':0,'cms_defaults_satisfied':0}
  self.controls=self.m.Controls('qa',root=self.root,owner_uid=os.getuid(),data_uid=os.getuid(),run=self.command,environment=lambda stage:{'XYNES_ENV':stage,'POSTGRES_PASSWORD':'INERT_DB_PASSWORD'},container=lambda stage:self.cid)
 def command(self,args,*,env,data=None):
  self.calls.append((args,env,data))
  if args[0]=='/usr/bin/systemctl':return b'inactive\n'
  if 'ps' in args:return b''
  self.assertEqual(args[:5],self.m.host_compose.BASE)
  self.assertNotIn('INERT_DB_PASSWORD',str(args));self.assertEqual(env['PGPASSWORD'],'INERT_DB_PASSWORD')
  self.assertNotIn('B2_APPLICATION_KEY',env)
  return json.dumps(self.counts).encode()
 def data(self):
  path=self.root/'srv/xynes/qa/supabase/db';path.mkdir(parents=True,mode=0o700)
  for p in path.parents:
   if p==self.root:break
   p.chmod(0o700)
  return path
 def policy(self,value):
  path=self.root/'etc/xynes/idle/qa/deployment-mode.json';path.parent.mkdir(parents=True,exist_ok=True)
  path.write_text(json.dumps(value));path.chmod(0o600);return path
 def test_seed_query_is_fixed_readonly_and_credentials_are_not_arguments(self):
  result=self.controls.seed_counts('qa',self.m.host_seed.POSTCONDITIONS_SQL)
  self.assertEqual(result,self.counts)
  self.assertIn(b'BEGIN READ ONLY',self.calls[0][2]);self.assertIn(b'COMMIT;',self.calls[0][2])
  with self.assertRaises(self.m.ControlError):self.controls.seed_counts('qa','DROP SCHEMA platform CASCADE;')
  with self.assertRaises(self.m.ControlError):self.controls.seed_counts('prod',self.m.host_seed.POSTCONDITIONS_SQL)
 def test_seed_result_shape_and_container_replacement_refuse(self):
  self.counts['raw_secret']='INERT';
  with self.assertRaises(self.m.ControlError):self.controls.seed_counts('qa',self.m.host_seed.POSTCONDITIONS_SQL)
  self.counts.pop('raw_secret');count=0
  def container(stage):
   nonlocal count;count+=1;return 'a'*64 if count==1 else 'b'*64
  self.controls.container=container
  with self.assertRaises(self.m.ControlError):self.controls.seed_counts('qa',self.m.host_seed.POSTCONDITIONS_SQL)
 def test_fresh_audit_requires_real_empty_owned_data_and_no_stage_db(self):
  self.data();self.controls.container=lambda stage:None
  current=type('Current',(),{'prepared':type('Prepared',(),{'images':{'IMAGE_SUPABASE_DB':self.m.pg_container_bridge.DB_IMAGE}})()})()
  result=self.controls.target_audit('qa',current,None)
  self.assertEqual(result['database_state'],'fresh');self.assertIsNone(result['database_container_id'])
  (self.root/'srv/xynes/qa/supabase/db/unknown').write_text('preserve')
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',current,None)
 def test_disabled_warmth_needs_sealed_policy_no_idle_containers_and_no_units(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'disabled','groups':[]})
  with self.controls.warm('qa',type('Current',(),{'controller':{'run_id':123}})(),None):pass
  self.assertEqual(len([c for c in self.calls if c[0][0]=='/usr/bin/systemctl']),6)
  self.controls.run=lambda *a,**kw:b'active\n'
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',type('Current',(),{'controller':{'run_id':123}})(),None):pass
 def test_enabled_leases_acquire_and_release_only_exact_two_groups(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'enabled','groups':['auth','cms']})
  events=[]
  def lease(group,path,owner):events.append((group,path,owner))
  self.controls.lease=lease
  with self.controls.warm('qa',type('Current',(),{'controller':{'run_id':123}})(),None):events.append('inside')
  self.assertEqual(events,[('auth','/warm','deploy-123'),('cms','/warm','deploy-123'),'inside',('cms','/release','deploy-123'),('auth','/release','deploy-123')])
 def test_partial_warm_acquisition_releases_owned_first_group(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'enabled','groups':['auth','cms']});events=[]
  def lease(group,path,owner):
   events.append((group,path))
   if group=='cms' and path=='/warm':raise RuntimeError('INERT_PRIVATE_ERROR')
  self.controls.lease=lease
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',type('Current',(),{'controller':{'run_id':123}})(),None):pass
  self.assertEqual(events,[('auth','/warm'),('cms','/warm'),('cms','/release'),('auth','/release')])

 def test_enabled_native_coordinator_owns_fresh_warmth_and_finish(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'enabled','groups':['auth','cms']});events=[]
  class Idle:
   stage='qa'
   def begin(inner,owner,*,first,lease):events.append(('begin',owner,first,lease==self.controls.lease))
   def finish(inner,owner):events.append(('finish',owner))
  self.controls.idle_control=Idle()
  with self.controls.warm('qa',self.current(),None):events.append('product-smoke-after-rebind')
  self.assertEqual(events,[('begin','deploy-123',True,True),'product-smoke-after-rebind',('finish','deploy-123')])

 def test_native_finish_failure_is_not_hidden_by_context_cleanup(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'enabled','groups':['auth','cms']})
  class Idle:
   stage='qa'
   def begin(inner,*a,**kw):pass
   def finish(inner,*a):raise ValueError('inert failure')
  self.controls.idle_control=Idle()
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',self.current(),None):pass

 def current(self):
  return type('Current',(),{'prepared':type('Prepared',(),{'images':{'IMAGE_SUPABASE_DB':self.m.pg_container_bridge.DB_IMAGE}})(), 'controller':{'run_id':123}})()

 def test_fresh_database_audit_rechecks_container_after_empty_directory_inspection(self):
  self.data();calls=0
  def container(stage):
   nonlocal calls;calls+=1;return None if calls==1 else self.cid
  self.controls.container=container
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)

 def test_existing_pg_version_is_bounded_single_link_and_owned(self):
  data=self.data();marker=data/'PG_VERSION';marker.write_bytes(b'17\n');marker.chmod(0o600)
  self.assertEqual(self.controls.target_audit('qa',self.current(),None)['database_state'],'existing')
  alias=data/'alias';os.link(marker,alias)
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)
  alias.unlink();marker.write_bytes(b'17\n'+b'X'*65536)
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)

 def test_existing_database_accepts_data_uid_distinct_from_root_without_relaxing_ancestors(self):
  data=self.data();marker=data/'PG_VERSION';marker.write_bytes(b'17\n');marker.chmod(0o600)
  self.controls.data_uid=os.getuid()+10000
  original_lstat=Path.lstat;original_fstat=os.fstat;original_stat=os.stat
  selected={(p.stat().st_dev,p.stat().st_ino) for p in (data,marker)}
  def data_owned(info):
   if (info.st_dev,info.st_ino) not in selected:return info
   fields=('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
   return types.SimpleNamespace(**{k:self.controls.data_uid if k in ('st_uid','st_gid') else getattr(info,k) for k in fields})
  with patch.object(Path,'lstat',lambda p:data_owned(original_lstat(p))),patch.object(os,'fstat',lambda fd:data_owned(original_fstat(fd))),patch.object(os,'stat',lambda *a,**kw:data_owned(original_stat(*a,**kw))):
   self.assertEqual(self.controls.target_audit('qa',self.current(),None)['database_state'],'existing')
   # The generic root-owned ancestor rule must remain strict for other reads.
   with self.assertRaises(self.m.ControlError):self.controls.protected(marker,leaf_uid=self.controls.data_uid,mode=0o600)

 def test_pg_version_symlink_and_wrong_mode_refuse(self):
  data=self.data();marker=data/'PG_VERSION';target=data/'saved';target.write_bytes(b'17\n');target.chmod(0o600)
  marker.symlink_to(target)
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)
  marker.unlink();target.rename(marker);marker.chmod(0o640)
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)

 def test_pg_version_replacement_between_stat_and_open_refuses(self):
  data=self.data();marker=data/'PG_VERSION';marker.write_bytes(b'17\n');marker.chmod(0o600)
  original_open=os.open;replaced=False
  def replace(path,*args,**kwargs):
   nonlocal replaced
   if path=='PG_VERSION' and not replaced:
    replaced=True;marker.rename(data/'preserved');marker.write_bytes(b'17\n');marker.chmod(0o600)
   return original_open(path,*args,**kwargs)
  with patch.object(os,'open',replace),self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)
  self.assertTrue(replaced)

 def test_disabled_policy_cannot_hide_an_active_broker(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'disabled','groups':[]})
  def command(args,**options):
   if args[0]=='/usr/bin/systemctl' and args[-1]=='xynes-idle-qa-broker.service':return b'active\n'
   return self.command(args,**options)
  self.controls.run=command
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',self.current(),None):pass

 def test_policy_replacement_and_inplace_bytes_drift_refuse_even_equal_json(self):
  for mode in ('replacement','bytes'):
   path=self.policy({'schema_version':1,'stage':'qa','mode':'disabled','groups':[]})
   with self.assertRaises(self.m.ControlError):
    with self.controls.warm('qa',self.current(),None):
     if mode=='replacement':
      new=path.parent/'new';new.write_bytes(path.read_bytes());new.chmod(0o600);os.replace(new,path)
     else:path.write_bytes(path.read_bytes()+b'\n')

 def test_readonly_command_adapter_cannot_execute_arbitrary_sql(self):
  args=self.m.host_compose.BASE+['exec','-i','--env','PGPASSWORD',self.cid,'psql']
  with patch.object(self.m.host_migrate,'_execute',return_value=b'{}') as execute:
   with self.assertRaises(self.m.ControlError):self.m.command(args,env={'PGPASSWORD':'INERT'},data=b'DROP SCHEMA platform CASCADE;')
   execute.assert_not_called()

 def test_readonly_command_adapter_cannot_stop_or_execute_containers(self):
  with patch.object(self.m.host_compose,'run_command',return_value=b'') as execute:
   for args in (['stop',self.cid],['exec',self.cid,'sh','-c','id']):
    with self.assertRaises(self.m.ControlError):self.m.command(self.m.host_compose.BASE+args,env=self.m.host_compose.CLEAN_ENV)
   execute.assert_not_called()

 def unix_controls(self):
  peer=None if sys.platform.startswith('linux') else lambda sock:(os.getpid(),os.getuid(),os.getgid())
  return self.m.Controls('qa',root=self.root,owner_uid=os.getuid(),data_uid=os.getuid(),run=self.command,
   environment=lambda stage:{'XYNES_ENV':stage,'POSTGRES_PASSWORD':'INERT'},container=lambda stage:self.cid,
   peer_reader=peer,proxy_identity=(os.getuid(),os.getgid()),lease_timeout=.15)

 def serve(self,response=b'HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n',slow=False,mutate=None,linger=0):
  token=self.root/'etc/xynes/idle/qa/control.token';token.parent.mkdir(parents=True,exist_ok=True)
  if token.exists():token.chmod(0o600)
  token.write_bytes(b'f'*64+b'\n');os.chown(token,-1,os.getgid());token.chmod(0o440)
  target=self.root/'run/xynes/idle/qa/auth/leases.sock';target.parent.mkdir(parents=True,mode=0o700,exist_ok=True);target.parent.chmod(0o700)
  os.chown(target.parent,-1,os.getgid())
  server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(target));os.chown(target,-1,os.getgid());target.chmod(0o600);server.listen(1);server.settimeout(1)
  messages=[];errors=[]
  def worker():
   try:
    connection,_=server.accept()
    with connection:
     connection.settimeout(1);request=b''
     while b'\r\n\r\n' not in request:
      chunk=connection.recv(4096)
      if not chunk:return
      request+=chunk
     header,body=request.split(b'\r\n\r\n',1);length=int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
     while len(body)<length:
      chunk=connection.recv(4096)
      if not chunk:return
      body+=chunk
     messages.append((header,json.loads(body)))
     if mutate:mutate()
     if slow:
      for byte in response:
       connection.sendall(bytes([byte]));time.sleep(.01)
     else:connection.sendall(response)
     if linger:time.sleep(linger)
   except (BrokenPipeError,ConnectionResetError,socket.timeout):pass
   except Exception as error:errors.append(error)
   finally:server.close()
  thread=threading.Thread(target=worker);thread.start()
  self.addCleanup(lambda:thread.join(2));return token,target,messages,thread,errors

 def test_native_unix_http_warm_auth_and_typed_body(self):
  token,target,messages,thread,errors=self.serve()
  controls=self.unix_controls();controls.lease('auth','/warm','deploy-123');thread.join(2)
  self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
  header,body=messages[0]
  self.assertIn(b'Authorization: Bearer '+b'f'*64,header)
  self.assertEqual(body,{'group':'qa-auth','owner':'deploy-123','ttl_seconds':3600})

 def test_unix_header_trickle_has_total_deadline(self):
  self.serve(slow=True);controls=self.unix_controls();started=time.monotonic()
  with self.assertRaises(self.m.ControlError):controls.lease('auth','/warm','deploy-123')
  self.assertLess(time.monotonic()-started,.6)

 def test_unix_response_oversize_body_and_wrong_token_socket_permissions_refuse(self):
  for response in (b'HTTP/1.1 204 No Content\r\nContent-Length: 1\r\n\r\nX',b'HTTP/1.1 204 No Content\r\nX-Header: '+b'X'*9000+b'\r\n\r\n'):
   token,target,messages,thread,errors=self.serve(response=response);controls=self.unix_controls()
   with self.assertRaises(self.m.ControlError):controls.lease('auth','/warm','deploy-123')
   thread.join(2);target.unlink()
  token,target,*_=self.serve();controls=self.unix_controls()
  token.chmod(0o644)
  with self.assertRaises(self.m.ControlError):controls.lease('auth','/warm','deploy-123')
  token.chmod(0o440);target.chmod(0o666)
  with self.assertRaises(self.m.ControlError):controls.lease('auth','/warm','deploy-123')

 def test_release_failure_attempts_every_owned_group_and_refuses(self):
  self.policy({'schema_version':1,'stage':'qa','mode':'enabled','groups':['auth','cms']});events=[]
  def lease(group,path,owner):
   events.append((group,path))
   if group=='cms' and path=='/release':raise RuntimeError('INERT_PRIVATE')
  self.controls.lease=lease
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',self.current(),None):pass
  self.assertEqual(events[-2:],[('cms','/release'),('auth','/release')])

 def test_lease_token_inode_change_and_wrong_peer_refuse_without_secret_diagnostics(self):
  def rotate():
   token=self.root/'etc/xynes/idle/qa/control.token';new=token.parent/'rotated'
   new.write_bytes(token.read_bytes());os.chown(new,-1,os.getgid());new.chmod(0o440);os.replace(new,token)
  token,target,messages,thread,errors=self.serve(mutate=rotate);controls=self.unix_controls()
  with self.assertRaises(self.m.ControlError) as caught:controls.lease('auth','/warm','deploy-123')
  self.assertEqual(str(caught.exception),'Host runtime control refused');thread.join(2);target.unlink()
  token,target,messages,thread,errors=self.serve();controls=self.unix_controls()
  controls.peer_reader=lambda sock:(os.getpid(),os.getuid()+1,os.getgid())
  with self.assertRaises(self.m.ControlError):controls.lease('auth','/warm','deploy-123')
  thread.join(2);self.assertEqual(messages,[])

 def test_lease_no_eof_after_headers_hits_total_deadline(self):
  self.serve(linger=.4);controls=self.unix_controls();started=time.monotonic()
  with self.assertRaises(self.m.ControlError):controls.lease('auth','/release','deploy-123')
  self.assertLess(time.monotonic()-started,.35)

 def test_replaced_data_directory_and_disabled_private_broker_socket_refuse(self):
  data=self.data();calls=0
  def container(stage):
   nonlocal calls;calls+=1
   if calls==2:
    data.rename(data.with_name('preserved'));data.mkdir(mode=0o700)
   return None
  self.controls.container=container
  with self.assertRaises(self.m.ControlError):self.controls.target_audit('qa',self.current(),None)
  self.assertTrue(data.with_name('preserved').is_dir())
  self.policy({'schema_version':1,'stage':'qa','mode':'disabled','groups':[]})
  socket_path=self.root/'run/xynes/idle/qa/control.sock';socket_path.parent.mkdir(parents=True);socket_path.write_bytes(b'unknown preserve')
  with self.assertRaises(self.m.ControlError):
   with self.controls.warm('qa',self.current(),None):pass
  self.assertEqual(socket_path.read_bytes(),b'unknown preserve')
if __name__=='__main__':unittest.main()
