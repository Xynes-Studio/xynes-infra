#!/usr/bin/env python3
"""Download one native GitHub artifact, with no credential forwarding or logs."""
import http.client
import os
from pathlib import Path
import re
import ssl
import stat
import time
from urllib.parse import urlsplit

try:
    from . import admission, github_read, registry_io
except ImportError:
    import admission
    import github_read
    import registry_io

MAX_BYTES=32*1024**3


class ArtifactDownloadError(ValueError):pass


def require(value):
    if not value:raise ArtifactDownloadError('Artifact download refused')


def metadata(response):
    pairs=response.getheaders();require(isinstance(pairs,list) and len(pairs)<=100)
    result={}
    for key,value in pairs:
        require(isinstance(key,str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}',key)
                and key.lower() not in result and isinstance(value,str) and len(value)<=16384
                and all(32<=ord(c)<=126 for c in value))
        result[key.lower()]=value
    return result


def cdn_url(value):
    require(isinstance(value,str) and 0<len(value)<=8192 and all(33<=ord(c)<=126 for c in value))
    url=urlsplit(value)
    require(url.scheme=='https' and not url.username and not url.password and not url.fragment
            and url.port is None and url.hostname==url.netloc
            and re.fullmatch(r'[a-z0-9-]+\.(?:blob\.core\.windows\.net|actions\.githubusercontent\.com)',url.hostname or '')
            and url.path.startswith('/') and url.path!='/' and '\\' not in value)
    return url


class ArtifactDownloader:
    def __init__(self,token,*,connection_factory=http.client.HTTPSConnection):
        require(isinstance(token,str) and registry_io.TOKEN_PATTERN.fullmatch(token) and callable(connection_factory))
        self._token,self._factory=token,connection_factory

    def __repr__(self):return 'ArtifactDownloader()'

    def _reply(self,host,target,headers,timeout):
        context=ssl.create_default_context();require(context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED)
        connection=self._factory(host,timeout=min(timeout,15),context=context)
        deadline=time.monotonic()+timeout
        def remaining():
            left=deadline-time.monotonic();require(left>0)
            if connection.sock is not None:connection.sock.settimeout(left)
        def response_class(sock,*args,**kwargs):
            response=http.client.HTTPResponse(sock,*args,**kwargs)
            response.fp=github_read.DeadlineReader(response.fp,sock,deadline)
            return response
        connection.response_class=response_class
        try:
            connection.connect();remaining()
            connection.request('GET',target,headers=headers,body=b'')
            remaining();response=connection.getresponse();require(type(response.status) is int)
            return connection,response,remaining
        except Exception:
            try:connection.close()
            except Exception:pass
            raise ArtifactDownloadError('Artifact download refused') from None

    def download(self,artifact_id,destination,max_bytes):
        connections=[];directory=descriptor=None;owned=None
        try:
            require(admission.positive(artifact_id) and type(max_bytes) is int and 0<max_bytes<=MAX_BYTES)
            path=Path(destination)
            require(path.is_absolute() and not path.exists() and not path.is_symlink())
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            info=os.fstat(directory);require(stat.S_IMODE(info.st_mode)==0o700 and info.st_uid==os.getuid())
            headers={'Authorization':'Bearer '+self._token,'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':github_read.API_VERSION,'User-Agent':github_read.USER_AGENT,'Accept-Encoding':'identity'}
            connection,response,_=self._reply('api.github.com',admission.PREFIX+f'/actions/artifacts/{artifact_id}/zip',headers,15)
            connections.append(connection);require(response.status==302)
            redirect=cdn_url(metadata(response).get('location'))
            connection.close();connections.pop()
            # The signed CDN URL is data from verified GitHub HTTPS. It is never
            # printed, serialized or followed with the GitHub Authorization header.
            target=redirect.path+('?' + redirect.query if redirect.query else '')
            connection,response,remaining=self._reply(redirect.hostname,target,{},1800)
            connections.append(connection);require(response.status==200)
            values=metadata(response)
            require(values.get('content-type','').split(';',1)[0].strip() in ('application/zip','application/octet-stream')
                    and values.get('content-encoding','identity').lower()=='identity')
            length=values.get('content-length')
            require(length is None or re.fullmatch(r'[0-9]{1,12}',length) and 0<int(length)<=max_bytes)
            descriptor=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
            info=os.fstat(descriptor);owned=(info.st_dev,info.st_ino)
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600 and info.st_uid==os.getuid() and info.st_nlink==1)
            total=0
            while True:
                remaining();limit=min(65536,max_bytes+1-total);chunk=response.read1(limit)
                require(isinstance(chunk,bytes) and len(chunk)<=limit)
                if not chunk:break
                total+=len(chunk);require(total<=max_bytes)
                position=0
                while position<len(chunk):
                    written=os.write(descriptor,chunk[position:]);require(written>0);position+=written
            require(total>0 and (length is None or total==int(length)))
            os.fsync(descriptor)
            final=os.stat(path.name,dir_fd=directory,follow_symlinks=False)
            require((final.st_dev,final.st_ino)==owned and stat.S_ISREG(final.st_mode)
                    and final.st_nlink==1 and final.st_size==total and stat.S_IMODE(final.st_mode)==0o600)
        except Exception:
            if owned is not None:
                try:
                    current=os.stat(path.name,dir_fd=directory,follow_symlinks=False)
                    if (current.st_dev,current.st_ino)==owned:os.unlink(path.name,dir_fd=directory)
                except OSError:pass
            raise ArtifactDownloadError('Artifact download refused') from None
        finally:
            failed=False
            for connection in connections:
                try:connection.close()
                except Exception:failed=True
            for fd in (descriptor,directory):
                if fd is not None:
                    try:os.close(fd)
                    except OSError:failed=True
            if failed:raise ArtifactDownloadError('Artifact download refused') from None
