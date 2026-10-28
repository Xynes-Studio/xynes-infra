#!/usr/bin/env python3
"""Mandatory HTTPS product flows for a dedicated, protected deployment account.

No signup, invitations or email operations occur. Parent provisions confirmed
example.invalid users, owned primary/nonmember workspaces and stage providers.
Root config: /etc/xynes/deploy/smoke/<stage>.json, mode0600. Detailed evidence
contains no passwords, sessions, HTTP bodies or signed URLs. Injected transports,
configuration/key loaders and clocks are trusted fixture/integration boundaries,
never request fields. Health checks or unavailable flows cannot produce passed.
"""
import base64
import copy
import grp
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import time
import uuid
from urllib.parse import parse_qs,urlsplit
try:
    from . import environment_inputs,github_read,host_compose,release_manifest as records
except ImportError:
    import environment_inputs,github_read,host_compose,release_manifest as records

ERROR='Hosted product smoke refused'
SMOKE_UID=0
MAX_RESPONSE=1024*1024
HTTP_TIMEOUT=15
MAX_HEADERS=16384
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNgYGD4DwABBAEAgLvRWwAAAABJRU5ErkJggg==')
CONFIG_KEYS={'schema_version','stage','account_kind','email','password','user_id','workspace_id','workspace_slug',
             'workspace_owned_for_smoke','forbidden_workspace_id','forbidden_workspace_exists','storage_host','storage_bucket','foreign_probe'}

class SmokeError(ValueError):pass

def require(value):
    if not value:raise SmokeError(ERROR)

def gateway_data(value):
    # Gateway wrapResponse wraps the service's own success envelope: exactly two {ok,data,meta} layers.
    for _ in range(2):
        require(type(value) is dict and value.get('ok') is True and 'data' in value and set(value)<={'ok','data','meta'})
        value=value['data']
    return value

def origins(stage):
    require(stage in ('qa','staging','prod'))
    suffix={'qa':'qa','staging':'staging','prod':'erp'}[stage]+'.xynes.com'
    return {'auth':'https://supabase.'+suffix,'gateway':'https://api.'+suffix,
            'auth_app':'https://auth.'+suffix,'cms_app':'https://cms.'+suffix}

def identifier(value):
    require(type(value) is str and str(uuid.UUID(value))==value)
    return value

def stage_keys(stage):
    require(os.getuid()==SMOKE_UID)
    host_compose.stage_environment(stage)
    raw=host_compose.private_environment(Path('/srv/xynes')/stage/'env'/('.env.'+stage),uid=0,gid=grp.getgrnam('xynes').gr_gid)
    values=environment_inputs.parse_existing(raw,stage)
    return values['JWT_SECRET'],values['JWT_ISSUER']

def jwt(token,stage,user,keys_loader,now,*,allow_expired=False):
    require(type(allow_expired) is bool and type(token) is str and 0<len(token)<=8192)
    parts=token.split('.');require(len(parts)==3 and all(re.fullmatch(r'[A-Za-z0-9_-]+',part) for part in parts))
    decode=lambda part:base64.b64decode(part+'='*(-len(part)%4),altchars=b'-_',validate=True)
    header=records.strict_json(decode(parts[0]).decode());claims=records.strict_json(decode(parts[1]).decode())
    require(type(header) is dict and header.get('alg')=='HS256' and type(claims) is dict)
    secret,issuer=keys_loader(stage)
    require(type(secret) is str and re.fullmatch(r'[a-f0-9]{64}',secret) and issuer==origins(stage)['auth']+'/auth/v1'
            and hmac.compare_digest(decode(parts[2]),hmac.new(secret.encode(),('.'.join(parts[:2])).encode(),hashlib.sha256).digest())
            and claims.get('iss')==issuer and claims.get('aud')=='authenticated' and claims.get('role')=='authenticated'
            and claims.get('sub')==user and type(claims.get('iat')) is int and type(claims.get('exp')) is int
            and 0<=claims['iat']<=now+10 and 0<claims['exp']-claims['iat']<=3610
            and (allow_expired or now<claims['exp']<=now+3610))
    return claims

