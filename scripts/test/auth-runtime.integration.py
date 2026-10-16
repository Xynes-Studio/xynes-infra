"""Disposable local amd64 Auth fixture. No provider credentials or hosted targets."""
import base64,hashlib,hmac,json,os,pwd,re,secrets,subprocess,sys,tempfile,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
LOCAL_SOCKET=(Path(pwd.getpwuid(os.getuid()).pw_dir)/'.docker/run/docker.sock' if sys.platform=='darwin' else Path('/var/run/docker.sock'))
nonce=secrets.token_hex(12);project='apex-auth-fixture-'+nonce
phase='preparation';containers=[];network=None
def require(ok):
 if not ok:raise ValueError('isolated Auth fixture refused')
def command(args,input=None,timeout=30):
 return subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout,
  env={**{k:os.environ[k] for k in ('PATH','HOME') if k in os.environ},'DOCKER_HOST':'unix://'+str(LOCAL_SOCKET)})
def checked(args,input=None,timeout=30):
 result=command(args,input,timeout);require(result.returncode==0);return result.stdout
def request(path,data=None,token=None):
 args=['docker','exec','-i',auth_cid,'wget','--quiet','--output-document=-','--header=Content-Type: application/json']
 if token:args.append('--header=Authorization: Bearer '+token)
 if data is not None:args.append('--post-file=/dev/stdin')
 args.append('http://127.0.0.1:9999'+path)
 body=checked(args,None if data is None else json.dumps(data),timeout=10)
 require(len(body)<=65536);return json.loads(body)
def wait_ready():
 deadline=time.monotonic()+100
 while time.monotonic()<deadline:
  try:
   value=request('/health')
   if isinstance(value,dict):return
  except (OSError,ValueError):pass
  time.sleep(.5)
 raise ValueError('isolated Auth fixture readiness refused')
def sql(text):
 return checked(compose+['exec','-T','supabase-db','psql','-X','-At','-v','ON_ERROR_STOP=1','-U','postgres','-d','postgres'],text).strip()
def verify_owned():
 rows=json.loads(checked(['docker','inspect',*containers]));require(len(rows)==2)
 for row in rows:
  labels=row['Config'].get('Labels') or {}
  require(row['Id'] in containers and labels.get('io.xynes.apex.fixture-nonce')==nonce
    and labels.get('com.docker.compose.project')==project and not row['HostConfig']['Privileged'])
  require(set(row['NetworkSettings']['Networks'])=={project+'-net'})
  expected=document['services'][labels['com.docker.compose.service']]['image']
  require(expected == row['Config']['Image'] and not row['HostConfig'].get('PortBindings'))
  allowed={str(ROOT/'infra/release/auth/bootstrap.sh'):'/docker-entrypoint-initdb.d/10-apex-auth.sh',
   str(ROOT/'infra/release/auth/bootstrap.sql'):'/opt/apex-auth/bootstrap.sql'}
  for mount in row.get('Mounts',[]):
   require(mount['Type']=='tmpfs' or (mount['Type']=='bind' and mount.get('Source') in allowed and mount['Destination']==allowed[mount['Source']] and not mount['RW']))
 value=json.loads(checked(['docker','network','inspect',network]))[0]
 require(value['Id']==network and value['Internal'] is True and value['Labels'].get('io.xynes.apex.fixture-nonce')==nonce)

