#!/usr/bin/env python3
"""Inert host idle generation fixtures; never contacts Docker or systemd."""
import copy,hashlib,importlib.util,json,os,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/release'))
class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/host_idle.py';self.assertTrue(path.is_file(),'native idle coordinator missing')
  spec=importlib.util.spec_from_file_location('host_idle',path);self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
 def test_fixed_systemd_units_have_no_stage_docker_access(self):
  for stage in ('qa','staging','prod'):
   for group in ('broker','auth','cms'):
    raw=self.m.unit(stage,group).decode();self.assertIn('NoNewPrivileges=true',raw)
    self.assertIn('systemd-tmpfiles-setup.service',raw)
    self.assertIn('ConditionPathExists=/etc/xynes/idle/'+stage+'/generation.json',raw)
    self.assertEqual((ROOT/'infra/release/systemd'/('xynes-idle-'+stage+'-'+group+'.service')).read_bytes(),raw.encode())
    self.assertIn('/etc/xynes/idle/'+stage+'/',raw)
    if group!='broker':
     self.assertIn('User='+str(self.m.UID[stage]),raw);self.assertIn('InaccessiblePaths=/var/run/docker.sock',raw)
    self.assertNotIn('0.0.0.0',raw)
  with self.assertRaises(self.m.IdleError):self.m.unit('dev','auth')
 def test_policy_closed_and_private_ingress_binding(self):
  value=self.m.validate_policy(self.policy(),'qa');self.assertEqual(value['ingress']['gateway'],'172.21.0.1')
  for mutation in ('extra','public','foreign-stage','wildcard-caddy','binary','compiler'):
   value=self.policy()
   if mutation=='extra':value['unreviewed']=True
   if mutation=='public':value['ingress']['gateway']='8.8.8.8'
   if mutation=='foreign-stage':value['network']['name']='xynes_prod_net'
   if mutation=='wildcard-caddy':value['caddy']['ip']='172.21.0.2/16'
   if mutation=='binary':value['binary_sha256']='latest'
   if mutation=='compiler':value['compiler_version']='go1.0'
   with self.subTest(mutation=mutation):
    with self.assertRaises(self.m.IdleError):self.m.validate_policy(value,'qa')
 def policy(self):
  return dict(schema_version=1,stage='qa',binary_sha256='a'*64,source_sha256='b'*64,compiler_version='go1.27.2',idle_seconds=900,
   network=dict(name='xynes_qa_net',id='c'*64,gateway='172.19.0.1',subnet='172.19.0.0/16'),
   ingress=dict(name='caddy_net',id='d'*64,gateway='172.21.0.1',subnet='172.21.0.0/16'),
   caddy=dict(id='e'*64,image_id='sha256:'+'f'*64,ip='172.21.0.2'))
 def fixture(self):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name).resolve();root.chmod(0o700)
  calls=[];events=[];policy=self.policy();binary=b'inert-static-binary';policy['binary_sha256']=hashlib.sha256(binary).hexdigest()
  def put(name,raw,mode):
   path=root/name.lstrip('/');path.parent.mkdir(parents=True,exist_ok=True)
   path.write_bytes(raw);path.chmod(mode);return path
  put(self.m.BINARY,binary,0o555)
  for group in ('broker','auth','cms'):put('/etc/systemd/system/xynes-idle-qa-'+group+'.service',self.m.unit('qa',group),0o644)
  put('/etc/xynes/idle/qa/host.json',self.m.canonical(policy),0o600)
  put('/etc/xynes/idle/qa/deployment-mode.json',self.m.canonical(dict(schema_version=1,stage='qa',mode='enabled',groups=['auth','cms'])),0o600)
  for group in self.m.GROUPS:
   path=root/'var/lib/xynes/idle/qa'/group;path.mkdir(parents=True);path.chmod(0o700)
  images={key:'ghcr.io/xynes-studio/apex/'+service+'@sha256:'+'1'*64 for service,key,_ in self.m.GROUPS.values()}
  prepared=types.SimpleNamespace(images=images)
  inventory={service:dict(id=str(n)*64,image=images[key]) for n,(service,key,_) in enumerate(self.m.GROUPS.values(),2)}
  rows={}
  for n,(service,key,_) in enumerate(self.m.GROUPS.values(),10):
   row=inventory[service];rows[row['id']]=dict(id=row['id'],image=row['image'],image_id='sha256:'+'8'*64,state='running',health='healthy',
    labels={'com.xynes.managed':'true','com.xynes.environment':'qa','com.xynes.role':'application',
     'com.docker.compose.project':'xynes-qa','com.docker.compose.service':service},
    networks={'xynes_qa_net':dict(NetworkID=policy['network']['id'],Gateway=policy['network']['gateway'],IPAddress='172.19.0.'+str(n))})
  rows[policy['caddy']['id']]=dict(id=policy['caddy']['id'],image_id=policy['caddy']['image_id'],state='running',networks={
   'caddy_net':dict(NetworkID=policy['ingress']['id'],Gateway=policy['ingress']['gateway'],IPAddress=policy['caddy']['ip'])})
  def run(args,**kwargs):
   calls.append(args)
   if args[0]=='/usr/bin/systemctl':return b'inactive\n' if args[1]=='show' else b''
   if 'network' in args:
    network=next(row for row in (policy['network'],policy['ingress']) if row['id']==args[-1])
    return self.m.canonical(dict(id=network['id'],name=network['name'],driver='bridge',ipam=[dict(Subnet=network['subnet'],Gateway=network['gateway'])]))
   return self.m.canonical(rows[args[-1]])
  idle=self.m.Idle('qa',root=root,owner_uid=os.getuid(),run=run,clock=lambda:100*10**9,proxy_identity=(os.getuid(),os.getgid()))
  idle.wait_proxy=lambda group:events.append(('socket-ready',group));idle.wait_broker=lambda:events.append(('broker-ready',))
  lease=lambda group,path,owner:events.append((group,path,owner))
  return idle,prepared,inventory,policy,rows,calls,events,lease
 def test_fresh_enabled_generation_seeds_real_warm_state_then_rebinds_before_resume(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture()
  idle.begin('deploy-123',first=True,lease=lease)
  self.assertFalse(events)
  warm={g:(idle.path('/var/lib/xynes/idle/qa/'+g+'/warm.json').read_bytes()) for g in self.m.GROUPS}
  for group,raw in warm.items():
   self.assertEqual(json.loads(raw),dict(version=1,environment='qa',group='qa-'+group,leases=[dict(owner='deploy-123',until=3700*10**9)]))
  idle.rebind(prepared,inventory)
  for group in self.m.GROUPS:
   config=json.loads(idle.config_path(group+'-proxy.json').read_bytes())
   self.assertTrue(config['start_drained']);self.assertTrue(config['host_network'])
   self.assertTrue(config['listen'].startswith('172.21.0.1:'))
   self.assertEqual(config['policy']['trusted_ingress'],['172.21.0.2/32'])
   self.assertTrue(config['policy']['dynamic_target'])
   self.assertEqual(config['wake_seconds'],90)
   self.assertEqual(idle.path('/var/lib/xynes/idle/qa/'+group+'/warm.json').read_bytes(),warm[group])
  broker=json.loads(idle.config_path('broker.json').read_bytes())
  self.assertTrue(all(target['network_id']==policy['network']['id'] for target in broker['targets'].values()))
  self.assertEqual([a[1:] for a in calls if a[0]=='/usr/bin/systemctl' and a[1] in ('stop','start')],
   [[action,'xynes-idle-qa-'+group+'.service'] for action,groups in [('stop',('auth','cms','broker')),('start',('broker','auth','cms'))] for group in groups])
  idle.finish('deploy-123');self.assertFalse(idle.config_path('deployment.json').exists())
  for group in self.m.GROUPS:self.assertFalse(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  self.assertEqual(events[-2:],[('cms','/release','deploy-123'),('auth','/release','deploy-123')])
 def test_changed_application_and_rollback_rebind_exact_new_and_old_ids_ips(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory)
  original=copy.deepcopy(inventory);oldrows=copy.deepcopy(rows);events.clear()
  idle.before_replace();self.assertEqual(events[:2],[('auth','/drain','deploy-123'),('cms','/drain','deploy-123')])
  for n,(service,key,_) in enumerate(self.m.GROUPS.values(),4):
   old=inventory[service]['id'];row=copy.deepcopy(rows[old]);row['id']=str(n)*64;row['networks']['xynes_qa_net']['IPAddress']='172.19.0.'+str(n+10)
   inventory[service]['id']=row['id'];rows[row['id']]=row
  idle.rebind(prepared,inventory);changed=idle.generation()['bindings'];self.assertEqual(changed['auth']['id'],'4'*64)
  idle.before_replace();rows.update(oldrows);idle.rebind(prepared,original)
  self.assertEqual(idle.generation()['bindings']['auth']['id'],'2'*64);idle.finish('deploy-123')
 def test_foreign_native_binding_prevents_start_or_resume_and_retains_owner(self):
  for mutation in ('container','network','image','caddy','binary'):
   idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);calls.clear()
   app=rows[inventory['auth-app']['id']]
   if mutation=='container':app['id']='9'*64
   if mutation=='network':app['networks']['xynes_qa_net']['NetworkID']='9'*64
   if mutation=='image':app['image']='ghcr.io/foreign/app@sha256:'+'9'*64
   if mutation=='caddy':rows[policy['caddy']['id']]['image_id']='sha256:'+'9'*64
   if mutation=='binary':
    path=idle.path(self.m.BINARY);path.chmod(0o755);path.write_bytes(b'changed');path.chmod(0o555)
   with self.subTest(mutation=mutation):
    with self.assertRaises(self.m.IdleError):idle.rebind(prepared,inventory)
    self.assertTrue(idle.config_path('deployment.json').exists());self.assertFalse(events)
    self.assertFalse(any(a[0]=='/usr/bin/systemctl' and a[1]=='start' for a in calls))
 def test_unknown_fresh_state_and_missing_generation_never_adopt_existing_runtime(self):
  for name in ('warm.json','warm.lock'):
   idle,*rest=self.fixture();path=idle.path('/var/lib/xynes/idle/qa/auth/'+name);path.write_text('preserve');path.chmod(0o600)
   with self.assertRaises(self.m.IdleError):idle.begin('deploy-123',first=True,lease=rest[-1])
   self.assertEqual(path.read_text(),'preserve')
  idle,*rest=self.fixture()
  with self.assertRaises(self.m.IdleError):idle.begin('deploy-123',first=False,lease=rest[-1])
 def test_restart_during_later_deployment_remains_drained_until_finish(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture()
  idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory);idle.finish('deploy-123')
  for group in self.m.GROUPS:self.assertFalse(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  observed=[]
  def verify_restart_policy(group,path,owner):
   if path=='/drain':
    # RunProxy reloads this exact root-protected flag after a systemd restart.
    restarted={g:json.loads(idle.config_path(g+'-proxy.json').read_bytes())['start_drained'] for g in self.m.GROUPS}
    self.assertEqual(restarted,{'auth':True,'cms':True});idle.generation();observed.append(group)
   lease(group,path,owner)
  idle.begin('deploy-456',first=False,lease=verify_restart_policy)
  self.assertEqual(observed,['auth','cms'])
  idle.rebind(prepared,inventory)
  for group in self.m.GROUPS:self.assertTrue(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  idle.finish('deploy-456')
  for group in self.m.GROUPS:self.assertFalse(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
 def test_finish_partial_normal_policy_write_redrains_and_restores_durable_closed_state(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory)
  original=idle.write;events.clear()
  def write(path,raw,**kwargs):
   if path.name=='cms-proxy.json' and json.loads(raw).get('start_drained') is False:raise OSError('inert fsync fault after auth config write')
   return original(path,raw,**kwargs)
  idle.write=write
  with self.assertRaises(self.m.IdleError):idle.finish('deploy-123')
  for group in self.m.GROUPS:self.assertTrue(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  idle.generation()
  self.assertEqual(events[-2:],[('auth','/drain','deploy-123'),('cms','/drain','deploy-123')])
  self.assertTrue(idle.config_path('deployment.json').exists())
  self.assertFalse(any(e[1]=='/release' for e in events if len(e)==3))
 def test_finish_generation_fsync_fault_restores_matching_closed_configuration_hashes(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory)
  original=idle.write;fault=[False]
  def write(path,raw,**kwargs):
   result=original(path,raw,**kwargs)
   if path.name=='generation.json' and not fault[0] and json.loads(idle.config_path('auth-proxy.json').read_bytes())['start_drained'] is False:
    fault[0]=True;raise OSError('inert failure after generation replacement')
   return result
  idle.write=write
  with self.assertRaises(self.m.IdleError):idle.finish('deploy-123')
  self.assertTrue(fault[0]);idle.generation()
  for group in self.m.GROUPS:self.assertTrue(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
 def test_partial_warm_release_reacquires_both_holds_and_retains_closed_restart_state(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory);events.clear()
  def fail_release(group,path,owner):
   lease(group,path,owner)
   if group=='auth' and path=='/release':raise OSError('inert second release failure')
  idle.lease=fail_release
  with self.assertRaises(self.m.IdleError):idle.finish('deploy-123')
  idle.generation()
  for group in self.m.GROUPS:self.assertTrue(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  self.assertEqual(events[-4:],[('auth','/warm','deploy-123'),('cms','/warm','deploy-123'),('auth','/drain','deploy-123'),('cms','/drain','deploy-123')])
  self.assertTrue(idle.config_path('deployment.json').exists())
 def test_owner_unlink_failure_after_release_reacquires_holds_and_closes_restart_policy(self):
  idle,prepared,inventory,policy,rows,calls,events,lease=self.fixture();idle.begin('deploy-123',first=True,lease=lease);idle.rebind(prepared,inventory);events.clear()
  original=Path.unlink
  def unlink(path,*args,**kwargs):
   if path==idle.config_path('deployment.json'):raise OSError('inert unlink failure after release')
   return original(path,*args,**kwargs)
  with patch.object(Path,'unlink',unlink),self.assertRaises(self.m.IdleError):idle.finish('deploy-123')
  idle.generation()
  for group in self.m.GROUPS:self.assertTrue(json.loads(idle.config_path(group+'-proxy.json').read_bytes())['start_drained'])
  self.assertEqual(events[-4:],[('auth','/warm','deploy-123'),('cms','/warm','deploy-123'),('auth','/drain','deploy-123'),('cms','/drain','deploy-123')])
  self.assertTrue(idle.config_path('deployment.json').exists())
 def test_fallback_attempts_both_exact_proxy_stops_even_if_first_stop_fails(self):
  idle,*rest=self.fixture();original=idle.run;stops=[]
  def stop(args,**kwargs):
   if args[:2]==['/usr/bin/systemctl','stop']:
    stops.append(args[-1])
    if args[-1]=='xynes-idle-qa-auth.service':raise OSError('inert first stop failure')
   return original(args,**kwargs)
  idle.run=stop
  with self.assertRaises(self.m.IdleError):idle.stop_proxies()
  self.assertEqual(stops,['xynes-idle-qa-auth.service','xynes-idle-qa-cms.service'])
 def test_tmpfiles_reconstructs_missing_run_without_touching_persistent_warmth(self):
  raw=self.m.tmpfiles()
  self.assertEqual((ROOT/'infra/release/systemd/xynes-idle-tmpfiles.conf').read_bytes(),raw)
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);(root/'run').mkdir();warm=root/'var/lib/xynes/idle/qa/auth/warm.json';warm.parent.mkdir(parents=True);warm.write_bytes(b'preserve-known-owner')
   actions=[]
   for line in raw.decode().splitlines():
    kind,path,mode,uid,gid,age=line.split()
    self.assertEqual(kind,'d');self.assertEqual(age,'-');self.assertTrue(path.startswith('/run/xynes'))
    target=root/path.lstrip('/');target.mkdir(exist_ok=True);target.chmod(int(mode,8));actions.append((path,int(mode,8),int(uid),int(gid)))
   self.assertEqual(warm.read_bytes(),b'preserve-known-owner')
   for stage,uid in self.m.UID.items():
    self.assertIn(('/run/xynes/idle/'+stage,0o750,0,uid),actions)
    for group in self.m.GROUPS:self.assertIn(('/run/xynes/idle/'+stage+'/'+group,0o700,uid,uid),actions)
 def test_fresh_enabled_state_refuses_live_broker_before_warm_seeding(self):
  idle,*rest=self.fixture();run=idle.run
  def active_broker(args,**kwargs):
   if args[0]=='/usr/bin/systemctl' and args[-1]=='xynes-idle-qa-broker.service':return b'active\n'
   return run(args,**kwargs)
  idle.run=active_broker
  with self.assertRaises(self.m.IdleError):idle.begin('deploy-123',first=True,lease=rest[-1])
  self.assertFalse(idle.path('/var/lib/xynes/idle/qa/auth/warm.json').exists())
 def policies(self):
  result={}
  for stage in self.m.UID:
   value=self.policy();value['stage']=stage;value['network']['name']='xynes_'+stage+'_net';result[stage]=value
  return result
 def test_provision_plan_is_closed_and_limits_legacy_traversal_changes(self):
  plan=self.m.provision_plan(self.policies())
  self.assertFalse(plan['installed']);self.assertFalse(plan['source_qualified']);self.assertFalse(plan['application_services_activated'])
  self.assertEqual({r['path'] for r in plan['parent_mode_changes']},{'/var/lib/xynes','/etc/xynes/idle'})
  self.assertTrue(all(r['old_mode']==0o700 and r['mode']==0o711 and r['uid']==r['gid']==0 for r in plan['parent_mode_changes']))
  for stage,uid in self.m.UID.items():
   account=plan['accounts'][stage];self.assertEqual(account,dict(name='xynes_idle_'+stage,uid=uid,gid=uid,home='/nonexistent',shell='/usr/sbin/nologin',groups=['xynes_idle_'+stage]))
  files={r['path']:r for r in plan['files']}
  self.assertEqual(len(files),20)
  self.assertEqual(files['/etc/tmpfiles.d/xynes-idle.conf']['sha256'],self.m.digest(self.m.tmpfiles()))
  self.assertEqual(files[self.m.BINARY]['mode'],0o555)
  bad=self.policies();bad['prod']['binary_sha256']='c'*64
  with self.assertRaises(self.m.IdleError):self.m.provision_plan(bad)
 def test_stage_cold_wake_budgets_match_release_maximums(self):
  self.assertEqual(self.m.WAKE_SECONDS,{'qa':90,'staging':90,'prod':30})
  for stage in self.m.UID:self.assertLessEqual(self.m.WAKE_SECONDS[stage],90 if stage!='prod' else 30)
if __name__=='__main__':unittest.main()
