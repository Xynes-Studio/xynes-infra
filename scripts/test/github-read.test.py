#!/usr/bin/env python3
"""Injected HTTP responses exercise actual bytes without network or token reads."""
import importlib.util
import http.client
import io
import json
from pathlib import Path
import ssl
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import admission
TOKEN='ghs_INERT_DO_NOT_PRINT_TOKEN_123456789'

class Reply:
 def __init__(self,body=b'{"id":1415400917}',status=200,headers=None):
  self.body=io.BytesIO(body);self.status=status;self.headers=headers if headers is not None else [('Content-Type','application/json'),('Content-Length',str(len(body)))];self.reads=[]
 def getheaders(self):return self.headers
 def read(self,size):self.reads.append(size);return self.body.read(size)
 def read1(self,size):self.reads.append(size);return self.body.read(size)
class Connection:
 def __init__(self,reply):self.reply=reply;self.closed=False;self.calls=[];self.sock=self
 def connect(self):pass
 def settimeout(self,value):self.timeout=value
 def request(self,*args,**kwargs):self.calls.append((args,kwargs))
 def getresponse(self):return self.reply
 def close(self):self.closed=True

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/github_read.py';self.assertTrue(path.exists(),'bounded GitHub read adapter is missing')
  spec=importlib.util.spec_from_file_location('github_read',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.reply=Reply();self.connections=[];self.options=[]
 def factory(self,host,**options):
  self.assertEqual(host,'api.github.com');self.options.append(options);connection=Connection(self.reply);self.connections.append(connection);return connection
 def reader(self):return self.module.GitHubReadAdapter(TOKEN,connection_factory=self.factory)
 def refused(self,callback):
  with self.assertRaises(self.module.GitHubReadError) as caught:callback()
  self.assertNotIn(TOKEN,str(caught.exception));self.assertNotIn('secret-body',str(caught.exception))
 def test_reviewer_lookup_allows_only_current_independent_account(self):
  self.reader().read('/users/xynes-admin')
  self.refused(lambda:self.reader().read('/users/Aman9804'))
 def test_real_json_bytes_and_verified_tls_fixed_headers_connection_closed(self):
  reader=self.reader();result=reader.read('/repos/Xynes-Studio/xynes-infra');self.assertEqual(result,{'id':1415400917})
  self.assertNotIn(TOKEN,repr(reader));connection=self.connections[0];self.assertTrue(connection.closed)
  self.assertEqual(self.options[0]['timeout'],15);context=self.options[0]['context'];self.assertTrue(context.check_hostname);self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
  args,kwargs=connection.calls[0];self.assertEqual(args[:2],('GET','/repos/Xynes-Studio/xynes-infra'))
  self.assertEqual(kwargs['headers']['Authorization'],'Bearer '+TOKEN);self.assertEqual(kwargs['headers']['X-GitHub-Api-Version'],'2022-11-28');self.assertTrue(all(0<n<=65536 for n in self.reply.reads))
 def test_known_repository_user_and_exact_native_package_paths(self):
  for path in ['/repos/Xynes-Studio/xynes-infra/actions/runs/123','/repos/Xynes-Studio/xynes-infra/contents/.github/workflows/apex-build.yml?ref='+'a'*40,'/users/xynes-admin','/orgs/Xynes-Studio/packages/container/apex%2Fxynes-gateway']:
   self.reply=Reply()
   self.assertEqual(self.reader().read(path),{'id':1415400917})
 def test_only_authenticated_package_not_found_can_be_reported_as_missing(self):
  self.reply=Reply(b'{}',404)
  self.assertIsNone(self.reader().package_metadata('xynes-gateway'))
  self.assertTrue(self.connections[-1].closed)
  self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'))
  for status in (401,403,429,500):
   self.reply=Reply(b'{"secret-body":1}',status)
   self.refused(lambda:self.reader().package_metadata('xynes-gateway'))
  before=len(self.connections)
  self.refused(lambda:self.reader().package_metadata('foreign'))
  self.assertEqual(len(self.connections),before)
 def test_foreign_urls_paths_controls_and_traversal_refuse_before_connecting(self):
  for path in ['https://api.github.com/repos/Xynes-Studio/xynes-infra','//evil.invalid/','/repos/Xynes-Studio/xynes-infra-foreign','/repos/Other/repo','/users/Other','/orgs/Xynes-Studio/packages/container/apex%2Fother','/repos/Xynes-Studio/xynes-infra/../other','/repos/Xynes-Studio/xynes-infra/%2e%2e/other','/repos/Xynes-Studio/xynes-infra/%252e%252e/other','/repos/Xynes-Studio/xynes-infra/x?value=%0a','/repos/Xynes-Studio/xynes-infra/x#fragment','/repos/Xynes-Studio/xynes-infra/\r\nx']:
   with self.subTest(path=path):self.refused(lambda:self.reader().read(path))
  self.assertEqual(self.connections,[])
 def test_only_fixed_graphql_query_uses_post(self):
  result=self.reader().read_graphql(admission.NO_BYPASS_QUERY);self.assertEqual(result,{'id':1415400917})
  args,kwargs=self.connections[0].calls[0];self.assertEqual(args[:2],('POST','/graphql'));self.assertEqual(json.loads(kwargs['body']),{'query':admission.NO_BYPASS_QUERY})
  self.refused(lambda:self.reader().read_graphql('mutation { deleteRepository(input:{repositoryId:"x"}) { clientMutationId } }'))
  self.refused(lambda:self.reader().read_graphql('query { viewer { login } }'))
  self.refused(lambda:self.reader()._request('DELETE','/repos/Xynes-Studio/xynes-infra',b''))
 def test_redirect_error_and_oversized_body_refuse_without_retries(self):
  for reply in [Reply(b'{"secret-body":1}',302,[('Location','https://foreign.invalid/token')]),Reply(b'{"secret-body":1}',403),Reply(b' '* (4*1024*1024+1)),Reply(b'{}',200,[('Content-Type','application/json'),('Content-Length',str(4*1024*1024+1))])]:
   self.reply=reply;before=len(self.connections);self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'));self.assertEqual(len(self.connections),before+1);self.assertTrue(self.connections[-1].closed)
 def test_duplicate_nan_depth_nodes_wrong_type_and_json_fail_closed(self):
  deep=b'{"x":'*22+b'null'+b'}'*22
  for body in [b'{"id":1,"id":2}',b'{"n":NaN}',b'{"n":Infinity}',deep,b'null',b'"secret-body"',b'{',b'\xff',b'{"n":1e9999}',b'['+b'0,'*100000+b'0]']:
   self.reply=Reply(body);self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'))
 def test_provider_metadata_headers_must_be_valid_bounded_and_uncompressed(self):
  for headers in [[('Content-Type','text/html')],[('Content-Type','application/json'),('content-type','application/json')],[('Content-Type','application/json'),('Content-Encoding','gzip')],[('Content-Type','application/json'),('Content-Length','1')],[('Content-Type','application/json'),('X-Unsafe','secret-body\n')]]:
   self.reply=Reply(headers=headers);self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'))
 def test_underlying_constructor_or_close_errors_are_redacted(self):
  def bad(*args,**kwargs):raise RuntimeError(TOKEN+' secret-body')
  self.refused(lambda:self.module.GitHubReadAdapter(TOKEN,connection_factory=bad).read('/repos/Xynes-Studio/xynes-infra'))
  def closing(*args,**kwargs):
   connection=self.factory(*args,**kwargs);connection.close=lambda:(_ for _ in ()).throw(ValueError(TOKEN+' secret-body'));return connection
  self.refused(lambda:self.module.GitHubReadAdapter(TOKEN,connection_factory=closing).read('/repos/Xynes-Studio/xynes-infra'))
 def test_deadline_stops_before_authenticated_request_and_missing_length_is_bounded(self):
  with patch.object(self.module.time,'monotonic',side_effect=[0,16]):
   self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'))
  self.assertEqual(self.connections[0].calls,[]);self.assertTrue(self.connections[0].closed)
  self.reply=Reply(b' '*(4*1024*1024+1),headers=[('Content-Type','application/json')])
  self.refused(lambda:self.reader().read('/repos/Xynes-Studio/xynes-infra'));self.assertTrue(self.connections[-1].closed)
  self.assertLessEqual(sum(self.reply.reads),4*1024*1024+1)
 def test_real_http_response_trickle_obeys_absolute_header_and_body_deadline(self):
  for stage in ['body','headers','chunk_framing']:
   with self.subTest(stage=stage):
    client,server=socket.socketpair();stop=threading.Event();response_holder=[]
    body=b'{"id":1415400917}'
    header=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'
    initial,slow=(header,body) if stage=='body' else (b'',header+body)
    if stage=='chunk_framing':initial=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n';slow=b'10;extension='+b'x'*30+b'\r\n'+body+b'\r\n0\r\n\r\n'
    def serve():
     try:
      server.sendall(initial)
      for byte in slow:
       if stop.wait(0.03):break
       server.sendall(bytes([byte]))
     except OSError:pass
     finally:server.close()
    worker=threading.Thread(target=serve,daemon=True);worker.start()
    class LocalConnection:
     def __init__(self,*args,**kwargs):self.sock=client;self.response_class=http.client.HTTPResponse
     def connect(self):pass
     def request(self,*args,**kwargs):pass
     def getresponse(self):
      response=self.response_class(self.sock);response_holder.append(response);response.begin();return response
     def close(self):
      for response in response_holder:response.close()
      client.close()
    try:
     with patch.object(self.module,'TIMEOUT',0.15):
      start=time.monotonic()
      self.refused(lambda:self.module.GitHubReadAdapter(TOKEN,connection_factory=LocalConnection).read('/repos/Xynes-Studio/xynes-infra'))
      elapsed=time.monotonic()-start
     self.assertLess(elapsed,0.4,'socket inactivity timeouts must not replace the absolute response deadline')
    finally:
     stop.set();client.close();worker.join(timeout=1);self.assertFalse(worker.is_alive())
 def test_real_http_response_complete_length_chunked_and_close_delimited_json(self):
  for framing in ['length','chunked','close']:
   with self.subTest(framing=framing):
    client,server=socket.socketpair();response_holder=[];body=b'{"id":1415400917}'
    if framing=='length':headers=b'Content-Length: '+str(len(body)).encode()+b'\r\n';content=body
    elif framing=='chunked':headers=b'Transfer-Encoding: chunked\r\n';content=f'{len(body):x}\r\n'.encode()+body+b'\r\n0\r\nX-Trailer: complete\r\n\r\n'
    else:headers=b'Connection: close\r\n';content=body
    server.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'+headers+b'\r\n'+content);server.close()
    class LocalConnection:
     def __init__(self,*args,**kwargs):self.sock=client;self.response_class=http.client.HTTPResponse
     def connect(self):pass
     def request(self,*args,**kwargs):pass
     def getresponse(self):
      response=self.response_class(self.sock);response_holder.append(response);response.begin()
      if response.will_close:client.close();self.sock=None
      return response
     def close(self):
      for response in response_holder:response.close()
      client.close()
    try:self.assertEqual(self.module.GitHubReadAdapter(TOKEN,connection_factory=LocalConnection).read('/repos/Xynes-Studio/xynes-infra'),{'id':1415400917})
    finally:client.close()

if __name__=='__main__':unittest.main()
