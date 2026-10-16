#!/usr/bin/env python3
"""Fixed root runtime composition, called only by the supervised sealed worker.

Nothing here creates an approval or bypasses the release coordinator. Credentials
come from a separate two-key root file; provider credentials are read only by the
backup/product operations that need them. Immutable code and the actual systemd
worker identity are validated by the outer boundary and supervisor, respectively.
Private attempt files remain for reconciliation on both success and failure.
"""
from dataclasses import dataclass,field
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile

try:
    from . import artifact_download,deploy_request,github_read,host_assets,host_backup,host_controls
    from . import native_attestation,registry_io,root_executor
except ImportError:
    import artifact_download,deploy_request,github_read,host_assets,host_backup,host_controls
    import native_attestation,registry_io,root_executor

ERROR='Host deployment runtime refused'
CLEAN_ENV={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C','LC_ALL':'C'}
KEYS={'RELEASE_POLICY_READ_TOKEN','GHCR_READ_TOKEN'}

class RuntimeError(ValueError):pass

def require(value):
    if not value:raise RuntimeError(ERROR)

@dataclass(frozen=True)
class Configuration:
    root:Path=field(default=Path('/'))
    owner_uid:int=0

    def path(self,name):
        require(type(name) is str and name.startswith('/'))
        return self.root/name.lstrip('/')

    def check(self):
        require(isinstance(self.root,Path) and self.root.is_absolute()
                and type(self.owner_uid) is int and self.owner_uid>=0
                and os.geteuid()==self.owner_uid and self.root.resolve(strict=True)==self.root)
        if self.root==Path('/'):
            require(self.owner_uid==0 and sys.platform.startswith('linux')
                    and platform.machine() in ('x86_64','amd64'))
        # Alternate roots are explicit inert trusted fixture configuration.
        # The installed supervisor always invokes Runtime(stage) defaults.
        self.protected(self.root,directory=True)

    def protected(self,path,*,directory=False,mode=None):
        require(isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path
                and (path==self.root or self.root in path.parents))
        current=path
        while True:
            info=current.lstat()
            require(info.st_uid==self.owner_uid and not info.st_mode&0o022
                    and (stat.S_ISDIR(info.st_mode) if current!=path or directory else stat.S_ISREG(info.st_mode)))
            if current==path:
                require(mode is None or stat.S_IMODE(info.st_mode)==mode)
                if not directory:require(info.st_nlink==1)
            if current==self.root:break
            current=current.parent

@dataclass(frozen=True,repr=False)
class Credentials:
    values:dict=field(repr=False)
    def __repr__(self):return 'Credentials()'

def stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def credentials(config):
    try:
        config.check();path=config.path('/etc/xynes/deploy/controller.env')
        config.protected(path,mode=0o600)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as file:
            before=os.fstat(file.fileno());require(0<before.st_size<=4096)
            raw=file.read(4097)
            require(len(raw)==before.st_size and stamp(before)==stamp(os.fstat(file.fileno()))
                    and stamp(before)==stamp(path.lstat()))
        values={}
        for line in raw.decode('ascii').splitlines():
            key,separator,value=line.partition('=')
            require(separator and key in KEYS and key not in values
                    and registry_io.TOKEN_PATTERN.fullmatch(value))
            values[key]=value
        require(set(values)==KEYS)
        return Credentials(values)
    except Exception:raise RuntimeError(ERROR) from None

def workflow(raw):
    try:
        require(type(raw) in (str,bytes) and 0<len(raw)<=1024*1024)
        value=github_read.strict_json(raw.encode('utf-8') if type(raw) is str else raw)
        require(type(value) is dict)
        return value
    except Exception:raise RuntimeError(ERROR) from None

def native(config,secrets):
    policy=secrets.values['RELEASE_POLICY_READ_TOKEN'];registry=secrets.values['GHCR_READ_TOKEN']
    reader=github_read.GitHubReadAdapter(policy)
    packages=github_read.GitHubReadAdapter(registry)
    downloader=artifact_download.ArtifactDownloader(policy)
    def read(path):
        # Both native adapters validate the exact repository/package allowlist.
        # Source, reviews, environment policy and artifacts never use registry auth.
        require(type(path) is str)
        return (packages if path.startswith('/orgs/Xynes-Studio/packages/container/') else reader).read(path)
    def verify(*,image,controller):
        return native_attestation.verify(image=image,controller=controller,
            binary=config.path('/usr/local/lib/xynes/tools/gh/2.102.0/gh'),
            api_token=policy,registry_token=registry)
    return root_executor.NativeAdapters(read=read,read_graphql=reader.read_graphql,
        download=downloader.download,verify=verify,parse_workflow=workflow)

class Runtime:
    def __init__(self,stage,*,configuration=Configuration()):
        require(stage in deploy_request.STAGES and type(configuration) is Configuration)
        self.stage,self.configuration=stage,configuration

    def operations(self):
        # Only the default actual host creates deployment operation adapters.
        # Alternate-root tests replace this method with inert dependencies.
        require(self.configuration==Configuration())
        try:
            from . import host_smoke,host_smoke_setup,host_seed,host_compose
        except ImportError:
            import host_smoke,host_smoke_setup,host_seed,host_compose
        controls=host_controls.Controls(self.stage)
        store=host_assets.Store(self.stage)
        product=host_smoke.Smoke(self.stage)
        fixtures=host_smoke_setup.Setup(self.stage)
        def product_smoke(assets,*,phase):
            try:
                require(phase in ('current','rollback') and assets.prepared.stage==self.stage)
                setup=fixtures.run()
                require(type(setup) is dict and setup.get('stage')==self.stage and setup.get('configured') is True)
                # Workspace creation occurs after the initial platform seeds.
                # Run the same immutable implementations again for its new CMS
                # defaults before accepting directory-first product flows.
                seed=host_seed.Driver(host_compose.Driver(self.stage),verify_counts=controls.seed_counts)
                result=seed.run(assets.prepared,assets.migration,work=Path('/var/lib/xynes/deploy/work'))
                require(type(result) is dict and result.get('stage')==self.stage and result.get('seeds_applied') is True)
                return product.run(assets,phase=phase)
            except Exception:raise RuntimeError(ERROR) from None
        def backup(stage,*,phase,current,previous,state,token):
            require(stage==self.stage and phase in ('before-bootstrap','before-sql')
                    and state.stage==stage and current.prepared.stage==stage)
            return host_backup.create(state,token)
        return root_executor.Operations(target_audit=controls.target_audit,warm=controls.warm,
            backup=backup,product_smoke=product_smoke,load_release=store.load_release,
            persist=store.persist,persist_rollback=store.persist_rollback,seed_counts=controls.seed_counts)

    def execute(self,raw):
        old_environment=old_umask=None
        try:
            # Refuse malformed data before opening credentials or creating files.
            request=deploy_request.parse(raw,stage=self.stage)
            config=self.configuration;config.check()
            parent=config.path('/var/lib/xynes/deploy/work')
            state=config.path('/var/lib/xynes/deploy/state')
            for path in (parent,state):config.protected(path,directory=True,mode=0o700)
            secrets=credentials(config)
            clients=native(config,secrets);operations=self.operations()
            old_environment=dict(os.environ);old_umask=os.umask(0o077)
            os.environ.clear();os.environ.update(CLEAN_ENV)
            work=Path(tempfile.mkdtemp(prefix=self.stage+'-',dir=parent))
            config.protected(work,directory=True,mode=0o700)
            descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
            result=root_executor.run(raw,stage=self.stage,work=work,state_directory=state,
                                     native=clients,operations=operations)
            require(type(result) is dict and set(result)=={'artifact_kind','stage','token','controller',
                    'publication','source_manifest_sha256','deployment_accepted'}
                    and result['artifact_kind']=='root-deployment-result' and result['stage']==self.stage
                    and result['deployment_accepted'] is True and result['controller']==request['controller']
                    and result['publication']==request['publication']
                    and type(result['token']) is str and re.fullmatch(r'[a-f0-9]{64}',result['token'])
                    and type(result['source_manifest_sha256']) is str
                    and re.fullmatch(r'[a-f0-9]{64}',result['source_manifest_sha256']))
            return result
        except Exception:raise RuntimeError(ERROR) from None
        finally:
            if old_environment is not None:
                os.environ.clear();os.environ.update(old_environment)
            if old_umask is not None:os.umask(old_umask)
