#!/usr/bin/env python3
"""Run the isolated CI product smoke on hash-bound exact built-image inputs.

The verifier is data-only; the native entrypoint delegates verified inputs to
the disposable stack. Neither grants hosted deployment or publication authority.
"""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

try:
    from . import artifact_download, artifact_handoff, build_pipeline, ci_smoke_runtime, github_read, image_build, migration_transport, publish_batch, publish_images, release_manifest as records
except ImportError:
    import artifact_download, artifact_handoff, build_pipeline, ci_smoke_runtime, github_read, image_build, migration_transport, publish_batch, publish_images
    import release_manifest as records

BACKENDS = frozenset(('xynes-gateway', 'xynes-accounts-service', 'xynes-authz-service',
                      'xynes-cms-core', 'xynes-doc-service', 'xynes-telemetry-service'))
STACK_PHASES = frozenset(('input-qualification','image-import','external-images','substrate',
                         'identities','postgres','gotrue','migrations','seeds','services','flow','cleanup'))
ROOT=Path(__file__).resolve().parents[2]


class SmokeError(ValueError):
    """Refusals contain no input, credentials, paths, or service log bytes."""


def require(value):
    if not value:
        raise SmokeError('CI product smoke refused')


def verify_build(*, manifest, public, controller, receipt, archives):
    """Verify the entire fifteen-image batch before selecting six backends.

    The other two runtime services use the manifest's pinned Postgres/GoTrue
    images. Docker execution and transport are deliberately separate from this
    data-only qualification; no callback can load an image before it completes.
    """
    try:
        manifest, public, controller, receipt = copy.deepcopy((manifest, public, controller, receipt))
        records.bounded_tree(receipt)
        tasks = image_build.build_tasks(manifest, public, controller, 'release')
        require(type(receipt) is dict and set(receipt) == publish_batch.BUILD_KEYS
                and type(receipt['schema_version']) is int and receipt['schema_version'] == 1
                and receipt['artifact_kind'] == 'local-oci-build-receipts'
                and receipt['controller'] == controller
                and receipt['source_manifest_sha256'] == records.canonical_sha256(manifest)
                and receipt['external_images'] == manifest['external_images']
                and receipt['published'] is False and receipt['deployable'] is False)
        images = receipt['images']
        require(type(images) is list and len(images) == len(tasks) == 15)
        by_key = {}
        for image in images:
            require(type(image) is dict and type(image.get('workload')) is str)
            key = (image['workload'], image.get('environment'))
            require(key not in by_key)
            by_key[key] = image
        archives = Path(archives)
        require(archives.is_absolute() and archives.is_dir() and not archives.is_symlink()
                and archives.resolve() == archives)
        require({path.name for path in archives.iterdir()} == {task['id'] + '.oci.tar' for task in tasks})
        selected = []
        for task in tasks:
            path = archives / (task['id'] + '.oci.tar')
            actual = image_build.inspect_oci(path, task)
            require(actual == by_key.get((task['workload'], task['environment'])))
            if task['workload'] in BACKENDS and task['environment'] is None:
                selected.append((task, path, actual))
        require(len(selected) == len(BACKENDS) and {row[0]['workload'] for row in selected} == BACKENDS)
        return tuple(selected)
    except Exception:
        raise SmokeError('CI product smoke refused') from None


def controller_from(env):
    require(os.geteuid() == 0 and env.get('GITHUB_JOB') == 'product-smoke')
    return publish_images.controller_from({**env, 'GITHUB_JOB':'publish-images'})


def tracked_git_command(controller):
    """Permit root's two Git reads in the runner-owned, fixed checkout only.

    Clean Git configuration deliberately discards sudo ownership exceptions.
    Supply this exact checkout exception on each read, never a global wildcard.
    """
    records.validate_controller(controller)
    allowed=(['git','--no-replace-objects','rev-parse','HEAD'],
             ['git','--no-replace-objects','show',controller['commit']+':infra/release/source-manifest.json'])
    def command(argv,*,env,cwd,timeout):
        require(cwd==ROOT and type(argv) is list and argv in allowed)
        return build_pipeline.bounded_command(['git','-c','safe.directory='+str(ROOT)]+argv[1:],
            env=env,cwd=ROOT,timeout=timeout)
    return command