def mint_foreign_probe(target_stage,foreign_stage,user_id,*,email,keys_loader=stage_keys,clock=time.time):
    """Return memory-only synthetic isolation probe; never claim a GoTrue session."""
    try:
        origins(target_stage);origins(foreign_stage);identifier(user_id);require(target_stage!=foreign_stage)
        require(type(email) is str and re.fullmatch(r'[a-z0-9._+-]+@example\.invalid',email))
        now=int(clock());secret,issuer=keys_loader(foreign_stage)
        require(re.fullmatch(r'[a-f0-9]{64}',secret) and issuer==origins(foreign_stage)['auth']+'/auth/v1')
        claims={'iss':issuer,'aud':'authenticated','role':'authenticated','sub':user_id,'email':email,'iat':now,'exp':now+1800,
                'jti':str(uuid.uuid4()),'app_metadata':{'apex_smoke_probe':'synthetic-stage-isolation','target_stage':target_stage}}
        enc=lambda value:base64.urlsafe_b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=')
        payload=enc({'alg':'HS256','typ':'JWT'})+'.'+enc(claims)
        token=payload+'.'+base64.urlsafe_b64encode(hmac.new(secret.encode(),payload.encode(),hashlib.sha256).digest()).decode().rstrip('=')
        return {'kind':'synthetic-hs256-stage-probe','stage':foreign_stage,'token':token,'claims_sha256':records.canonical_sha256(claims)}
    except Exception:raise SmokeError(ERROR) from None

def read_config(path,stage):
    try:
        require(os.getuid()==SMOKE_UID and isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path)
        host_compose.root_path(path)
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(descriptor,'rb') as file:
            before=os.fstat(file.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid==SMOKE_UID and before.st_nlink==1
                    and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=65536)
            raw=file.read(65537)
            require(len(raw)==before.st_size and host_compose.identity(os.fstat(file.fileno()))==host_compose.identity(before)
                    and host_compose.identity(path.lstat())==host_compose.identity(before))
        value=records.strict_json(raw.decode());require(value['stage']==stage);return value
    except Exception:raise SmokeError(ERROR) from None

def load_config(stage):
    origins(stage)
    return read_config(Path('/etc/xynes/deploy/smoke')/(stage+'.json'),stage)

