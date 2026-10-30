#!/usr/bin/env python3
"""Ephemeral eight-service CI stack from admitted build artifacts.

No hosted state, remote daemon, credential store, source fetch, image build,
provider credentials or publication APIs. The caller admits native artifacts;
this module rechecks their bytes and owns only unpredictable labelled objects.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time
try:
    from . import ci_smoke_runtime, host_seed, image_build, migration_sql, migration_transport
except ImportError:
    import ci_smoke_runtime, host_seed, image_build, migration_sql, migration_transport

BACKENDS=frozenset(('xynes-gateway','xynes-accounts-service','xynes-authz-service','xynes-cms-core','xynes-doc-service','xynes-telemetry-service'))
SERVICES={'xynes-gateway':('gateway',4100),'xynes-accounts-service':('accounts-service',4203),
          'xynes-authz-service':('authz-service',4300),'xynes-cms-core':('cms-core',4202),
          'xynes-doc-service':('doc-service',4201),'xynes-telemetry-service':('telemetry-service',4400)}
ERROR='CI product stack refused'
LABEL='io.xynes.apex.ci-smoke'
CID=re.compile(r'[a-f0-9]{64}')
DIGEST=re.compile(r'sha256:[a-f0-9]{64}')
ROOT=Path(__file__).resolve().parents[2]
STACK_PHASES=frozenset(('input-qualification','image-import','external-images','substrate','identities','postgres','gotrue','migrations','seeds','services','flow','cleanup'))

COMMAND_KINDS=frozenset(('docker-load','docker-image-inspect','docker-image-tag','docker-image-rm',
    'docker-pull','docker-network-create','docker-network-inspect','docker-network-ls','docker-network-rm',
    'docker-volume-create','docker-volume-inspect','docker-volume-ls','docker-volume-rm',
    'docker-create','docker-start','docker-exec','docker-inspect','docker-ps','docker-rm',
    'openssl-genpkey','openssl-pkey','other'))
COMMAND_ERRORS=frozenset(('missing-legacy-image-json','image-reference-not-found','disk-capacity',
    'unsupported-media-type','command-failed','timeout'))

class StackError(ValueError):pass

def safe_command_evidence(value):
    """Author the complete command diagnostic shape, ignoring all other fields."""
    if type(value) is not dict:return {}
    kind=value.get('kind');code=value.get('exit_code');error=value.get('error_class')
    if (type(kind) is not str or kind not in COMMAND_KINDS
            or not (code is None or type(code) is int and -255<=code<=255)
            or type(error) is not str or error not in COMMAND_ERRORS):return {}
    return {'command':{'kind':kind,'exit_code':code,'error_class':error}}

def command_kind(argv):
    if type(argv) is not list or not argv or any(type(arg) is not str for arg in argv):return 'other'
    if argv[0]=='/usr/bin/docker':
        args=argv[3:] if len(argv)>3 and argv[1]=='--config' else argv[1:]
        count=2 if args and args[0] in ('image','network','volume') else 1
        kind='docker-'+'-'.join(args[:count])
    elif argv[0]=='/usr/bin/openssl' and len(argv)>1:kind='openssl-'+argv[1]
    else:kind='other'
    return kind if kind in COMMAND_KINDS else 'other'

def command_error_class(stderr,*,timeout):
    """Recognize reviewed literal stderr patterns; never return captured bytes."""
    if timeout:return 'timeout'
    lower=bytes(stderr).lower()
    if b'no such file' in lower and (b'/json' in lower or b'manifest.json' in lower):return 'missing-legacy-image-json'
    if b'nosuchimage' in lower or b'no such image' in lower:return 'image-reference-not-found'
    if b'no space left on device' in lower:return 'disk-capacity'
    if b'unsupported media type' in lower or b'unsupported media-type' in lower:return 'unsupported-media-type'
    return 'command-failed'

def require(value):
    if not value:raise StackError(ERROR)

def clean_environment():
    return {'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}

def command(argv,*,stdin=None,timeout=180,limit=2*1024*1024):
    """Bound subprocess streams and lifetime; never surface child output/errors."""
    process=None;source=None;errors=bytearray();code=None;timed_out=False
    try:
        require(type(argv) is list and argv[0] in ('/usr/bin/docker','/usr/bin/openssl')
                and all(type(arg) is str and '\x00' not in arg for arg in argv)
                and type(timeout) is int and 0<timeout<=600 and 0<limit<=2*1024*1024)
        if stdin is not None:
            require(isinstance(stdin,Path) and stdin.is_file() and not stdin.is_symlink()
                    and 0<stdin.stat().st_size<=16*1024*1024)
            source=stdin.open('rb')
        process=subprocess.Popen(argv,stdin=source or subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,env=clean_environment(),cwd='/',start_new_session=True)
        output=bytearray();deadline=time.monotonic()+timeout
        with selectors.DefaultSelector() as selector:
            for stream,kind in ((process.stdout,'out'),(process.stderr,'err')):
                selector.register(stream,selectors.EVENT_READ,kind)
            while selector.get_map():
                timed_out=time.monotonic()>=deadline;require(not timed_out)
                for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),65536)
                    if not data:selector.unregister(key.fileobj);continue
                    if key.data=='out':output.extend(data);require(len(output)<=limit)
                    else:
                        total=len(errors)+len(data);errors.extend(data[:max(0,65536-len(errors))]);require(total<=65536)
            code=process.wait(timeout=max(.001,deadline-time.monotonic()));require(code==0)
        return bytes(output)
    except Exception as cause:
        if process is not None and code is None and not timed_out and not isinstance(cause,subprocess.TimeoutExpired):
            observed=process.poll()
            if type(observed) is int:code=observed
        error=StackError(ERROR)
        error.safe_command=safe_command_evidence({'kind':command_kind(argv),'exit_code':code,
            'error_class':command_error_class(errors,timeout=timed_out or isinstance(cause,subprocess.TimeoutExpired))}).get('command')
        raise error from None
    finally:
        if process:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            for stream in (process.stdout,process.stderr):
                if stream:stream.close()
        if source:source.close()

def private_file(path,data,*,uid=None):
    require(isinstance(data,bytes) and not path.exists() and not path.is_symlink())
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as file:file.write(data)
    if uid is not None:os.chown(path,uid,uid)
    return path

def environment_file(directory,name,values):
    require(all(re.fullmatch(r'[A-Z][A-Z0-9_]*',key) and type(value) is str
                and not any(c in value for c in '\x00\r\n') for key,value in values.items()))
    return private_file(directory/(name+'.env'),''.join(key+'='+values[key]+'\n' for key in sorted(values)).encode())

def identities(directory):
    directory.mkdir(mode=0o700);public=[]
    for owner,keyid in (('gateway','g1'),('accounts','a1'),('cms','c1'),('docs','d1'),('telemetry','t1')):
        path=directory/(owner+'-private.pem')
        command(['/usr/bin/openssl','genpkey','-algorithm','ED25519','-out',str(path)],limit=16384)
        require(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=4096)
        path.chmod(0o600)
        raw=command(['/usr/bin/openssl','pkey','-in',str(path),'-pubout'],limit=16384)
        public.append({'issuer':owner,'keyId':keyid,'publicKey':raw.decode('ascii')})
        os.chown(path,1001,1001)
    for receiver in ('accounts','authz','cms','docs','telemetry'):
        issuers={'gateway','accounts','cms','docs'} if receiver=='authz' else {'gateway'}
        private_file(directory/(receiver+'-trust.json'),json.dumps([row for row in public if row['issuer'] in issuers]).encode(),uid=1001)
    return directory

def service_role(secret,now):
    encode=lambda value:base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).rstrip(b'=')
    body=encode({'alg':'HS256','typ':'JWT'})+b'.'+encode({'iss':'https://supabase.qa.xynes.com/auth/v1','aud':'authenticated','role':'service_role','iat':now,'exp':now+3600})
    return (body+b'.'+base64.urlsafe_b64encode(hmac.new(secret.encode(),body,hashlib.sha256).digest()).rstrip(b'=')).decode()

class Stack:
    def __init__(self,work):
        self.work=work;self.nonce=secrets.token_hex(16);self.prefix='apex-ci-'+self.nonce
        self.label=LABEL+'='+self.nonce;self.objects=[];self.tags=[];self.expected={};self.deadline=time.monotonic()+1800
        self.config=work/'docker-config';self.config.mkdir(mode=0o700)
        private_file(self.config/'config.json',b'{}')
        self.network=self.prefix+'-net';self.volume=self.prefix+'-db';self.network_id=None
    def call(self,args,**kwargs):
        require(time.monotonic()<self.deadline)
        return command(['/usr/bin/docker','--config',str(self.config),*args],**kwargs)
    def read(self,args):return json.loads(self.call(args).decode())
    def image(self,reference,*,receipt=None,labels=None,pinned=False):
        row=self.read(['image','inspect','--format','{{json .}}',reference])
        require(type(row) is dict and row.get('Os')=='linux' and row.get('Architecture')=='amd64' and DIGEST.fullmatch(row.get('Id','')))
        if pinned:
            digest=reference.rsplit('@',1)[1]
            require(row.get('Descriptor',{}).get('digest')==digest or any(type(value) is str and value.endswith('@'+digest) for value in row.get('RepoDigests',[])))
        if receipt:
            require(row['Id']==receipt['config_digest'] or
                (row['Id']==receipt['manifest_digest'] and type(row.get('Descriptor')) is dict
                 and row['Descriptor'].get('digest')==receipt['manifest_digest']))
            actual=row.get('Config',{}).get('Labels',{})
            require(type(actual) is dict and all(actual.get(key)==value for key,value in labels.items()))
        return row['Id']
    def load(self,selected):
        for task,path,receipt in selected:
            require(image_build.inspect_oci(path,task)==receipt)
            self.call(['load','--input',str(path)],timeout=600)
            image=None;detail={}
            for candidate in (receipt['config_digest'],receipt['manifest_digest']):
                try:image=self.image(candidate,receipt=receipt,labels=task['labels']);break
                except Exception as error:
                    detail=safe_command_evidence(getattr(error,'safe_command',None));continue
            if image is None:
                error=StackError(ERROR)
                if detail:error.safe_command=detail['command']
                raise error
            tag='apex-ci-smoke:'+self.nonce+'-'+task['workload'];self.tags.append((tag,image))
            self.call(['image','tag',image,tag]);require(self.image(tag,receipt=receipt,labels=task['labels'])==image)
            self.expected[tag]=image
    def owned(self,kind,name,reference):
        args=['inspect','--format','{{json .}}',reference] if kind=='container' else [kind,'inspect','--format','{{json .}}',reference]
        row=self.read(args);labels=row.get('Config',{}).get('Labels') if kind=='container' else row.get('Labels')
        require(type(labels) is dict and labels.get(LABEL)==self.nonce and row.get('Name')==('/'+name if kind=='container' else name))
        if kind!='volume':require(CID.fullmatch(row.get('Id','')) and row['Id']==reference)
        if kind=='network':require(row.get('Internal') is True and row.get('Driver')=='bridge')
        if kind=='container':
            image=row['Config'].get('Image');require(image in self.expected and row.get('Image')==self.expected[image])
            require(row.get('HostConfig',{}).get('NetworkMode') in (self.network,self.network_id))
            networks=row.get('NetworkSettings',{}).get('Networks')
            require(type(networks) is dict and set(networks)=={self.network}
                    and networks[self.network].get('NetworkID') in ('',self.network_id))
        return row
    def substrate(self):
        for kind,name,flags in (('network',self.network,['--internal','--driver','bridge']),('volume',self.volume,[])):
            self.objects.append((kind,name,None))
            raw=self.call([kind,'create',*flags,'--label',self.label,name]).decode().strip()
            row=self.owned(kind,name,raw);self.objects[-1]=(kind,name,row.get('Id',name))
            if kind=='network':self.network_id=row['Id']
    def create(self,name,image,env,*,mounts=(),health=None,oneoff=None,alias=None):
        full=self.prefix+'-'+name;self.objects.append(('container',full,None))
        args=['create','--name',full,'--platform','linux/amd64','--network',self.network,
              '--label',self.label,'--restart','no','--log-driver','none','--pids-limit','256','--memory','512m']
        if alias:args+=['--network-alias',alias]
        args+=['--env-file',str(env)]
        for mount in mounts:args+=['--mount',mount]
        if name!='supabase-db':args+=['--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777']
        if health:args+=['--health-cmd',health,'--health-interval','2s','--health-timeout','5s','--health-retries','60']
        if oneoff:args+=['--no-healthcheck','--user','1001:1001','--workdir','/app','--entrypoint','/usr/local/bin/bun']
        args+=[image]
        if oneoff:args+=['--eval',oneoff]
        cid=self.call(args).decode().strip();require(CID.fullmatch(cid))
        self.owned('container',full,cid);self.objects[-1]=('container',full,cid)
        return full,cid
    def start(self,item,*,oneoff=False):
        name,cid=item;self.owned('container',name,cid)
        output=self.call(['start',*(['--attach'] if oneoff else []),cid],timeout=240)
        if oneoff:
            row=self.owned('container',name,cid);require(row['State'].get('Status')=='exited' and row['State'].get('ExitCode')==0)
            return json.loads(output.decode())
        for _ in range(90):
            row=self.owned('container',name,cid);state=row.get('State',{})
            require(state.get('Status')=='running')
            if state.get('Health',{}).get('Status')=='healthy':return row
            time.sleep(2)
        raise StackError(ERROR)
    def endpoint(self,item,port):
        # Linux hosts can reach their internal bridge directly. Docker does not
        # publish ports for containers with only internal-network attachments.
        require(type(port) is int and (item[0],port) in (
            (self.prefix+'-supabase-auth',9999),(self.prefix+'-gateway',4100)))
        row=self.owned('container',item[0],item[1])
        network=self.owned('network',self.network,self.network_id)
        configs=network.get('IPAM',{}).get('Config')
        require(type(configs) is list and len(configs)==1 and type(configs[0]) is dict)
        entry=row['NetworkSettings']['Networks'][self.network]
        require(entry.get('NetworkID')==self.network_id)
        try:
            subnet=ipaddress.IPv4Network(configs[0]['Subnet'])
            address=ipaddress.IPv4Address(entry['IPAddress'])
            gateway=ipaddress.IPv4Address(configs[0]['Gateway'])
        except (KeyError,ValueError,TypeError):raise StackError(ERROR) from None
        require(any(subnet.subnet_of(ipaddress.IPv4Network(cidr))
                    for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
                and subnet.prefixlen<=30 and address in subnet and gateway in subnet
                and address not in (subnet.network_address,subnet.broadcast_address,gateway)
                and type(entry.get('IPPrefixLen')) is int and entry['IPPrefixLen']==subnet.prefixlen)
        return str(address),port
    def sql(self,db,password_file,*,source=None,query=None):
        self.owned('container',db[0],db[1])
        args=['exec','-i','--env-file',str(password_file),db[1],'psql','-X','-qAt','--no-password',
              '--host','127.0.0.1','--username','postgres','--dbname','postgres','--set=ON_ERROR_STOP=1']
        if query:args+=['--command',query]
        return self.call(args,stdin=source,timeout=600)
    def cleanup(self):
        errors=False;detail={}
        # Permit cleanup even when the overall startup deadline expired.
        self.deadline=time.monotonic()+300
        for kind,name,reference in reversed(self.objects):
            try:
                if reference is None:
                    listing=['ps','--all','--no-trunc'] if kind=='container' else [kind,'ls',*(['--no-trunc'] if kind=='network' else [])]
                    rows=self.call([*listing,'--filter','name='+('^/'+name+'$' if kind=='container' else name),
                        '--filter','label='+self.label,'--format','{{.Name}}' if kind=='volume' else '{{.ID}}']).decode().splitlines()
                    require(len(rows)<=1)
                    if not rows:continue
                    reference=rows[0]
                self.owned(kind,name,reference)
                self.call(['rm','--force',reference] if kind=='container' else [kind,'rm',reference])
            except Exception as error:
                errors=True
                if not detail:detail=safe_command_evidence(getattr(error,'safe_command',None))
        for tag,identity in reversed(self.tags):
            try:
                require(self.image(tag)==identity);self.call(['image','rm',tag])
            except Exception as error:
                errors=True
                if not detail:detail=safe_command_evidence(getattr(error,'safe_command',None))
        if errors:
            error=StackError(ERROR)
            if detail:error.safe_command=detail['command']
            raise error

def run(*,selected,manifest,manifest_bytes,migration_bundle,controller,work):
    stack=None;failure=None;phase='input-qualification'
    try:
        require(isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True)==work
                and stat.S_ISDIR(work.stat().st_mode) and stat.S_IMODE(work.stat().st_mode)==0o700
                and work.stat().st_uid==os.getuid() and ',' not in str(work))
        require(type(selected) is tuple and len(selected)==6 and {row[0]['workload'] for row in selected}==BACKENDS)
        # Every selected artifact is requalified before any Docker/identity work.
        for task,path,receipt in selected:require(image_build.inspect_oci(path,task)==receipt)
        external={}
        for name in ('supabase-db','supabase-auth'):
            row=manifest['external_images'][name];reference=row['image']
            require(row['platform']=='linux/amd64' and type(reference) is str
                    and re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',reference))
            external[name]=reference
        stack=Stack(work);phase='image-import';stack.load(selected)
        phase='external-images'
        for reference in external.values():
            stack.call(['pull','--platform','linux/amd64',reference],timeout=600)
            stack.expected[reference]=stack.image(reference,pinned=True)
        phase='substrate';stack.substrate()
        phase='identities';keys=identities(work/'identities')
        password=secrets.token_hex(32);auth_password=secrets.token_hex(32);secret=secrets.token_hex(32);foreign=secrets.token_hex(32)
        database='postgres://postgres:'+password+'@supabase-db:5432/postgres'
        pg_env=environment_file(work,'postgres',{'POSTGRES_USER':'postgres','POSTGRES_DB':'postgres','POSTGRES_PASSWORD':password,
            'AUTH_DATABASE_PASSWORD':auth_password,'POSTGRES_INITDB_ARGS':'--auth-host=scram-sha-256'})
        pg_password=environment_file(work,'psql',{'PGPASSWORD':password,'PGSSLMODE':'disable','PGCONNECT_TIMEOUT':'15'})
        auth_dir=ROOT/'infra/release/auth'
        phase='postgres'
        db=stack.create('supabase-db',external['supabase-db'],pg_env,alias='supabase-db',health='pg_isready --host 127.0.0.1 --username postgres --dbname postgres',mounts=(
            'type=volume,src='+stack.volume+',dst=/var/lib/postgresql/data',
            'type=bind,src='+str(auth_dir/'bootstrap.sh')+',dst=/docker-entrypoint-initdb.d/10-apex-auth.sh,readonly',
            'type=bind,src='+str(auth_dir/'bootstrap.sql')+',dst=/opt/apex-auth/bootstrap.sql,readonly'))
        stack.start(db)
        issuer='https://supabase.qa.xynes.com/auth/v1'
        auth_env=environment_file(work,'gotrue',{'GOTRUE_API_HOST':'0.0.0.0','GOTRUE_API_PORT':'9999','GOTRUE_DB_DRIVER':'postgres','GOTRUE_DB_NAMESPACE':'auth',
            'GOTRUE_DB_DATABASE_URL':'postgres://supabase_auth_admin:'+auth_password+'@supabase-db:5432/postgres',
            'API_EXTERNAL_URL':issuer,'GOTRUE_SITE_URL':'https://auth.qa.xynes.com','GOTRUE_URI_ALLOW_LIST':'https://auth.qa.xynes.com',
            'GOTRUE_JWT_SECRET':secret,'GOTRUE_JWT_ISSUER':issuer,'GOTRUE_JWT_AUD':'authenticated','GOTRUE_JWT_ADMIN_ROLES':'service_role',
            'GOTRUE_JWT_DEFAULT_GROUP_NAME':'authenticated','GOTRUE_JWT_EXP':'3600','GOTRUE_EXTERNAL_EMAIL_ENABLED':'true',
            'GOTRUE_EXTERNAL_PHONE_ENABLED':'false','GOTRUE_EXTERNAL_GOOGLE_ENABLED':'false','GOTRUE_MAILER_AUTOCONFIRM':'true',
            'GOTRUE_SMTP_HOST':'127.0.0.1','GOTRUE_SMTP_PORT':'1','GOTRUE_SMTP_ADMIN_EMAIL':'ci@example.invalid',
            'GOTRUE_SECURITY_REFRESH_TOKEN_ROTATION_ENABLED':'true','GOTRUE_LOG_LEVEL':'warn'})
        phase='gotrue'
        auth=stack.create('supabase-auth',external['supabase-auth'],auth_env,health='wget --quiet --tries=1 --spider http://127.0.0.1:9999/health')
        stack.start(auth)
        phase='migrations'
        require(isinstance(migration_bundle,Path) and migration_bundle.is_absolute() and not migration_bundle.is_symlink()
                and migration_bundle.is_file() and 0<migration_bundle.stat().st_size<=migration_transport.MAX_BUNDLE)
        migration_work=work/'migration-input';migration_work.mkdir(mode=0o700)
        verified=migration_transport.extract(bundle=migration_bundle,expected_sha256=hashlib.sha256(migration_bundle.read_bytes()).hexdigest(),
            manifest_bytes=manifest_bytes,controller=controller,work=migration_work)
        units,payloads=migration_transport.application_inputs(verified)
        sql=private_file(work/'migrations.sql',migration_sql.render('qa','postgres',units,payloads).encode())
        stack.sql(db,pg_password,source=sql)
        phase='seeds'
        host_seed.requirements(verified)
        seed_env=environment_file(work,'seed',{'DATABASE_URL':database});seed_results={}
        tags={task['workload']:'apex-ci-smoke:'+stack.nonce+'-'+task['workload'] for task,_,_ in selected}
        for kind,owner,code in (('authz','xynes-authz-service',host_seed.AUTHZ_CODE),('cms','xynes-cms-core',host_seed.CMS_CODE)):
            item=stack.create('seed-'+kind,tags[owner],seed_env,oneoff=code)
            seed_results[kind]=stack.start(item,oneoff=True)
        counts=json.loads(stack.sql(db,pg_password,query=host_seed.POSTCONDITIONS_SQL).decode())
        a=seed_results['authz'];c=seed_results['cms']
        require(type(a) is dict and set(a)=={'kind','permissions','roles','mappings'} and a['kind']=='authz'
                and all(type(a[key]) is int and 0<a[key]<=65536 for key in ('permissions','roles','mappings')))
        require(type(c) is dict and set(c)=={'kind','templates','workspaces','defaults'} and c['kind']=='cms'
                and c['templates']==3 and c['workspaces']==0 and c['defaults']==0)
        require(counts=={'authz_permissions':a['permissions'],'authz_roles':a['roles'],'authz_mappings':a['mappings'],
                         'cms_templates':3,'workspaces':0,'cms_defaults_satisfied':0})
        phase='services';active={}
        for owner in ('xynes-authz-service','xynes-accounts-service','xynes-cms-core','xynes-doc-service','xynes-telemetry-service','xynes-gateway'):
            service,port=SERVICES[owner];identity={'xynes-gateway':('gateway','g1'),'xynes-accounts-service':('accounts','a1'),
                'xynes-cms-core':('cms','c1'),'xynes-doc-service':('docs','d1'),'xynes-telemetry-service':('telemetry','t1')}.get(owner)
            env={'NODE_ENV':'production','PORT':str(port),'DATABASE_URL':database,'INTERNAL_SERVICE_TOKEN':'','INTERNAL_JWT_SIGNING_KEY':'',
                 'AUTHZ_SERVICE_URL':'http://authz-service:4300','MAIL_PROVIDER':'noop','POSTHOG_API_KEY':'','AUTHZ_AUTO_SEED':'0'}
            mounts=[]
            if identity:
                who,keyid=identity;env.update(INTERNAL_REQUEST_PRIVATE_KEY_FILE='/run/secrets/internal-request-private.pem',INTERNAL_REQUEST_KEY_ID=keyid)
                mounts.append('type=bind,src='+str(keys/(who+'-private.pem'))+',dst=/run/secrets/internal-request-private.pem,readonly')
            if owner!='xynes-gateway':
                who='authz' if owner=='xynes-authz-service' else identity[0]
                env['INTERNAL_REQUEST_TRUST_FILE']='/run/secrets/internal-request-trust.json'
                mounts.append('type=bind,src='+str(keys/(who+'-trust.json'))+',dst=/run/secrets/internal-request-trust.json,readonly')
            if owner=='xynes-gateway':
                env.update(JWT_SECRET=secret,JWT_ISSUER=issuer,JWT_AUDIENCE='authenticated',JWT_REQUIRE_ISS_AUD_IN_PROD='true',
                    JWT_MAX_TOKEN_LIFETIME_SECONDS='3600',TELEMETRY_IP_HASH_SALT=secrets.token_hex(32),
                    GATEWAY_CORS_ALLOWED_ORIGINS='https://auth.qa.xynes.com,https://cms.qa.xynes.com',
                    ACCOUNTS_SERVICE_URL='http://accounts-service:4203',CMS_CORE_URL='http://cms-core:4202',DOC_SERVICE_URL='http://doc-service:4201',
                    TELEMETRY_SERVICE_URL='http://telemetry-service:4400',STORAGE_SERVICE_URL='')
            file=environment_file(work,service,env)
            item=stack.create(service,tags[owner],file,alias=service,mounts=mounts)
            stack.start(item);active[service]=item
        keys_loader=lambda stage:({'qa':secret,'staging':foreign}[stage],ci_smoke_runtime.smoke.origins(stage)['auth']+'/auth/v1')
        phase='flow'
        auth_host,auth_port=stack.endpoint(auth,9999);gateway_host,gateway_port=stack.endpoint(active['gateway'],4100)
        result=ci_smoke_runtime.execute_flow(auth_port=auth_port,gateway_port=gateway_port,auth_host=auth_host,gateway_host=gateway_host,
            values={'SUPABASE_SERVICE_ROLE_KEY':service_role(secret,int(time.time())),'R2_ACCOUNT_ID':'0'*32,'R2_BUCKET':'apex-ci-unused'},keys_loader=keys_loader)
        require(type(result) is dict and result.get('result')=='passed' and result.get('storage_verified') is False)
        return result
    except Exception as error:
        failure=error
        if isinstance(error,ci_smoke_runtime.RuntimeError):raise
        safe=StackError(ERROR)
        safe.safe_evidence=ci_smoke_runtime.failure_evidence(phase='provision',failed_flow_phase=None,steps=[])
        safe.safe_evidence['stack_phase']=phase if phase in STACK_PHASES else 'input-qualification'
        safe.safe_evidence.update(safe_command_evidence(getattr(error,'safe_command',None)))
        raise safe from None
    finally:
        if stack:
            try:stack.cleanup()
            except Exception as cleanup_error:
                error=StackError(ERROR)
                if failure is not None and type(getattr(failure,'safe_evidence',None)) is dict:error.safe_evidence=dict(failure.safe_evidence)
                else:error.safe_evidence=ci_smoke_runtime.failure_evidence(phase='cleanup',failed_flow_phase=None,steps=[])
                previous_command=error.safe_evidence.pop('command',None)
                error.safe_evidence.update(safe_command_evidence(getattr(cleanup_error,'safe_command',None)) or safe_command_evidence(previous_command))
                error.safe_evidence['stack_phase']='cleanup'
                raise error from None
