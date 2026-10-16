#!/usr/bin/env python3
"""Verified HTTPS, bounded bytes, no automatic redirects/retries or diagnostics."""
import http.client
import math
import re
import ssl
import time
from urllib.parse import urlsplit
try:
    from .registry_io import Response
except ImportError:
    from registry_io import Response

class TransportError(ValueError):
    pass

def require(value):
    if not value:
        raise TransportError('HTTPS registry request refused')

class HTTPSRegistryTransport:
    def request(self, method, url, *, headers, body, timeout, max_response_bytes, follow_redirects=False):
        connection = None
        try:
            require(method in {'GET','HEAD','POST','PUT'} and follow_redirects is False)
            require(isinstance(url,str) and 0 < len(url) <= 8192 and all(33 <= ord(c) <= 126 for c in url))
            parsed=urlsplit(url)
            require(parsed.scheme=='https' and parsed.netloc in {'ghcr.io','pkg-containers.githubusercontent.com'} and not parsed.fragment)
            require(isinstance(timeout,(int,float)) and not isinstance(timeout,bool) and math.isfinite(timeout) and 0 < timeout <= 120)
            require(type(max_response_bytes) is int and 0 < max_response_bytes <= 256*1024)
            require(isinstance(headers,dict))
            normalized={}
            for name,value in headers.items():
                require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}',name) and name.lower() not in normalized)
                require(isinstance(value,str) and len(value)<=16384 and all(32 <= ord(c) <= 126 for c in value))
                require(name.lower() not in {'host','transfer-encoding','connection','proxy-authorization','cookie'})
                normalized[name.lower()]=value
            if parsed.netloc=='pkg-containers.githubusercontent.com':
                require(method=='GET' and not normalized and body==b'')
            stream=not isinstance(body,bytes)
            require(not stream or callable(getattr(body,'read',None)))
            if stream:
                length=normalized.get('content-length','')
                require(re.fullmatch(r'[0-9]{1,12}',length))
                length=int(length)
            else:
                length=len(body)
                require('content-length' not in normalized or normalized['content-length']==str(length))
            require(length <= 2*1024**3 and (method not in {'GET','HEAD'} or length==0))
            deadline=time.monotonic()+timeout
            def remaining():
                value=deadline-time.monotonic();require(value>0)
                if connection.sock is not None:connection.sock.settimeout(value)
                return value
            connection=http.client.HTTPSConnection(parsed.netloc,timeout=timeout,context=ssl.create_default_context())
            connection.connect();remaining()
            target=parsed.path or '/'
            if parsed.query:target+='?'+parsed.query
            connection.putrequest(method,target,skip_accept_encoding=True)
            for name,value in normalized.items():connection.putheader(name,value)
            if method in {'POST','PUT'} and 'content-length' not in normalized:connection.putheader('Content-Length',str(length))
            connection.endheaders()
            if stream:
                sent=0
                while sent<length:
                    remaining();chunk=body.read(min(1024*1024,length-sent))
                    require(isinstance(chunk,bytes) and 0<len(chunk)<=min(1024*1024,length-sent))
                    connection.send(chunk);sent+=len(chunk)
                require(body.read(1)==b'')
            elif body:
                for offset in range(0,length,1024*1024):remaining();connection.send(body[offset:offset+1024*1024])
            remaining();response=connection.getresponse()
            pairs=response.getheaders();require(len(pairs)<=100)
            response_headers={};seen=set()
            for name,value in pairs:
                require(name.lower() not in seen);seen.add(name.lower());response_headers[name]=value
            chunks=[];received=0
            while True:
                remaining();chunk=response.read(min(65536,max_response_bytes+1-received))
                if not chunk:break
                received+=len(chunk);require(received<=max_response_bytes);chunks.append(chunk)
            return Response(response.status,response_headers,b''.join(chunks))
        except Exception:
            raise TransportError('HTTPS registry request refused') from None
        finally:
            if connection is not None:
                try:connection.close()
                except Exception:raise TransportError("HTTPS registry request refused") from None
