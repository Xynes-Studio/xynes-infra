#!/usr/bin/env python3
"""Confirmed, dedicated example.invalid product fixtures; no invitations/email.

Parent invokes after admitted services/migrations/seeds start, under its host
lock. This is provisioning only, never a product acceptance observation. HTTP
uses host_smoke's verified TLS/absolute bounds. Fixed root files preserve intent
before remote writes and retain known IDs across interruptions. No delete,
password reset, arbitrary resource adoption or customer-data access exists.
Injected stores/transports/loaders are trusted local-test boundaries only.
"""
import fcntl
import grp
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
try:
    from . import environment_inputs,host_compose,host_smoke as smoke,release_manifest as records
except ImportError:
    import environment_inputs,host_compose,host_smoke as smoke,release_manifest as records

ERROR='Hosted smoke fixture setup refused'
ROOT_UID=0
DIRECTORY=Path('/etc/xynes/deploy/smoke')
ROLES=('primary','secondary')
ACCOUNT_KEYS={'email','password','user_id','user_intent','workspace_id','workspace_intent'}

class SetupError(ValueError):pass

def require(value):
    if not value:raise SetupError(ERROR)

def stage_values(stage):
    smoke.origins(stage);require(os.getuid()==ROOT_UID)
    host_compose.stage_environment(stage)
    path=Path('/srv/xynes')/stage/'env'/('.env.'+stage)
    raw=host_compose.private_environment(path,uid=0,gid=grp.getgrnam('xynes').gr_gid)
    values=environment_inputs.parse_existing(raw,stage)
    return {key:values[key] for key in ('SUPABASE_SERVICE_ROLE_KEY','R2_ACCOUNT_ID','R2_BUCKET')}

class Store:
    def __init__(self,directory=DIRECTORY):self.directory=directory;self.lock=None
    def __enter__(self):
        require(os.getuid()==ROOT_UID and self.directory.is_absolute())
        # Create only protected ancestors; reject links and nonroot writable paths.
        for path in reversed((self.directory,*tuple(self.directory.parents)[:-1])):
            if not path.exists():
                host_compose.root_path(path.parent,directory=True);path.mkdir(mode=0o700)
            host_compose.root_path(path,directory=True)
        require(stat.S_IMODE(self.directory.stat().st_mode)==0o700)
        path=self.directory/'setup.lock'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,0o600)
        try:
            self.check(os.fstat(fd));host_compose.root_path(path)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);self.lock=fd
        except BaseException:os.close(fd);raise
        return self
    def __exit__(self,*args):
        if self.lock is not None:os.close(self.lock);self.lock=None
    def check(self,info):
        require(stat.S_ISREG(info.st_mode) and info.st_uid==ROOT_UID and info.st_nlink==1
                and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=65536)
    def path(self,name):
        require(re.fullmatch(r'(qa|staging|prod)(\.setup)?\.json',name));return self.directory/name
    def read(self,name):
        path=self.path(name)
        try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        except FileNotFoundError:
            require(not path.is_symlink());return None
        with os.fdopen(fd,'rb') as file:
            before=os.fstat(file.fileno());self.check(before);host_compose.root_path(path)
            raw=file.read(65537)
            require(0<len(raw)==before.st_size and host_compose.identity(os.fstat(file.fileno()))==host_compose.identity(before)
                    and host_compose.identity(path.lstat())==host_compose.identity(before))
        return records.strict_json(raw.decode())
    def write(self,name,value):
        require(self.lock is not None)
        path=self.path(name);self.read(name)  # Refuse an existing unsafe leaf before replacement.
        raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode();require(0<len(raw)<=65536)
        temporary=self.directory/('.setup-'+secrets.token_hex(16));owned=None
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            with os.fdopen(fd,'wb') as file:
                owned=os.fstat(file.fileno());self.check(owned);file.write(raw);file.flush();os.fsync(file.fileno())
            require(temporary.lstat().st_ino==owned.st_ino and temporary.lstat().st_dev==owned.st_dev)
            os.replace(temporary,path)
            directory=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if owned is not None and temporary.exists():
                actual=temporary.lstat()
                if actual.st_ino==owned.st_ino and actual.st_dev==owned.st_dev:temporary.unlink()


