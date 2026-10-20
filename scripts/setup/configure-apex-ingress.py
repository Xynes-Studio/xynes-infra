#!/usr/bin/env python3
"""Reviewed, dry-run-first Caddy ingress migration for the existing Apex VPS.

No DNS, application starts, published ports or existing endpoint-IP changes.
The original single-file bind mount is updated in place and backed up privately.
"""
import argparse
from dataclasses import dataclass,field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import tempfile

ERROR='Apex ingress refused'
BASELINE_SHA256='cb74ded562569e8c2a953fa3d0348f841108b990918e2c38d93bc3a63c7581c7'
CADDY_ID='775dcb0593b7a05551ca46fd25ac18721714a4cb9ca79aa042b0729d18d53afa'
IMAGE_ID='sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
STAGING_NETWORK='8436b3c90c4e1646bd43edc86ff6b8f31f610abf8415f97db7f7781586c341e6'
NETWORKS={
 'caddy_net':dict(id='99199a632cdf75b63469636056dbf264483884d4b343b6d06ac27c3acc17dfba',gateway='172.21.0.1',subnet='172.21.0.0/16',address='172.21.0.2'),
 'xynes_dev_net':dict(id='915de8978e2e766c26241ccba7af16fc5a47f22c51aa9fb18d1da9386bd145ce',gateway='172.18.0.1',subnet='172.18.0.0/16',address='172.18.0.2'),
 'xynes_prod_net':dict(id='ae0b0d341f5dceb3b745f6215ea0f11e8c1b8fdf9757958fb95b8b5250c00f90',gateway='172.20.0.1',subnet='172.20.0.0/16',address='172.20.0.2'),
 'xynes_qa_net':dict(id='63b93dc90bac0c75958847ecddfc7cfb59d63612a98e245e6ac2dfba754796e6',gateway='172.19.0.1',subnet='172.19.0.0/16',address='172.19.0.2'),
 'xynes_staging_net':dict(id=STAGING_NETWORK,gateway='172.22.0.1',subnet='172.22.0.0/16')}
for name,value in NETWORKS.items():
 value['labels']=({'io.xynes.apex.substrate':'1','io.xynes.environment':'staging','io.xynes.managed':'true','io.xynes.owner':'xynes-infra'} if name=='xynes_staging_net' else {'com.xynes.managed':'true','com.xynes.network':name})
KINDS=('confirmation','magic_link','recovery','invite','email_change','reauthentication')
BEGIN=b'# BEGIN XYNES APEX INGRESS v1\n'
END=b'# END XYNES APEX INGRESS v1\n'
BASE=['/usr/bin/docker','--config','/etc/xynes/deploy/docker','--host','unix:///var/run/docker.sock']
SEAL=object()

class IngressError(ValueError):pass

def require(value):
 if not value:raise IngressError(ERROR)

def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def identity(info):return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

@dataclass(frozen=True,repr=False)
class Configuration:
 root:Path=Path('/')
 source:Path=Path(__file__).resolve().parents[2]
 @property
 def uid(self):return 0 if self.root==Path('/') else os.geteuid()
 def path(self,value):return self.root/value.lstrip('/')

@dataclass(frozen=True,repr=False)
class Plan:
 original:bytes
 proposed:bytes
 templates:dict
 container:dict
 networks:list
 file_identity:tuple
 plan_sha256:str
 configuration_changed:bool
 seal:object=field(repr=False)

def secure(path,config,*,directory=False,source=False):
 limit=config.source if source else config.root
 require(path.is_absolute() and path.resolve(strict=True)==path and (path==limit or limit in path.parents))
 here=path
 while True:
  info=here.lstat()
  require(info.st_uid==config.uid and not info.st_mode&0o022 and not stat.S_ISLNK(info.st_mode)
          and (stat.S_ISDIR(info.st_mode) if here!=path or directory else stat.S_ISREG(info.st_mode)))
  if here==limit:break
  here=here.parent

def read(path,config,*,source=False):
 secure(path,config,source=source)
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as file:
  before=os.fstat(file.fileno());require(before.st_nlink==1 and 0<before.st_size<=1024*1024)
  raw=file.read(1024*1024+1)
  require(len(raw)==before.st_size and identity(before)==identity(os.fstat(file.fileno()))==identity(path.lstat()))
 return raw,identity(before)

