#!/usr/bin/env python3
"""Real verified TLS/HTTP on Unix socket pairs; no provider or hosted target."""
import base64
import copy
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import struct
import zlib
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))

class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/host_smoke.py';self.assertTrue(path.exists(),'required product smoke implementation missing')
        spec=importlib.util.spec_from_file_location('host_smoke',path);self.module=importlib.util.module_from_spec(spec)
        sys.modules['host_smoke']=self.module;spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory(prefix='apex-product-tls-');self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name).resolve();self.work.chmod(0o700)
        self.owner=patch.object(self.module,'SMOKE_UID',os.getuid());self.owner.start();self.addCleanup(self.owner.stop)
        self.now=1700000000;self.user='11111111-1111-4111-8111-111111111111';self.workspace='22222222-2222-4222-8222-222222222222'
        self.forbidden='33333333-3333-4333-8333-333333333333';self.directory='44444444-4444-4444-8444-444444444444'
        self.entry='55555555-5555-4555-8555-555555555555';self.document='66666666-6666-4666-8666-666666666666'
        self.object='77777777-7777-4777-8777-777777777777';self.upload='88888888-8888-4888-8888-888888888888'
        self.storage_host='a'*32+'.r2.cloudflarestorage.com';self.bucket='apex-qa-smoke';self.virtual=self.bucket+'.'+self.storage_host;self.secrets={'qa':'a'*64,'staging':'b'*64,'prod':'c'*64}
        self.keys=lambda stage:(self.secrets[stage],self.module.origins(stage)['auth']+'/auth/v1')
        self.current=self.jwt('qa');self.foreign=self.module.mint_foreign_probe('qa','staging',self.user,email='apex-smoke-qa@example.invalid',keys_loader=self.keys,clock=lambda:self.now)
        self.config={'schema_version':1,'stage':'qa','account_kind':'dedicated-deployment-smoke',
            'email':'apex-smoke-qa@example.invalid','password':'INERT_FIXTURE_PASSWORD_123456','user_id':self.user,
            'workspace_id':self.workspace,'workspace_slug':'apex-smoke-qa-fixture','workspace_owned_for_smoke':True,
            'forbidden_workspace_id':self.forbidden,'forbidden_workspace_exists':True,
            'storage_host':self.storage_host,'storage_bucket':self.bucket,'foreign_probe':self.foreign}
        self.publication={'id':123,'digest':'sha256:'+'d'*64};self.source='sha256:'+'e'*64
        self.calls=[];self.state={};self.mode=None;self.threads=[]
        certificate=self.work/'certificate.pem';key=self.work/'key.pem'
        hosts=['api.qa.xynes.com','supabase.qa.xynes.com',self.storage_host,self.virtual]
        result=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(certificate),
                               '-days','1','-subj','/CN=apex-local-fixture','-addext','subjectAltName='+','.join('DNS:'+h for h in hosts)],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,'TLS fixture certificate preparation refused');key.chmod(0o600)
        self.server_ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.server_ctx.load_cert_chain(certificate,key)
        self.client_ctx=ssl.create_default_context(cafile=str(certificate))
        self.transport=self.module.HTTPS(context=self.client_ctx,connection_factory=self.connection)

    def jwt(self,stage):
        enc=lambda obj:base64.urlsafe_b64encode(json.dumps(obj,separators=(',',':')).encode()).decode().rstrip('=')
        head=enc({'alg':'HS256','typ':'JWT'});body=enc({'iss':self.keys(stage)[1],'aud':'authenticated','role':'authenticated',
              'sub':self.user,'iat':self.now,'exp':self.now+3600})
        sign=base64.urlsafe_b64encode(hmac.new(self.secrets[stage].encode(),(head+'.'+body).encode(),hashlib.sha256).digest()).decode().rstrip('=')
        return head+'.'+body+'.'+sign

    def connection(self,host,port,context,timeout):
        client,server=socket.socketpair();parent=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def do_PUT(self):self.respond()
            def do_DELETE(self):self.respond()
            def respond(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                headers={k.lower():v for k,v in self.headers.items()}
                status,data,kind=parent.handle(host,self.command,self.path,headers,raw)
                self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers();self.wfile.write(data)
        def serve():
            try:
                wrapped=parent.server_ctx.wrap_socket(server,server_side=True)
                try:Handler(wrapped,('local',0),object())
                finally:wrapped.close()
            except Exception:server.close()
        thread=threading.Thread(target=serve,daemon=True);thread.start();self.threads.append(thread)
        connection=http.client.HTTPSConnection(host,port,context=context,timeout=timeout)
        connection.sock=context.wrap_socket(client,server_hostname=host)
        return connection

    def handle(self,host,method,path,headers,raw):
        self.calls.append((host,method,path,headers,raw));body=json.loads(raw) if raw and headers.get('content-type')=='application/json' else None
        def response(value,status=200):return status,json.dumps(value).encode(),'application/json'
        # Gateway wrapResponse wraps the service's own {ok,data,meta} success envelope.
        def envelope(value,status=200):
            if self.mode=='single-envelope':return response({'ok':True,'data':value},status)
            return response({'ok':True,'data':{'ok':True,'data':value,'meta':{'requestId':'service'}},'meta':{'requestId':'gateway'}},status)
        if host==self.virtual:
            if method=='PUT':self.state['bytes']=raw;return 200,b'','application/octet-stream'
            if method=='GET':return 200,self.module.PNG,'image/png'
        if host.startswith('supabase.'):
            return response({'access_token':self.current,'refresh_token':'inert-refresh-token','token_type':'bearer',
                             'user':{'id':self.user,'email':self.config['email']}})
        if headers.get('authorization')!='Bearer '+self.current:
            if self.mode=='allow-denial':return envelope({'user':{'id':self.user,'email':self.config['email']}})
            return response({'error':{'code':'UNAUTHORIZED','message':'inert-private'}},401)
        if self.forbidden in path:
            if self.mode=='allow-foreign-workspace':return envelope([])
            return response({'error':{'code':'FORBIDDEN'}},403)
        prefix='/workspaces/'+self.workspace
        if path=='/me':return envelope({'user':{'id':self.user,'email':self.config['email']},'workspaces':[{'id':self.workspace,'slug':self.config['workspace_slug'],'role':'workspace_owner'}]})
        if path=='/workspaces':return envelope({'workspaces':[{'id':self.workspace,'slug':self.config['workspace_slug'],'role':'workspace_owner'}]})
        if path==prefix+'/content-directories' and method=='POST':self.state['directory_name']=body['name'];return envelope({'id':self.directory,'name':body['name'],'parentId':None})
        if path==prefix+'/documents' and method=='POST':
            self.state['document']=body
            return envelope({'id':self.document,'workspaceId':self.workspace,'createdBy':self.user,**body},201)
        if path==prefix+'/documents/'+self.document:return envelope({'id':self.document,'workspaceId':self.workspace,**self.state['document']})
        if path==prefix+'/content/entries' and method=='POST':self.state['entry']=body;return envelope({'entry':{'id':self.entry,'workspaceId':self.workspace,'status':'draft',**body}})
        if path==prefix+'/content/entries/'+self.entry and method=='GET':return envelope({'entry':{'id':self.entry,'workspaceId':self.workspace,'status':self.state.get('status','draft'),**self.state['entry']}})
        if path.startswith(prefix+'/content/entries?'):return envelope({'items':[{'id':self.entry,'directoryId':self.directory}]})
        if path.endswith('/publish'):
            if self.mode=='publish-failure':return response({'error':{'code':'UNAVAILABLE'}},503)
            # cms-core publication requires a Lexical editor document ({root:...}); anything else is PUBLICATION_INVALID.
            if not self.lexical(self.state['entry'].get('body')):return response({'error':{'code':'PUBLICATION_INVALID'}},400)
            self.state['status']='published';return envelope({'entry':{'id':self.entry,'workspaceId':self.workspace,'status':'published','publishedAt':'2023-11-14T22:13:20Z',**self.state['entry']}})
        if path==prefix+'/storage/uploads' and method=='POST':
            if self.mode=='storage-failure':return response({'error':{'code':'NO_PROVIDER'}},503)
            self.state['filename']=body['filename']
            url='https://'+self.virtual+'/fixture.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature='+'a'*64
            if self.mode=='foreign-provider':url=url.replace(self.virtual,'evil.example.invalid')
            if self.mode=='path-style':url=url.replace(self.virtual+'/',self.storage_host+'/'+self.bucket+'/')
            return envelope({'uploadId':self.upload,'objectId':self.object,'uploadMethod':'single','uploadUrl':url,'uploadHeaders':({'content-type':'image/png','content-length':str(len(self.module.PNG))} if self.mode=='actual-upload-headers' else {'Content-Type':'image/png'}),'dedupHit':False,
                'object':{'id':self.object,'workspaceId':self.workspace,'filename':body['filename'],'createdBy':self.user}})
        if path.endswith('/complete'):return envelope({'object':{'id':self.object,'status':'processing'}})
        if '/storage/objects/'+self.object in path and method=='GET':
            return envelope({'object':{'id':self.object,'workspaceId':self.workspace,'filename':self.state['filename'],'status':'failed' if self.mode=='processing-failure' else 'ready'}})
        if path.endswith('/download-url'):return envelope({'objectId':self.object,'url':'https://'+self.virtual+'/fixture.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature='+'b'*64,'expiresAt':'2023-11-14T22:28:20Z'})
        if method=='DELETE':return envelope({'success':True,'entryId':self.entry,'object':{'id':self.object,'status':'deleted'}})
        if path.endswith('/abort'):return envelope({'session':{'id':self.upload,'status':'aborted'}})
        return response({'error':{'code':'UNIMPLEMENTED'}},501)

    @staticmethod
    def lexical(body):
        if body is None:return True
        def node(value):
            return (type(value) is dict and value.get('version')==1 and value.get('type') in ('root','paragraph','text')
                    and all(node(child) for child in value.get('children',[])))
        return type(body) is dict and set(body)=={'root'} and node(body['root']) and body['root']['type']=='root'

    def execute(self):return self.module.run(stage='qa',publication=self.publication,source_sha256=self.source,work=self.work,
        transport=self.transport,config_loader=lambda stage:copy.deepcopy(self.config),keys_loader=self.keys,clock=lambda:self.now,sleep=lambda value:None)

    def refused(self,callback):
        with self.assertRaises(self.module.SmokeError) as caught:callback()
        self.assertEqual(str(caught.exception),'Hosted product smoke refused')

    def test_real_verified_tls_all_required_flows_exact_observation_and_private_redaction(self):
        result=self.execute();self.assertEqual(result,{'stage':'qa','publication':self.publication,'source_sha256':self.source,'result':'passed','observed_at':self.now})
        self.assertEqual(self.state['bytes'],self.module.PNG)
        self.assertNotIn('contentTypeId',self.state['entry']);self.assertEqual(self.state['entry']['directoryId'],self.directory)
        self.assertEqual(self.state['entry']['body']['root']['children'][0]['children'][0]['text'],self.state['directory_name'])
        self.assertEqual(self.state['status'],'published')
        evidence=list(self.work.glob('smoke-evidence-*.json'));self.assertEqual(len(evidence),1)
        text=evidence[0].read_text()
        for sensitive in (self.current,self.foreign['token'],self.config['password'],'X-Amz-Signature','inert-refresh-token'):self.assertNotIn(sensitive,text)
        self.assertEqual(evidence[0].stat().st_mode&0o777,0o600)
        self.assertFalse(any('/health' in path for _,_,path,_,_ in self.calls))

    def test_required_product_failures_never_return_passed(self):
        for mode in ('publish-failure','storage-failure','processing-failure'):
            self.mode=mode;self.refused(self.execute)

    def test_unauthorized_cross_stage_and_cross_workspace_denial_are_required(self):
        for mode in ('allow-denial','allow-foreign-workspace'):
            self.mode=mode;self.refused(self.execute)

    def test_foreign_signed_url_is_refused_before_any_external_provider_request(self):
        self.mode='foreign-provider';self.refused(self.execute)
        self.assertFalse(any(host=='evil.example.invalid' for host,_,_,_,_ in self.calls))

    def test_gateway_response_without_service_envelope_is_refused(self):
        self.mode='single-envelope';self.refused(self.execute)

    def test_gateway_data_requires_exact_gateway_and_service_envelopes(self):
        inner={'user':{'id':self.user}}
        for value in ({'ok':True,'data':{'ok':True,'data':inner}},{'ok':True,'data':{'ok':True,'data':inner,'meta':{}},'meta':{}}):
            self.assertEqual(self.module.gateway_data(value),inner)
        for value in (inner,{'data':inner},{'ok':True,'data':inner},{'data':{'data':inner}},{'ok':True,'data':{'data':inner}},
                      {'data':{'ok':True,'data':inner}},{'ok':False,'data':{'ok':True,'data':inner}},
                      {'ok':True,'data':{'ok':True,'data':inner,'error':{'code':'X'}}},{'ok':True,'success':False,'data':{'ok':True,'data':inner}},
                      {'ok':True,'data':[{'ok':True,'data':inner}]},['ok']):
            self.refused(lambda:self.module.gateway_data(value))

    def test_path_style_r2_url_is_refused_before_any_external_provider_request(self):
        self.mode='path-style';self.refused(self.execute)
        self.assertFalse(any(host in (self.storage_host,self.virtual) for host,_,_,_,_ in self.calls))

    def test_arbitrary_or_expired_synthetic_probe_is_refused_before_http(self):
        for value in ('pasted-token',self.current):
            self.config['foreign_probe']['token']=value;self.refused(self.execute)
        self.assertEqual(self.calls,[])

    def test_expired_verified_foreign_probe_refreshes_without_changing_protected_config(self):
        original=copy.deepcopy(self.config['foreign_probe'])
        self.now+=24*3600;self.current=self.jwt('qa')
        try:result=self.execute()
        except self.module.SmokeError:self.fail('verified expired foreign probe was not renewed')
        self.assertEqual(result['result'],'passed')
        self.assertEqual(self.config['foreign_probe'],original)
        foreign=[headers['authorization'].removeprefix('Bearer ') for _,_,path,headers,_ in self.calls
                 if path=='/me' and headers.get('authorization') not in (None,'Bearer '+self.current)]
        self.assertEqual(len(foreign),1);self.assertNotEqual(foreign[0],original['token'])
        claims=self.module.jwt(foreign[0],'staging',self.user,self.keys,self.now)
        self.assertEqual(claims['iat'],self.now);self.assertEqual(claims['email'],self.config['email'])

    def test_https_total_deadline_refuses_trickle_body_and_headers(self):
        def trickle_factory(part):
            def factory(host,port,context,timeout):
                client,server=socket.socketpair();parent=self
                def serve():
                    wrapped=None
                    try:
                        wrapped=parent.server_ctx.wrap_socket(server,server_side=True)
                        wrapped.recv(8192)
                        if part=='body':
                            wrapped.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n')
                            for _ in range(10):time.sleep(.07);wrapped.sendall(b'0')
                        else:
                            for byte in b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n':
                                time.sleep(.03);wrapped.sendall(bytes([byte]))
                    except Exception:pass
                    finally:
                        if wrapped:wrapped.close()
                        else:server.close()
                thread=threading.Thread(target=serve,daemon=True);thread.start();self.threads.append(thread)
                connection=http.client.HTTPSConnection(host,443,context=context,timeout=timeout)
                connection.sock=context.wrap_socket(client,server_hostname=host);return connection
            return factory
        with patch.object(self.module,'HTTP_TIMEOUT',.15,create=True):
            for part in ('body','headers'):
                transport=self.module.HTTPS(context=self.client_ctx,connection_factory=trickle_factory(part))
                start=time.monotonic();self.refused(lambda:transport.request('GET','https://api.qa.xynes.com/me',{},None))
                self.assertLess(time.monotonic()-start,.6,'trickle extended total request deadline')

    def test_actual_s3_upload_headers_include_exact_signed_length_once(self):
        self.mode='actual-upload-headers';self.assertEqual(self.execute()['result'],'passed')
        upload=[headers for host,method,_,headers,_ in self.calls if host==self.virtual and method=='PUT'][0]
        self.assertEqual(upload['content-length'],str(len(self.module.PNG)))
        self.assertEqual(upload['content-type'],'image/png')

    def test_private_evidence_failure_is_redacted_and_cannot_return_passed(self):
        with patch.object(self.module.os,'fsync',side_effect=RuntimeError('inert-sensitive-evidence-error')):
            self.refused(self.execute)

    def test_https_refuses_redirects_ambiguous_lengths_and_unbounded_framing(self):
        cases=[b'HTTP/1.1 302 Found\r\nLocation: https://evil.example.invalid/\r\nContent-Length: 2\r\n\r\n{}',
               b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
               b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n',
               b'HTTP/1.1 200 OK\r\nX-Large: '+b'x'*17000+b'\r\n\r\n{}',
               b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;'+b'x'*1024+b'\r\nx\r\n0\r\n\r\n']
        for raw in cases:
            def factory(host,port,context,timeout):
                client,server=socket.socketpair();parent=self
                def serve():
                    wrapped=None
                    try:
                        wrapped=parent.server_ctx.wrap_socket(server,server_side=True);wrapped.recv(8192);wrapped.sendall(raw)
                    except Exception:pass
                    finally:
                        if wrapped:wrapped.close()
                        else:server.close()
                thread=threading.Thread(target=serve,daemon=True);thread.start();self.threads.append(thread)
                connection=http.client.HTTPSConnection(host,443,context=context,timeout=timeout)
                connection.sock=context.wrap_socket(client,server_hostname=host);return connection
            transport=self.module.HTTPS(context=self.client_ctx,connection_factory=factory)
            self.refused(lambda:transport.request('GET','https://api.qa.xynes.com/me',{},None))

    def test_image_fixture_has_valid_png_crc_for_actual_processors(self):
        raw=self.module.PNG;self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n');offset=8
        while offset<len(raw):
            size=struct.unpack('>I',raw[offset:offset+4])[0];kind=raw[offset+4:offset+8]
            data=raw[offset+8:offset+8+size];checksum=struct.unpack('>I',raw[offset+8+size:offset+12+size])[0]
            self.assertEqual(zlib.crc32(kind+data)&0xffffffff,checksum,'PNG processor fixture CRC is invalid')
            offset+=12+size
        self.assertEqual(offset,len(raw))

    def test_http_tls_cannot_be_disabled_or_replaced_with_plain_http(self):
        context=ssl._create_unverified_context();self.refused(lambda:self.module.HTTPS(context=context))
        self.refused(lambda:self.transport.request('GET','http://api.qa.xynes.com/me',{},None))

if __name__=='__main__':unittest.main()