def new_journal(stage):
    nonce=secrets.token_hex(16)
    return {'schema_version':1,'stage':stage,'nonce':nonce,'accounts':{
        role:{'email':'apex-smoke-'+stage+'-'+nonce+'-'+role+'@example.invalid',
              'password':secrets.token_urlsafe(48),'user_id':None,'user_intent':False,
              'workspace_id':None,'workspace_intent':False} for role in ROLES}}

def qualify_journal(value,stage):
    require(type(value) is dict and set(value)=={'schema_version','stage','nonce','accounts'}
            and type(value['schema_version']) is int and value['schema_version']==1 and value['stage']==stage
            and type(value['nonce']) is str and re.fullmatch(r'[a-f0-9]{32}',value['nonce'])
            and type(value['accounts']) is dict and set(value['accounts'])==set(ROLES))
    for role in ROLES:
        row=value['accounts'][role]
        require(type(row) is dict and set(row)==ACCOUNT_KEYS and row['email']=='apex-smoke-'+stage+'-'+value['nonce']+'-'+role+'@example.invalid'
                and type(row['password']) is str and re.fullmatch(r'[A-Za-z0-9_-]{64}',row['password'])
                and type(row['user_intent']) is bool and type(row['workspace_intent']) is bool)
        for key in ('user_id','workspace_id'):
            if row[key] is not None:smoke.identifier(row[key])
        require((row['user_id'] is None or row['user_intent']) and (not row['workspace_intent'] or row['user_id'] is not None)
                and (row['workspace_id'] is None or row['workspace_intent']))
    for key in ('email','password','user_id','workspace_id'):
        a,b=(value['accounts'][role][key] for role in ROLES);require(a is None or b is None or a!=b)
    return value

