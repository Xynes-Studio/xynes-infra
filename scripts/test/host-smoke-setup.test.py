#!/usr/bin/env python3
"""Stateful HTTP fixtures only; no Docker, VPS, provider or external socket."""
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
import stat
import socket
import ssl
import subprocess
import threading
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_smoke as smoke

class Server:
    def __init__(self,keys,now):
        self.keys=keys;self.now=now;self.users={};self.workspaces={};self.calls=[];self.fail=None;self.lost=None;self.mode=None
    def request(self,method,url,headers,raw):
        body=json.loads(raw) if raw else None;path=urlsplit(url).path;self.calls.append((method,path,body))
        if self.fail==path:return self.response(503,{'error':'private server diagnostic'})
        bearer=headers.get('Authorization','')[7:]
        if path.endswith('/admin/users'):
            assert bearer=='INERT_SERVICE_KEY' and method=='POST' and body['email_confirm'] is True
            assert set(body)=={'email','password','email_confirm','app_metadata'}
            if body['email'] in self.users:return self.response(422,{'error':'email exists'})
            user={'id':str(uuid.uuid4()),'email':body['email'],'email_confirmed_at':'2026-10-14T00:00:00Z','app_metadata':body['app_metadata']}
            self.users[body['email']]={'user':user,'password':body['password']}
            if self.lost=='user':self.lost=None;raise OSError('lost secret response')
            return self.response(200,user)
        if '/admin/users/' in path:
            assert bearer=='INERT_SERVICE_KEY';uid=path.rsplit('/',1)[1]
            user=next(item['user'] for item in self.users.values() if item['user']['id']==uid)
            if self.mode=='wrong_metadata':user=copy.deepcopy(user);user['app_metadata']={'customer':True}
            return self.response(200,user)
        if path.endswith('/token'):
            item=self.users.get(body['email'])
            if not item or item['password']!=body['password']:return self.response(400,{'error':'invalid_credentials'})
            user=item['user'];enc=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
            payload=enc({'alg':'HS256'})+'.'+enc({'iss':self.keys('qa')[1],'aud':'authenticated','role':'authenticated','sub':user['id'],'email':user['email'],'iat':self.now,'exp':self.now+3600})
            token=payload+'.'+base64.urlsafe_b64encode(hmac.new(self.keys('qa')[0].encode(),payload.encode(),hashlib.sha256).digest()).decode().rstrip('=')
            return self.response(200,{'user':user,'access_token':token,'refresh_token':'INERT_REFRESH'})
        claims=json.loads(base64.urlsafe_b64decode(bearer.split('.')[1]+'='*(-len(bearer.split('.')[1])%4)))
        user=next(item['user'] for item in self.users.values() if item['user']['id']==claims['sub'])
        if path=='/me':return self.response(200,{'data':{'user':{'id':user['id'],'email':user['email']}}})
        if path=='/workspaces' and method=='GET':
            rows=[dict(row,role='workspace_owner') for row in self.workspaces.values() if row['createdBy']==user['id']]
            if self.mode=='customer_workspace':rows.append({'id':str(uuid.uuid4()),'slug':'customer-data','name':'Customer','role':'workspace_owner'})
            if self.mode=='wrong_role' and rows:rows[0]['role']='workspace_member'
            return self.response(200,{'data':{'workspaces':rows}})
        if path=='/workspaces' and method=='POST':
            assert set(body)=={'name','slug'}
            if body['slug'] in self.workspaces:return self.response(409,{'error':'duplicate slug'})
            row=dict(body,id=str(uuid.uuid4()),createdBy=user['id'],planType='free');self.workspaces[row['slug']]=row
            if self.lost=='workspace':self.lost=None;raise OSError('lost workspace response')
            return self.response(200,{'data':row})
        if path.endswith('/content-directories'):
            wid=path.split('/')[2];row=next(row for row in self.workspaces.values() if row['id']==wid)
            return self.response(200 if self.mode=='denial_bypass' or row['createdBy']==user['id'] else 403,{'error':'forbidden'})
        raise AssertionError('unexpected fixture request')
    def response(self,status,value):return status,{'content-type':'application/json'},json.dumps(value).encode()

