#!/usr/bin/env python3
"""CI adapter tests; fixture success is not real service product evidence."""
import base64
import importlib.util
import json
from pathlib import Path
import sys
import threading
import http.server
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_smoke as smoke
spec=importlib.util.spec_from_file_location('setup_fixture',ROOT/'scripts/test/host-smoke-setup.test.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class Product(fixture.Server):
    def __init__(self,keys,now):
        super().__init__(keys,now);self.entries={};self.documents={};self.directory=None;self.phase_failure=None
    def request(self,method,url,headers,raw):
        path=urlsplit(url).path;body=json.loads(raw) if raw else None
        if self.phase_failure=='publish' and path.endswith('/publish'):
            return self.response(503,{'secret':'DO_NOT_EXPOSE'})
        if '/token?' in url and body.get('refresh_token'):
            user=next(iter(self.users.values()));raw=json.dumps({'email':user['user']['email'],'password':user['password']}).encode()
        if path=='/me' and not headers.get('Authorization'):return self.response(401,{})
        bearer=headers.get('Authorization','')[7:]
        if path=='/me' and bearer.count('.')==2:
            claims=json.loads(base64.urlsafe_b64decode(bearer.split('.')[1]+'='*(-len(bearer.split('.')[1])%4)))
            if claims['iss']!=self.keys('qa')[1]:return self.response(403,{})
        if method=='DELETE':self.calls.append((method,path,body));return self.gateway(200,{'success':True})
        if '/documents' in path or '/content/entries' in path or (path.endswith('/content-directories') and method=='POST'):
            self.calls.append((method,path,body));wid=path.split('/')[2]
            user=next(row['createdBy'] for row in self.workspaces.values() if row['id']==wid)
            if path.endswith('/content-directories'):
                self.directory='44444444-4444-4444-8444-444444444444';return self.gateway(200,dict(body,id=self.directory))
            if '/documents' in path:
                if method=='POST':self.documents={'id':'66666666-6666-4666-8666-666666666666','workspaceId':wid,'createdBy':user,**body}
                return self.gateway(201 if method=='POST' else 200,self.documents)
            if path.endswith('/publish'):
                assert self.entries['body']['root']['type']=='root'
                self.entries.update(status='published',publishedAt='2026-10-29T00:00:00Z')
            elif method=='POST':self.entries={'id':'55555555-5555-4555-8555-555555555555','workspaceId':wid,'status':'draft',**body}
            if '?' in url:return self.gateway(200,{'items':[self.entries]})
            return self.gateway(200,{'entry':self.entries})
        return super().request(method,url,headers,raw)

class Tests(unittest.TestCase):
    def setUp(self):
        import ci_smoke_runtime
        self.module=ci_smoke_runtime;self.now=1800000000
        self.keys=lambda stage:({'qa':'a','staging':'b'}[stage]*64,smoke.origins(stage)['auth']+'/auth/v1')
        self.env={'SUPABASE_SERVICE_ROLE_KEY':'INERT_SERVICE_KEY','R2_ACCOUNT_ID':'d'*32,'R2_BUCKET':'apex-ci-fixture'}
        self.product=Product(self.keys,self.now)
    def execute(self):
        return self.module.execute_flow(auth_port=12345,gateway_port=12346,values=self.env,keys_loader=self.keys,
            clock=lambda:self.now,sleep=lambda _:None,transport=self.product)
    def test_real_flow_classes_with_fixture_and_sanitized_summary(self):
        result=self.execute();self.assertEqual(result['result'],'passed');self.assertIs(result['storage_verified'],False)
        labels=[row['flow'] for row in result['steps']]
        for label in ('auth-refresh','unauthorized-denial','foreign-stage-denial','nonmember-workspace-denial','cms-entry-publish','cms-published-read','owned-entry-delete','owned-directory-delete'):
            self.assertIn(label,labels)
        self.assertNotIn('storage-create',labels)
        serialized=json.dumps(result)
        for secret in ('INERT_SERVICE_KEY','INERT_REFRESH','password','access_token','@example.invalid'):
            self.assertNotIn(secret,serialized)
    def test_cleanup_on_failure_and_safe_exception(self):
        self.product.phase_failure='publish'
        with self.assertRaises(self.module.RuntimeError) as caught:self.execute()
        self.assertEqual(str(caught.exception),self.module.ERROR)
        diagnostic=caught.exception.safe_evidence
        self.assertEqual(diagnostic['phase'],'flow')
        self.assertEqual(diagnostic['failed_flow_phase'],'cms-entry-publish')
        self.assertIn({'flow':'cms-entry-publish','status':503,'observed_at':self.now},diagnostic['steps'])
        self.assertEqual(diagnostic['result'],'refused');self.assertIs(diagnostic['storage_verified'],False)
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(diagnostic))
        deletes=[path for method,path,_ in self.product.calls if method=='DELETE']
        self.assertEqual(len(deletes),2)
    def test_cleanup_failure_refuses_success(self):
        original=self.product.request
        def request(method,url,headers,raw):
            if method=='DELETE':return self.product.response(503,{'secret':'DO_NOT_EXPOSE'})
            return original(method,url,headers,raw)
        self.product.request=request
        with self.assertRaises(self.module.RuntimeError) as caught:self.execute()
        self.assertEqual(str(caught.exception),self.module.ERROR)
        self.assertEqual(caught.exception.safe_evidence['phase'],'cleanup')
        self.assertEqual(caught.exception.safe_evidence['failed_flow_phase'],'owned-entry-delete')
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(caught.exception.safe_evidence))

    def test_product_and_cleanup_failure_preserve_original_failure_phase(self):
        self.product.phase_failure='publish'
        original=self.product.request
        def request(method,url,headers,raw):
            if method=='DELETE':return self.product.response(503,{'secret':'DO_NOT_EXPOSE'})
            return original(method,url,headers,raw)
        self.product.request=request
        with self.assertRaises(self.module.RuntimeError) as caught:self.execute()
        evidence=caught.exception.safe_evidence
        self.assertEqual(evidence['phase'],'flow')
        self.assertEqual(evidence['failed_flow_phase'],'cms-entry-publish')
        self.assertEqual(evidence['result'],'refused')
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(evidence))
    def test_provision_failure_has_safe_phase_without_product_data(self):
        self.product.fail='/me'
        with self.assertRaises(self.module.RuntimeError) as caught:self.execute()
        self.assertEqual(caught.exception.safe_evidence,{'result':'refused','storage_verified':False,
            'phase':'provision','failed_flow_phase':None,'steps':[]})
        self.assertEqual(str(caught.exception),self.module.ERROR)
    def test_failure_evidence_enforces_allowlisted_shape(self):
        diagnostic=self.module.failure_evidence(phase='provision',failed_flow_phase='INERT_SECRET',steps=[
            {'flow':'cms-entry-publish','status':503,'observed_at':self.now,'body':'DO_NOT_EXPOSE'},
            {'flow':'INERT_SECRET','status':200,'observed_at':self.now},
            {'flow':'auth-login','status':True,'observed_at':self.now},
            {'flow':'auth-login','status':200,'observed_at':-1}])
        self.assertEqual(set(diagnostic),{'phase','failed_flow_phase','steps','result','storage_verified'})
        self.assertIsNone(diagnostic['failed_flow_phase'])
        self.assertEqual(diagnostic['steps'],[{'flow':'cms-entry-publish','status':503,'observed_at':self.now}])
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(diagnostic))
        self.assertNotIn('INERT_SECRET',json.dumps(diagnostic))
    def test_store_copies_and_clears(self):
        store=self.module.EphemeralStore()
        with store:
            row={'secret':['inert']};store.write('qa.json',row);row['secret'].append('mutated')
            value=store.read('qa.json');value['secret'].append('also mutated')
            self.assertEqual(store.read('qa.json'),{'secret':['inert']})
        with self.assertRaises(self.module.RuntimeError):store.read('qa.json')
    def test_local_transport_exact_origins_and_ports_before_io(self):
        transport=self.module.LocalTransport(auth_port=12345,gateway_port=12346)
        for url in ('http://api.qa.xynes.com/me','https://api.qa.xynes.com:443/me','https://api.qa.xynes.com.evil/me','https://user@api.qa.xynes.com/me','https://api.qa.xynes.com/me#x','https://supabase.qa.xynes.com/token','https://api.qa.xynes.com//evil','https://api.qa.xynes.com/workspaces/id/storage/uploads'):
            with self.subTest(url=url),patch('http.client.HTTPConnection') as connection:
                with self.assertRaises(self.module.RuntimeError):transport.request('GET',url,{},None)
                connection.assert_not_called()
        for port in (True,0,65536,'123'):
            with self.assertRaises(self.module.RuntimeError):self.module.LocalTransport(auth_port=port,gateway_port=12346)
    def test_bridge_transport_uses_only_explicit_private_endpoint(self):
        transport=self.module.LocalTransport(auth_port=9999,gateway_port=4100,
            auth_host='172.28.0.10',gateway_host='172.28.0.11')
        with patch('http.client.HTTPConnection',side_effect=OSError('DO_NOT_EXPOSE')) as connect:
            with self.assertRaises(self.module.RuntimeError):
                transport.request('GET','https://api.qa.xynes.com/me',{},None)
            self.assertEqual(connect.call_args.args,('172.28.0.11',4100))
        for host in ('8.8.8.8','169.254.169.254','0.0.0.0','localhost','::1','172.28.0.11:4100','DO_NOT_EXPOSE'):
            with self.subTest(host=host),self.assertRaises(self.module.RuntimeError):
                self.module.LocalTransport(auth_port=9999,gateway_port=4100,auth_host=host)

    def test_loopback_http_mapping_query_and_redirect_refusal(self):
        calls=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append((self.path,self.headers['Host']))
                self.send_response(302 if self.path=='/redirect' else 200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(smoke.MAX_RESPONSE+1) if self.path=='/oversized' else '4' if self.path=='/truncated' else '2')
                if self.path=='/duplicate':self.send_header('Content-Length','2')
                self.end_headers();self.wfile.write(b'{}')
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            port=server.server_port;transport=self.module.LocalTransport(auth_port=port,gateway_port=port)
            result=transport.request('GET','https://supabase.qa.xynes.com/auth/v1/token?grant_type=password',{},None)
            self.assertEqual(result[0],200);self.assertEqual(calls[0],('/token?grant_type=password','supabase.qa.xynes.com'))
            for path in ('/redirect','/oversized','/truncated','/duplicate'):
                with self.subTest(path=path),self.assertRaises(self.module.RuntimeError):
                    transport.request('GET','https://api.qa.xynes.com'+path,{},None)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