class Provision:
    def __init__(self,stage,store,journal,transport,values,keys_loader,clock):
        self.stage=stage;self.store=store;self.journal=journal;self.transport=transport;self.values=values
        self.keys_loader=keys_loader;self.clock=clock;self.base=smoke.origins(stage);self.deadline=time.monotonic()+240
    def save(self):
        qualify_journal(self.journal,self.stage);self.store.write(self.stage+'.setup.json',self.journal)
    def request(self,method,url,body=None,*,token,expected=(200,)):
        require(time.monotonic()<self.deadline)
        headers={'Authorization':'Bearer '+token}
        if body is not None:headers['Content-Type']='application/json'
        raw=None if body is None else json.dumps(body,separators=(',',':')).encode()
        status,metadata,data=self.transport.request(method,url,headers,raw)
        require(type(status) is int and status in expected and type(metadata) is dict
                and metadata.get('content-type','').split(';')[0]=='application/json'
                and type(data) is bytes and len(data)<=smoke.MAX_RESPONSE and time.monotonic()<self.deadline)
        value=records.strict_json(data.decode());require(type(value) is dict);return status,value
    def api(self,method,path,token,body=None):
        _,value=self.request(method,self.base['gateway']+path,body,token=token,expected=(200,201))
        return smoke.gateway_data(value)
    def marker(self,role):return {'version':1,'stage':self.stage,'nonce':self.journal['nonce'],'identity':role}
    def user(self,role):
        row=self.journal['accounts'][role];session=None
        if row['user_intent']:
            status,session=self.login(row,expected=(200,400))
            if status!=200:require(row['user_id'] is None);session=None
        else:
            row['user_intent']=True;self.save()
        if session is None:
            _,user=self.request('POST',self.base['auth']+'/auth/v1/admin/users',
                {'email':row['email'],'password':row['password'],'email_confirm':True,
                 'app_metadata':{'apex_smoke_fixture':self.marker(role)}},token=self.values['SUPABASE_SERVICE_ROLE_KEY'],expected=(200,201))
            self.check_user(user,role)
            row['user_id']=smoke.identifier(user['id']);self.save()
            _,session=self.login(row)
        require(type(session.get('user')) is dict);self.check_user(session['user'],role)
        uid=smoke.identifier(session['user']['id']);require(row['user_id'] in (None,uid))
        _,user=self.request('GET',self.base['auth']+'/auth/v1/admin/users/'+uid,token=self.values['SUPABASE_SERVICE_ROLE_KEY'])
        self.check_user(user,role);require(user['id']==uid)
        smoke.jwt(session['access_token'],self.stage,uid,self.keys_loader,int(self.clock()))
        require(type(session.get('refresh_token')) is str and 0<len(session['refresh_token'])<=8192)
        if row['user_id'] is None:row['user_id']=uid;self.save()
        token=session['access_token'];me=self.api('GET','/me',token)
        require(type(me) is dict and me.get('user',{}).get('id')==uid and me['user'].get('email')==row['email'])
        return token
    def check_user(self,user,role):
        row=self.journal['accounts'][role]
        require(type(user) is dict and user.get('email')==row['email'] and type(user.get('app_metadata')) is dict
                and user['app_metadata'].get('apex_smoke_fixture')==self.marker(role)
                and type(user.get('email_confirmed_at')) is str and len(user['email_confirmed_at'])>0)
        smoke.identifier(user['id']);require(row['user_id'] in (None,user['id']))
    def login(self,row,expected=(200,)):
        return self.request('POST',self.base['auth']+'/auth/v1/token?grant_type=password',
                            {'email':row['email'],'password':row['password']},token=self.values['SUPABASE_SERVICE_ROLE_KEY'],expected=expected)
    def slug(self,role):return 'apex-smoke-'+self.stage+'-'+self.journal['nonce']+'-'+role[0]
    def memberships(self,role,token):
        value=self.api('GET','/workspaces',token);require(type(value) is dict and type(value.get('workspaces')) is list)
        rows=value['workspaces'];require(len(rows)<=1)
        for row in rows:
            require(type(row) is dict and row.get('slug')==self.slug(role) and row.get('name')==self.slug(role)
                    and row.get('role')=='workspace_owner')
            smoke.identifier(row['id'])
        return rows
    def workspace(self,role,token):
        account=self.journal['accounts'][role];rows=self.memberships(role,token)
        if rows:
            require(account['workspace_intent'] and account['workspace_id'] in (None,rows[0]['id']))
            if account['workspace_id'] is None:account['workspace_id']=rows[0]['id'];self.save()
        else:
            require(account['workspace_id'] is None)
            account['workspace_intent']=True;self.save()
            result=self.api('POST','/workspaces',token,{'name':self.slug(role),'slug':self.slug(role)})
            require(type(result) is dict and result.get('name')==self.slug(role) and result.get('slug')==self.slug(role)
                    and result.get('createdBy')==account['user_id'])
            account['workspace_id']=smoke.identifier(result['id']);self.save()
        rows=self.memberships(role,token);require(len(rows)==1 and rows[0]['id']==account['workspace_id'])
    def run(self):
        tokens={}
        for role in ROLES:
            tokens[role]=self.user(role);self.workspace(role,tokens[role])
        primary,secondary=(self.journal['accounts'][role] for role in ROLES)
        require(primary['workspace_id']!=secondary['workspace_id'] and primary['user_id']!=secondary['user_id'])
        for role,other in [('primary','secondary'),('secondary','primary')]:
            foreign=self.journal['accounts'][other]['workspace_id']
            self.request('GET',self.base['gateway']+'/workspaces/'+foreign+'/content-directories',token=tokens[role],expected=(401,403))
        config=fixture_config(self.stage,self.journal,self.values,self.keys_loader,self.clock)
        existing=self.store.read(self.stage+'.json')
        if existing is not None:check_existing(existing,config,self.stage,self.keys_loader,self.clock)
        else:self.store.write(self.stage+'.json',config)
        return {'stage':self.stage,'configured':True,'user_id':primary['user_id'],
                'workspace_id':primary['workspace_id'],'forbidden_workspace_id':secondary['workspace_id']}