def command(argv,*,input=None):
 def limit():resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024*1024,2*1024*1024))
 with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
  result=subprocess.run(BASE+argv,input=input,stdout=output,stderr=errors,timeout=90,
   env={'PATH':'/usr/bin:/bin','HOME':'/','LANG':'C','LC_ALL':'C'},preexec_fn=limit)
  require(result.returncode==0 and output.tell()<=2*1024*1024 and errors.tell()<=65536)
  output.seek(0);return output.read()

def invoke(run,argv,*,input=None):
 raw=run(argv,input=input);require(type(raw) is bytes and len(raw)<=2*1024*1024);return raw

def snapshot(run):
 containers=json.loads(invoke(run,['inspect',CADDY_ID]));require(type(containers) is list and len(containers)==1)
 value=containers[0];require(value['Id']==CADDY_ID and value['Image']==IMAGE_ID and value['Name']=='/caddy' and value['State']['Running'] is True)
 networks=value['NetworkSettings']['Networks'];require(set(networks) in (set(NETWORKS)-{'xynes_staging_net'},set(NETWORKS)))
 for name,endpoint in networks.items():
  expected=NETWORKS[name];require(endpoint['NetworkID']==expected['id'] and endpoint['Gateway']==expected['gateway'] and endpoint['IPPrefixLen']==16)
  # Docker regenerates endpoint IDs on restart; bind the observed ID in the plan.
  require(type(endpoint['EndpointID']) is str and re.fullmatch(r'[a-f0-9]{64}',endpoint['EndpointID']))
  if name!='xynes_staging_net':require(endpoint['IPAddress']==expected['address'])
  else:require(re.fullmatch(r'172\.22\.(?:[0-9]{1,3})\.(?:[0-9]{1,3})',endpoint['IPAddress']))
 ports=value['NetworkSettings']['Ports'];require(type(ports) is dict and not ports.get('8090/tcp'))
 mounts=value['Mounts'];require(type(mounts) is list)
 for source,destination,rw in (('/srv/xynes/caddy/config','/config',True),('/srv/xynes/caddy/Caddyfile','/etc/caddy/Caddyfile',False)):
  matches=[row for row in mounts if row['Destination']==destination]
  require(len(matches)==1 and matches[0]['Source']==source and matches[0]['RW'] is rw)
 # Docker may return identical mounts in a different order; retain every field.
 mounts=sorted(mounts,key=canonical)
 # Include only identity/routing metadata, never container environment values.
 container={'id':value['Id'],'image':value['Image'],'networks':networks,'mounts':mounts,'ports':ports}
 rows=json.loads(invoke(run,['network','inspect',*[v['id'] for v in NETWORKS.values()]]));require(type(rows) is list and len(rows)==len(NETWORKS))
 result=[]
 for name,expected in NETWORKS.items():
  matching=[row for row in rows if row['Name']==name];require(len(matching)==1);row=matching[0]
  require(row['Id']==expected['id'] and row['Driver']=='bridge' and row['Internal'] is False
          and all(row.get('Labels',{}).get(k)==v for k,v in expected['labels'].items())
          and row['IPAM']['Driver']=='default' and row['IPAM']['Config']==[{'Subnet':expected['subnet'],'Gateway':expected['gateway']}])
  result.append({'name':name,'id':row['Id'],'labels':row['Labels'],'ipam':row['IPAM']})
 return container,result

def managed(template):
 blocks=[]
 for stage,base in (('qa',18080),('staging',18180)):
  for app,offset in (('auth',1),('cms',2)):
   blocks.append(f'{app}.{stage}.xynes.com {{\n    reverse_proxy 172.21.0.1:{base+offset}\n}}\n')
  blocks.append(f'api.{stage}.xynes.com {{\n    reverse_proxy gateway_{stage}:4100\n}}\n')
 for stage,zone in (('qa','qa'),('staging','staging'),('prod','erp')):
  blocks.append(f'supabase.{zone}.xynes.com {{\n    handle_path /auth/v1/* {{\n        reverse_proxy supabase_auth_{stage}:9999\n    }}\n    handle {{\n        respond "Not found" 404\n    }}\n}}\n')
 return BEGIN+('\n'.join(blocks)+'\n').encode()+template+END

