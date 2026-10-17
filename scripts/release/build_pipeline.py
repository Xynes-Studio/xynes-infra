#!/usr/bin/env python3
"""Read-only admission and credential-free immutable-source Actions builds."""
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
try:
    from . import artifact_download, artifact_handoff, github_read, image_build, publish_batch, publish_images, source_context, migration_transport
    from . import release_manifest as records
except ImportError:
    import artifact_download, artifact_handoff, github_read, image_build, publish_batch, publish_images, source_context, migration_transport
    import release_manifest as records

ROOT=Path(__file__).resolve().parents[2]
BUILDKIT='image=moby/buildkit:v0.33.1@sha256:98cc6a3fc46220d00f8224ae483f3274fc874e9be8d7dd1e2e2c5481209228b5'
ERROR='Protected build pipeline refused'
MAX_OUTPUT=4*1024*1024

class PipelineError(ValueError):pass

def require(condition):
    if not condition:raise PipelineError(ERROR)


def bounded_command(argv,*,env,cwd,timeout):
    """Bound stdout and stderr together; discard diagnostics and kill our group."""
    process=None;selector=selectors.DefaultSelector();out=bytearray();size=0
    try:
        process=subprocess.Popen(argv,env=env,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,start_new_session=True)
        for stream in (process.stdout,process.stderr):
            os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
        deadline=time.monotonic()+timeout
        while selector.get_map():
            require(time.monotonic()<deadline)
            for key,_ in selector.select(min(0.2,max(0,deadline-time.monotonic()))):
                data=os.read(key.fileobj.fileno(),65536)
                if not data:selector.unregister(key.fileobj);continue
                size+=len(data);require(size<=MAX_OUTPUT)
                if key.fileobj is process.stdout:out.extend(data)
        require(process.wait(timeout=max(0.01,deadline-time.monotonic()))==0)
        return bytes(out)
    except Exception:raise PipelineError(ERROR) from None
    finally:
        selector.close()
        if process is not None:
            # Until wait() reaps the child, its PID cannot be reused. Only
            # signal while that ownership holds, including pipe-held timeouts.
            if process.returncode is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            process.wait()
            for stream in (process.stdout,process.stderr):stream.close()


def controller(env,job):
    require(job in ('admission','build-images') and env.get('GITHUB_JOB')==job)
    # Share the exact native controller grammar; the readonly jobs differ only
    # in their identity, not repository/ref/run/workflow provenance.
    return publish_images.controller_from({**env,'GITHUB_JOB':'publish-images'})


def runner_temp(env):
    base=Path(env['RUNNER_TEMP']);require(base.is_absolute() and base.resolve()==base and not base.is_symlink())
    info=base.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o022)
    return base


def private_write(path,data):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(descriptor,'wb') as file:file.write(data);file.flush();os.fsync(file.fileno())


def clean_environment(env,home):
    # PATH comes from the trusted runner. Every secret/agent/helper variable and
    # caller-selected Docker endpoint/config is omitted.
    require(isinstance(env.get('PATH'),str) and bool(env['PATH']))
    return {'PATH':env['PATH'],'HOME':str(home),'LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1',
            'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
            'GIT_NO_REPLACE_OBJECTS':'1','GIT_NO_LAZY_FETCH':'1'}


def tracked_manifest(bound,environment,command):
    require(command(['git','--no-replace-objects','rev-parse','HEAD'],env=environment,cwd=ROOT,timeout=30).strip().decode()==bound['commit'])
    raw=command(['git','--no-replace-objects','show',bound['commit']+':infra/release/source-manifest.json'],env=environment,cwd=ROOT,timeout=30)
    require(isinstance(raw,bytes) and len(raw)<=MAX_OUTPUT)
    path=ROOT/'infra/release/source-manifest.json'
    records.read_json(path)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(descriptor,'rb') as file:
        info=os.fstat(file.fileno());require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=MAX_OUTPUT)
        require(file.read(MAX_OUTPUT+1)==raw)
    manifest=records.strict_json(raw.decode());source_context.validate_manifest(manifest,'release')
    return raw,manifest


