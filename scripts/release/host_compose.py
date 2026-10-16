#!/usr/bin/env python3
"""Fixed hosted Compose commands; this driver grants no release/stage authority.

The privileged caller must hold the host lock and warm leases and complete fresh
admission, signature verification, backup, migration and product acceptance phases.
Injected runner/environment_loader are trusted integration boundaries, never
request fields. No shell, build, deletion, database downgrade or data reset exists.
"""
import copy
from dataclasses import dataclass, field
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time

try:
    from . import publication_record
except ImportError:
    import publication_record

ERROR = 'Hosted Compose operation refused'
STAGES = {'qa', 'staging', 'prod'}
SERVICE_IMAGES = {
    'gateway':'IMAGE_GATEWAY', 'accounts-service':'IMAGE_ACCOUNTS',
    'authz-service':'IMAGE_AUTHZ', 'cms-core':'IMAGE_CMS', 'doc-service':'IMAGE_DOC',
    'storage-service':'IMAGE_STORAGE', 'telemetry-service':'IMAGE_TELEMETRY',
    'auth-app':'IMAGE_AUTH_APP', 'cms-console-web':'IMAGE_CMS_APP',
    'clamav-clamd':'IMAGE_ARCHIVE_SCANNER', 'clamav-freshclam':'IMAGE_FRESHCLAM',
    'libreoffice-sidecar':'IMAGE_LIBREOFFICE', 'supabase-db':'IMAGE_SUPABASE_DB',
    'supabase-auth':'IMAGE_SUPABASE_AUTH',
}
WORKLOAD_KEYS = {
    'xynes-gateway':'IMAGE_GATEWAY', 'xynes-accounts-service':'IMAGE_ACCOUNTS',
    'xynes-authz-service':'IMAGE_AUTHZ', 'xynes-cms-core':'IMAGE_CMS',
    'xynes-doc-service':'IMAGE_DOC', 'xynes-storage-service':'IMAGE_STORAGE',
    'xynes-telemetry-service':'IMAGE_TELEMETRY', 'xynes-auth-app':'IMAGE_AUTH_APP',
    'xynes-cms-console-web':'IMAGE_CMS_APP', 'archive-scanner':'IMAGE_ARCHIVE_SCANNER',
    'libreoffice':'IMAGE_LIBREOFFICE',
}
SERVICES = set(SERVICE_IMAGES)
PROCESSORS = {'clamav-clamd','clamav-freshclam','libreoffice-sidecar'}
CONTROLLED = PROCESSORS | {'storage-service'}
BASE = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock',
        '--config', '/etc/xynes/deploy/docker']
CLEAN_ENV = {'PATH':'/usr/bin:/bin', 'HOME':'/nonexistent', 'LANG':'C', 'LC_ALL':'C'}
MAX_CONFIG = 1024 * 1024
CID = re.compile(r'[a-f0-9]{64}')
INSPECT = ('{"id":{{json .Id}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},'
           '"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}')

class ComposeError(ValueError):
    """Diagnostics contain neither paths, rejected values nor command output."""

def require(condition):
    if not condition:
        raise ComposeError(ERROR)

def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

def private_environment(path, *, uid, gid):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as file:
            before = os.fstat(file.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == uid and before.st_gid == gid
                    and stat.S_IMODE(before.st_mode) == 0o640 and before.st_nlink == 1
                    and 0 < before.st_size <= 65536)
            raw = file.read(65537)
            require(len(raw) == before.st_size and identity(os.fstat(file.fileno())) == identity(before)
                    and identity(path.lstat()) == identity(before) and b'\x00' not in raw)
            return raw
    except Exception:
        raise ComposeError(ERROR) from None

def root_path(path, *, directory=False):
    # Reject every symlink and writable-by-nonroot ancestor, not only the leaf.
    for parent in reversed((path, *tuple(path.parents)[:-1])):
        info = parent.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode)
                and (stat.S_ISDIR(info.st_mode) if parent != path or directory else stat.S_ISREG(info.st_mode)))

