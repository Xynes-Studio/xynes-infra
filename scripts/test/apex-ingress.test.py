#!/usr/bin/env python3
"""Inert Docker/owned fixture roots; no VPS, DNS, app starts or ACME."""
import contextlib,copy,hashlib,importlib.util,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
TOOL=ROOT/'scripts/setup/configure-apex-ingress.py'
class Tests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(TOOL.is_file(),'reviewed ingress configurator missing')
  spec=importlib.util.spec_from_file_location('apex_ingress',TOOL);self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
  self.tmp=tempfile.TemporaryDirectory(prefix='apex-ingress-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  self.original=b'# retained fixture config\n(secure) {\n header X-Example preserve\n}\napi.erp.xynes.com {\n reverse_proxy gateway_prod:4100\n}\nauth.erp.xynes.com {\n    reverse_proxy auth_app_prod:3100 {\n      transport http {}\n    }\n}\ncms.erp.xynes.com {\n    reverse_proxy cms_console_web_prod:3000 {\n      transport http {}\n    }\n}\nqa-auth.erp.xynes.com {\n reverse_proxy auth_app_qa:3100\n}\ndev-auth.erp.xynes.com {\n reverse_proxy auth_app_dev:3100\n}\nstatus.erp.xynes.com {\n reverse_proxy uptime_kuma:3001\n}\n'
  self.hash=hashlib.sha256(self.original).hexdigest();self.patch=patch.object(self.m,'BASELINE_SHA256',self.hash);self.patch.start();self.addCleanup(self.patch.stop)
  self.config=self.m.Configuration(root=self.root,source=ROOT)
  self.file=self.config.path('/srv/xynes/caddy/Caddyfile');self.file.parent.mkdir(parents=True);self.file.write_bytes(self.original);self.file.chmod(0o644)
  self.mount=self.config.path('/srv/xynes/caddy/config');self.mount.mkdir(mode=0o755)
  directory=self.config.path('/var/lib/xynes/deploy/state');directory.mkdir(parents=True,mode=0o700)
  self.container=self.fixture_container();self.networks=self.fixture_networks();self.calls=[];self.fail_reload=False
 def fixture_container(self):
  return {'Id':self.m.CADDY_ID,'Name':'/caddy','Image':self.m.IMAGE_ID,'State':{'Running':True},'NetworkSettings':{'Networks':{name:{'NetworkID':value['id'],'EndpointID':value['endpoint'],'Gateway':value['gateway'],'IPAddress':value['address'],'IPPrefixLen':16} for name,value in self.m.NETWORKS.items() if name!='xynes_staging_net'},'Ports':{'80/tcp':[{'HostPort':'80'}],'443/tcp':[{'HostPort':'443'}]}},'Mounts':[{'Source':'/srv/xynes/caddy/config','Destination':'/config','RW':True},{'Source':'/srv/xynes/caddy/Caddyfile','Destination':'/etc/caddy/Caddyfile','RW':False}]}
 def fixture_networks(self):
  return [{'Name':name,'Id':value['id'],'Driver':'bridge','Internal':False,'IPAM':{'Driver':'default','Config':[{'Subnet':value['subnet'],'Gateway':value['gateway']}]},'Labels':value['labels'].copy()} for name,value in self.m.NETWORKS.items()]
 def docker_run(self,argv,*,input=None):
  self.calls.append((argv,input))
  if 'inspect' in argv:
   return json.dumps(self.networks if 'network' in argv else [self.container]).encode()
  if 'connect' in argv:
   self.container['NetworkSettings']['Networks']['xynes_staging_net']={'NetworkID':self.m.STAGING_NETWORK,'EndpointID':'e'*64,'Gateway':'172.22.0.1','IPAddress':'172.22.0.2','IPPrefixLen':16}
  if 'disconnect' in argv:self.container['NetworkSettings']['Networks'].pop('xynes_staging_net',None)
  if 'adapt' in argv:return b'{"apps":{}}'
  if 'reload' in argv and self.fail_reload:self.fail_reload=False;raise self.m.IngressError(self.m.ERROR)
  return b''
 def prepare(self):return self.m.prepare(configuration=self.config,run=self.docker_run)
 def test_preserves_original_except_two_upstream_literals_and_appends_fixed_routes(self):
  plan=self.prepare();expected=self.original.replace(b'auth_app_prod:3100',b'172.21.0.1:18281').replace(b'cms_console_web_prod:3000',b'172.21.0.1:18282')
  self.assertTrue(plan.proposed.startswith(expected));self.assertEqual(plan.original,self.original)
  for host,port in (('auth.qa.xynes.com',18081),('cms.qa.xynes.com',18082),('auth.staging.xynes.com',18181),('cms.staging.xynes.com',18182)):
   self.assertIn((host+' {\n    reverse_proxy 172.21.0.1:'+str(port)).encode(),plan.proposed)
  for stage,zone in (('qa','qa'),('staging','staging'),('prod','erp')):
   self.assertIn(('supabase.'+zone+'.xynes.com {\n    handle_path /auth/v1/* {\n        reverse_proxy supabase_auth_'+stage+':9999').encode(),plan.proposed)
  self.assertIn(b'gateway_qa:4100',plan.proposed);self.assertIn(b'gateway_staging:4100',plan.proposed)
  self.assertIn(b'root * /config/apex-auth/templates',plan.proposed);self.assertIn(b'http://:8090',plan.proposed)
  self.assertTrue(all('reload' not in call[0] and 'connect' not in call[0] for call in self.calls));self.assertEqual(self.file.read_bytes(),self.original)
 def test_unknown_or_ambiguous_config_and_managed_blocks_refuse(self):
  for raw in (self.original+b'# unexpected\n',self.original+b'auth.qa.xynes.com {}\n',self.original+b'# BEGIN XYNES APEX INGRESS v1\n'):
   self.file.write_bytes(raw)
   with self.assertRaises(self.m.IngressError):self.prepare()
  self.file.write_bytes(self.original)
  plan=self.prepare();self.file.write_bytes(plan.proposed)
  again=self.prepare();self.assertEqual(again.proposed,plan.proposed);self.assertFalse(again.configuration_changed)
  self.file.write_bytes(plan.proposed.replace(b'172.21.0.1:18081',b'example.invalid:80'))
  with self.assertRaises(self.m.IngressError):self.prepare()
 def test_changed_container_image_published_template_port_and_network_ownership_refuse(self):
  for mutate in (lambda:self.container.update(Id='f'*64),lambda:self.container.update(Image='sha256:'+'f'*64),lambda:self.container['NetworkSettings']['Ports'].update({'8090/tcp':[{'HostPort':'8090'}]}),lambda:self.networks[-1]['Labels'].update({'io.xynes.owner':'other'}),lambda:self.container['NetworkSettings']['Networks']['caddy_net'].update(IPAddress='172.21.0.99'),lambda:self.container['Mounts'][0].update(Source='/other'),lambda:self.container['Mounts'][0].update(RW=False),lambda:self.networks[-1]['IPAM']['Config'][0].update(Gateway='172.22.0.99')):
   self.container=self.fixture_container();self.networks=self.fixture_networks();mutate()
   with self.assertRaises(self.m.IngressError):self.prepare()
 def test_apply_validates_before_mutation_preserves_inode_and_all_existing_endpoints(self):
  plan=self.prepare();inode=self.file.stat().st_ino;before=copy.deepcopy(self.container['NetworkSettings']['Networks']);self.calls=[]
  result=self.m.apply(plan,configuration=self.config,run=self.docker_run,reviewed_plan_sha256=plan.plan_sha256,current_sha256=self.hash)
  self.assertEqual(self.file.stat().st_ino,inode);self.assertEqual(self.file.read_bytes(),plan.proposed);self.assertTrue(result['applied'])
  for name,value in before.items():self.assertEqual(self.container['NetworkSettings']['Networks'][name],value)
  backup=Path(result['backup_path']);self.assertEqual(backup.read_bytes(),self.original);self.assertEqual(backup.stat().st_mode&0o777,0o600)
  calls=[x[0] for x in self.calls];adapt=next(i for i,c in enumerate(calls) if 'adapt' in c);connect=next(i for i,c in enumerate(calls) if 'connect' in c);self.assertLess(adapt,connect)
  for argv,raw in self.calls:
   if 'adapt' in argv or 'reload' in argv:self.assertEqual(raw,plan.proposed);self.assertIn('-',argv)
  for name,raw in plan.templates.items():
   p=self.mount/'apex-auth/templates'/name;self.assertEqual(p.read_bytes(),raw);self.assertEqual(p.stat().st_mode&0o777,0o444)
 def test_partial_host_write_restores_original_bytes_before_refusing(self):
  plan=self.prepare();inode=self.file.stat().st_ino;original_write=self.m.os.write;calls=[0]
  def short_then_fail(fd,raw):
   calls[0]+=1
   if calls[0]==1:return original_write(fd,raw[:220])
   if calls[0]==2:raise OSError('inert disk failure')
   return original_write(fd,raw)
  with patch.object(self.m.os,'write',short_then_fail),self.assertRaises(self.m.IngressError):
   self.m.apply(plan,configuration=self.config,run=self.docker_run,reviewed_plan_sha256=plan.plan_sha256,current_sha256=self.hash)
  self.assertEqual(self.file.read_bytes(),self.original);self.assertEqual(self.file.stat().st_ino,inode)
  self.assertNotIn('xynes_staging_net',self.container['NetworkSettings']['Networks'])

 def test_reload_failure_restores_original_inode_bytes_and_prior_network_set(self):
  plan=self.prepare();inode=self.file.stat().st_ino;self.fail_reload=True
  with self.assertRaises(self.m.IngressError):self.m.apply(plan,configuration=self.config,run=self.docker_run,reviewed_plan_sha256=plan.plan_sha256,current_sha256=self.hash)
  self.assertEqual(self.file.read_bytes(),self.original);self.assertEqual(self.file.stat().st_ino,inode);self.assertNotIn('xynes_staging_net',self.container['NetworkSettings']['Networks'])
  reloads=[raw for argv,raw in self.calls if 'reload' in argv];self.assertEqual(reloads[-1],self.original)
 def test_cli_is_dry_run_by_default_and_apply_requires_both_review_hashes(self):
  plan=self.prepare();out=io.StringIO()
  with patch.object(self.m,'prepare',return_value=plan),patch.object(self.m.os,'geteuid',return_value=0),patch.object(self.m,'apply',side_effect=AssertionError('must not apply')),contextlib.redirect_stdout(out):
   self.assertEqual(self.m.main([]),0)
   self.assertFalse(json.loads(out.getvalue())['applied']);self.assertNotIn('retained fixture config',out.getvalue())
   with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(self.m.main(['--apply']),1)
 def test_adapt_failure_and_symlink_substitution_refuse_before_mutation(self):
  def reject(argv,*,input=None):
   if 'adapt' in argv:raise self.m.IngressError(self.m.ERROR)
   return self.docker_run(argv,input=input)
  with self.assertRaises(self.m.IngressError):self.m.prepare(configuration=self.config,run=reject)
  self.assertEqual(self.file.read_bytes(),self.original);self.assertNotIn('xynes_staging_net',self.container['NetworkSettings']['Networks'])
  alias=self.file.parent/'alias';alias.write_bytes(self.original);self.file.unlink();self.file.symlink_to(alias)
  with self.assertRaises(self.m.IngressError):self.prepare()
  self.assertEqual(alias.read_bytes(),self.original)
 def test_stale_hash_bad_plan_or_unknown_templates_refuse_without_mutation(self):
  plan=self.prepare()
  for sha,current in (('f'*64,self.hash),(plan.plan_sha256,'f'*64)):
   with self.assertRaises(self.m.IngressError):self.m.apply(plan,configuration=self.config,run=self.docker_run,reviewed_plan_sha256=sha,current_sha256=current)
  dest=self.mount/'apex-auth/templates';dest.mkdir(parents=True);(dest/'unknown.html').write_bytes(b'preserve')
  with self.assertRaises(self.m.IngressError):self.m.apply(plan,configuration=self.config,run=self.docker_run,reviewed_plan_sha256=plan.plan_sha256,current_sha256=self.hash)
  self.assertEqual((dest/'unknown.html').read_bytes(),b'preserve');self.assertEqual(self.file.read_bytes(),self.original)
if __name__=='__main__':unittest.main()