def qualify(config,stage,keys_loader,now):
    require(type(config) is dict and set(config)==CONFIG_KEYS and type(config['schema_version']) is int
            and config['schema_version']==1 and config['stage']==stage and config['account_kind']=='dedicated-deployment-smoke'
            and type(config['email']) is str and re.fullmatch(r'[a-z0-9._+-]+@example\.invalid',config['email'])
            and type(config['password']) is str and 16<=len(config['password'])<=200
            and all(32<=ord(c)<127 for c in config['password'])
            and config['workspace_owned_for_smoke'] is True and config['forbidden_workspace_exists'] is True
            and type(config['workspace_slug']) is str and re.fullmatch('apex-smoke-'+stage+r'-[a-z0-9-]{1,40}',config['workspace_slug'])
            and re.fullmatch(r'[a-f0-9]{32}\.r2\.cloudflarestorage\.com',config['storage_host'])
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',config['storage_bucket']))
    for key in ('user_id','workspace_id','forbidden_workspace_id'):identifier(config[key])
    require(config['workspace_id']!=config['forbidden_workspace_id'])
    probe=config['foreign_probe']
    require(type(probe) is dict and set(probe)=={'kind','stage','token','claims_sha256'}
            and probe['kind']=='synthetic-hs256-stage-probe' and probe['stage']!=stage)
    claims=jwt(probe['token'],probe['stage'],config['user_id'],keys_loader,now,allow_expired=True)
    require(claims.get('email')==config['email'] and claims.get('app_metadata')=={'apex_smoke_probe':'synthetic-stage-isolation','target_stage':stage}
            and claims.get('jti') and probe['claims_sha256']==records.canonical_sha256(claims))
    identifier(claims['jti']);require(claims['exp']-claims['iat']<=1800)
    # Renew only after verifying the protected original probe's signature and
    # actor/issuer/hash binding. An expired token is identity evidence here,
    # never permission to use an expired real Auth session.
    fresh=copy.deepcopy(config)
    fresh['foreign_probe']=mint_foreign_probe(stage,probe['stage'],config['user_id'],
                            email=config['email'],keys_loader=keys_loader,clock=lambda:now)
    return fresh

class ResponseReader(github_read.DeadlineReader):
    def __init__(self,buffered,sock,deadline):
        super().__init__(buffered,sock,deadline);self.headers=True;self.header_bytes=0

    def readline(self,size):
        # Bound aggregate response headers before HTTPResponse allocates them,
        # then bound chunk-size/trailer lines separately during body framing.
        maximum=MAX_HEADERS if self.headers else 1024
        line=super().readline(min(size,maximum+1))
        require(len(line)<=maximum and (not line or line.endswith(b'\n')))
        if self.headers:
            self.header_bytes+=len(line);require(self.header_bytes<=MAX_HEADERS)
        return line

class HTTPS:
    def __init__(self,*,context=None,connection_factory=None):
        self.context=context or ssl.create_default_context()
        require(isinstance(self.context,ssl.SSLContext) and self.context.check_hostname
                and self.context.verify_mode==ssl.CERT_REQUIRED)
        self.factory=connection_factory

    def request(self,method,url,headers,body):
        connection=None
        try:
            parsed=urlsplit(url)
            require(method in ('GET','POST','PUT','DELETE') and parsed.scheme=='https' and parsed.hostname
                    and parsed.port in (None,443) and not parsed.username and not parsed.password and not parsed.fragment
                    and len(url)<=16384 and not any(ord(c)<33 for c in url) and type(headers) is dict
                    and all(type(k) is str and type(v) is str and '\r' not in k+v and '\n' not in k+v for k,v in headers.items())
                    and (body is None or type(body) is bytes and len(body)<=MAX_RESPONSE))
            deadline=time.monotonic()+HTTP_TIMEOUT
            if self.factory:connection=self.factory(parsed.hostname,443,self.context,HTTP_TIMEOUT)
            else:connection=http.client.HTTPSConnection(parsed.hostname,443,context=self.context,timeout=HTTP_TIMEOUT)
            def deadline_response(sock,*args,**kwargs):
                response=http.client.HTTPResponse(sock,*args,**kwargs)
                response.fp=ResponseReader(response.fp,sock,deadline);return response
            connection.response_class=deadline_response
            if connection.sock is None:connection.connect()
            remaining=deadline-time.monotonic();require(remaining>0);connection.sock.settimeout(remaining)
            connection.request(method,parsed.path+('?' + parsed.query if parsed.query else ''),body=body,headers=headers)
            remaining=deadline-time.monotonic();require(remaining>0);connection.sock.settimeout(remaining)
            response=connection.getresponse();items=response.getheaders();require(len(items)<=100 and not 300<=response.status<400)
            require(sum(len(key)+len(value)+4 for key,value in items)<=MAX_HEADERS)
            critical={'content-length','transfer-encoding','content-encoding','location'}
            for name in critical:require(sum(key.lower()==name for key,_ in items)<=1)
            metadata={key.lower():value for key,value in items}
            require(metadata.get('content-encoding','identity')=='identity')
            length=metadata.get('content-length');transfer=metadata.get('transfer-encoding')
            require(transfer in (None,'chunked') and not (transfer and length is not None))
            if length is not None:require(length.isdigit() and int(length)<=MAX_RESPONSE)
            response.fp.headers=False
            chunks=[];received=0
            while True:
                require(time.monotonic()<deadline)
                chunk=response.read1(min(65536,MAX_RESPONSE+1-received))
                if not chunk:break
                received+=len(chunk);require(received<=MAX_RESPONSE);chunks.append(chunk)
            require(time.monotonic()<deadline)
            return response.status,metadata,b''.join(chunks)
        except Exception:raise SmokeError(ERROR) from None
        finally:
            if connection:connection.close()

class Flow:
    def __init__(self,stage,config,transport,evidence,clock,sleep):
        self.stage=stage;self.config=config;self.transport=transport;self.evidence=evidence;self.clock=clock;self.sleep=sleep
        self.base=origins(stage);self.token=None;self.deadline=time.monotonic()+240;self.resources={};self.phase='login'

    def request(self,label,method,url,body=None,*,token=None,expected=(200,),binary=False,headers=None):
        self.phase=label;require(time.monotonic()<self.deadline)
        supplied={'Content-Type':'application/json'} if body is not None and not binary else {}
        supplied.update(headers or {})
        if token:supplied['Authorization']='Bearer '+token
        raw=body if binary else None if body is None else json.dumps(body,separators=(',',':')).encode()
        status,metadata,data=self.transport.request(method,url,supplied,raw)
        require(type(status) is int and type(metadata) is dict and type(data) is bytes and len(data)<=MAX_RESPONSE)
        self.evidence['steps'].append({'flow':label,'status':status,'observed_at':int(self.clock())})
        require(status in expected)
        if binary:return data
        require(metadata.get('content-type','').split(';')[0]=='application/json')
        return records.strict_json(data.decode())

    def api(self,label,method,path,body=None,*,expected=(200,),token=None):
        return gateway_data(self.request(label,method,self.base['gateway']+path,body,token=self.token if token is None else token,expected=expected))

    def entry(self,value,identifier_value,title,directory,status):
        require(type(value) is dict and type(value.get('entry')) is dict)
        entry=value['entry']
        require(entry.get('id')==identifier_value and entry.get('workspaceId')==self.config['workspace_id']
                and entry.get('title')==title and entry.get('directoryId')==directory and entry.get('status')==status)
        return entry

    def signed(self,url):
        parsed=urlsplit(url)
        # R2 presigned URLs are virtual-hosted: https://<bucket>.<account>.r2.cloudflarestorage.com/<key>.
        require(parsed.scheme=='https' and parsed.hostname==self.config['storage_bucket']+'.'+self.config['storage_host'] and parsed.port is None
                and not parsed.username and not parsed.password and not parsed.fragment
                and parsed.path.startswith('/') and len(parsed.path)>1)
        query=parse_qs(parsed.query,strict_parsing=True)
        require(query.get('X-Amz-Algorithm')==['AWS4-HMAC-SHA256'] and len(query.get('X-Amz-Signature',[]))==1
                and re.fullmatch(r'[a-f0-9]{64}',query['X-Amz-Signature'][0]))
        return url

    def execute(self,keys_loader):
        cfg=self.config;prefix='/workspaces/'+cfg['workspace_id'];name='apex-smoke-'+self.evidence['nonce'];title=name
        session=self.request('auth-login','POST',self.base['auth']+'/auth/v1/token?grant_type=password',{'email':cfg['email'],'password':cfg['password']})
        require(type(session) is dict and session.get('user',{}).get('id')==cfg['user_id'] and session['user'].get('email')==cfg['email']
                and type(session.get('refresh_token')) is str and 0<len(session['refresh_token'])<=8192)
        jwt(session['access_token'],self.stage,cfg['user_id'],keys_loader,int(self.clock()))
        refreshed=self.request('auth-refresh','POST',self.base['auth']+'/auth/v1/token?grant_type=refresh_token',{'refresh_token':session['refresh_token']})
        require(type(refreshed) is dict and refreshed.get('user',{}).get('id')==cfg['user_id'])
        jwt(refreshed['access_token'],self.stage,cfg['user_id'],keys_loader,int(self.clock()));self.token=refreshed['access_token']
        me=self.api('gateway-me','GET','/me')
        require(me.get('user',{}).get('id')==cfg['user_id'] and me['user'].get('email')==cfg['email'])
        memberships=self.api('workspace-membership','GET','/workspaces')
        require(type(memberships.get('workspaces')) is list and any(row.get('id')==cfg['workspace_id'] and row.get('slug')==cfg['workspace_slug']
                and row.get('role')=='workspace_owner' for row in memberships['workspaces']))
        for label,token in [('unauthorized-denial',None),('foreign-stage-denial',cfg['foreign_probe']['token'])]:
            self.request(label,'GET',self.base['gateway']+'/me',token=token,expected=(401,403))
        self.request('nonmember-workspace-denial','GET',self.base['gateway']+'/workspaces/'+cfg['forbidden_workspace_id']+'/content-directories',
                     token=self.token,expected=(401,403))
        directory=self.api('cms-directory-create','POST',prefix+'/content-directories',{'name':name,'parentId':None})
        did=identifier(directory['id']);require(directory.get('name')==name and directory.get('parentId') is None)
        self.resources['directory']=did
        document_body={'title':title,'type':'doc','status':'draft','content':{'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':name}]}]}}
        document=self.api('document-create','POST',prefix+'/documents',document_body,expected=(200,201))
        docid=identifier(document['id']);require(document.get('workspaceId')==cfg['workspace_id'] and document.get('createdBy')==cfg['user_id'] and document.get('title')==title)
        self.resources['document']=docid
        doc=self.api('document-read','GET',prefix+'/documents/'+docid)
        require(doc.get('id')==docid and doc.get('workspaceId')==cfg['workspace_id'] and doc.get('content')==document_body['content'])
        created=self.api('cms-entry-create','POST',prefix+'/content/entries',{'directoryId':did,'title':title,'body':document_body['content']})
        eid=identifier(created['entry']['id']);self.entry(created,eid,title,did,'draft');self.resources['entry']=eid
        self.entry(self.api('cms-entry-read','GET',prefix+'/content/entries/'+eid),eid,title,did,'draft')
        items=self.api('cms-directory-list','GET',prefix+'/content/entries?directoryId='+did+'&limit=20&offset=0')
        require(type(items.get('items')) is list and any(row.get('id')==eid and row.get('directoryId')==did for row in items['items']))
        published=self.entry(self.api('cms-entry-publish','POST',prefix+'/content/entries/'+eid+'/publish',{}),eid,title,did,'published')
        require(type(published.get('publishedAt')) is str and published['publishedAt'])
        self.entry(self.api('cms-published-read','GET',prefix+'/content/entries/'+eid),eid,title,did,'published')
        filename=name+'.png';owner=str(uuid.uuid4())
        upload=self.api('storage-create','POST',prefix+'/storage/uploads',{'operation':'create','filename':filename,'contentType':'image/png',
                        'byteSize':len(PNG),'purpose':'cms_media','visibility':'private','compression':False,'ownerKind':'platform_generic','ownerId':owner})
        oid=identifier(upload['objectId']);uid=identifier(upload['uploadId']);obj=upload.get('object',{})
        require(obj.get('id')==oid and obj.get('workspaceId')==cfg['workspace_id'] and obj.get('filename')==filename and obj.get('createdBy')==cfg['user_id'])
        self.resources.update(object=oid,upload=uid,owner=owner)
        require(upload.get('uploadMethod')=='single' and upload.get('dedupHit',False) is False and type(upload.get('uploadHeaders')) is dict)
        raw_headers=upload['uploadHeaders'];require(len(raw_headers)<=10 and all(type(key) is str and type(value) is str for key,value in raw_headers.items()))
        headers={key.lower():value for key,value in raw_headers.items()};require(len(headers)==len(raw_headers))
        headers.setdefault('content-type','image/png')
        require(headers['content-type']=='image/png' and all(key in {'content-type','content-length','x-amz-checksum-crc32','x-amz-checksum-crc32c','x-amz-checksum-sha256','x-amz-sdk-checksum-algorithm'} for key in headers)
                and headers.get('content-length',str(len(PNG)))==str(len(PNG)))
        self.request('storage-provider-put','PUT',self.signed(upload['uploadUrl']),PNG,binary=True,headers=headers,expected=(200,201,204))
        complete=self.api('storage-complete','POST',prefix+'/storage/uploads/'+uid+'/complete',{'operation':'complete','uploadId':uid})
        require(complete.get('object',{}).get('id')==oid)
        self.resources['completed']=True;deadline=time.monotonic()+120
        while True:
            value=self.api('storage-processing','GET',prefix+'/storage/objects/'+oid+'?operation=get');obj=value.get('object',{})
            require(obj.get('id')==oid and obj.get('workspaceId')==cfg['workspace_id'] and obj.get('filename')==filename and obj.get('status')!='failed')
            if obj.get('status')=='ready':break
            require(time.monotonic()<deadline);self.sleep(2)
        download=self.api('storage-download-url','POST',prefix+'/storage/objects/'+oid+'/download-url',{'operation':'download_url','objectId':oid})
        require(type(download) is dict and set(download)=={'objectId','url','expiresAt'} and download['objectId']==oid)
        returned=self.request('storage-provider-read','GET',self.signed(download['url']),binary=True)
        require(hmac.compare_digest(returned,PNG))

    def cleanup(self):
        if not self.token:return
        prefix='/workspaces/'+self.config['workspace_id']
        if 'upload' in self.resources and not self.resources.get('completed'):
            self.api('owned-upload-abort','POST',prefix+'/storage/uploads/'+self.resources['upload']+'/abort',
                     {'operation':'abort','uploadId':self.resources['upload']})
        if 'object' in self.resources:
            result=self.api('owned-storage-delete','DELETE',prefix+'/storage/objects/'+self.resources['object'],
                       {'operation':'delete','objectId':self.resources['object'],'ownerKind':'platform_generic','ownerId':self.resources['owner']})
            require(result.get('object',{}).get('id')==self.resources['object'] and result['object'].get('status')=='deleted')
        if 'entry' in self.resources:self.api('owned-entry-delete','DELETE',prefix+'/content/entries/'+self.resources['entry'])
        if 'directory' in self.resources:self.api('owned-directory-delete','DELETE',prefix+'/content-directories/'+self.resources['directory'])
        # Pinned gateway exposes no document-delete API. Retain the exact owned
        # synthetic document ID privately for coordinator-owned maintenance.
        self.evidence['owned_document_retained']=self.resources.get('document')