def admit(env,*,command=bounded_command):
    try:
        bound=controller(env,'admission');base=runner_temp(env)
        # Validate all input before creating the public artifact directory.
        clean=clean_environment(env,base)
        raw,manifest=tracked_manifest(bound,clean,command)
        public={stage:records.strict_json(env['APEX_PUBLIC_'+stage.upper()]) for stage in records.STAGES}
        records.validate_public_maps(public,'release')
        checks=records.strict_json(command(['bun',str(ROOT/'scripts/release/source_checks.ts'),'--manifest',
                    str(ROOT/'infra/release/source-manifest.json')],env={**clean,'GH_TOKEN':env['GH_TOKEN']},cwd=ROOT,timeout=300).decode())
        publish_images.qualified_sources(checks,manifest)
        target=base/'apex-admission';target.mkdir(mode=0o700)
        for name,value in [('controller.json',bound),('public-inputs.json',public),('source-checks.json',checks)]:
            private_write(target/name,(json.dumps(value,sort_keys=True)+'\n').encode())
        private_write(target/'source-manifest.json',raw)
        return target
    except Exception:raise PipelineError(ERROR) from None


def materialize(manifest,workspace,environment,command):
    workspace.mkdir(mode=0o700)
    for area in ('xynes','xynes-front-end'):(workspace/area).mkdir(mode=0o700)
    flags=['git','--no-replace-objects','-c','core.hooksPath=/dev/null','-c','credential.helper=',
           '-c','protocol.file.allow=never','-c','protocol.ext.allow=never']
    for name in source_context.SOURCES:
        source=manifest['sources'][name]
        repo=workspace/('xynes' if name in source_context.BACKENDS else 'xynes-front-end')/name
        repo.mkdir(mode=0o700)
        command(flags+['init','--template='],env=environment,cwd=repo,timeout=30)
        command(flags+['fetch','--no-tags','--depth=1','https://github.com/'+source['repository']+'.git',source['commit']],
                env=environment,cwd=repo,timeout=180)
        command(flags+['checkout','--detach',source['commit']],env=environment,cwd=repo,timeout=30)
        require(command(flags+['rev-parse','HEAD'],env=environment,cwd=repo,timeout=30).strip().decode()==source['commit'])


def qualify_build(output,result,manifest,public,bound):
    saved=records.read_json(output/'image-receipts.json')
    require(saved==result and isinstance(result,dict) and set(result)==publish_batch.BUILD_KEYS
            and type(result['schema_version']) is int and result['schema_version']==1
            and result['artifact_kind']=='local-oci-build-receipts' and result['controller']==bound
            and result['source_manifest_sha256']==records.canonical_sha256(manifest)
            and result['external_images']==manifest['external_images']
            and result['published'] is False and result['deployable'] is False)
    tasks=image_build.build_tasks(manifest,public,bound,'release');archives=output/'archives'
    require(len(tasks)==15 and {p.name for p in archives.iterdir()}=={t['id']+'.oci.tar' for t in tasks})
    actual=[image_build.inspect_oci(archives/(task['id']+'.oci.tar'),task) for task in tasks]
    require(result['images']==actual)
    for path in [output/'image-receipts.json',*[archives/(task['id']+'.oci.tar') for task in tasks]]:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(descriptor,'rb') as file:
            info=os.fstat(file.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1)
            os.fchmod(file.fileno(),0o600)
    os.chmod(archives,0o700)