def stage_environment(stage):
    require(stage in STAGES)
    base = Path('/srv/xynes') / stage
    env_file = base/'env'/('.env.'+stage)
    root_path(env_file)
    private_environment(env_file, uid=0, gid=grp.getgrnam('xynes').gr_gid)
    for relative in ('hosted.yml', stage+'.yml', 'internal-request-identities.yml',
                     'auth/compose.fragment.yml', 'auth/bootstrap.sh', 'auth/bootstrap.sql'):
        root_path(base/'compose'/relative)
    root_path(Path('/etc/xynes/deploy/docker'), directory=True)

def image_pointers(*, stage, receipt, manifest, public, controller):
    try:
        require(stage in STAGES)
        checked = publication_record.qualify(receipt=receipt, manifest=manifest,
                                             public=public, controller=controller)
        result = {}
        for row in checked['images']:
            if row['environment'] in (None, stage):
                key = WORKLOAD_KEYS[row['workload']]
                require(key not in result)
                result[key] = row['image']
        for workload, key in [('clamav-freshclam','IMAGE_FRESHCLAM'),
                              ('supabase-db','IMAGE_SUPABASE_DB'), ('supabase-auth','IMAGE_SUPABASE_AUTH')]:
            result[key] = checked['external_images'][workload]['image']
        require(set(result) == set(SERVICE_IMAGES.values()))
        return result
    except Exception:
        raise ComposeError(ERROR) from None