def observation(value):
    """Project the trusted flow's observations rather than copying product data."""
    require(type(value) is dict and value.get('stage') == 'qa' and value.get('result') == 'passed'
            and value.get('storage_verified') is False
            and value.get('storage_skip_reason') == 'policy:qa-storage-provider-deferred'
            and type(value.get('observed_at')) is int and 0 <= value['observed_at'] <= 9007199254740991
            and type(value.get('steps')) is list)
    return {'stage':'qa','result':'passed','storage_verified':False,
            'storage_skip_reason':'policy:qa-storage-provider-deferred',
            'observed_at':value['observed_at'],'steps':ci_smoke_runtime.safe_steps(value['steps'])}


def run(env, work, *, read, download, command=None, stack_run=None):
    """Hash-bound native artifact intake; only verified data reaches the stack.

    Trusted I/O callbacks are fixture boundaries. The native entrypoint supplies
    real read-only GitHub adapters and the actual disposable Docker stack.
    """
    result={'schema_version':1,'artifact_kind':'ci-product-smoke','result':'refused',
            'published':False,'deployable':False,'storage_verified':False}
    phase='controller'
    try:
        controller=controller_from(env)
        result['controller']=copy.deepcopy(controller)
        work=Path(work)
        migration_transport.private_directory(work,os.getuid())
        home=work/'home';home.mkdir(mode=0o700)
        clean=build_pipeline.clean_environment(env,home)
        references={}
        for kind in ('admission','build','migration'):
            reference=records.strict_json(env['APEX_'+kind.upper()+'_RECEIPT'])
            require(type(reference) is dict and set(reference)=={'id','digest'}
                    and type(reference['id']) is int and 0<reference['id']<=9007199254740991)
            require(type(reference['digest']) is str and reference['digest'].startswith('sha256:'))
            records.hash_value(reference['digest'][7:])
            references[kind]=reference
        require(len({reference['id'] for reference in references.values()})==3)
        result['artifacts']=copy.deepcopy(references)
        phase='admission-artifact'
        intake=artifact_handoff.acquire(kind='admission',reference=references['admission'],
            controller=controller,read=read,download=download,output=work/'admission')
        require(records.read_json(intake/'controller.json')==controller)
        phase='source-qualification'
        raw,manifest=build_pipeline.tracked_manifest(controller,clean,
            tracked_git_command(controller) if command is None else command)
        require((intake/'source-manifest.json').read_bytes()==raw)
        public=records.read_json(intake/'public-inputs.json')
        records.validate_public_maps(public,'release')
        publish_images.qualified_sources(records.read_json(intake/'source-checks.json'),manifest)
        result['source_manifest_sha256']=records.canonical_sha256(manifest)
        phase='build-artifact'
        build=artifact_handoff.acquire(kind='build',reference=references['build'],controller=controller,
            read=read,download=download,output=work/'build')
        selected=verify_build(manifest=manifest,public=public,controller=controller,
            receipt=records.read_json(build/'image-receipts.json'),archives=build/'archives')
        result['images']=[copy.deepcopy(actual) for _,_,actual in selected]
        phase='migration-artifact'
        migration=artifact_handoff.acquire(kind='migration',reference=references['migration'],controller=controller,
            read=read,download=download,output=work/'migration-artifact')
        bundle=migration/'migration-bundle.tar'
        verified_work=work/'migration-qualification';verified_work.mkdir(mode=0o700)
        migration_transport.extract(bundle=bundle,expected_sha256=migration_transport.sha(
            migration_transport.private_bytes(bundle,os.getuid())),manifest_bytes=raw,
            controller=controller,work=verified_work)
        phase='runtime'
        if stack_run is None:
            try:from . import ci_smoke_stack
            except ImportError:import ci_smoke_stack
            stack_run=ci_smoke_stack.run
        runtime_work=work/'runtime';runtime_work.mkdir(mode=0o700)
        previous=dict(os.environ)
        try:
            # GitHub credentials and ambient Docker/agent endpoints cannot reach
            # the Docker lifecycle or service processes, even on exceptions.
            os.environ.clear();os.environ.update(clean)
            summary=stack_run(selected=selected,manifest=manifest,manifest_bytes=raw,
                migration_bundle=bundle,controller=controller,work=runtime_work)
        finally:
            os.environ.clear();os.environ.update(previous)
        result.update(observation(summary))
        return result
    except Exception as cause:
        diagnostic=getattr(cause,'safe_evidence',{})
        if phase=='runtime' and type(diagnostic) is dict:
            result.update(ci_smoke_runtime.failure_evidence(phase=diagnostic.get('phase'),
                failed_flow_phase=diagnostic.get('failed_flow_phase'),steps=diagnostic.get('steps')))
            try:from . import ci_smoke_stack as stack_tools
            except ImportError:import ci_smoke_stack as stack_tools
            result.update(stack_tools.safe_command_evidence(diagnostic.get('command')))
            if type(diagnostic.get('stack_phase')) is str and diagnostic['stack_phase'] in STACK_PHASES:
                result['stack_phase']=diagnostic['stack_phase']
        else:
            result.update(phase=phase,failed_flow_phase=None,steps=[])
        error=SmokeError('CI product smoke refused');error.safe_evidence=result
        raise error from None