def render(raw,template):
 require(raw.endswith(b'\n') and b'\0' not in raw)
 suffix=b'\n'+managed(template)
 substitutions=((b'auth_app_prod:3100',b'172.21.0.1:18281'),(b'cms_console_web_prod:3000',b'172.21.0.1:18282'))
 if BEGIN in raw or END in raw:
  require(raw.count(BEGIN)==raw.count(END)==1 and raw.endswith(suffix))
  original=raw[:-len(suffix)]
  for old,new in substitutions:require(original.count(new)==1);original=original.replace(new,old)
  require(digest(original)==BASELINE_SHA256);return raw
 require(digest(raw)==BASELINE_SHA256)
 for old,new in substitutions:require(raw.count(old)==1 and new not in raw);raw=raw.replace(old,new)
 for token in (b'auth.qa.xynes.com',b'cms.qa.xynes.com',b'api.qa.xynes.com',b'auth.staging.xynes.com',b'cms.staging.xynes.com',b'api.staging.xynes.com',b'supabase.qa.xynes.com',b'supabase.staging.xynes.com',b'supabase.erp.xynes.com',b':8090'):
  require(token not in raw)
 return raw+suffix

def plan_hash(original,proposed,templates,container,networks,file_identity):
 return digest(canonical({'original_sha256':digest(original),'proposed_sha256':digest(proposed),
  'templates':{k:digest(v) for k,v in templates.items()},'container':container,'networks':networks,'file_identity':file_identity}))

def prepare(*,configuration=Configuration(),run=command):
 try:
  config=configuration;require(type(config) is Configuration and os.geteuid()==config.uid)
  raw,stamp=read(config.path('/srv/xynes/caddy/Caddyfile'),config)
  secure(config.path('/srv/xynes/caddy/config'),config,directory=True)
  template,_=read(config.source/'infra/release/auth/templates.Caddyfile',config,source=True)
  require(b'root * /config/apex-auth/templates' in template and template.endswith(b'\n'))
  templates={kind+'.html':read(config.source/'infra/release/auth/templates'/str(kind+'.html'),config,source=True)[0] for kind in KINDS}
  proposed=render(raw,template);container,networks=snapshot(run)
  invoke(run,['exec','-i',CADDY_ID,'caddy','adapt','--config','-','--adapter','caddyfile','--validate'],input=proposed)
  return Plan(raw,proposed,templates,container,networks,stamp,plan_hash(raw,proposed,templates,container,networks,stamp),raw!=proposed,SEAL)
 except Exception:raise IngressError(ERROR) from None

def inplace(path,expected,raw,config,stamp=None):
 secure(path,config)
 fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
 try:
  info=os.fstat(fd);require(info.st_nlink==1 and (stamp is None or identity(info)==stamp) and identity(info)==identity(path.lstat()))
  old=os.read(fd,1024*1024+1);require(old==expected)
  def write_all(value):
   os.lseek(fd,0,os.SEEK_SET);offset=0
   while offset<len(value):
    count=os.write(fd,value[offset:]);require(count>0);offset+=count
   os.ftruncate(fd,len(value));os.fsync(fd)
  try:
   write_all(raw)
   require(os.pread(fd,len(raw)+1,0)==raw and (info.st_dev,info.st_ino)==(path.stat().st_dev,path.stat().st_ino))
  except Exception:
   # A single-file bind mount forbids rename. Restore through the same owned FD
   # if a short write/disk error interrupts the in-place transaction.
   require((info.st_dev,info.st_ino)==(path.lstat().st_dev,path.lstat().st_ino))
   os.ftruncate(fd,len(expected));write_all(expected)
   raise

 finally:os.close(fd)

def template_preflight(plan,config):
 parent=config.path('/srv/xynes/caddy/config/apex-auth');dest=parent/'templates'
 for directory in (parent,dest):
  if directory.exists() or directory.is_symlink():secure(directory,config,directory=True)
 if dest.exists():
  require(set(p.name for p in dest.iterdir())<=set(plan.templates))
  for path in dest.iterdir():require(read(path,config)[0]==plan.templates[path.name] and stat.S_IMODE(path.stat().st_mode)==0o444)
 return parent,dest

