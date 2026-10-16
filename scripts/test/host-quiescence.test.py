#!/usr/bin/env python3
"""Private filesystem/native-stream fixtures; no hosted drain acceptance."""
import importlib.util,json,os,socket,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
MODULE=ROOT/'scripts/release/host_quiescence.py'

class Tests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(MODULE.is_file(),'host Storage guard is missing')
  spec=importlib.util.spec_from_file_location('host_quiescence',MODULE)
  self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
  self.tmp=tempfile.TemporaryDirectory(prefix='sq-',dir='/private/tmp' if sys.platform=='darwin' else '/tmp');self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  self.state_dir=self.root/'state';self.state_dir.mkdir(mode=0o700)
  import host_release_state
  self.state=host_release_state.ReleaseState(self.state_dir,stage='qa',owner_uid=os.geteuid())
  self.state.__enter__();self.addCleanup(self.state.__exit__,None,None,None)
  request={'schema_version':1,'artifact_kind':'apex-deployment-request','controller':{'repository':'Xynes-Studio/xynes-infra','commit':'a'*40,'workflow':'apex-build.yml','run_id':123,'run_attempt':1},'publication':{'id':456,'digest':'sha256:'+'b'*64},'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
  self.token=self.state.begin(request,source_sha256='sha256:'+'c'*64,started_at=int(time.time()))
  self.generation='d'*64;self.cid='e'*64
  self.token_file=self.root/'etc/xynes/storage-control/qa/control.token'
  self.token_file.parent.mkdir(parents=True,mode=0o700);self.token_file.write_text('f'*64);self.token_file.chmod(0o400)
  os.chown(self.token_file,os.geteuid(),os.getegid())
  self.directory=self.root/('run/xynes/storage/qa/'+self.generation[:32])
  self.directory.mkdir(parents=True,mode=0o700)
  os.chown(self.directory,os.geteuid(),os.getegid())
  self.path=self.directory/'control.sock';self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
  self.listener.bind(str(self.path));self.path.chmod(0o600);self.listener.listen(8)
  os.chown(self.path,os.geteuid(),os.getegid())
  self.addCleanup(self.listener.close)
  self.messages=[];self.response=None;self.stop=threading.Event();self.addCleanup(self.stop.set)
  self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
  self.images={key:'ghcr.io/xynes-studio/apex/'+key.lower()+'@sha256:'+'1'*64 for key in self.m.SERVICE_KEYS.values()}
  self.prepared=SimpleNamespace(stage='qa',images=self.images,storage_generation=self.generation)
  self.rows={'storage-service':self.storage_row()};self.running=0
  self.guard=self.m.Guard('qa',self.state,root=self.root,owner_uid=os.geteuid(),service_uid=os.geteuid(),service_gid=os.getegid(),runner=self.docker,peer_reader=lambda sock:(os.getpid(),os.geteuid(),os.getegid()),process_verifier=lambda *args:None,claims_reader=lambda stage:self.running)

 def storage_row(self):
  return {'id':self.cid,'image':self.images['IMAGE_STORAGE'],'labels':{'com.docker.compose.project':'xynes-qa','com.docker.compose.service':'storage-service','com.docker.compose.oneoff':'False','com.xynes.managed':'true','com.xynes.environment':'qa','com.xynes.storage-generation':self.generation},'state':'running','health':'healthy','pid':os.getpid(),'user':'1001:1001','enabled':True,'mounts':[{'Type':'bind','Source':'/etc/xynes/storage-control/qa/control.token','Destination':'/run/secrets/storage-quiesce-token','RW':False},{'Type':'bind','Source':'/run/xynes/storage/qa/'+self.generation[:32],'Destination':'/run/xynes/storage-control','RW':True}]}

 def inventory(self):return {service:{key:row[key] for key in ('id','image','labels','state','health')} for service,row in self.rows.items()}
 def docker(self,argv,**kwargs):
  self.assertEqual(argv[:5],self.m.host_compose.BASE)
  if argv[5]=='ps':
   exact=next((x.split('=',1)[1] for x in argv if x.startswith('id=')),None)
   if exact is not None:
    return (exact+'\n').encode() if any(row['id']==exact for row in self.rows.values()) or getattr(self,'old_present',False) else b''
   service=next(x.split('=',1)[1] for x in argv if x.startswith('label=com.docker.compose.service=')).split('=',1)[1]
   return (self.rows.get(service,{}).get('id','')+'\n').encode() if service in self.rows else b''
  self.assertEqual(argv[5:7],['inspect','--format'])
  row=next(row for row in self.rows.values() if row['id']==argv[-1]);return json.dumps(row).encode()

 def serve(self):
  self.listener.settimeout(.1)
  while not self.stop.is_set():
   try:conn,_=self.listener.accept()
   except (TimeoutError,socket.timeout):continue
   except OSError:return
   with conn:
    raw=b''
    while b'\r\n\r\n' not in raw:raw+=conn.recv(4096)
    header,body=raw.split(b'\r\n\r\n',1);length=int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
    while len(body)<length:body+=conn.recv(4096)
    path=header.split(b' ')[1].decode();value=json.loads(body);self.messages.append((path,value,header))
    owner=value['owner']
    data=({'resumed':True} if path=='/resume' else {'state':'open','owner':None,'expires_at':None,'admission_open':True,'active_http':0,'active_background':0} if path=='/status' and any(item[0]=='/resume' for item in self.messages) else {'state':'quiesced','owner':owner,'expires_at':int(time.time()*1000)+3600000,'admission_open':False,'active_http':0,'active_background':0})
    if self.response is not None:data=self.response(path,data)
    encoded=json.dumps(data,separators=(',',':')).encode()
    # Exercise native chunked framing, including headers/body coalescing.
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n'+format(len(encoded),'x').encode()+b'\r\n'+encoded+b'\r\n0\r\n\r\n')

 def acquire(self,replace=None):return self.guard.before_replacement(self.prepared,None,self.inventory(),replace or {'storage-service'})
 def test_real_authenticated_stream_and_held_state_binding(self):
  self.acquire();self.guard.verify_before_stop(stop_budget_seconds=900)
  self.assertEqual(self.messages[0][0],'/quiesce');self.assertEqual(self.messages[0][1]['owner'],self.token[:32])
  self.assertIn(b'Authorization: Bearer '+b'f'*64,self.messages[0][2])
  self.guard.resume();self.assertEqual(self.messages[-1][0],'/status')

 def test_false_proof_running_claims_short_lease_and_wrong_owner_refuse_resume(self):
  for change in ({'active_background':1},{'state':'timeout'},{'owner':'0'*32},{'expires_at':int(time.time()*1000)+1000}):
   with self.subTest(change=change):
    self.messages=[];self.response=lambda path,data:{**data,**change} if path!='/resume' else data
    with self.assertRaises(self.m.QuiescenceError):self.acquire()
    self.assertTrue(any(path=='/resume' for path,_,_ in self.messages))
  self.response=None;self.running=1;self.messages=[]
  with self.assertRaises(self.m.QuiescenceError):self.acquire()
  self.assertTrue(any(path=='/resume' for path,_,_ in self.messages))

 def test_native_replacement_and_peer_refuse_without_new_adoption(self):
  self.acquire();self.rows['storage-service']['id']='2'*64
  with self.assertRaises(self.m.QuiescenceError):self.guard.verify_before_stop()
  self.rows['storage-service']['id']=self.cid;self.guard.lease=None
  self.guard.peer_reader=lambda sock:(os.getpid(),os.geteuid()+1,os.getegid())
  with self.assertRaises(self.m.QuiescenceError):self.acquire()

 def test_protected_assets_and_generation_mount_contract(self):
  self.token_file.chmod(0o644)
  with self.assertRaises(self.m.QuiescenceError):self.acquire()
  self.token_file.chmod(0o400);self.rows['storage-service']['mounts'][0]['RW']=True
  with self.assertRaises(self.m.QuiescenceError):self.acquire()

 def test_processors_remain_awake_but_actual_replacement_refuses(self):
  row={'id':'3'*64,'image':self.images['IMAGE_LIBREOFFICE'],'labels':{'com.docker.compose.project':'xynes-qa','com.docker.compose.service':'libreoffice-sidecar','com.docker.compose.oneoff':'False','com.xynes.managed':'true','com.xynes.environment':'qa'},'state':'running','health':'healthy','pid':os.getpid(),'user':'1001:1001','enabled':False,'mounts':[]}
  self.rows['libreoffice-sidecar']=row;self.acquire()
  with self.assertRaises(self.m.QuiescenceError):self.acquire({'libreoffice-sidecar'})
  self.guard.resume()

 def test_fresh_absence_is_native_not_inventory_boolean(self):
  claimed=self.inventory();self.rows={}
  with self.assertRaises(self.m.QuiescenceError):self.guard.before_replacement(self.prepared,None,claimed,{'storage-service'})
  self.guard.before_replacement(self.prepared,None,{}, {'storage-service'})
  self.assertEqual(self.messages,[])

 def test_after_replacement_resume_and_reacquire_for_same_process_rollback(self):
  self.acquire();self.guard.after_replacement(self.prepared,None,self.inventory())
  self.assertIsNone(self.guard.lease);self.messages=[]
  self.acquire();self.assertEqual(self.messages[0][0],'/quiesce');self.guard.resume()

 def test_no_pending_or_unproven_catalog_refuses(self):
  self.acquire()
  with self.assertRaises(self.m.QuiescenceError):self.guard.assert_sql_safe(None)
  self.guard.resume();self.state.fail(self.token,reason='runtime',failed_at=int(time.time()))
  with self.assertRaises(self.m.QuiescenceError):self.acquire()

 def test_partial_upgrade_reacquires_new_process_only_after_exact_old_cid_gone(self):
  self.acquire();old=self.guard.lease['row']['id']
  self.stop.set();self.thread.join(timeout=1);self.assertFalse(self.thread.is_alive())
  self.listener.close();self.generation='4'*64;self.cid='2'*64
  self.directory=self.root/('run/xynes/storage/qa/'+self.generation[:32]);self.directory.mkdir(mode=0o700)
  os.chown(self.directory,os.geteuid(),os.getegid());self.path=self.directory/'control.sock'
  self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.listener.bind(str(self.path))
  self.path.chmod(0o600);os.chown(self.path,os.geteuid(),os.getegid());self.listener.listen(8);self.addCleanup(self.listener.close)
  self.stop=threading.Event();self.addCleanup(self.stop.set);self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
  self.rows={'storage-service':self.storage_row()};self.messages=[];self.acquire()
  self.assertEqual(self.guard.lease['row']['id'],self.cid);self.assertNotEqual(self.cid,old)
  self.assertEqual(self.messages[0][0],'/quiesce');self.guard.resume()

class ProcessTests(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('host_quiescence',MODULE)
  self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
  self.cid='e'*64;self.reads=0
 def stat(self,pid,ppid,*,start=300,cpu=0):
  fields=['S',str(ppid)]+['0']*18;fields[11]=str(cpu);fields[19]=str(start)
  return (str(pid)+' (inert worker) '+' '.join(fields)).encode()
 def test_live_cpu_accounting_does_not_invalidate_same_peer_process(self):
  def read(pid,name):
   if name=='cgroup':return ('0::/system.slice/docker-'+self.cid+'.scope\n').encode()
   self.reads+=1;return self.stat(pid,100 if pid==101 else 1,cpu=self.reads)
  with patch.object(self.m.sys,'platform','linux'),patch.object(self.m,'proc',read):
   self.m.process_member(101,100,self.cid)
 def test_reused_peer_pid_and_foreign_cgroup_refuse(self):
  def read(pid,name):
   if name=='cgroup':return ('0::/system.slice/docker-'+self.cid+'.scope\n').encode()
   self.reads+=1;return self.stat(pid,100 if pid==101 else 1,start=300 if self.reads==1 else 301)
  with patch.object(self.m.sys,'platform','linux'),patch.object(self.m,'proc',read):
   with self.assertRaises(self.m.QuiescenceError):self.m.process_member(101,100,self.cid)
  with patch.object(self.m.sys,'platform','linux'),patch.object(self.m,'proc',lambda pid,name:b'0::/foreign\n'):
   with self.assertRaises(self.m.QuiescenceError):self.m.process_member(101,100,self.cid)

if __name__=='__main__':unittest.main()
