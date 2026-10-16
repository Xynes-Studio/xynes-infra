#!/usr/bin/env python3
"""Executable bounds and credential boundary for the concrete HTTPS adapter."""
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import registry_https

class Reply:
    status=200
    def __init__(self, body=b'ok', headers=None):self.body=io.BytesIO(body);self.headers=headers or [('Content-Length',str(len(body)))];self.read_sizes=[]
    def getheaders(self):return self.headers
    def read(self,size):self.read_sizes.append(size);return self.body.read(size)
class Connection:
    def __init__(self, reply):self.reply=reply;self.closed=False;self.parts=[];self.headers=[];self.sock=self
    def connect(self):pass
    def settimeout(self,value):self.timeout=value
    def putrequest(self,*args,**kwargs):self.request=(args,kwargs)
    def putheader(self,*args):self.headers.append(args)
    def endheaders(self):pass
    def send(self,b):self.parts.append(b)
    def getresponse(self):return self.reply
    def close(self):self.closed=True
class Tests(unittest.TestCase):
    def call(self,url='https://ghcr.io/v2/',**overrides):
        params=dict(headers={},body=b'',timeout=2,max_response_bytes=20,follow_redirects=False);params.update(overrides)
        return registry_https.HTTPSRegistryTransport().request('GET',url,**params)
    def test_verified_tls_bounded_response_and_connection_closure(self):
        c=Connection(Reply())
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c) as construct:
            r=self.call()
        self.assertEqual(r.body,b'ok');self.assertTrue(c.closed)
        ctx=construct.call_args.kwargs['context'];self.assertTrue(ctx.check_hostname);self.assertEqual(ctx.verify_mode,registry_https.ssl.CERT_REQUIRED)
        self.assertEqual(c.request[0],('GET','/v2/'));self.assertTrue(all(0<n<=21 for n in c.reply.read_sizes))
    def test_redirect_is_returned_without_following(self):
        reply=Reply(headers=[('Location','https://foreign.invalid/private')]);reply.status=307;c=Connection(reply)
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c) as construct:r=self.call()
        self.assertEqual(r.status,307);self.assertEqual(construct.call_count,1)
    def test_cdn_never_receives_credentials_or_writes(self):
        for headers in [{'Authorization':'Bearer fake'}, {'Cookie':'private'}, {'Proxy-Authorization':'Basic fake'}]:
            with self.assertRaises(registry_https.TransportError):self.call('https://pkg-containers.githubusercontent.com/blob?sig=fake',headers=headers)
        with self.assertRaises(registry_https.TransportError):
            registry_https.HTTPSRegistryTransport().request('PUT','https://pkg-containers.githubusercontent.com/blob',headers={},body=b'',timeout=2,max_response_bytes=20,follow_redirects=False)
    def test_foreign_origins_url_controls_ports_and_userinfo_refused(self):
        for url in ['http://ghcr.io/v2/','https://ghcr.io:443/v2/','https://ghcr.io@evil.invalid/v2/','https://ghcr.io/v2/#x','https://evil.invalid/v2/','https://ghcr.io/\r\nx']:
            with self.subTest(url=url),self.assertRaises(registry_https.TransportError):self.call(url)
    def test_no_redirect_retry_or_unbounded_limits(self):
        for changes in [{'follow_redirects':True},{'timeout':0},{'timeout':121},{'max_response_bytes':0},{'max_response_bytes':262145}]:
            with self.assertRaises(registry_https.TransportError):self.call(**changes)
    def test_oversized_body_refused_while_receiving(self):
        c=Connection(Reply(b'x'*21))
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c),self.assertRaises(registry_https.TransportError):self.call()
        self.assertTrue(c.closed)
    def test_duplicate_response_headers_and_injected_request_headers_refused(self):
        c=Connection(Reply(headers=[('Location','one'),('location','two')]))
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c),self.assertRaises(registry_https.TransportError):self.call()
        for h in [{'Authorization':'Bearer a\nb'}, {'Host':'foreign.invalid'}, {'Transfer-Encoding':'chunked'}]:
            with self.assertRaises(registry_https.TransportError):self.call(headers=h)
    def test_hashed_stream_contract_is_consumed_with_explicit_length(self):
        c=Connection(Reply(b''));stream=io.BytesIO(b'abc')
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c):
            registry_https.HTTPSRegistryTransport().request('PUT','https://ghcr.io/v2/repo/blobs/uploads/id',headers={'Content-Length':'3'},body=stream,timeout=2,max_response_bytes=20,follow_redirects=False)
        self.assertEqual(b''.join(c.parts),b'abc');self.assertTrue(c.closed)
    def test_incomplete_and_extra_upload_streams_refused_without_retry(self):
        for body in [b'ab',b'abcd']:
            c=Connection(Reply())
            with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c) as construct,self.assertRaises(registry_https.TransportError):
                registry_https.HTTPSRegistryTransport().request('PUT','https://ghcr.io/upload',headers={'Content-Length':'3'},body=io.BytesIO(body),timeout=2,max_response_bytes=20,follow_redirects=False)
            self.assertEqual(construct.call_count,1);self.assertTrue(c.closed)
    def test_connection_close_exception_cannot_escape_redaction(self):
        c=Connection(Reply());c.close=lambda: (_ for _ in ()).throw(ValueError('SECRET-CLOSE'))
        with patch.object(registry_https.http.client,'HTTPSConnection',return_value=c):
            try:self.call()
            except Exception as e:self.assertNotIn('SECRET-CLOSE',str(e))

    def test_underlying_exception_redacted_and_single_attempt(self):
        with patch.object(registry_https.http.client,'HTTPSConnection',side_effect=ValueError('SECRET')) as construct:
            with self.assertRaises(registry_https.TransportError) as e:self.call()
        self.assertNotIn('SECRET',str(e.exception));self.assertEqual(construct.call_count,1)
if __name__=='__main__':unittest.main()