def fixture_config(stage,journal,values,keys_loader,clock):
    primary,secondary=(journal['accounts'][role] for role in ROLES)
    foreign_stage={'qa':'staging','staging':'qa','prod':'staging'}[stage]
    config={'schema_version':1,'stage':stage,'account_kind':'dedicated-deployment-smoke',
            'email':primary['email'],'password':primary['password'],'user_id':primary['user_id'],
            'workspace_id':primary['workspace_id'],'workspace_slug':'apex-smoke-'+stage+'-'+journal['nonce']+'-p',
            'workspace_owned_for_smoke':True,'forbidden_workspace_id':secondary['workspace_id'],'forbidden_workspace_exists':True,
            'storage_host':values['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com','storage_bucket':values['R2_BUCKET'],
            'foreign_probe':smoke.mint_foreign_probe(stage,foreign_stage,primary['user_id'],email=primary['email'],keys_loader=keys_loader,clock=clock)}
    smoke.qualify(config,stage,keys_loader,int(clock()));return config

def check_existing(existing,expected,stage,keys_loader,clock):
    smoke.qualify(existing,stage,keys_loader,int(clock()))
    require({key:value for key,value in existing.items() if key!='foreign_probe'}=={key:value for key,value in expected.items() if key!='foreign_probe'}
            and existing['foreign_probe']['stage']==expected['foreign_probe']['stage'])


def ensure(stage,*,transport=None,environment_loader=stage_values,keys_loader=smoke.stage_keys,clock=time.time,store=None):
    try:
        smoke.origins(stage)
        with (store or Store()) as protected:
            journal=protected.read(stage+'.setup.json');existing=protected.read(stage+'.json')
            if journal is None:
                require(existing is None);journal=new_journal(stage);protected.write(stage+'.setup.json',journal)
            qualify_journal(journal,stage)
            values=environment_loader(stage)
            require(type(values) is dict and set(values)=={'SUPABASE_SERVICE_ROLE_KEY','R2_ACCOUNT_ID','R2_BUCKET'}
                    and type(values['SUPABASE_SERVICE_ROLE_KEY']) is str and 0<len(values['SUPABASE_SERVICE_ROLE_KEY'])<=8192
                    and re.fullmatch(r'[a-f0-9]{32}',values['R2_ACCOUNT_ID'])
                    and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',values['R2_BUCKET']))
            if existing is not None:
                expected=fixture_config(stage,journal,values,keys_loader,clock)
                check_existing(existing,expected,stage,keys_loader,clock)
            return Provision(stage,protected,journal,transport or smoke.HTTPS(),values,keys_loader,clock).run()
    except Exception:raise SetupError(ERROR) from None

class Setup:
    def __init__(self,stage):smoke.origins(stage);self.stage=stage
    def run(self):return ensure(self.stage)


# This hook is coordinator-only after Setup.run(), under the existing deploy
# lock. The retained setup journal and database both prove fixture ownership.
# It adds only one R2 pointer row; it never creates a bucket, handles R2 secrets,
# changes an existing row, or confers product/storage acceptance.
STORAGE_PROVIDER_SQL="""BEGIN ISOLATION LEVEL READ COMMITTED;
SET LOCAL search_path=pg_catalog;
SET LOCAL statement_timeout='15s';
SET LOCAL lock_timeout='3s';
DO $apex_provider$
DECLARE input jsonb := __PAYLOAD__; fixture_role text; account jsonb; provider_count integer;
BEGIN
 IF current_database() <> 'postgres' OR current_user <> 'postgres' OR pg_is_in_recovery() THEN
  RAISE EXCEPTION 'Smoke provider target refused';
 END IF;
 PERFORM id FROM platform.workspaces
  WHERE id IN ((input->'primary'->>'workspace_id')::uuid,(input->'secondary'->>'workspace_id')::uuid)
  ORDER BY id FOR UPDATE;
 PERFORM id FROM identity.users
  WHERE id IN ((input->'primary'->>'user_id')::uuid,(input->'secondary'->>'user_id')::uuid)
  ORDER BY id FOR SHARE;
 PERFORM id FROM auth.users
  WHERE id IN ((input->'primary'->>'user_id')::uuid,(input->'secondary'->>'user_id')::uuid)
  ORDER BY id FOR SHARE;
 PERFORM user_id FROM platform.workspace_members
  WHERE workspace_id IN ((input->'primary'->>'workspace_id')::uuid,(input->'secondary'->>'workspace_id')::uuid)
  ORDER BY workspace_id,user_id FOR SHARE;
 PERFORM user_id FROM authz.user_roles
  WHERE workspace_id IN ((input->'primary'->>'workspace_id')::uuid,(input->'secondary'->>'workspace_id')::uuid)
   AND role_key='workspace_owner'
  ORDER BY workspace_id,user_id FOR SHARE;
 FOREACH fixture_role IN ARRAY ARRAY['primary','secondary'] LOOP
  account := input->fixture_role;
  IF NOT EXISTS (
   SELECT 1 FROM platform.workspaces w
    JOIN identity.users i ON i.id=w.created_by
    JOIN auth.users a ON a.id=i.id
    JOIN platform.workspace_members member ON member.workspace_id=w.id AND member.user_id=i.id
    JOIN authz.user_roles owner_role ON owner_role.workspace_id=w.id AND owner_role.user_id=i.id AND owner_role.role_key='workspace_owner'
   WHERE w.id=(account->>'workspace_id')::uuid AND w.created_by=(account->>'user_id')::uuid
    AND w.slug=account->>'slug' AND w.name=account->>'slug'
    AND i.email=account->>'email' AND a.email=account->>'email'
    AND a.email_confirmed_at IS NOT NULL AND member.status='active'
    AND a.raw_app_meta_data->'apex_smoke_fixture'=jsonb_build_object(
     'version',1,'stage',input->>'stage','nonce',input->>'nonce','identity',fixture_role)
  ) THEN RAISE EXCEPTION 'Smoke provider ownership refused'; END IF;
 END LOOP;
 IF EXISTS (SELECT 1 FROM platform.workspace_members
  WHERE workspace_id=(input->'secondary'->>'workspace_id')::uuid
   AND user_id=(input->'primary'->>'user_id')::uuid AND status='active')
 THEN RAISE EXCEPTION 'Smoke provider foreign membership refused'; END IF;
 -- The immediate workspace FK makes every insert/move-in acquire KEY SHARE
 -- on the parent, conflicting with the FOR UPDATE above. Existing provider
 -- rows also need locks because non-key updates can avoid the FK parent lock.
 -- These are separate READ COMMITTED statements: re-read after waiting.
 PERFORM id FROM platform.workspace_storage_providers
  WHERE workspace_id=(input->'primary'->>'workspace_id')::uuid FOR UPDATE;
 SELECT count(*) INTO provider_count FROM platform.workspace_storage_providers
  WHERE workspace_id=(input->'primary'->>'workspace_id')::uuid;
 IF provider_count=0 THEN
  INSERT INTO platform.workspace_storage_providers
   (workspace_id,provider_kind,display_name,bucket,region,endpoint,credential_ref,status,is_default,created_by)
  VALUES ((input->'primary'->>'workspace_id')::uuid,'r2',input->>'display_name',input->>'bucket',
   input->>'region',input->>'endpoint',input->>'credential_ref','active',true,(input->'primary'->>'user_id')::uuid);
 END IF;
 IF (SELECT count(*) FROM platform.workspace_storage_providers
      WHERE workspace_id=(input->'primary'->>'workspace_id')::uuid) <> 1
  OR NOT EXISTS (SELECT 1 FROM platform.workspace_storage_providers
   WHERE workspace_id=(input->'primary'->>'workspace_id')::uuid AND provider_kind='r2'
    AND display_name=input->>'display_name' AND bucket=input->>'bucket' AND region=input->>'region'
    AND endpoint=input->>'endpoint' AND credential_ref=input->>'credential_ref'
    AND status='active' AND is_default IS TRUE AND created_by=(input->'primary'->>'user_id')::uuid)
 THEN RAISE EXCEPTION 'Smoke provider conflict refused'; END IF;
END $apex_provider$;
SELECT json_build_object('stage',(__PAYLOAD__)->>'stage','storage_provider_ready',true);
COMMIT;
"""

def storage_provider_values(stage):
    require(stage in ('staging','prod') and os.getuid()==ROOT_UID)
    host_compose.stage_environment(stage)
    path=Path('/srv/xynes')/stage/'env'/('.env.'+stage)
    raw=host_compose.private_environment(path,uid=0,gid=grp.getgrnam('xynes').gr_gid)
    values=environment_inputs.parse_existing(raw,stage)
    return {key:values[key] for key in ('R2_ACCOUNT_ID','R2_BUCKET','R2_REGION')}

def storage_provider_payload(stage,setup,journal,values):
    require(stage in ('staging','prod'));qualify_journal(journal,stage)
    require(type(setup) is dict and set(setup)=={'stage','configured','user_id','workspace_id','forbidden_workspace_id'}
            and setup['stage']==stage and setup['configured'] is True)
    primary,secondary=(journal['accounts'][role] for role in ROLES)
    require(all(row['workspace_intent'] and row['user_intent'] and row['user_id'] is not None and row['workspace_id'] is not None for row in (primary,secondary)))
    require(setup['user_id']==primary['user_id'] and setup['workspace_id']==primary['workspace_id']
            and setup['forbidden_workspace_id']==secondary['workspace_id'])
    require(type(values) is dict and set(values)=={'R2_ACCOUNT_ID','R2_BUCKET','R2_REGION'}
            and type(values['R2_ACCOUNT_ID']) is str and re.fullmatch(r'[a-f0-9]{32}',values['R2_ACCOUNT_ID'])
            and type(values['R2_BUCKET']) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',values['R2_BUCKET'])
            and values['R2_REGION']=='auto')
    accounts={role:{'user_id':journal['accounts'][role]['user_id'],'workspace_id':journal['accounts'][role]['workspace_id'],
                   'email':journal['accounts'][role]['email'],'slug':'apex-smoke-'+stage+'-'+journal['nonce']+'-'+role[0]} for role in ROLES}
    return {'stage':stage,'nonce':journal['nonce'],**accounts,'bucket':values['R2_BUCKET'],'region':'auto',
            'endpoint':'https://'+values['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',
            'credential_ref':'secret://xynes/storage/r2-'+stage,'display_name':'Apex deployment smoke '+stage}

def storage_provider_sql(payload):
    # The sole substitution is bounded JSON DATA encoded as hex. No identifier,
    # SQL fragment or quoted string is supplied by a request/environment value.
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    require(0<len(raw)<=4096 and STORAGE_PROVIDER_SQL.count('__PAYLOAD__')==2)
    expression="convert_from(decode('"+raw.hex()+"','hex'),'UTF8')::jsonb"
    return STORAGE_PROVIDER_SQL.replace('__PAYLOAD__',expression).encode()

def storage_provider_execute(stage,sql):
    try:
        from . import host_migrate,pg_container_bridge
    except ImportError:
        import host_migrate,pg_container_bridge
    require(stage in ('staging','prod') and os.geteuid()==ROOT_UID)
    values=pg_container_bridge.stage_environment(stage)
    require(values['XYNES_ENV']==stage)
    container=pg_container_bridge.inspected_container(stage)
    result=host_migrate._execute(container,values['POSTGRES_PASSWORD'],sql=sql)
    require(pg_container_bridge.inspected_container(stage)==container)
    return result

def ensure_storage_provider(stage,setup,*,store=None,values_loader=storage_provider_values,executor=storage_provider_execute):
    try:
        smoke.origins(stage)
        if stage=='qa':return {'stage':'qa','storage_provider_ready':False}
        require(stage in ('staging','prod'))
        with (store or Store()) as protected:
            journal=protected.read(stage+'.setup.json')
            payload=storage_provider_payload(stage,setup,journal,values_loader(stage))
            raw=executor(stage,storage_provider_sql(payload))
            require(type(raw) is bytes and 0<len(raw)<=1024)
            result=records.strict_json(raw.decode())
            require(type(result) is dict and set(result)=={'stage','storage_provider_ready'}
                    and result['stage']==stage and result['storage_provider_ready'] is True)
            return result
    except Exception:raise SetupError(ERROR) from None