try:
 require(sys.platform=='darwin' or sys.platform.startswith('linux'))
 with tempfile.TemporaryDirectory(prefix='apex-auth-real-') as temporary:
  directory=Path(temporary);directory.chmod(0o700)
  document=json.loads((ROOT/'infra/release/auth/compose.fragment.yml').read_text())
  values={'XYNES_ENV':'qa','POSTGRES_PASSWORD':secrets.token_hex(32),'AUTH_DATABASE_PASSWORD':secrets.token_hex(32),
   'JWT_SECRET':secrets.token_hex(32),'JWT_ISSUER':'https://supabase.qa.example.invalid/auth/v1',
   'API_EXTERNAL_URL':'https://supabase.qa.example.invalid/auth/v1','SITE_URL':'https://auth.qa.example.invalid',
   'ADDITIONAL_REDIRECT_URLS':'https://auth.qa.example.invalid/callback',
   'SMTP_PASS':'INERT_FIXTURE_NOT_SENT','SMTP_ADMIN_EMAIL':'fixture@example.invalid',
   'GOTRUE_EXTERNAL_GOOGLE_ENABLED':'false','GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID':'inert',
   'GOTRUE_EXTERNAL_GOOGLE_SECRET':'inert','GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI':'https://supabase.qa.example.invalid/auth/v1/callback'}
  values['GOTRUE_DATABASE_URL']='postgres://supabase_auth_admin:'+values['AUTH_DATABASE_PASSWORD']+'@supabase-db:5432/postgres'
  for service in document['services'].values():
   service['restart']='no';service['labels']['io.xynes.apex.fixture-nonce']=nonce
   for key,value in service['environment'].items():
    m=re.fullmatch(r'\$\{([A-Z_]+):\?required\}',value)
    if m:service['environment'][key]=values[m[1]]
   service['labels']['com.xynes.environment']='qa'
   for attachment in service['networks'].values():
    if 'aliases' in attachment:attachment['aliases']=[alias.replace('${XYNES_ENV}','qa') for alias in attachment['aliases']]
  pg=document['services']['supabase-db'];pg['user']='70:70';pg['read_only']=True
  pg['cap_drop']=['ALL'];pg['security_opt']=['no-new-privileges:true']
  pg['volumes']=[{'type':'bind','source':str(ROOT/'infra/release/auth/bootstrap.sh'),'target':'/docker-entrypoint-initdb.d/10-apex-auth.sh','read_only':True},
   {'type':'bind','source':str(ROOT/'infra/release/auth/bootstrap.sql'),'target':'/opt/apex-auth/bootstrap.sql','read_only':True}]
  pg['tmpfs']=['/var/lib/postgresql/data:rw,nosuid,nodev,size=256m,uid=70,gid=70,mode=0700',
   '/var/run/postgresql:rw,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700','/tmp:rw,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700']
  auth=document['services']['supabase-auth'];auth['environment']['GOTRUE_MAILER_AUTOCONFIRM']='true'
  # No published fixture ports: requests use exec within the owned Auth container.
  document['networks']={'environment':{'name':project+'-net','internal':True,'labels':{'io.xynes.apex.fixture-nonce':nonce}}}
  file=directory/'compose.json';file.write_text(json.dumps(document));file.chmod(0o600)
  compose=['docker','compose','--project-name',project,'--file',str(file)]
  phase='startup';result=command(compose+['up','-d','--pull','never'],timeout=150)
  # Discover only this nonce/project even when startup failed halfway through.
  ids=checked(['docker','ps','-aq','--no-trunc','--filter','label=io.xynes.apex.fixture-nonce='+nonce]).splitlines()
  containers=ids
  nets=checked(['docker','network','ls','-q','--filter','label=io.xynes.apex.fixture-nonce='+nonce]).splitlines()
  if len(nets)==1:network=json.loads(checked(['docker','network','inspect',nets[0]]))[0]['Id']
  require(result.returncode==0 and len(containers)==2 and network is not None)
  verify_owned()
  rows=json.loads(checked(['docker','inspect',*containers]))
  row=next(r for r in rows if r['Config']['Labels']['com.docker.compose.service']=='supabase-auth')
  auth_cid=row['Id'];require(not row['HostConfig'].get('PortBindings'))
  phase='readiness';wait_ready()
  phase='migrations'
  migration_count=int(sql('SELECT count(*) FROM auth.schema_migrations;'))
  require(migration_count>=60)
  require(sql("SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname='supabase_auth_admin';")=='t')
  require(sql("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='auth' AND p.proname IN ('uid','role','email','jwt');")=='4')
  phase='signup'
  email='fixture-'+nonce+'@example.invalid';password=secrets.token_hex(20)
  session=request('/signup',{'email':email,'password':password})
  def verify_token(session):
   access=session['access_token'];header,claims,signature=access.split('.')
   decode=lambda s:base64.urlsafe_b64decode(s+'='*(-len(s)%4))
   require(json.loads(decode(header))['alg']=='HS256')
   require(hmac.compare_digest(decode(signature),hmac.new(values['JWT_SECRET'].encode(),(header+'.'+claims).encode(),hashlib.sha256).digest()))
   payload=json.loads(decode(claims));require(payload['iss']==values['JWT_ISSUER'] and payload['aud']=='authenticated' and payload['role']=='authenticated' and payload['exp']-payload['iat']==3600)
   require(request('/user',token=access)['email']==email)
  verify_token(session)
  phase='password-login';session=request('/token?grant_type=password',{'email':email,'password':password});verify_token(session)
  phase='refresh';session=request('/token?grant_type=refresh_token',{'refresh_token':session['refresh_token']});verify_token(session)
  phase='restart';checked(compose+['restart','supabase-auth'],timeout=30);wait_ready()
  session=request('/token?grant_type=refresh_token',{'refresh_token':session['refresh_token']});verify_token(session)
  require(int(sql('SELECT count(*) FROM auth.schema_migrations;'))==migration_count)
  require(sql('SELECT count(*) FROM auth.users;')=='1')
  receipt={'artifact_kind':'isolated-local-auth-runtime','platform':'linux/amd64','postgres_version':sql('SHOW server_version;'),
   'auth_migrations':migration_count,'least_privilege_auth_login_verified':True,'hs256_issuer_audience_verified':True,
   'signup_password_login_refresh_verified':True,'auth_restart_retained_session_verified':True,
   'fixture_autoconfirm_only':True,'provider_credentials_used':False,'emails_sent':0,'hosted_acceptance':False,
   'candidate_sha256':{name:hashlib.sha256((ROOT/'infra/release/auth'/name).read_bytes()).hexdigest() for name in ('compose.fragment.yml','bootstrap.sh','bootstrap.sql')}}
  phase='cleanup';verify_owned();checked(['docker','rm','-f',*containers]);containers=[]
  checked(['docker','network','rm',network]);network=None
  descriptor,path=tempfile.mkstemp(prefix='apex-auth-runtime-receipt-',suffix='.json')
  with os.fdopen(descriptor,'w') as output:json.dump(receipt,output,indent=2);output.write('\n');output.flush();os.fsync(output.fileno())
  print(json.dumps(receipt));print('Receipt: '+str(path))
except Exception:
 print(json.dumps({'isolated_auth_fixture':'refused','phase':phase,'credentials_emitted':False,'hosted_acceptance':False}))
 # Exact-owned failed resources retained for inspection; no broad cleanup.
 if containers:print('Owned fixture project retained: '+project)
 raise SystemExit(1)
