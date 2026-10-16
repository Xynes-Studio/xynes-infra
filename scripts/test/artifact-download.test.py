#!/usr/bin/env python3
"""Use injected HTTPS replies and real temp files; no network or credentials."""
import importlib.util
import io
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
TOKEN='ghs_INERT_DO_NOT_PRINT_DOWNLOAD_TOKEN'
URL='https://productionresultssa1.blob.core.windows.net/actions-results/fixture.zip?sig=inert-private-url'

class Reply:
 def __init__(self,status,headers,body=b''):self.status=status;self.headers=headers;self.body=io.BytesIO(body)
 def getheaders(self):return self.headers
 def read1(self,size):return self.body.read(size)
class Connection:
 def __init__(self,host,reply):self.host=host;self.reply=reply;self.calls=[];self.sock=self;self.closed=False
 def connect(self):pass
 def settimeout(self,timeout):self.timeout=timeout
 def request(self,*args,**kwargs):self.calls.append((args,kwargs))
 def getresponse(self):return self.reply
 def close(self):self.closed=True

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/artifact_download.py';self.assertTrue(path.exists(),'artifact HTTPS download adapter missing')
  spec=importlib.util.spec_from_file_location('artifact_download',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.directory=Path(self.temp.name);self.directory.chmod(0o700);self.path=self.directory/'bundle.zip'
  self.replies=[Reply(302,[('Location',URL)]),Reply(200,[('Content-Type','application/zip'),('Content-Length','7')],b'fixture')]
  self.connections=[];self.contexts=[]
 def factory(self,host,**options):
  self.contexts.append(options['context']);connection=Connection(host,self.replies[len(self.connections)]);self.connections.append(connection);return connection
 def adapter(self):return self.module.ArtifactDownloader(TOKEN,connection_factory=self.factory)
 def refused(self,call):
  with self.assertRaises(self.module.ArtifactDownloadError) as caught:call()
  self.assertEqual(str(caught.exception),'Artifact download refused')
  for private in (TOKEN,URL,'inert-private-url'):self.assertNotIn(private,repr(caught.exception))
 def test_exact_api_redirect_and_credential_free_cdn_download(self):
  self.adapter().download(123,self.path,7)
  self.assertEqual(self.path.read_bytes(),b'fixture');self.assertEqual(self.path.stat().st_mode&0o777,0o600)
  self.assertEqual([c.host for c in self.connections],['api.github.com','productionresultssa1.blob.core.windows.net'])
  args,kwargs=self.connections[0].calls[0]
  self.assertEqual(args,('GET','/repos/Xynes-Studio/xynes-infra/actions/artifacts/123/zip'))
  self.assertEqual(kwargs['headers']['Authorization'],'Bearer '+TOKEN)
  self.assertEqual(self.connections[1].calls[0][1]['headers'],{})
  for context in self.contexts:self.assertTrue(context.check_hostname);self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
  self.assertTrue(all(c.closed for c in self.connections));self.assertNotIn(TOKEN,repr(self.adapter()))
 def test_unsafe_redirects_refuse_without_forwarding_credentials(self):
  for url in ['http://productionresultssa1.blob.core.windows.net/a','https://foreign.invalid/a','https://api.github.com/a','https://user@productionresultssa1.blob.core.windows.net/a','https://productionresultssa1.blob.core.windows.net:444/a','https://127.0.0.1/a',URL+'#fragment']:
   self.connections=[];self.replies[0]=Reply(302,[('Location',url)])
   self.refused(lambda:self.adapter().download(123,self.path,7));self.assertEqual(len(self.connections),1);self.assertFalse(self.path.exists())
 def test_existing_output_and_unsafe_parent_refuse_without_network(self):
  self.path.write_bytes(b'preserve');self.refused(lambda:self.adapter().download(123,self.path,7))
  self.assertEqual(self.path.read_bytes(),b'preserve');self.assertEqual(self.connections,[])
  self.path.unlink();self.directory.chmod(0o755);self.refused(lambda:self.adapter().download(123,self.path,7));self.assertEqual(self.connections,[])
 def test_bad_status_redirect_chain_and_duplicate_headers_refuse(self):
  for replies in [[Reply(401,[])],[Reply(302,[('Location',URL),('location',URL)])],
                  [Reply(302,[('Location',URL)]),Reply(302,[('Location',URL)])],
                  [Reply(302,[('Location',URL)]),Reply(200,[('Content-Type','text/html')],b'fixture')]]:
   self.connections=[];self.replies=replies;self.refused(lambda:self.adapter().download(123,self.path,7));self.assertFalse(self.path.exists())
 def test_partial_oversize_and_length_mismatch_remove_owned_output(self):
  for reply in [Reply(200,[('Content-Type','application/zip')],b'012345678'),Reply(200,[('Content-Type','application/zip'),('Content-Length','7')],b'bad'),Reply(200,[('Content-Type','application/zip'),('Content-Encoding','gzip')],b'fixture')]:
   self.connections=[];self.replies=[Reply(302,[('Location',URL)]),reply]
   self.refused(lambda:self.adapter().download(123,self.path,7));self.assertFalse(self.path.exists())
 def test_invalid_id_size_and_token_refuse(self):
  for identifier in (True,0,-1,'123'):self.refused(lambda:self.adapter().download(identifier,self.path,7))
  for size in (True,0,-1,33*1024**3):self.refused(lambda:self.adapter().download(123,self.path,size))
  self.refused(lambda:self.module.ArtifactDownloader('bad\n'+TOKEN));self.assertEqual(self.connections,[])

if __name__=='__main__':unittest.main()
