#!/usr/bin/env python3
"""Disposable CI product flow adapter; no hosted files, DNS or release state.

Only the two fixed QA origins map to explicit loopback ports. Injected transports
are trusted local fixture boundaries. Success here does not publish or accept a
release; artifact identity and the eight-service Docker lifecycle belong to the
caller. Credentials and provisioned configuration remain in memory only.
"""
import copy
import http.client
import secrets
import time
from urllib.parse import urlsplit
try:
    from . import host_smoke as smoke, host_smoke_setup as setup
except ImportError:
    import host_smoke as smoke, host_smoke_setup as setup

ERROR='CI product smoke refused'
class RuntimeError(ValueError):pass

STEP_LABELS=frozenset({
    'login','auth-login','auth-refresh','gateway-me','workspace-membership',
    'unauthorized-denial','foreign-stage-denial','nonmember-workspace-denial',
    'cms-directory-create','document-create','document-read','cms-entry-create',
    'cms-entry-read','cms-directory-list','cms-entry-publish','cms-published-read',
    'owned-entry-delete','owned-directory-delete',
})

def safe_steps(steps):
    """Project only closed-set flow observations; discard malformed rows."""
    if type(steps) is not list:return []
    rows=[]
    for row in steps[:128]:
        if (type(row) is dict and type(row.get('flow')) is str and row['flow'] in STEP_LABELS
                and type(row.get('status')) is int and 100<=row['status']<=599
                and type(row.get('observed_at')) is int and 0<=row['observed_at']<=9007199254740991):
            rows.append({key:row[key] for key in ('flow','status','observed_at')})
    return rows

def failure_evidence(*,phase,failed_flow_phase,steps):
    """Author the sole diagnostic shape; never copy exceptions or product data."""
    return {'result':'refused','storage_verified':False,
            'phase':phase if type(phase) is str and phase in ('provision','flow','cleanup') else 'provision',
            'failed_flow_phase':failed_flow_phase if type(failed_flow_phase) is str and failed_flow_phase in STEP_LABELS else None,
            'steps':safe_steps(steps)}

def require(value):
    if not value:raise RuntimeError(ERROR)

class EphemeralStore:
    def __init__(self):self.rows={};self.active=False
    def __enter__(self):
        require(not self.active);self.active=True;return self
    def __exit__(self,*args):self.rows.clear();self.active=False
    def check(self,name):require(self.active and name in ('qa.json','qa.setup.json'))
    def read(self,name):self.check(name);return copy.deepcopy(self.rows.get(name))
    def write(self,name,value):self.check(name);self.rows[name]=copy.deepcopy(value)