def run_command(argv, *, env, timeout, limit):
    """Bound both streams while reading, kill only our own process group on refusal."""
    process = None
    try:
        readonly_idle=(type(argv) is list and len(argv)==5
            and argv[:4]==['/usr/bin/systemctl','show','--property=ActiveState','--value']
            and type(argv[4]) is str
            and re.fullmatch(r'xynes-idle-(qa|staging|prod)-(auth|cms|broker)\.service',argv[4]))
        idle_lifecycle=(type(argv) is list and len(argv)==3 and argv[0]=='/usr/bin/systemctl'
            and argv[1] in ('start','stop') and type(argv[2]) is str
            and re.fullmatch(r'xynes-idle-(qa|staging|prod)-(auth|cms|broker)\.service',argv[2]))
        require(type(argv) is list and (argv[:5] == BASE or readonly_idle or idle_lifecycle)
                and type(timeout) is int and 0 < timeout <= 600
                and type(limit) is int and 0 < limit <= MAX_CONFIG)
        process = subprocess.Popen(argv, env=env, cwd='/', stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        streams = selectors.DefaultSelector()
        streams.register(process.stdout, selectors.EVENT_READ, 'stdout')
        streams.register(process.stderr, selectors.EVENT_READ, 'stderr')
        output = bytearray(); errors = 0; deadline = time.monotonic()+timeout
        with streams:
            while streams.get_map():
                remaining = deadline-time.monotonic()
                require(remaining > 0)
                events = streams.select(min(remaining, 1))
                for event, _ in events:
                    data = os.read(event.fileobj.fileno(), 65536)
                    if not data:
                        streams.unregister(event.fileobj); continue
                    if event.data == 'stdout':
                        output.extend(data); require(len(output) <= limit)
                    else:
                        errors += len(data); require(errors <= 65536)
            require(process.wait(timeout=max(0.001,deadline-time.monotonic())) == 0)
        return bytes(output)
    except Exception:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
        raise ComposeError(ERROR) from None
    finally:
        if process is not None:
            for file in (process.stdout, process.stderr):
                if file: file.close()

def transform(value, replace):
    if isinstance(value, dict):
        return {key:transform(item,replace) for key,item in value.items()}
    if isinstance(value, list):
        return [transform(item,replace) for item in value]
    return replace(value) if isinstance(value,str) else value

def validator(stage, config):
    spec = importlib.util.spec_from_file_location('apex_host_validator', Path(__file__).with_name('validate-hosted.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.validate(stage, config)

@dataclass(frozen=True)
class Prepared:
    stage: str
    file: Path = field(repr=False)
    sha256: str
    images: dict = field(repr=False)
    host_approved: bool = False
    storage_generation: str = None

class Driver:
    def __init__(self, stage, *, runner=run_command, environment_loader=stage_environment, storage_control=None,idle_control=None):
        require(stage in STAGES and callable(runner) and callable(environment_loader))
        self.stage, self.runner, self.environment_loader = stage, runner, environment_loader
        require(storage_control is None or (getattr(storage_control,'stage',None)==stage
                and all(callable(getattr(storage_control,name,None)) for name in
                        ('provision_token','provision_generation','verify_generation','bind_generation'))))
        self.storage_control, self.guard, self.generations = storage_control, None, {}
        require(idle_control is None or (getattr(idle_control,'stage',None)==stage and all(callable(getattr(idle_control,n,None)) for n in ('before_replace','rebind'))))
        if idle_control is None and environment_loader is stage_environment:
            try:from .host_idle import Idle
            except ImportError:from host_idle import Idle
            idle_control=Idle(stage)
        self.idle_control=idle_control

    def bind_guard(self, guard):
        # Only privileged integration code supplies this capability, never a request.
        require(getattr(guard,'stage',None)==self.stage and all(callable(getattr(guard,name,None))
                for name in ('before_replacement','after_replacement','verify_before_stop')))
        require(self.guard is None or self.guard is guard)
        self.guard=guard

    def provision(self):
        if self.storage_control is None:
            try:
                from .host_storage_control import StorageControl
            except ImportError:
                from host_storage_control import StorageControl
            self.storage_control=StorageControl(self.stage)
        self.storage_control.provision_token()
        generation=self.storage_control.provision_generation()
        require(generation.stage==self.stage and type(generation.name) is str
                and re.fullmatch(r'[a-f0-9]{64}',generation.name) and generation.name not in self.generations)
        self.storage_control.verify_generation(generation)
        self.generations[generation.name]=generation
        return generation.name

    def store(self, config, pointers, generation, work):
        require(isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True)==work)
        info=work.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700)
        validator(self.stage,config)
        require(all(config['services'][service]['image']==pointers[key] for service,key in SERVICE_IMAGES.items()))
        require(config['services']['storage-service']['labels']['com.xynes.storage-generation']==generation)
        encoded=json.dumps(transform(config,lambda s:s.replace('$','$$')),separators=(',',':')).encode()
        require(len(encoded)<=MAX_CONFIG)
        descriptor,name=tempfile.mkstemp(prefix='compose-',suffix='.json',dir=work)
        file=Path(name)
        try:
            with os.fdopen(descriptor,'wb') as out:
                os.fchmod(out.fileno(),0o600);out.write(encoded);out.flush();os.fsync(out.fileno())
            return Prepared(self.stage,file,hashlib.sha256(encoded).hexdigest(),copy.deepcopy(pointers),storage_generation=generation)
        except Exception:
            file.unlink(missing_ok=True);raise

    def rebind(self, previous, *, work):
        # Accepted image/config rollback always starts a new process generation.
        try:
            config=self.config(previous);generation=self.provision()
            storage=config['services']['storage-service']
            storage['labels']['com.xynes.storage-generation']=generation
            for mount in storage['volumes']:
                if mount['target']=='/run/xynes/storage-control':
                    mount['source']=f'/run/xynes/storage/{self.stage}/'+generation[:32]
            return self.store(config,previous.images,generation,work)
        except Exception:raise ComposeError(ERROR) from None

    def before(self, prepared, previous, inventory, replace_services):
        if self.guard is None:
            require(not (set(inventory)&CONTROLLED))
        else:
            self.guard.before_replacement(prepared,previous,inventory,replace_services,stop_budget_seconds=900)

    def bind_generation(self, prepared, inventory):
        generation=self.generations.get(prepared.storage_generation)
        require(generation is not None)
        row=inventory['storage-service']
        require(row['labels'].get('com.xynes.storage-generation')==prepared.storage_generation)
        self.storage_control.bind_generation(generation,row['id'],image=prepared.images['IMAGE_STORAGE'])

    def call(self, argv, *, pointers=None, generation=None, timeout=30, limit=MAX_CONFIG):
        env = {**CLEAN_ENV, 'XYNES_ENV':self.stage,
               'XYNES_INTERNAL_IDENTITIES_DIR':f'/srv/xynes/{self.stage}/identities', **(pointers or {})}
        if generation is not None:
            require(type(generation) is str and re.fullmatch(r'[a-f0-9]{64}',generation))
            env['STORAGE_CONTROL_GENERATION']=generation
            env['STORAGE_CONTROL_DIRECTORY']=f'/run/xynes/storage/{self.stage}/'+generation[:32]
        raw = self.runner(BASE+argv, env=env, timeout=timeout, limit=limit)
        require(type(raw) is bytes and len(raw) <= limit)
        return raw

    def compose(self, file=None):
        base = f'/srv/xynes/{self.stage}'
        argv = ['compose', '--project-name','xynes-'+self.stage,
                '--project-directory',base+'/compose', '--env-file',base+'/env/.env.'+self.stage]
        if file:
            argv += ['-f',str(file)]
        else:
            for relative in ('hosted.yml',self.stage+'.yml','internal-request-identities.yml','auth/compose.fragment.yml'):
                argv += ['-f',base+'/compose/'+relative]
        return argv

    def prepare(self, *, receipt, manifest, public, controller, work):
        try:
            pointers = image_pointers(stage=self.stage, receipt=receipt, manifest=manifest,
                                      public=public, controller=controller)
            require(isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True) == work)
            info=work.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            self.environment_loader(self.stage)
            generation=self.provision()
            raw = self.call(self.compose()+['config','--format','json'], pointers=pointers,generation=generation)
            # Native Compose config escapes dollars for a reusable document.
            config = transform(json.loads(raw),lambda s:s.replace('$$','$'))
            return self.store(config,pointers,generation,work)
        except Exception:
            raise ComposeError(ERROR) from None

    def config(self, prepared):
        require(type(prepared) is Prepared and prepared.stage == self.stage and prepared.host_approved is False
                and type(prepared.storage_generation) is str and re.fullmatch(r'[a-f0-9]{64}',prepared.storage_generation))
        descriptor=os.open(prepared.file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(descriptor,'rb') as file:
            before=os.fstat(file.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1
                    and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=MAX_CONFIG)
            raw=file.read(MAX_CONFIG+1)
            require(len(raw)==before.st_size and identity(os.fstat(file.fileno()))==identity(before)
                    and identity(prepared.file.lstat())==identity(before)
                    and hashlib.sha256(raw).hexdigest()==prepared.sha256)
        config=transform(json.loads(raw),lambda s:s.replace('$$','$'))
        validator(self.stage,config)
        require(config['services']['storage-service']['labels']['com.xynes.storage-generation']==prepared.storage_generation)
        require(set(prepared.images)==set(SERVICE_IMAGES.values())
                and all(config['services'][service]['image']==prepared.images[key] for service,key in SERVICE_IMAGES.items()))
        return config

    def inspect(self, allowed, *, ready=False, required=None):
        required = SERVICES if required is None else required
        require(type(required) is set and required <= SERVICES)
        raw=self.call(['ps','--all','--filter','label=com.docker.compose.project=xynes-'+self.stage,
                       '--format','{{.ID}}','--no-trunc'],limit=65536)
        ids=raw.decode('ascii').splitlines()
        require(len(ids)<=14 and len(set(ids))==len(ids) and all(CID.fullmatch(cid) for cid in ids))
        seen=set();inventory={}
        for cid in ids:
            row=json.loads(self.call(['inspect','--format',INSPECT,cid],limit=65536))
            require(type(row) is dict and set(row)=={'id','image','labels','state','health'} and row['id']==cid
                    and type(row['labels']) is dict)
            labels=row['labels'];service=labels.get('com.docker.compose.service')
            require(service in SERVICES and service not in seen
                    and labels.get('com.docker.compose.project')=='xynes-'+self.stage
                    and labels.get('com.docker.compose.oneoff')=='False'
                    and labels.get('com.xynes.managed')=='true'
                    and labels.get('com.xynes.environment')==self.stage
                    and row['image'] in allowed[service])
            seen.add(service);inventory[service]=copy.deepcopy(row)
            if ready and service in required:
                require(row['state']=='running' and row['health']=='healthy')
        if ready: require(required <= seen)
        return inventory

    def bootstrap(self, prepared, *, previous=None):
        """Only start pinned DB/Auth; the caller must migrate/seed before start()."""
        try:
            self.config(prepared);self.environment_loader(self.stage)
            allowed={service:{prepared.images[key]} for service,key in SERVICE_IMAGES.items()}
            if previous is not None:
                self.config(previous)
                # This operation does not perform a database image upgrade/downgrade.
                require(previous.images['IMAGE_SUPABASE_DB']==prepared.images['IMAGE_SUPABASE_DB'])
                for service,key in SERVICE_IMAGES.items(): allowed[service].add(previous.images[key])
            inventory=self.inspect(allowed)
            self.before(prepared,previous,inventory,set())
            infrastructure={'supabase-db','supabase-auth'}
            self.call(self.compose(prepared.file)+['up','-d','--no-build','--no-deps','--pull','always',
                      '--wait','--wait-timeout','180',*sorted(infrastructure)],timeout=600,limit=65536)
            # Existing owned apps can remain stopped/unready while migrations run.
            # Still inspect their ownership/image bindings; never adopt strangers.
            for service in infrastructure:
                allowed[service]={prepared.images[SERVICE_IMAGES[service]]}
            self.inspect(allowed,ready=True,required=infrastructure)
            return {'stage':self.stage,'services_ready':2,'product_accepted':False,'migrations_applied':False}
        except Exception:
            raise ComposeError(ERROR) from None

    def replace(self, prepared, *, previous=None, exclude=None):
        config=self.config(prepared);self.environment_loader(self.stage)
        previous_config=self.config(previous) if previous is not None else None
        allowed={service:{prepared.images[key]} for service,key in SERVICE_IMAGES.items()}
        if previous is not None:
            require(previous.images['IMAGE_SUPABASE_DB']==prepared.images['IMAGE_SUPABASE_DB'])
            for service,key in SERVICE_IMAGES.items():allowed[service].add(previous.images[key])
        inventory=self.inspect(allowed)
        # Only an accepted identical rendered processor spec proves no recreation.
        unchanged={service for service in PROCESSORS & set(inventory)
                   if previous_config is not None and previous_config['services'][service]==config['services'][service]
                   and inventory[service]['image']==prepared.images[SERVICE_IMAGES[service]]}
        for service in sorted(unchanged):
            raw=self.call(self.compose(prepared.file)+['config','--hash',service],timeout=30,limit=1024)
            match=re.fullmatch((service+r' ([a-f0-9]{64})\n').encode(),raw)
            require(match is not None and inventory[service]['labels'].get('com.docker.compose.config-hash')==match[1].decode('ascii'))
        selected=SERVICES-(exclude or set())
        replacing=selected-unchanged
        if self.idle_control is not None:self.idle_control.before_replace()
        self.before(prepared,previous,inventory,replacing)
        common=['up','-d','--no-build','--no-deps','--pull','always','--wait','--wait-timeout','180']
        if unchanged:
            self.call(self.compose(prepared.file)+common[:3]+['--no-recreate']+common[3:]+sorted(unchanged),timeout=600,limit=65536)
            preserved=self.inspect(allowed,ready=True,required=unchanged)
            require(all(preserved[service]['id']==inventory[service]['id']
                        and preserved[service]['labels']==inventory[service]['labels'] for service in unchanged))
        if 'storage-service' in inventory:
            require(self.guard is not None)
            self.guard.verify_before_stop(stop_budget_seconds=900)
        self.storage_control.verify_generation(self.generations[prepared.storage_generation])
        self.call(self.compose(prepared.file)+common+sorted(replacing),timeout=600,limit=65536)
        final=self.inspect({service:{prepared.images[key]} for service,key in SERVICE_IMAGES.items()},ready=True)
        require(all(final[service]['id']==inventory[service]['id']
                    and final[service]['labels']==inventory[service]['labels'] for service in unchanged))
        self.bind_generation(prepared,final)
        if self.guard is not None:self.guard.after_replacement(prepared,previous,final)
        if self.idle_control is not None:self.idle_control.rebind(prepared,final)
        return {'stage':self.stage,'services_ready':14,'product_accepted':False}

    def start(self, prepared, *, previous=None):
        try:return self.replace(prepared,previous=previous)
        except Exception:raise ComposeError(ERROR) from None

    def rollback(self, previous, *, current):
        try:
            self.config(previous);self.config(current)
            require(previous.images['IMAGE_SUPABASE_DB']==current.images['IMAGE_SUPABASE_DB'])
            rebound=self.rebind(previous,work=current.file.parent)
            result=self.replace(rebound,previous=current,exclude={'supabase-db'})
            result.update(database_rolled_back=False,prepared=rebound)
            return result
        except Exception:raise ComposeError(ERROR) from None