class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/host_smoke_setup.py';self.assertTrue(path.exists(),'fixture provisioning implementation missing')
        spec=importlib.util.spec_from_file_location('host_smoke_setup',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory(prefix='apex-smoke-setup-');self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name).resolve();self.directory.chmod(0o700);self.now=1800000000
        self.keys=lambda stage:({'qa':'a','staging':'b','prod':'c'}[stage]*64,smoke.origins(stage)['auth']+'/auth/v1')
        self.server=Server(self.keys,self.now)
        self.env={'SUPABASE_SERVICE_ROLE_KEY':'INERT_SERVICE_KEY','R2_ACCOUNT_ID':'d'*32,'R2_BUCKET':'apex-qa-fixture'}
        def owned(path,**kwargs):
            if path in self.directory.parents:return
            self.assertTrue(path==self.directory or self.directory in path.parents)
            for item in [path,*path.parents]:
                if item==self.directory.parent:break
                info=item.lstat();self.assertEqual(info.st_uid,os.getuid());self.assertFalse(stat.S_ISLNK(info.st_mode));self.assertFalse(info.st_mode&0o022)
        for owner,name,value in [(self.module,'ROOT_UID',os.getuid()),(smoke,'SMOKE_UID',os.getuid()),(self.module.host_compose,'root_path',owned)]:
            p=patch.object(owner,name,value);p.start();self.addCleanup(p.stop)
        self.store=self.module.Store(self.directory)
    def run_setup(self):return self.module.ensure('qa',transport=self.server,environment_loader=lambda stage:self.env,keys_loader=self.keys,clock=lambda:self.now,store=self.store)
    def config(self):return json.loads((self.directory/'qa.json').read_text())
    def test_complete_then_idempotent_and_protected_config(self):
        result=self.run_setup();config=self.config();self.assertEqual(set(config),smoke.CONFIG_KEYS)
        smoke.qualify(config,'qa',self.keys,self.now)
        self.assertEqual(result['configured'],True);self.assertNotIn('password',result)
        self.assertEqual(len(self.server.users),2);self.assertEqual(len(self.server.workspaces),2)
        writes=sum(call[0]=='POST' and call[1] in ('/auth/v1/admin/users','/workspaces') for call in self.server.calls)
        self.run_setup();self.assertEqual(writes,sum(call[0]=='POST' and call[1] in ('/auth/v1/admin/users','/workspaces') for call in self.server.calls))
        for path in self.directory.iterdir():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertTrue(all(user['user']['email'].endswith('@example.invalid') for user in self.server.users.values()))
    def test_lost_user_response_recovers_without_duplicate(self):
        self.server.lost='user'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(len(self.server.users),1);self.assertFalse((self.directory/'qa.json').exists())
        self.run_setup();self.assertEqual(len(self.server.users),2)
        self.assertEqual(sum(path=='/auth/v1/admin/users' for _,path,_ in self.server.calls),2)
    def test_lost_workspace_response_recovers_exact_owner(self):
        self.server.lost='workspace'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(len(self.server.workspaces),1)
        self.run_setup();self.assertEqual(len(self.server.workspaces),2)
        self.assertEqual(sum(method=='POST' and path=='/workspaces' for method,path,_ in self.server.calls),2)
    def test_unknown_workspace_never_adopted_deleted_or_completed(self):
        self.server.mode='customer_workspace'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertFalse((self.directory/'qa.json').exists());self.assertEqual(len(self.server.workspaces),0)
        self.assertFalse(any(method in ('DELETE','PUT') for method,_,_ in self.server.calls))
    def test_admin_metadata_change_refused_and_redacted(self):
        self.run_setup();self.server.mode='wrong_metadata'
        with self.assertRaises(self.module.SetupError) as caught:self.run_setup()
        self.assertEqual(str(caught.exception),self.module.ERROR)
    def test_membership_or_cross_workspace_denial_required(self):
        for mode in ('wrong_role','denial_bypass'):
            with self.subTest(mode=mode):
                self.server.mode=mode
                with self.assertRaises(self.module.SetupError):self.run_setup()
                self.assertFalse((self.directory/'qa.json').exists())
    def test_existing_config_or_journal_tamper_refused(self):
        self.run_setup();path=self.directory/'qa.json';value=self.config();value['workspace_id']=str(uuid.uuid4());path.write_text(json.dumps(value));before=len(self.server.calls)
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(len(self.server.workspaces),2);self.assertEqual(len(self.server.calls),before)
    def test_symlink_permissive_mode_and_stage_refused_before_http(self):
        with self.assertRaises(self.module.SetupError):self.module.ensure('dev',transport=self.server,store=self.store)
        path=self.directory/'qa.setup.json';path.write_text('{}');path.chmod(0o644)
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(self.server.calls,[])
        path.unlink();path.symlink_to(self.directory/'elsewhere')
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(self.server.calls,[])

    def test_partial_service_failure_keeps_exact_ids_for_retry(self):
        self.server.fail='/me'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        saved=json.loads((self.directory/'qa.setup.json').read_text())
        uid=saved['accounts']['primary']['user_id'];self.assertTrue(uid)
        self.assertEqual(len(self.server.users),1);self.assertEqual(len(self.server.workspaces),0)
        self.server.fail=None;self.run_setup();self.assertEqual(self.config()['user_id'],uid)
        self.assertEqual(len(self.server.users),2)
    def test_changed_pending_user_marker_never_adopted(self):
        self.server.lost='user'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.server.mode='wrong_metadata'
        with self.assertRaises(self.module.SetupError):self.run_setup()
        self.assertEqual(len(self.server.users),1);self.assertEqual(len(self.server.workspaces),0)
        self.assertFalse((self.directory/'qa.json').exists())
    def test_durability_failure_has_no_remote_mutation_or_secret_exception(self):
        with patch.object(self.module.os,'fsync',side_effect=OSError('INERT_PRIVATE_SECRET')):
            with self.assertRaises(self.module.SetupError) as caught:self.run_setup()
        self.assertEqual(str(caught.exception),self.module.ERROR);self.assertEqual(self.server.calls,[])
        self.assertFalse(any(path.name.startswith('.setup-') for path in self.directory.iterdir()))
    def test_native_verified_tls_full_provisioning(self):
        certificate=self.directory/'fixture-cert.pem';key=self.directory/'fixture-key.pem'
        result=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(certificate),
            '-days','1','-subj','/CN=apex-local-fixture','-addext','subjectAltName=DNS:api.qa.xynes.com,DNS:supabase.qa.xynes.com'],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,'TLS fixture preparation failed')
        server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(certificate,key)
        client_context=ssl.create_default_context(cafile=str(certificate));threads=[];backend=self.server
        def connection(host,port,context,timeout):
            client,server=socket.socketpair()
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):self.respond()
                def do_POST(self):self.respond()
                def respond(self):
                    raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                    status,headers,body=backend.request(self.command,'https://'+host+self.path,dict(self.headers),raw)
                    self.send_response(status)
                    for name,value in headers.items():self.send_header(name,value)
                    self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
            def serve():
                wrapped=None
                try:
                    wrapped=server_context.wrap_socket(server,server_side=True);Handler(wrapped,('local',0),object())
                finally:
                    (wrapped or server).close()
            thread=threading.Thread(target=serve,daemon=True);thread.start();threads.append(thread)
            result=http.client.HTTPSConnection(host,port,context=context,timeout=timeout)
            result.sock=context.wrap_socket(client,server_hostname=host);return result
        transport=smoke.HTTPS(context=client_context,connection_factory=connection)
        result=self.module.ensure('qa',transport=transport,environment_loader=lambda stage:self.env,keys_loader=self.keys,clock=lambda:self.now,store=self.store)
        self.assertTrue(result['configured'])
        for thread in threads:thread.join(timeout=1);self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()