def build(env,*,read,download,command=bounded_command,execute=image_build.execute,migration_writer=migration_transport.create):
    created=False;docker=None;clean=None
    try:
        with image_build.diagnostic('controller'):
            bound=controller(env,'build-images');base=runner_temp(env)
        with image_build.diagnostic('admission-intake'):
            intake=artifact_handoff.acquire(kind='admission',reference=records.strict_json(env['APEX_ADMISSION_RECEIPT']),
                controller=bound,read=read,download=download,output=base/'apex-build-admission')
        with image_build.diagnostic('input-qualification'):
            require(records.read_json(intake/'controller.json')==bound)
            manifest=records.read_json(intake/'source-manifest.json');public=records.read_json(intake/'public-inputs.json')
            source_context.validate_manifest(manifest,'release');records.validate_public_maps(public,'release')
            publish_images.qualified_sources(records.read_json(intake/'source-checks.json'),manifest)
        with image_build.diagnostic('tracked-source-manifest'):
            home=base/'apex-build-home';home.mkdir(mode=0o700);clean=clean_environment(env,home)
            raw,tracked=tracked_manifest(bound,clean,command)
            require(tracked==manifest and (intake/'source-manifest.json').read_bytes()==raw)
        with image_build.diagnostic('source-materialize'):
            workspace=base/'apex-source-workspace';materialize(manifest,workspace,clean,command)
        # A separate data artifact preserves the exact reviewed migration source.
        # The host will independently recompute its inventory/plan before SQL use.
        with image_build.diagnostic('migration-export'):
            migration_dir=base/'apex-migrations';migration_dir.mkdir(mode=0o700)
            repositories={'xynes-infra':ROOT,**{name:workspace/'xynes'/name for name in source_context.BACKENDS}}
            previous=dict(os.environ)
            try:
                os.environ.clear();os.environ.update(clean)
                migration_receipt=migration_writer(manifest_bytes=raw,controller=bound,
                    repositories=repositories,output=migration_dir/'migration-bundle.tar')
            finally:os.environ.clear();os.environ.update(previous)
        with image_build.diagnostic('migration-qualification'):
            migration_raw=migration_transport.private_bytes(migration_dir/'migration-bundle.tar',os.getuid())
            require(type(migration_receipt) is dict
                    and migration_receipt.get('artifact_kind')=='immutable-migration-bundle-receipt'
                    and migration_receipt.get('controller')==bound
                    and migration_receipt.get('source_manifest_sha256')==records.canonical_sha256(manifest)
                    and migration_receipt.get('source_manifest_bytes_sha256')==migration_transport.sha(raw)
                    and migration_receipt.get('bundle_sha256')==migration_transport.sha(migration_raw)
                    and type(migration_receipt.get('bundle_size')) is int and migration_receipt['bundle_size']==len(migration_raw)
                    and migration_receipt.get('approved') is False and migration_receipt.get('deployable') is False
                    and {p.name for p in migration_dir.iterdir()}=={'migration-bundle.tar'})
        with image_build.diagnostic('builder-create'):
            config=base/'apex-docker';config.mkdir(mode=0o700);private_write(config/'config.json',b'{}\n')
            builder='apex-build-'+str(bound['run_id'])+'-1';docker=['docker','--config',str(config),'buildx']
            command(docker+['create','--name',builder,'--driver','docker-container','--driver-opt',BUILDKIT],
                    env=clean,cwd=ROOT,timeout=120);created=True
        with image_build.diagnostic('image-batch'):
            output=base/'apex-images'
            # image_build/source_context use os.environ internally. Replace it only
            # for this single-process build, restoring it on every success/failure.
            previous=dict(os.environ)
            try:
                os.environ.clear();os.environ.update(clean)
                result=execute(manifest,public,bound,'release',workspace,output,builder,config)
            finally:os.environ.clear();os.environ.update(previous)
        with image_build.diagnostic('image-qualification'):
            qualify_build(output,result,manifest,public,bound)
        return output
    except Exception:raise PipelineError(ERROR) from None
    finally:
        if created:
            try:
                with image_build.diagnostic('builder-cleanup'):
                    command(docker+['rm','--force',builder],env=clean,cwd=ROOT,timeout=300)
            except Exception:raise PipelineError(ERROR) from None


def main():
    try:
        require(len(sys.argv)==2 and sys.argv[1] in ('admission','build'))
        if sys.argv[1]=='admission':admit(os.environ)
        else:
            api=github_read.GitHubReadAdapter(os.environ.get('GH_TOKEN',''))
            download=artifact_download.ArtifactDownloader(os.environ.get('GH_TOKEN',''))
            build(os.environ,read=api.read,download=download.download)
        print('Protected build pipeline data prepared');return 0
    except Exception:
        print(ERROR,file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())