class LocalTransport:
    def __init__(self,*,auth_port,gateway_port):
        require(all(type(port) is int and 1<=port<=65535 for port in (auth_port,gateway_port)))
        self.ports={'supabase.qa.xynes.com':auth_port,'api.qa.xynes.com':gateway_port}
    def request(self,method,url,headers,body):
        connection=None
        try:
            require(type(url) is str and 0<len(url)<=16384 and not any(ord(c)<33 or ord(c)==127 for c in url))
            parsed=urlsplit(url)
            require(parsed.scheme=='https' and parsed.netloc in self.ports and not parsed.fragment
                    and url.startswith('https://'+parsed.netloc+'/') and parsed.path.startswith('/')
                    and not parsed.path.startswith('//') and '\\' not in url
                    and method in ('GET','POST','PUT','DELETE') and type(headers) is dict
                    and (body is None or type(body) is bytes and len(body)<=smoke.MAX_RESPONSE))
            require(all(type(k) is str and type(v) is str and k and all(32<=ord(c)<127 for c in k+v)
                        and ':' not in k and not any(c.isspace() for c in k)
                        and k.lower() not in ('host','connection','content-length','transfer-encoding','accept-encoding')
                        for k,v in headers.items()))
            require(len({k.lower() for k in headers})==len(headers)
                    and sum(len(k)+len(v)+4 for k,v in headers.items())<=smoke.MAX_HEADERS)
            path=parsed.path
            if parsed.netloc=='supabase.qa.xynes.com':
                require(path.startswith('/auth/v1/'));path=path[len('/auth/v1'):]
            require('/storage/' not in path)
            target=path+('?' + parsed.query if parsed.query else '')
            deadline=time.monotonic()+smoke.HTTP_TIMEOUT
            connection=http.client.HTTPConnection('127.0.0.1',self.ports[parsed.netloc],timeout=smoke.HTTP_TIMEOUT)
            def response_class(sock,*args,**kwargs):
                response=http.client.HTTPResponse(sock,*args,**kwargs)
                response.fp=smoke.ResponseReader(response.fp,sock,deadline);return response
            connection.response_class=response_class
            connection.connect()
            connection.request(method,target,body=body,headers={**headers,'Host':parsed.netloc,'Connection':'close','Accept-Encoding':'identity'})
            require(time.monotonic()<deadline);connection.sock.settimeout(max(.001,deadline-time.monotonic()))
            response=connection.getresponse();items=response.getheaders()
            require(len(items)<=100 and not 300<=response.status<400
                    and sum(len(k)+len(v)+4 for k,v in items)<=smoke.MAX_HEADERS)
            for key in ('content-length','transfer-encoding','content-encoding','location','content-type'):
                require(sum(k.lower()==key for k,_ in items)<=1)
            metadata={k.lower():v for k,v in items}
            length=metadata.get('content-length');transfer=metadata.get('transfer-encoding')
            require(metadata.get('content-encoding','identity')=='identity' and transfer in (None,'chunked')
                    and not (transfer and length is not None))
            if length is not None:require(length.isdigit() and int(length)<=smoke.MAX_RESPONSE)
            response.fp.headers=False
            chunks=[];received=0
            while True:
                require(time.monotonic()<deadline)
                chunk=response.read1(min(65536,smoke.MAX_RESPONSE+1-received))
                if not chunk:break
                received+=len(chunk);require(received<=smoke.MAX_RESPONSE);chunks.append(chunk)
            require(time.monotonic()<deadline and (length is None or received==int(length)))
            return response.status,metadata,b''.join(chunks)
        except Exception:raise RuntimeError(ERROR) from None
        finally:
            if connection:connection.close()

def execute_flow(*,auth_port,gateway_port,values,keys_loader,clock=time.time,sleep=time.sleep,transport=None):
    """Run real provisioning and smoke logic; return only sanitized observations.

    The trusted injected transport exists for tests, not workflow request fields.
    No hosted UID checks or hosted smoke wrapper is invoked. Cleanup is mandatory
    on both success and failure and may never turn a failed flow into success.
    """
    phase='provision';failed_flow_phase=None;flow=None
    evidence={'nonce':secrets.token_hex(16),'steps':[]}
    try:
        local=LocalTransport(auth_port=auth_port,gateway_port=gateway_port)
        require(type(values) is dict and set(values)=={'SUPABASE_SERVICE_ROLE_KEY','R2_ACCOUNT_ID','R2_BUCKET'}
                and all(type(value) is str and value for value in values.values())
                and all(callable(value) for value in (keys_loader,clock,sleep)))
        selected=transport if transport is not None else local
        with EphemeralStore() as store:
            journal=setup.new_journal('qa');store.write('qa.setup.json',journal)
            setup.Provision('qa',store,journal,selected,values,keys_loader,clock).run()
            config=smoke.qualify(store.read('qa.json'),'qa',keys_loader,int(clock()))
            flow=smoke.Flow('qa',config,selected,evidence,clock,sleep)
            phase='flow'
            try:flow.execute(keys_loader)
            except Exception:
                # Capture before cleanup's request labels overwrite Flow.phase.
                failed_flow_phase=flow.phase
                raise
            finally:
                try:flow.cleanup()
                except Exception:
                    if failed_flow_phase is None:
                        phase='cleanup';failed_flow_phase=flow.phase
                    raise
            require(evidence.get('storage_verified') is False)
            return {'stage':'qa','result':'passed','storage_verified':False,
                    'storage_skip_reason':evidence['storage_skip_reason'],
                    'steps':safe_steps(evidence['steps']),'observed_at':int(clock())}
    except Exception:
        error=RuntimeError(ERROR)
        error.safe_evidence=failure_evidence(phase=phase,failed_flow_phase=failed_flow_phase,
                                             steps=evidence['steps'])
        raise error from None
