#!/usr/bin/env python3
"""Root-only native idle generation reconciliation under the deployment lock.

Fixed stage units and private bridge listeners only. A failed replacement leaves
durable warmth and drained admission in place for explicit reconciliation.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
try:
    from . import host_compose, release_manifest as records
except ImportError:
    import host_compose, release_manifest as records

ERROR='Hosted idle generation refused'
UID={'qa':12002,'staging':12003,'prod':12004}
PORT={'qa':18081,'staging':18181,'prod':18281}
WAKE_SECONDS={'qa':90,'staging':90,'prod':30}
BINARY='/usr/local/lib/xynes-idle/xynes-idle'
GROUPS={'auth':('auth-app','IMAGE_AUTH_APP',3100),'cms':('cms-console-web','IMAGE_CMS_APP',3000)}
INSPECT='{"id":{{json .Id}},"image_id":{{json .Image}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},"networks":{{json .NetworkSettings.Networks}}}'
NETWORK='{"id":{{json .Id}},"name":{{json .Name}},"driver":{{json .Driver}},"ipam":{{json .IPAM.Config}}}'
class IdleError(ValueError):pass
def require(value):
    if not value:raise IdleError(ERROR)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
def private_ip(value):
    require(type(value) is str)
    address=ipaddress.ip_address(value)
    require(type(address) is ipaddress.IPv4Address and any(address in ipaddress.ip_network(block)
        for block in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')))
    return address
def validate_policy(value,stage):
    try:
        require(type(value) is dict and set(value)=={'schema_version','stage','binary_sha256','source_sha256',
            'compiler_version','idle_seconds','network','ingress','caddy'} and type(value['schema_version']) is int
            and value['schema_version']==1 and stage in UID and value['stage']==stage)
        require(all(type(value[k]) is str and re.fullmatch('[a-f0-9]{64}',value[k]) for k in ('binary_sha256','source_sha256'))
                and value['compiler_version']=='go1.27.2' and type(value['idle_seconds']) is int and 60<=value['idle_seconds']<=86400)
        for key,name in (('network','xynes_'+stage+'_net'),('ingress','caddy_net')):
            row=value[key];require(type(row) is dict and set(row)=={'name','id','gateway','subnet'} and row['name']==name
                and type(row['id']) is str and re.fullmatch('[a-f0-9]{64}',row['id']))
            network=ipaddress.ip_network(row['subnet'],strict=True)
            require(type(network) is ipaddress.IPv4Network and network.is_private and private_ip(row['gateway']) in network)
        caddy=value['caddy'];require(type(caddy) is dict and set(caddy)=={'id','image_id','ip'}
            and type(caddy['id']) is str and re.fullmatch('[a-f0-9]{64}',caddy['id'])
            and type(caddy['image_id']) is str and re.fullmatch('sha256:[a-f0-9]{64}',caddy['image_id'])
            and private_ip(caddy['ip']) in ipaddress.ip_network(value['ingress']['subnet'])
            and caddy['ip']!=value['ingress']['gateway'])
        return value
    except Exception:raise IdleError(ERROR) from None

def unit(stage,group):
    require(stage in UID and group in ('auth','cms','broker'))
    name='xynes-idle-'+stage+'-'+group
    config='broker.json' if group=='broker' else group+'-proxy.json'
    common=f'''[Unit]
Description=Xynes {stage} idle {group}
After=docker.service network-online.target systemd-tmpfiles-setup.service
Requires=docker.service
ConditionPathExists=/etc/xynes/idle/{stage}/generation.json

[Service]
Type=simple
ExecStart={BINARY} -role {'broker' if group=='broker' else 'proxy'} -config /etc/xynes/idle/{stage}/{config}
Restart=on-failure
RestartSec=3s
TimeoutStopSec=100s
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET
LockPersonality=true
LimitNOFILE=1024
'''
    if group=='broker':common+='User=0\nGroup=0\nReadWritePaths=/run/xynes/idle/'+stage+'\n'
    else:
        common+=f'User={UID[stage]}\nGroup={UID[stage]}\nInaccessiblePaths=/var/run/docker.sock\nReadWritePaths=/run/xynes/idle/{stage}/{group} /var/lib/xynes/idle/{stage}/{group}\n'
    return (common+'\n[Install]\nWantedBy=multi-user.target\n').encode()

def tmpfiles():
    # Only ephemeral sockets/directories are recreated at boot. Persistent warm
    # state is never removed, rewritten or aged out by tmpfiles.
    lines=['d /run/xynes 0755 0 0 -','d /run/xynes/idle 0755 0 0 -']
    for stage,uid in UID.items():
        lines.append(f'd /run/xynes/idle/{stage} 0750 0 {uid} -')
        for group in GROUPS:lines.append(f'd /run/xynes/idle/{stage}/{group} 0700 {uid} {uid} -')
    return ('\n'.join(lines)+'\n').encode()

def provision_assets(policies):
    """Pure public bytes; binaries and fresh private tokens are supplied separately."""
    require(type(policies) is dict and set(policies)==set(UID))
    checked={stage:validate_policy(policies[stage],stage) for stage in UID}
    for field in ('binary_sha256','source_sha256','compiler_version','ingress','caddy'):
        require(all(checked[stage][field]==checked['qa'][field] for stage in UID))
    assets={'/etc/tmpfiles.d/xynes-idle.conf':(tmpfiles(),0o644,0,0)}
    for stage,uid in UID.items():
        for group in ('broker','auth','cms'):
            assets['/etc/systemd/system/xynes-idle-'+stage+'-'+group+'.service']=(unit(stage,group),0o644,0,0)
        assets['/etc/xynes/idle/'+stage+'/host.json']=(canonical(checked[stage]),0o600,0,0)
        assets['/etc/xynes/idle/'+stage+'/deployment-mode.json']=(canonical({'schema_version':1,'stage':stage,'mode':'enabled','groups':['auth','cms']}),0o600,0,0)
    return assets

def provision_plan(policies):
    assets=provision_assets(policies);directories=[];accounts={}
    for path,mode in (('/usr/local/lib/xynes-idle',0o755),('/etc/xynes/idle',0o711),('/var/lib/xynes',0o711),
                      ('/var/lib/xynes/idle',0o711),('/run/xynes',0o755),('/run/xynes/idle',0o755)):
        directories.append({'path':path,'uid':0,'gid':0,'mode':mode})
    for stage,uid in UID.items():
        name='xynes_idle_'+stage
        accounts[stage]={'name':name,'uid':uid,'gid':uid,'home':'/nonexistent','shell':'/usr/sbin/nologin','groups':[name]}
        for prefix in ('/etc/xynes/idle/','/var/lib/xynes/idle/','/run/xynes/idle/'):
            directories.append({'path':prefix+stage,'uid':0,'gid':uid,'mode':0o750})
        for prefix in ('/var/lib/xynes/idle/','/run/xynes/idle/'):
            for group in GROUPS:directories.append({'path':prefix+stage+'/'+group,'uid':uid,'gid':uid,'mode':0o700})
    files=[{'path':path,'uid':uid,'gid':gid,'mode':mode,'sha256':digest(data),'action':'install-reviewed-bytes'}
           for path,(data,mode,uid,gid) in assets.items()]
    files.append({'path':BINARY,'uid':0,'gid':0,'mode':0o555,'sha256':policies['qa']['binary_sha256'],'action':'install-source-qualified-binary'})
    for stage,uid in UID.items():
        files.append({'path':'/etc/xynes/idle/'+stage+'/control.token','uid':0,'gid':uid,'mode':0o440,'sha256':None,'action':'generate-private-token-on-host'})
    return {'artifact_kind':'apex-native-idle-provision-dry-plan','accounts':accounts,'directories':directories,'files':files,
        'parent_mode_changes':[{'path':path,'uid':0,'gid':0,'old_mode':0o700,'mode':0o711} for path in ('/var/lib/xynes','/etc/xynes/idle')],
        'stage_directory_changes':[{'path':'/etc/xynes/idle/'+stage,'uid':0,'old_gid':0,'gid':uid,'old_mode':0o700,'mode':0o750} for stage,uid in UID.items()],
        'existing_policy_transition':'exact-disabled-policy-to-enabled-with-original-private-recovery-copy',
        'preflight':'refuse-foreign-account-path-link-mode-or-bytes; preserve-root-inodes-and-child-content',
        'units_enabled_without_start':True,'installed':False,'source_qualified':False,'application_services_activated':False}

class Idle:
    def __init__(self,stage,*,root=Path('/'),owner_uid=0,run=host_compose.run_command,clock=time.time_ns,proxy_identity=None):
        require(stage in UID and isinstance(root,Path) and root.is_absolute() and root.resolve(strict=True)==root
                and type(owner_uid) is int and owner_uid>=0 and callable(run) and callable(clock))
        require(proxy_identity is None or (root!=Path('/') and type(proxy_identity) is tuple and len(proxy_identity)==2
                and all(type(v) is int and v>=0 for v in proxy_identity)))
        self.stage,self.root,self.uid,self.run,self.clock=stage,root,owner_uid,run,clock
        self.proxy_uid,self.proxy_gid=proxy_identity or (UID[stage],UID[stage])
        def native_lease(group,path,owner):
            try:from .host_controls import Controls
            except ImportError:from host_controls import Controls
            Controls(self.stage,root=self.root,owner_uid=self.uid,proxy_identity=proxy_identity).lease(group,path,owner)
        self.lease=native_lease
    def path(self,name):return self.root/name.lstrip('/')
    def checked(self,path,*,directory=False,uid=None,mode=None):
        require(os.geteuid()==self.uid and self.root in (path,*path.parents) and path.resolve(strict=True)==path)
        current=path
        while True:
            info=current.lstat();require(info.st_uid==(uid if current==path and uid is not None else self.uid)
                and not info.st_mode&0o022 and (stat.S_ISDIR(info.st_mode) if current!=path or directory else stat.S_ISREG(info.st_mode)))
            if current==path and mode is not None:require(stat.S_IMODE(info.st_mode)==mode)
            if current==path and not directory:require(info.st_nlink==1)
            if current==self.root:return
            current=current.parent
    def read(self,path,*,uid=None,mode=0o600,limit=65536):
        self.checked(path,uid=uid,mode=mode)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as f:
            before=f.fileno();info=os.fstat(before)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=limit)
            raw=f.read(limit+1);stamp=host_compose.identity
            require(len(raw)==info.st_size and stamp(os.fstat(before))==stamp(info) and stamp(path.lstat())==stamp(info))
        return raw
    def write(self,path,raw,*,uid=None,gid=None,mode=0o600,new=False):
        self.checked(path.parent,directory=True,uid=uid)
        before=None
        try:
            self.read(path,uid=uid,mode=mode);before=host_compose.identity(path.lstat());require(not new)
        except FileNotFoundError:pass
        fd,name=tempfile.mkstemp(prefix='.idle-write-',dir=path.parent);temp=Path(name)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(raw);f.flush();os.fchmod(f.fileno(),mode)
                target_uid=self.uid if uid is None else uid;target_gid=os.getgid() if gid is None else gid
                if os.geteuid()==0:os.fchown(f.fileno(),target_uid,target_gid)
                else:require(target_uid==os.geteuid() and target_gid==os.getgid())
                os.fsync(f.fileno())
            if before is None:
                try:path.lstat()
                except FileNotFoundError:pass
                else:require(False)
            else:require(host_compose.identity(path.lstat())==before)
            os.replace(temp,path)
            fd=os.open(path.parent,os.O_DIRECTORY|os.O_RDONLY|os.O_CLOEXEC)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temp.exists():temp.unlink()
    def policy(self):
        raw=self.read(self.path('/etc/xynes/idle/'+self.stage+'/host.json'))
        return validate_policy(records.strict_json(raw),self.stage),digest(raw)
    def enabled(self):
        value=records.strict_json(self.read(self.path('/etc/xynes/idle/'+self.stage+'/deployment-mode.json')))
        require(type(value) is dict and set(value)=={'schema_version','stage','mode','groups'} and type(value['schema_version']) is int and value['schema_version']==1
            and value['stage']==self.stage and ((value['mode']=='disabled' and value['groups']==[])
            or(value['mode']=='enabled' and value['groups']==['auth','cms'])))
        return value['mode']=='enabled'
    def call(self,args,*,timeout=30):
        raw=self.run(args,env=host_compose.CLEAN_ENV,timeout=timeout,limit=65536)
        require(type(raw) is bytes and len(raw)<=65536);return raw
    def native(self,policy):
        require(digest(self.read(self.path(BINARY),mode=0o555,limit=32*1024*1024))==policy['binary_sha256'])
        for group in ('broker','auth','cms'):
            require(self.read(self.path('/etc/systemd/system/xynes-idle-'+self.stage+'-'+group+'.service'),mode=0o644)==unit(self.stage,group))
        for key in ('network','ingress'):
            expected=policy[key];row=records.strict_json(self.call(host_compose.BASE+['network','inspect','--format',NETWORK,expected['id']]))
            require(type(row) is dict and set(row)=={'id','name','driver','ipam'} and row['id']==expected['id']
                and row['name']==expected['name'] and row['driver']=='bridge' and type(row['ipam']) is list
                and len(row['ipam'])==1 and row['ipam'][0].get('Subnet')==expected['subnet'] and row['ipam'][0].get('Gateway')==expected['gateway'])
        row=records.strict_json(self.call(host_compose.BASE+['inspect','--format',INSPECT,policy['caddy']['id']]))
        require(row['id']==policy['caddy']['id'] and row['image_id']==policy['caddy']['image_id'] and row['state']=='running')
        net=row['networks'][policy['ingress']['name']]
        require(net['NetworkID']==policy['ingress']['id'] and net['IPAddress']==policy['caddy']['ip'] and net['Gateway']==policy['ingress']['gateway'])
    def config_path(self,name):return self.path('/etc/xynes/idle/'+self.stage+'/'+name)
    def deployment(self):
        value=records.strict_json(self.read(self.config_path('deployment.json')))
        require(type(value) is dict and set(value)=={'owner','policy_sha256'} and re.fullmatch(r'deploy-[1-9][0-9]{0,15}',value['owner'])
                and type(value['policy_sha256']) is str and re.fullmatch('[a-f0-9]{64}',value['policy_sha256']))
        return value
    def generation(self):
        value=records.strict_json(self.read(self.config_path('generation.json')))
        require(type(value) is dict and set(value)=={'schema_version','bindings','configuration_sha256','policy_sha256'}
            and type(value['schema_version']) is int and value['schema_version']==1
            and value['policy_sha256']==self.policy()[1] and type(value['bindings']) is dict and set(value['bindings'])==set(GROUPS)
            and type(value['configuration_sha256']) is dict and set(value['configuration_sha256'])=={'broker.json','auth-proxy.json','cms-proxy.json'})
        for group,binding in value['bindings'].items():
            require(type(binding) is dict and set(binding)=={'id','image','ip'}
                and type(binding['id']) is str and re.fullmatch('[a-f0-9]{64}',binding['id'])
                and type(binding['image']) is str and re.fullmatch(r'ghcr.io/xynes-studio/apex/[a-z0-9-]+@sha256:[a-f0-9]{64}',binding['image']))
            private_ip(binding['ip'])
        for name,sha in value['configuration_sha256'].items():
            require(type(sha) is str and re.fullmatch('[a-f0-9]{64}',sha)
                and digest(self.read(self.config_path(name),mode=0o640))==sha)
        return value
    def begin(self,owner,*,first,lease):
        require(re.fullmatch(r'deploy-[1-9][0-9]{0,15}',owner) and type(first) is bool and callable(lease))
        policy,sha=self.policy();self.native(policy);self.lease=lease
        self.write(self.config_path('deployment.json'),canonical({'owner':owner,'policy_sha256':sha}),new=True)
        generation=self.config_path('generation.json')
        try:self.generation()
        except FileNotFoundError:
            require(first)
            for name in ('broker.json','auth-proxy.json','cms-proxy.json'):
                try:self.config_path(name).lstat()
                except FileNotFoundError:pass
                else:require(False)
            for group in ('broker','auth','cms'):
                raw=self.call(['/usr/bin/systemctl','show','--property=ActiveState','--value','xynes-idle-'+self.stage+'-'+group+'.service'])
                require(raw.strip() in (b'inactive',b'failed'))
            for group in GROUPS:
                parent=self.path('/var/lib/xynes/idle/'+self.stage+'/'+group)
                self.checked(parent,directory=True,uid=self.proxy_uid,mode=0o700)
                try:(parent/'warm.lock').lstat()
                except FileNotFoundError:pass
                else:require(False)
                self.write(parent/'warm.json',canonical({'version':1,'environment':self.stage,'group':self.stage+'-'+group,
                    'leases':[{'owner':owner,'until':self.clock()+3600*10**9}]}),uid=self.proxy_uid,gid=self.proxy_gid,new=True)
            return
        for group in GROUPS:lease(group,'/warm',owner)
        self.before_replace()
    def before_replace(self):
        if not self.enabled():return
        state=self.deployment();require(self.policy()[1]==state['policy_sha256'])
        try:generation=self.generation()
        except FileNotFoundError:return
        require(callable(self.lease))
        self.restart_policy(generation,drained=True)
        for group in GROUPS:self.lease(group,'/drain',state['owner'])
    def stop_proxies(self):
        # Explicit stop does not trigger Restart=on-failure. Preserve all warmth
        # and the deployment owner when durable/IPC recovery is uncertain.
        failed=False
        for group in GROUPS:
            try:
                require(self.read(self.path('/etc/systemd/system/xynes-idle-'+self.stage+'-'+group+'.service'),mode=0o644)==unit(self.stage,group))
                self.call(['/usr/bin/systemctl','stop','xynes-idle-'+self.stage+'-'+group+'.service'],timeout=120)
            except Exception:failed=True
        require(not failed)
    def restart_policy(self,generation,*,drained):
        require(type(drained) is bool and generation==self.generation())
        original={group:records.strict_json(self.read(self.config_path(group+'-proxy.json'),mode=0o640)) for group in GROUPS}
        require(all(type(value.get('start_drained')) is bool for value in original.values()))
        closed={group:{**value,'start_drained':True} for group,value in original.items()}
        desired={group:{**value,'start_drained':drained} for group,value in original.items()}
        def document(values):
            updated={**generation,'configuration_sha256':dict(generation['configuration_sha256'])}
            for group,value in values.items():updated['configuration_sha256'][group+'-proxy.json']=digest(canonical(value))
            return updated
        permitted_generations=(canonical(generation),canonical(document(desired)),canonical(document(closed)))
        def persist(values):
            updated=document(values)
            for group,value in values.items():
                name=group+'-proxy.json';path=self.config_path(name);current=self.read(path,mode=0o640)
                require(current in (canonical(original[group]),canonical(desired[group]),canonical(closed[group])))
                raw=canonical(value)
                if current!=raw:self.write(path,raw,mode=0o640,gid=self.proxy_gid)
            path=self.config_path('generation.json');current=self.read(path)
            require(current in permitted_generations)
            if current!=canonical(updated):self.write(path,canonical(updated))
            require(self.generation()==updated)
            return updated
        try:return persist(desired)
        except Exception:
            try:persist(closed)
            except Exception:
                try:self.stop_proxies()
                except Exception:pass
            raise IdleError(ERROR) from None
    def bindings(self,policy,prepared,inventory):
        self.native(policy);result={}
        for group,(service,key,port) in GROUPS.items():
            expected=inventory[service];require(re.fullmatch('[a-f0-9]{64}',expected['id']) and expected['image']==prepared.images[key])
            row=records.strict_json(self.call(host_compose.BASE+['inspect','--format',INSPECT,expected['id']]))
            labels=row['labels'];require(row['id']==expected['id'] and row['image']==expected['image'] and row['state']=='running'
                and row['health']=='healthy' and labels.get('com.xynes.managed')=='true' and labels.get('com.xynes.environment')==self.stage
                and labels.get('com.xynes.role')=='application' and labels.get('com.docker.compose.project')=='xynes-'+self.stage
                and labels.get('com.docker.compose.service')==service)
            net=row['networks'][policy['network']['name']];ip=private_ip(net['IPAddress'])
            require(net['NetworkID']==policy['network']['id'] and net['Gateway']==policy['network']['gateway']
                and ip in ipaddress.ip_network(policy['network']['subnet']) and str(ip)!=policy['network']['gateway'])
            result[group]={'id':row['id'],'image':row['image'],'ip':str(ip)}
        require(result['auth']['id']!=result['cms']['id'] and result['auth']['ip']!=result['cms']['ip'])
        return result
    def rebind(self,prepared,inventory):
        self.switch_started=False
        try:return self._rebind(prepared,inventory)
        except Exception:
            if self.switch_started:
                # A failure after a successful resume must close admission too.
                # Never delete warmth or claim recovery from this cleanup path.
                try:
                    owner=self.deployment()['owner']
                    for group in GROUPS:self.lease(group,'/drain',owner)
                except Exception:
                    for group in GROUPS:
                        try:
                            require(self.read(self.path('/etc/systemd/system/xynes-idle-'+self.stage+'-'+group+'.service'),mode=0o644)==unit(self.stage,group))
                            self.call(['/usr/bin/systemctl','stop','xynes-idle-'+self.stage+'-'+group+'.service'],timeout=120)
                        except Exception:pass
            raise IdleError(ERROR) from None
    def _rebind(self,prepared,inventory):
        if not self.enabled():return
        state=self.deployment();policy,sha=self.policy();require(sha==state['policy_sha256']);bindings=self.bindings(policy,prepared,inventory)
        try:self.generation()
        except FileNotFoundError:
            for name in ('broker.json','auth-proxy.json','cms-proxy.json'):
                try:self.config_path(name).lstat()
                except FileNotFoundError:pass
                else:require(False)
        root='/run/xynes/idle/'+self.stage;config={
            'broker.json':{'environment':self.stage,'control_socket':root+'/control.sock','docker_socket':'/var/run/docker.sock',
                'token_file':'/etc/xynes/idle/'+self.stage+'/control.token','proxy_uid':UID[self.stage],'socket_gid':UID[self.stage],
                'targets':{self.stage+'-'+g:{'container_id':b['id'],'image':b['image'],'service':GROUPS[g][0],'network_id':policy['network']['id']} for g,b in bindings.items()}}}
        for offset,(group,binding) in enumerate(bindings.items()):
            config[group+'-proxy.json']={'environment':self.stage,'control_socket':root+'/control.sock',
                'token_file':'/etc/xynes/idle/'+self.stage+'/control.token','listen':policy['ingress']['gateway']+':'+str(PORT[self.stage]+offset),
                'policy':{'group':self.stage+'-'+group,'target':'http://'+binding['ip']+':'+str(GROUPS[group][2]),
                    'hosts':[group+'.'+('erp' if self.stage=='prod' else self.stage)+'.xynes.com'],'max_body_bytes':2*1024*1024,
                    'max_queued_requests':32,'trusted_ingress':[policy['caddy']['ip']+'/32'],'dynamic_target':True},
                'idle_seconds':policy['idle_seconds'],'wake_seconds':WAKE_SECONDS[self.stage],'max_in_flight':64,
                'state_file':'/var/lib/xynes/idle/'+self.stage+'/'+group+'/warm.json','host_network':True,'start_drained':True}
        self.switch_started=True
        for group in ('auth','cms','broker'):self.call(['/usr/bin/systemctl','stop','xynes-idle-'+self.stage+'-'+group+'.service'],timeout=120)
        for name,value in config.items():self.write(self.config_path(name),canonical(value),mode=0o640,gid=self.proxy_gid)
        generation={'schema_version':1,'bindings':bindings,'configuration_sha256':{n:digest(canonical(v)) for n,v in config.items()},'policy_sha256':sha}
        self.write(self.config_path('generation.json'),canonical(generation))
        for group in ('broker','auth','cms'):
            self.call(['/usr/bin/systemctl','start','xynes-idle-'+self.stage+'-'+group+'.service'],timeout=120)
            if group=='broker':self.wait_broker()
        require(self.bindings(policy,prepared,inventory)==bindings and self.policy()[1]==sha)
        require(callable(self.lease))
        for group in GROUPS:
            self.wait_proxy(group)
            self.lease(group,'/resume',state['owner'])
        # Root may temporarily resume this live generation for product smoke,
        # but process/host restart remains closed until finish validates it.
        require(self.generation()==generation)
    def wait_proxy(self,group):
        require(group in GROUPS);deadline=time.monotonic()+15
        while True:
            try:
                info=self.path('/run/xynes/idle/'+self.stage+'/'+group+'/leases.sock').lstat()
                require(stat.S_ISSOCK(info.st_mode) and info.st_uid==self.proxy_uid and info.st_gid==self.proxy_gid
                    and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1)
                return
            except FileNotFoundError:
                require(time.monotonic()<deadline);time.sleep(.05)
    def wait_broker(self):
        deadline=time.monotonic()+15
        while True:
            try:
                info=self.path('/run/xynes/idle/'+self.stage+'/control.sock').lstat()
                require(stat.S_ISSOCK(info.st_mode) and info.st_uid==self.uid and info.st_gid==self.proxy_gid
                    and stat.S_IMODE(info.st_mode)==0o660 and info.st_nlink==1)
                return
            except FileNotFoundError:
                require(time.monotonic()<deadline);time.sleep(.05)
    def finish(self,owner):
        state=self.deployment();require(state['owner']==owner and self.policy()[1]==state['policy_sha256'] and callable(self.lease))
        try:
            generation=self.generation()
            policy,_=self.policy();images={};inventory={}
            for group,(service,key,_) in GROUPS.items():
                binding=generation['bindings'][group];images[key]=binding['image'];inventory[service]={'id':binding['id'],'image':binding['image']}
            prepared=type('Bindings',(),{'images':images})()
            require(self.bindings(policy,prepared,inventory)==generation['bindings'])
            require(set(generation['configuration_sha256'])=={'broker.json','auth-proxy.json','cms-proxy.json'})
            for name,sha in generation['configuration_sha256'].items():require(digest(self.read(self.config_path(name),mode=0o640))==sha)
            for group in GROUPS:self.lease(group,'/resume',owner)
            self.restart_policy(generation,drained=False)
            for group in reversed(GROUPS):self.lease(group,'/release',owner)
            path=self.config_path('deployment.json');require(self.deployment()==state);path.unlink()
        except Exception:
            try:
                self.restart_policy(self.generation(),drained=True)
                # A partial release must reacquire both durable holds before drain.
                for group in GROUPS:self.lease(group,'/warm',owner)
                for group in GROUPS:self.lease(group,'/drain',owner)
            except Exception:
                try:self.stop_proxies()
                except Exception:pass
            raise IdleError(ERROR) from None
