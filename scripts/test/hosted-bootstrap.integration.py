#!/usr/bin/env python3
"""Actual candidate 40-SQL baseline after local PostgreSQL/GoTrue bootstrap.

Explicit local Unix socket, fixed already-present amd64 child images, no pulls or
provider credentials. Only a private synthetic infra snapshot is committed. The
tracked source manifest and sibling repositories remain untouched. This fixture
cannot establish native source approval, hosted acceptance or deploy authority.
Integration owner must run it; importing/compiling does not start Docker.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
WORKSPACE=ROOT.parents[1]
sys.path.insert(0,str(ROOT/'scripts/release'))
import migration_inventory
import migration_plan
import migration_sql
import migration_transport
import release_manifest as records
import source_context

DB_IMAGE='postgres:17-alpine@sha256:5a6fcbc5d93831991d2386fa634509b3c49a1ac5ffb70c13c2322840f821d7e7'
AUTH_IMAGE='supabase/gotrue:v2.196.0@sha256:7e813221b93fbf54b515036438550e483bfaf057b9db52fe9bc1ce91c47e817e'
LABEL='io.xynes.apex.fixture-nonce'
MAX_OUTPUT=1024*1024
MAX_ERROR=32768
CID=re.compile(r'[a-f0-9]{64}')

class BootstrapError(ValueError):pass

def require(value):
    if not value:raise BootstrapError('isolated hosted bootstrap fixture refused')

def write(path,raw,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(descriptor,'wb') as file:
        os.fchmod(file.fileno(),mode);file.write(raw);file.flush();os.fsync(file.fileno())

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

class Fixture:
    def __init__(self):
        require(sys.platform=='darwin' or sys.platform.startswith('linux'))
        account_home=Path(pwd.getpwuid(os.getuid()).pw_dir)
        socket=account_home/'.docker/run/docker.sock' if sys.platform=='darwin' else Path('/var/run/docker.sock')
        self.directory=Path(tempfile.mkdtemp(prefix='apex-hosted-bootstrap-')).resolve();self.directory.chmod(0o700)
        self.nonce=secrets.token_hex(16);self.project='apex-bootstrap-fixture-'+self.nonce[:16]
        self.environment={'PATH':os.environ['PATH'],'HOME':str(self.directory),'LANG':'C.UTF-8',
                          'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
                          'GIT_NO_REPLACE_OBJECTS':'1','GIT_NO_LAZY_FETCH':'1'}
        docker=shutil.which('docker');require(docker is not None)
        config=self.directory/'docker';config.mkdir(mode=0o700)
        # Only plugin discovery is carried over, never user Docker auth/context.
        plugins=[account_home/'.docker/cli-plugins',Path('/usr/local/lib/docker/cli-plugins'),
                 Path('/usr/libexec/docker/cli-plugins'),Path('/opt/homebrew/lib/docker/cli-plugins')]
        write(config/'config.json',json.dumps({'cliPluginsExtraDirs':[str(path) for path in plugins if path.is_dir()]}).encode())
        self.docker=[docker,'--host','unix://'+str(socket),'--config',str(config)]
        self.phase='preparation';self.containers=[];self.network=None;self.image_ids={};self.compose=None
        self.document=None;self.auth_cid=None;self.db_cid=None;self.diagnostic=0;self.auth_sources={}

    def command(self,args,*,input=None,timeout=30,limit=MAX_OUTPUT,private_output=None):
        """Bound both pipes before allocation; privately retain failure diagnostics."""
        require(type(args) is list and 0<timeout<=600 and 0<limit<=MAX_OUTPUT)
        process=None;selector=selectors.DefaultSelector();stdout=bytearray();stderr=bytearray();output_size=0
        source=None
        try:
            # File-backed stdin avoids deadlock while large migration batches are
            # consumed and both output streams are drained concurrently.
            if input is not None:
                require(type(input) is str and len(input.encode())<=migration_sql.MAX_BATCH_SQL+1024*1024)
                source=tempfile.TemporaryFile(dir=self.directory);source.write(input.encode());source.seek(0)
            process=subprocess.Popen(args,stdin=source or subprocess.DEVNULL,stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,env=self.environment,cwd=self.directory,start_new_session=True)
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
            deadline=time.monotonic()+timeout
            while selector.get_map():
                require(time.monotonic()<deadline)
                for key,_ in selector.select(min(.2,max(0,deadline-time.monotonic()))):
                    raw=os.read(key.fileobj.fileno(),65536)
                    if not raw:selector.unregister(key.fileobj);continue
                    target=stdout if key.fileobj is process.stdout else stderr
                    if private_output is not None:
                        output_size+=len(raw);target.extend(raw[:max(0,MAX_ERROR-len(target))])
                        require(output_size<=MAX_OUTPUT)
                    else:
                        require(len(target)+len(raw)<=limit if target is stdout else len(target)+len(raw)<=MAX_ERROR)
                        target.extend(raw)
            require(process.wait(timeout=max(.01,deadline-time.monotonic()))==0)
            return stdout.decode('utf8')
        except Exception:
            self.diagnostic+=1
            # No argv, input, env or successful Auth responses enter this file.
            write(self.directory/f'diagnostic-{self.diagnostic:03d}.txt',
                  b'phase='+self.phase.encode()+b'\n'+bytes(stderr[:MAX_ERROR]))
            raise BootstrapError('bounded local fixture command refused') from None
        finally:
            selector.close()
            if process is not None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
                for stream in (process.stdout,process.stderr):stream.close()
            if source:source.close()
            if private_output is not None:
                require(isinstance(private_output,Path) and private_output.parent==self.directory)
                write(private_output,b'phase='+self.phase.encode()+b'\nstdout (bounded):\n'+bytes(stdout[:MAX_ERROR])+
                      b'\nstderr (bounded):\n'+bytes(stderr[:MAX_ERROR]))

    def checked(self,args,**kwargs):return self.command(self.docker+args,**kwargs)

    def git(self,repo,*args):
        return self.command(['git','--no-replace-objects','-c','core.hooksPath=/dev/null',
                             '-c','credential.helper=','-c','protocol.file.allow=never',
                             '-c','protocol.ext.allow=never','-C',str(repo),*args]).strip()

    def sources(self):
        self.auth_sources={name:migration_plan.read_owned(ROOT,'infra/release/auth/'+name)
                           for name in ('compose.fragment.yml','bootstrap.sh','bootstrap.sql')}
        for name in ('bootstrap.sh','bootstrap.sql'):
            write(self.directory/'auth-bootstrap'/name,self.auth_sources[name],mode=0o555 if name=='bootstrap.sh' else 0o444)
        original=migration_plan.read_owned(ROOT,'infra/release/source-manifest.json')
        manifest=records.strict_json(original.decode());source_context.validate_manifest(manifest,'planning')
        require(manifest['external_images']['supabase-db']['image']==DB_IMAGE
                and manifest['external_images']['supabase-auth']['image']==AUTH_IMAGE)
        snapshot=self.directory/'source-workspace';snapshot.mkdir(mode=0o700)
        repositories={}
        for owner in migration_inventory.OWNERS:
            repo=snapshot/owner;repo.mkdir(mode=0o700);repositories[owner]=repo
            self.git(repo,'init','--template=')
            if owner=='xynes-infra':
                paths=list(migration_plan.REVIEWED_FILES[owner])+['docs/DATABASE.md']
                for path in paths:write(repo/path,migration_plan.read_owned(ROOT,path))
                self.git(repo,'add','--all')
                self.environment.update(GIT_AUTHOR_NAME='Apex Local Fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',
                                        GIT_COMMITTER_NAME='Apex Local Fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid',
                                        GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00',GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00')
                self.git(repo,'-c','commit.gpgsign=false','commit','-qm','local candidate migration fixture, no source approval')
            else:
                source=WORKSPACE/'xynes'/owner;require(source.is_dir())
                commit=manifest['sources'][owner]['commit']
                require(self.git(source,'cat-file','-t',commit)=='commit')
                objects=Path(self.git(source,'rev-parse','--path-format=absolute','--git-path','objects')).resolve(strict=True)
                require(objects.is_absolute() and objects.is_dir() and objects.is_relative_to(WORKSPACE))
                write(repo/'.git/objects/info/alternates',(str(objects)+'\n').encode())
                self.git(repo,'update-ref','refs/heads/local-fixture',commit)
                self.git(repo,'symbolic-ref','HEAD','refs/heads/local-fixture')
                # Materialize only the source allowlist, directly from immutable
                # blobs, without checkout filters/hooks or program execution.
                for path,_mode,data in source_context.tracked_files(repo,commit,owner):write(repo/path,data)
                require(self.git(repo,'rev-parse','HEAD')==commit)
        controller={'repository':'Xynes-Studio/xynes-infra','commit':self.git(repositories['xynes-infra'],'rev-parse','HEAD'),
                    'workflow':'apex-build.yml','run_id':9007199254740991,'run_attempt':1}
        # The transport release grammar needs a syntactically approved manifest.
        # This PRIVATE copy uses an explicitly synthetic marker; it is never
        # submitted to GitHub/native admission and never replaces the real file.
        fixture_manifest=copy.deepcopy(manifest)
        fixture_manifest.update(status='approved',approval_reference='https://github.com/Xynes-Studio/xynes-infra/issues/9007199254740991')
        raw=(json.dumps(fixture_manifest,sort_keys=True,indent=2)+'\n').encode()
        write(self.directory/'fixture-source-manifest.json',raw)
        prior=dict(os.environ)
        try:
            os.environ.clear();os.environ.update(self.environment)
            bundle=migration_transport.create(manifest_bytes=raw,controller=controller,repositories=repositories,
                                              output=self.directory/'migration-bundle.tar')
        finally:os.environ.clear();os.environ.update(prior)
        # Root extraction is a deployment boundary. A nonroot local fixture must
        # not claim to exercise it: use the same inventory/plan read contracts on
        # its already exact source views and compare both archived metadata blobs.
        inventory=migration_inventory.inventory(fixture_manifest,repositories,controller['commit'])
        plan=migration_plan.plan(fixture_manifest,inventory,repositories)
        entries=migration_transport.tar_payloads((self.directory/'migration-bundle.tar').read_bytes())
        require(records.strict_json(entries['migration-inventory.json'].decode())==inventory
                and records.strict_json(entries['migration-plan.json'].decode())==plan and len(plan['units'])==40)
        payloads={(unit['owner'],unit['path']):entries['sources/'+unit['owner']+'/'+unit['path']] for unit in plan['units']}
        require(digest(ROOT/'infra/release/source-manifest.json')==hashlib.sha256(original).hexdigest())
        return manifest,controller,bundle,plan,payloads,hashlib.sha256(original).hexdigest()

    def preload(self):
        for service,image in [('supabase-db',DB_IMAGE),('supabase-auth',AUTH_IMAGE)]:
            rows=records.strict_json(self.checked(['image','inspect',image]))
            require(type(rows) is list and len(rows)==1)
            row=rows[0];require(row.get('Os')=='linux' and row.get('Architecture')=='amd64'
                               and re.fullmatch(r'sha256:[a-f0-9]{64}',row.get('Id','')))
            repository=image.split('@')[0].rsplit(':',1)[0];reference=repository+'@'+image.split('@')[1]
            require(reference in row.get('RepoDigests',[]) or 'docker.io/library/'+reference in row.get('RepoDigests',[]))
            self.image_ids[service]=row['Id']

    def start(self):
        document=records.strict_json(self.auth_sources['compose.fragment.yml'].decode())
        values={'XYNES_ENV':'qa','POSTGRES_PASSWORD':secrets.token_hex(32),'AUTH_DATABASE_PASSWORD':secrets.token_hex(32),
                'JWT_SECRET':secrets.token_hex(32),'JWT_ISSUER':'https://supabase.qa.example.invalid/auth/v1',
                'API_EXTERNAL_URL':'https://supabase.qa.example.invalid/auth/v1','SITE_URL':'https://auth.qa.example.invalid',
                'ADDITIONAL_REDIRECT_URLS':'https://auth.qa.example.invalid/callback','SMTP_PASS':'INERT_FIXTURE_NEVER_SENT',
                'SMTP_ADMIN_EMAIL':'fixture@example.invalid','GOTRUE_EXTERNAL_GOOGLE_ENABLED':'false',
                'GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID':'inert','GOTRUE_EXTERNAL_GOOGLE_SECRET':'inert',
                'GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI':'https://supabase.qa.example.invalid/auth/v1/callback'}
        values['GOTRUE_DATABASE_URL']='postgres://supabase_auth_admin:'+values['AUTH_DATABASE_PASSWORD']+'@supabase-db:5432/postgres'
        for name,service in document['services'].items():
            require(service['image']=={'supabase-db':DB_IMAGE,'supabase-auth':AUTH_IMAGE}[name])
            service['restart']='no';service['labels'][LABEL]=self.nonce
            service['labels']['com.xynes.environment']='qa'
            # Disposable fake-credential logs support failure diagnosis only.
            service['logging']={'driver':'json-file','options':{'max-size':'64k','max-file':'1'}}
            for key,value in service['environment'].items():
                match=re.fullmatch(r'\$\{([A-Z_]+):\?required\}',value)
                if match:service['environment'][key]=values[match[1]]
            for attachment in service['networks'].values():
                if 'aliases' in attachment:attachment['aliases']=[alias.replace('${XYNES_ENV}','qa') for alias in attachment['aliases']]
        pg=document['services']['supabase-db']
        pg['volumes']=[{'type':'bind','source':str(self.directory/'auth-bootstrap/bootstrap.sh'),'target':'/docker-entrypoint-initdb.d/10-apex-auth.sh','read_only':True},
                       {'type':'bind','source':str(self.directory/'auth-bootstrap/bootstrap.sql'),'target':'/opt/apex-auth/bootstrap.sql','read_only':True}]
        pg['tmpfs']=['/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=512m,uid=70,gid=70,mode=0700',
                     '/var/run/postgresql:rw,noexec,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700',
                     '/tmp:rw,noexec,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700']
        auth=document['services']['supabase-auth'];auth['environment']['GOTRUE_MAILER_AUTOCONFIRM']='true'
        # Only this disposable fixture enables autoconfirm, avoiding mail delivery.
        document['networks']={'environment':{'name':self.project+'-net','internal':True,'driver':'bridge','labels':{LABEL:self.nonce}}}
        self.document=document;file=self.directory/'compose.json';write(file,json.dumps(document).encode())
        self.compose=['compose','--project-name',self.project,'--file',str(file)]
        self.checked(self.compose+['up','-d','--no-build','--pull','never'],timeout=180)
        self.discover();self.verify_owned()
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            try:
                if type(self.request('/health')) is dict:return values
            except BootstrapError:pass
            time.sleep(.5)
        raise BootstrapError('Auth readiness refused')

    def discover(self):
        ids=self.checked(['ps','-aq','--no-trunc','--filter','label='+LABEL+'='+self.nonce]).splitlines()
        require(len(ids)<=2 and all(CID.fullmatch(cid) for cid in ids));self.containers=ids
        networks=self.checked(['network','ls','-q','--filter','label='+LABEL+'='+self.nonce]).splitlines()
        require(len(networks)<=1)
        if networks:
            rows=records.strict_json(self.checked(['network','inspect',networks[0]]));require(len(rows)==1)
            self.network=rows[0]['Id']

    def verify_owned(self,complete=True):
        require(bool(self.containers) and (not complete or len(self.containers)==2))
        rows=records.strict_json(self.checked(['inspect',*self.containers]));require(len(rows)==len(self.containers))
        allowed={str(self.directory/'auth-bootstrap/bootstrap.sh'):'/docker-entrypoint-initdb.d/10-apex-auth.sh',
                 str(self.directory/'auth-bootstrap/bootstrap.sql'):'/opt/apex-auth/bootstrap.sql'}
        services=set()
        for row in rows:
            labels=row['Config'].get('Labels') or {};service=labels.get('com.docker.compose.service');services.add(service)
            host=row['HostConfig'];require(row['Id'] in self.containers and labels.get(LABEL)==self.nonce
                and labels.get('com.docker.compose.project')==self.project and service in self.image_ids
                and row['Config']['Image']==self.document['services'][service]['image'] and row['Image']==self.image_ids[service]
                and host['Privileged'] is False and host['ReadonlyRootfs'] is True and not host.get('PortBindings')
                and host.get('CapDrop')==['ALL'] and not host.get('CapAdd') and host.get('SecurityOpt')==['no-new-privileges:true']
                and set(row['NetworkSettings']['Networks'])=={self.project+'-net'})
            require(all(mount['Type']=='tmpfs' or (mount['Type']=='bind' and mount.get('Source') in allowed
                        and mount['Destination']==allowed[mount['Source']] and mount.get('RW') is False) for mount in row.get('Mounts',[])))
            if service=='supabase-db':require(row['Config']['User']=='70:70');self.db_cid=row['Id']
            if service=='supabase-auth':self.auth_cid=row['Id']
        if complete:require(services=={'supabase-db','supabase-auth'})
        if self.network:
            row=records.strict_json(self.checked(['network','inspect',self.network]))[0]
            require(row['Id']==self.network and row['Name']==self.project+'-net' and row['Internal'] is True
                    and row['Labels'].get(LABEL)==self.nonce and row['Driver']=='bridge')

    def sql(self,text,timeout=30):
        require(self.db_cid is not None)
        return self.checked(['exec','-i','--user','70:70',self.db_cid,'psql','-X','-At','-v','ON_ERROR_STOP=1',
                             '-h','/var/run/postgresql','-U','postgres','-d','postgres'],input=text,timeout=timeout).strip()

    def request(self,path,data=None):
        require(self.auth_cid is not None and path in ('/health','/signup','/token?grant_type=password','/token?grant_type=refresh_token'))
        args=['exec','-i',self.auth_cid,'wget','--quiet','--output-document=-','--header=Content-Type: application/json']
        if data is not None:args+=['--post-file=/dev/stdin']
        args+=['http://127.0.0.1:9999'+path]
        raw=self.checked(args,input=None if data is None else json.dumps(data),timeout=10)
        require(len(raw)<=65536);return records.strict_json(raw)

    def preserve_logs(self):
        """Read bounded logs of nonce/image-owned containers before cleanup only."""
        self.discover()
        for cid in self.containers:
            rows=records.strict_json(self.checked(['inspect',cid]));require(type(rows) is list and len(rows)==1)
            row=rows[0];labels=row['Config'].get('Labels') or {};service=labels.get('com.docker.compose.service')
            require(row['Id']==cid and labels.get(LABEL)==self.nonce
                    and labels.get('com.docker.compose.project')==self.project and service in self.image_ids
                    and row['Config']['Image']==self.document['services'][service]['image']
                    and row['Image']==self.image_ids[service])
            self.checked(['logs','--tail','50','--timestamps',cid],timeout=10,
                         private_output=self.directory/('owned-'+service+'-logs.txt'))

    def cleanup(self):
        self.discover()
        if self.containers:
            self.verify_owned(complete=False);self.checked(['rm','-f',*self.containers]);self.containers=[]
        if self.network:
            row=records.strict_json(self.checked(['network','inspect',self.network]))[0]
            require(row['Id']==self.network and row['Internal'] is True and row['Labels'].get(LABEL)==self.nonce)
            self.checked(['network','rm',self.network]);self.network=None

    def run(self):
        receipt={'artifact_kind':'local-candidate-hosted-bootstrap-integration','local_fixture':True,
                 'source_approved':False,'deployable':False,'hosted_acceptance':False,'provider_credentials_used':False,
                 'seed_images_executed':False,'seed_acceptance':'deferred-to-admitted-source-image-seed-phase'}
        try:
            self.phase='source-binding'
            previous=dict(os.environ)
            try:
                os.environ.clear();os.environ.update(self.environment)
                manifest,controller,bundle,plan,payloads,original_hash=self.sources()
            finally:os.environ.clear();os.environ.update(previous)
            receipt.update(candidate_source_manifest_sha256=original_hash,fixture_controller=controller,
                           fixture_migration_bundle=bundle,application_sql_units=plan['units'],
                           seed_requirements=plan['seed_requirements'],
                           candidate_auth_sha256={name:hashlib.sha256(raw).hexdigest() for name,raw in self.auth_sources.items()})
            self.phase='image-preflight';self.preload()
            self.phase='db-auth-startup';values=self.start()
            self.phase='auth-prerequisites'
            postgres_version=self.sql('SHOW server_version;');require(postgres_version.split()[0]=='17.10')
            migrations=int(self.sql('SELECT count(*) FROM auth.schema_migrations;'));require(migrations>=70)
            require(self.sql("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='auth' AND p.proname IN ('uid','role','email','jwt');")=='4')
            require(self.sql("SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname='supabase_auth_admin';")=='t')
            batch=migration_sql.render('qa','postgres',plan['units'],payloads)
            write(self.directory/'batch.sql',batch.encode())
            self.phase='actual-40-sql-first-apply';self.sql(batch,timeout=600)
            require(self.sql('SELECT count(*) FROM xynes_release.migrations;')=='40'
                    and self.sql('SELECT completed FROM xynes_release.bootstrap;')=='t')
            self.phase='same-baseline-replay';self.sql(batch,timeout=600)
            require(self.sql('SELECT count(*) FROM xynes_release.migrations;')=='40'
                    and self.sql('SELECT completed FROM xynes_release.bootstrap;')=='t')
            require(self.sql("SELECT count(*) FROM xynes_release.migrations WHERE path='"+migration_plan.DEV_PROVIDER+"';")=='0')
            routes=int(self.sql('SELECT count(*) FROM platform.routes WHERE enabled;'));require(routes>0)
            require(self.sql("SELECT to_regclass('platform.workspaces') IS NOT NULL AND to_regclass('authz.permissions') IS NOT NULL AND to_regclass('cms.content_entries') IS NOT NULL AND to_regclass('docs.documents') IS NOT NULL;")=='t')
            self.phase='post-sql-auth-token-flows'
            email='fixture-'+self.nonce+'@example.invalid';password=secrets.token_hex(20)
            session=self.request('/signup',{'email':email,'password':password})
            def token(session):
                header,claims,signature=session['access_token'].split('.')
                decode=lambda value:base64.urlsafe_b64decode(value+'='*(-len(value)%4))
                require(json.loads(decode(header))['alg']=='HS256'
                        and hmac.compare_digest(decode(signature),hmac.new(values['JWT_SECRET'].encode(),(header+'.'+claims).encode(),hashlib.sha256).digest()))
                data=json.loads(decode(claims));require(data['iss']==values['JWT_ISSUER'] and data['aud']=='authenticated'
                                                      and data['role']=='authenticated' and data['exp']-data['iat']==3600)
            token(session)
            session=self.request('/token?grant_type=password',{'email':email,'password':password});token(session)
            session=self.request('/token?grant_type=refresh_token',{'refresh_token':session['refresh_token']});token(session)
            require(int(self.sql('SELECT count(*) FROM auth.schema_migrations;'))==migrations and self.sql('SELECT count(*) FROM auth.users;')=='1')
            receipt.update(platform='linux/amd64',postgres_version=self.sql('SHOW server_version;'),
                           auth_migrations=migrations,actual_sql_units=40,first_apply_verified=True,replay_verified=True,
                           ledger_complete=True,dev_provider_seed_excluded=True,enabled_routes=routes,
                           hs256_issuer_audience_verified=True,password_and_refresh_after_application_sql_verified=True,
                           candidate_source_manifest_sha256=original_hash,fixture_controller=controller,
                           fixture_migration_bundle=bundle,candidate_auth_sha256={name:hashlib.sha256(raw).hexdigest() for name,raw in self.auth_sources.items()},
                           application_sql_units=plan['units'],seed_requirements=plan['seed_requirements'])
            self.phase='cleanup';self.cleanup();receipt['owned_fixture_cleanup_verified']=True
            write(self.directory/'receipt.json',(json.dumps(receipt,sort_keys=True,indent=2)+'\n').encode())
            print('Local candidate bootstrap fixture passed. Private receipt: '+str(self.directory/'receipt.json'))
            return 0
        except Exception:
            private_error=traceback.format_exc().encode()[:MAX_ERROR]
            write(self.directory/'python-diagnostic.txt',private_error)
            receipt.update(result='refused',phase=self.phase,owned_fixture_project=self.project,
                           diagnostics_private=True,credentials_emitted=False)
            try:
                self.preserve_logs();receipt['owned_logs_preserved_privately']=True
            except Exception:receipt['owned_logs_preserved_privately']=False
            try:
                self.cleanup();receipt['owned_fixture_cleanup_verified']=True
            except Exception:receipt['owned_fixture_cleanup_verified']=False
            write(self.directory/'failure-receipt.json',(json.dumps(receipt,sort_keys=True,indent=2)+'\n').encode())
            print('Local candidate bootstrap fixture refused during '+self.phase+'. Private diagnostics: '+str(self.directory),file=sys.stderr)
            return 1


def main():
    if len(sys.argv)!=1:
        print('Local bootstrap fixture accepts no target, image or configuration arguments.',file=sys.stderr);return 2
    try:return Fixture().run()
    except Exception:
        print('Local candidate bootstrap fixture preparation refused.',file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())