def run(*,stage,publication,source_sha256,work,transport=None,config_loader=load_config,keys_loader=stage_keys,clock=time.time,sleep=time.sleep):
    evidence=None;flow=None;passed=False
    try:
        origins(stage);require(os.getuid()==SMOKE_UID and isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True)==work)
        info=work.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==SMOKE_UID and stat.S_IMODE(info.st_mode)==0o700)
        require(type(publication) is dict and set(publication)=={'id','digest'} and type(publication['id']) is int
                and 0<publication['id']<=9007199254740991 and re.fullmatch(r'sha256:[a-f0-9]{64}',publication['digest'])
                and type(source_sha256) is str and re.fullmatch(r'sha256:[a-f0-9]{64}',source_sha256))
        config=qualify(config_loader(stage),stage,keys_loader,int(clock()))
        evidence={'artifact_kind':'private-hosted-product-smoke','stage':stage,'publication':copy.deepcopy(publication),
                  'source_sha256':source_sha256,'nonce':secrets.token_hex(16),'steps':[],
                  'foreign_probe_kind':'synthetic-hs256-stage-probe','foreign_probe_stage':config['foreign_probe']['stage'],
                  'sibling_gotrue_session_claimed':False,'original_foreign_probe_signature_verified':True,
                  'synthetic_foreign_probe_refreshed':True,'emails_sent':0,'result':'refused'}
        flow=Flow(stage,config,transport or HTTPS(),evidence,clock,sleep);flow.execute(keys_loader);flow.cleanup();passed=True
        evidence.update(result='passed',resources=copy.deepcopy(flow.resources),observed_at=int(clock()))
        return {'stage':stage,'publication':copy.deepcopy(publication),'source_sha256':source_sha256,'result':'passed','observed_at':evidence['observed_at']}
    except Exception:raise SmokeError(ERROR) from None
    finally:
        if evidence is not None:
            if not passed and flow is not None:
                evidence['failed_phase']=flow.phase
                try:flow.cleanup();evidence['owned_cleanup_verified']=True
                except Exception:evidence['owned_cleanup_verified']=False
                evidence['resources']=copy.deepcopy(flow.resources)
            path=work/('smoke-evidence-'+evidence['nonce']+'.json');before=None
            try:
                descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
                with os.fdopen(descriptor,'w') as file:
                    before=os.fstat(file.fileno());os.fchmod(file.fileno(),0o600)
                    json.dump(evidence,file,sort_keys=True);file.write('\n');file.flush();os.fsync(file.fileno())
            except Exception:
                if before is not None:
                    try:
                        if os.path.samestat(before,path.lstat()):path.unlink()
                    except OSError:pass
                raise SmokeError(ERROR) from None

class Smoke:
    """Fixed adapter for root_runtime product_smoke(assets, phase=...)."""
    def __init__(self,stage):
        origins(stage);self.stage=stage

    def run(self,assets,*,phase):
        try:
            require(phase in ('current','rollback') and assets.prepared.stage==self.stage)
            source='sha256:'+assets.migration.source_manifest_sha256
            return run(stage=self.stage,publication=assets.publication,source_sha256=source,
                       work=Path('/var/lib/xynes/deploy/evidence')/self.stage)
        except Exception:raise SmokeError(ERROR) from None