def apply(plan,*,configuration=Configuration(),run=command,reviewed_plan_sha256,current_sha256):
 changed=False;connected=False;backup=None
 try:
  config=configuration;require(type(plan) is Plan and plan.seal is SEAL and reviewed_plan_sha256==plan.plan_sha256
   and current_sha256==digest(plan.original) and plan.plan_sha256==plan_hash(plan.original,plan.proposed,plan.templates,plan.container,plan.networks,plan.file_identity))
  fresh=prepare(configuration=config,run=run);require(fresh.plan_sha256==plan.plan_sha256)
  parent,dest=template_preflight(plan,config)
  # The host-wide deployment lock fences application/adapter convergence too.
  lockdir=config.path('/var/lib/xynes/deploy/state');secure(lockdir,config,directory=True)
  lock=os.open(lockdir/'host.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
  try:
   info=os.fstat(lock);require(stat.S_ISREG(info.st_mode) and info.st_uid==config.uid and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600 and info.st_size==0)
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   require(prepare(configuration=config,run=run).plan_sha256==plan.plan_sha256)
   for directory in (parent,dest):
    if not directory.exists():directory.mkdir(mode=0o755);directory.chmod(0o755)
    secure(directory,config,directory=True)
   for name,raw in plan.templates.items():
    path=dest/name
    if not path.exists():
     fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)
     with os.fdopen(fd,'wb') as file:file.write(raw);file.flush();os.fchmod(file.fileno(),0o444);os.fsync(file.fileno())
   if 'xynes_staging_net' not in plan.container['networks']:
    invoke(run,['network','connect',STAGING_NETWORK,CADDY_ID]);connected=True
   after,_=snapshot(run)
   for name,endpoint in plan.container['networks'].items():require(after['networks'][name]==endpoint)
   require(after['ports']==plan.container['ports'] and after['mounts']==plan.container['mounts'])
   path=config.path('/srv/xynes/caddy/Caddyfile')
   backup=path.parent/('Caddyfile.apex-backup-'+digest(plan.original))
   if backup.exists():require(read(backup,config)[0]==plan.original and stat.S_IMODE(backup.stat().st_mode)==0o600)
   else:
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as file:file.write(plan.original);file.flush();os.fsync(file.fileno())
   if plan.configuration_changed:
    inplace(path,plan.original,plan.proposed,config,plan.file_identity);changed=True
   invoke(run,['exec','-i',CADDY_ID,'caddy','reload','--config','-','--adapter','caddyfile'],input=plan.proposed)
   require(read(path,config)[0]==plan.proposed)
   final,_=snapshot(run)
   for name,endpoint in after['networks'].items():require(final['networks'][name]==endpoint)
   require(final['mounts']==after['mounts'] and final['ports']==after['ports'])
   return {'applied':True,'plan_sha256':plan.plan_sha256,'current_sha256':digest(plan.proposed),'backup_path':str(backup),'dns_changed':False,'applications_started':False}
  except Exception:
   if changed:
    try:
     inplace(config.path('/srv/xynes/caddy/Caddyfile'),plan.proposed,plan.original,config)
     invoke(run,['exec','-i',CADDY_ID,'caddy','reload','--config','-','--adapter','caddyfile'],input=plan.original)
    except Exception:raise IngressError('Apex ingress refused; recovery requires the protected backup') from None
   if connected:
    try:invoke(run,['network','disconnect',STAGING_NETWORK,CADDY_ID])
    except Exception:raise IngressError('Apex ingress refused; staging network recovery requires inspection') from None
   raise
  finally:os.close(lock)
 except IngressError:raise
 except Exception:raise IngressError(ERROR) from None

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--apply',action='store_true');parser.add_argument('--plan-sha256');parser.add_argument('--current-sha256')
 parser.add_argument('--source',type=Path,default=Configuration().source)
 args=parser.parse_args(argv)
 try:
  require(os.geteuid()==0 and args.source.is_absolute() and args.source.resolve()==args.source)
  config=Configuration(source=args.source);plan=prepare(configuration=config)
  if args.apply:
   require(args.plan_sha256 and args.current_sha256)
   result=apply(plan,configuration=config,reviewed_plan_sha256=args.plan_sha256,current_sha256=args.current_sha256)
  else:
   require(args.plan_sha256 is None and args.current_sha256 is None)
   result={'applied':False,'plan_sha256':plan.plan_sha256,'current_sha256':digest(plan.original),'proposed_sha256':digest(plan.proposed),'configuration_changed':plan.configuration_changed,'template_sha256':{k:digest(v) for k,v in plan.templates.items()},'attach_staging_network':'xynes_staging_net' not in plan.container['networks']}
  print(json.dumps(result,sort_keys=True));return 0
 except Exception:
  print(ERROR,file=__import__('sys').stderr);return 1
if __name__=='__main__':raise SystemExit(main())