def persist_result(result, env):
    """Write only the authored data receipt; hand ownership to the upload runner."""
    base_fd=None;directory_fd=None;file_fd=None
    try:
        require(type(result) is dict and result.get('artifact_kind')=='ci-product-smoke'
                and result.get('result') in ('passed','refused')
                and result.get('deployable') is False and result.get('published') is False
                and result.get('storage_verified') is False)
        base=Path(env['RUNNER_TEMP'])
        require(base.is_absolute() and base.resolve(strict=True)==base)
        info=base.lstat()
        sudo=env.get('SUDO_UID')
        require(sudo is None or type(sudo) is str and sudo.isascii() and sudo.isdecimal()
                and 0<int(sudo)<=2147483647 and str(int(sudo))==sudo)
        owner=os.getuid() if sudo is None else int(sudo)
        require(stat.S_ISDIR(info.st_mode) and not stat.S_IMODE(info.st_mode)&0o022
                and info.st_uid in (os.getuid(),owner))
        base_fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        require(os.path.samestat(info,os.fstat(base_fd)))
        os.mkdir('apex-ci-smoke',mode=0o700,dir_fd=base_fd)
        directory=base/'apex-ci-smoke'
        directory_fd=os.open('apex-ci-smoke',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=base_fd)
        created=os.fstat(directory_fd)
        require(stat.S_ISDIR(created.st_mode) and created.st_uid==os.getuid()
                and stat.S_IMODE(created.st_mode)==0o700)
        path=directory/'result.json'
        file_fd=os.open('result.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory_fd)
        with os.fdopen(file_fd,'wb') as output:
            file_fd=None
            output.write((json.dumps(result,sort_keys=True)+'\n').encode());output.flush();os.fsync(output.fileno())
            if owner!=os.getuid():os.fchown(output.fileno(),owner,-1)
        if owner!=os.getuid():os.fchown(directory_fd,owner,-1)
        return path
    except Exception:raise SmokeError('CI product smoke refused') from None
    finally:
        for descriptor in (file_fd,directory_fd,base_fd):
            if descriptor is not None:os.close(descriptor)


def main():
    try:
        require(len(sys.argv)==1)
        controller_from(os.environ)
        api=github_read.GitHubReadAdapter(os.environ['GH_TOKEN'])
        downloader=artifact_download.ArtifactDownloader(os.environ['GH_TOKEN'])
        # Root-owned scratch is separate from the non-root runner's temp area.
        with tempfile.TemporaryDirectory(prefix='apex-ci-product-',dir='/tmp') as directory:
            try:
                result=run(os.environ,Path(directory).resolve(),read=api.read,download=downloader.download)
            except SmokeError as error:
                persist_result(error.safe_evidence,os.environ)
                raise
            persist_result(result,os.environ)
        print('CI product smoke passed; storage unverified; deployable=false')
        return 0
    except Exception:
        print('CI product smoke refused',file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
