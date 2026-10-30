#!/usr/bin/env python3
"""PRIVATE validation harness, copied only to a temporary CI validation branch.

Historical build inputs exercise current smoke code. This is never a native
product-smoke/main release gate, publication receipt or hosted acceptance.
"""
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import artifact_download
import artifact_handoff
import build_pipeline
import ci_smoke
import ci_smoke_runtime
import ci_smoke_stack
import ci_smoke_engine
import github_read
import publish_images
import release_manifest as records
import source_context

REPOSITORY='Xynes-Studio/xynes-infra'
CONTROLLER={'repository':REPOSITORY,'commit':'8b98792caa5fb17166c8e6d10533fc43be14f5a3',
            'workflow':'apex-build.yml','run_id':40813783778,'run_attempt':1}
REFERENCES={
    'admission':{'id':12870679500,'digest':'sha256:330aa7eda9c038a7e66028a87b8b2a4da174ab84975cdf5a91735d44c51cf387'},
    'build':{'id':12871698815,'digest':'sha256:0ae31be527377bbca161c95422e95a593ff18c8bc294b28a6c17f29aaa024521'},
    'migration':{'id':12871634362,'digest':'sha256:200235900947967c7cece7ab3ec393458bb1fb5cb64147061063f521a6aaad64'},
}
PHASES=frozenset(('guard','native-run','admission','tracked-manifest','build','migration','engine-preparation','runtime','persist'))

class ValidationError(ValueError):pass

def require(value):
    if not value:raise ValidationError('Historical CI validation refused')

def guarded_context(env):
    require(os.geteuid()==0 and env.get('GITHUB_REPOSITORY')==REPOSITORY
            and env.get('GITHUB_JOB')=='historical-integration'
            and env.get('GITHUB_EVENT_NAME')=='workflow_dispatch')
    ref=env.get('GITHUB_REF');sha=env.get('GITHUB_SHA');run=env.get('GITHUB_RUN_ID')
    require(type(ref) is str and re.fullmatch(r'refs/heads/test/apex-ci-smoke-[a-z0-9-]{1,64}',ref)
            and type(sha) is str and re.fullmatch(r'[a-f0-9]{40}',sha)
            and type(run) is str and run.isascii() and run.isdecimal() and str(int(run))==run
            and 0<int(run)<=9007199254740991 and int(run)!=CONTROLLER['run_id'])
    return {'run_id':int(run),'head':sha,'ref':ref}

def native_run(read):
    row=read('/repos/'+REPOSITORY+'/actions/runs/'+str(CONTROLLER['run_id']))
    require(type(row) is dict and row.get('id')==CONTROLLER['run_id'] and type(row.get('id')) is int
            and row.get('run_attempt')==1 and type(row.get('run_attempt')) is int
            and row.get('head_sha')==CONTROLLER['commit'] and row.get('head_branch')=='main'
            and row.get('path')=='.github/workflows/apex-build.yml'
            and row.get('status')=='completed' and row.get('conclusion')=='success')
    for key in ('repository','head_repository'):
        repo=row.get(key)
        require(type(repo) is dict and repo.get('full_name')==REPOSITORY
                and type(repo.get('id')) is int and repo['id']==artifact_handoff.REPOSITORY_ID)

def diagnostic(cause,phase):
    """Project only controlled phases and flow observations; never exception text."""
    value={'result':'refused','phase':phase if phase in PHASES else 'guard'}
    safe=getattr(cause,'safe_evidence',None)
    if phase=='runtime' and type(safe) is dict:
        value.update(ci_smoke_runtime.failure_evidence(phase=safe.get('phase'),
            failed_flow_phase=safe.get('failed_flow_phase'),steps=safe.get('steps')))
        value.update(ci_smoke_stack.safe_command_evidence(safe.get('command')))
        stack_phase=safe.get('stack_phase')
        if type(stack_phase) is str and stack_phase in ci_smoke.STACK_PHASES:value['stack_phase']=stack_phase
    return value

def validate(env,work,*,read,download):
    """Trusted I/O parameters exist solely for local fixture verification."""
    phase='guard'
    result={'schema_version':1,'artifact_kind':'ci-product-smoke','validation_only':True,
            'native_release_eligible':False,'published':False,'deployable':False,'storage_verified':False,'result':'refused'}
    try:
        result['validation_workflow']=guarded_context(env)
        result['tested_controller']=copy.deepcopy(CONTROLLER)
        result['artifacts']=copy.deepcopy(REFERENCES)
        home=work/'home';home.mkdir(mode=0o700)
        clean=build_pipeline.clean_environment({'PATH':'/usr/local/bin:/usr/bin:/bin'},home)
        phase='native-run';native_run(read)
        phase='admission'
        intake=artifact_handoff.acquire(kind='admission',reference=REFERENCES['admission'],controller=CONTROLLER,
            read=read,download=download,output=work/'admission')
        require(records.read_json(intake/'controller.json')==CONTROLLER)
        phase='tracked-manifest'
        def git(args):
            return build_pipeline.bounded_command(['git','-c','safe.directory='+str(ROOT),'--no-replace-objects',*args],
                env=clean,cwd=ROOT,timeout=30)
        require(git(['rev-parse','HEAD']).strip().decode()==result['validation_workflow']['head'])
        raw=git(['show',CONTROLLER['commit']+':infra/release/source-manifest.json'])
        require(type(raw) is bytes and 0<len(raw)<=records.MAX_BYTES and (intake/'source-manifest.json').read_bytes()==raw)
        manifest=records.strict_json(raw.decode());source_context.validate_manifest(manifest,'release')
        public=records.read_json(intake/'public-inputs.json');records.validate_public_maps(public,'release')
        publish_images.qualified_sources(records.read_json(intake/'source-checks.json'),manifest)
        result['source_manifest_sha256']=records.canonical_sha256(manifest)
        phase='build'
        build=artifact_handoff.acquire(kind='build',reference=REFERENCES['build'],controller=CONTROLLER,
            read=read,download=download,output=work/'build')
        selected=ci_smoke.verify_build(manifest=manifest,public=public,controller=CONTROLLER,
            receipt=records.read_json(build/'image-receipts.json'),archives=build/'archives')
        result['images']=[copy.deepcopy(actual) for _,_,actual in selected]
        phase='migration'
        migration=artifact_handoff.acquire(kind='migration',reference=REFERENCES['migration'],controller=CONTROLLER,
            read=read,download=download,output=work/'migration')
        phase='native-run';native_run(read)
        phase='engine-preparation';engine_work=work/'engine';engine_work.mkdir(mode=0o700)
        engine=ci_smoke_engine.prepare(env,engine_work)
        require(type(engine) is dict and set(engine)=={'containerd_store','changed'}
                and engine['containerd_store'] is True and type(engine['changed']) is bool)
        result['runner_engine']=engine
        phase='runtime';runtime=work/'runtime';runtime.mkdir(mode=0o700)
        previous=dict(os.environ)
        try:
            os.environ.clear();os.environ.update(clean)
            summary=ci_smoke_stack.run(selected=selected,manifest=manifest,manifest_bytes=raw,
                migration_bundle=migration/'migration-bundle.tar',controller=CONTROLLER,work=runtime)
        finally:
            os.environ.clear();os.environ.update(previous)
        result.update(ci_smoke.observation(summary))
        return result
    except Exception as cause:
        result.update(diagnostic(cause,phase))
        error=ValidationError('Historical CI validation refused');error.safe_evidence=result
        raise error from None

def console_failure(result):
    """One controlled failed step/status for logs; full safe rows live in receipt."""
    allowed=PHASES|frozenset(('provision','flow','cleanup'))
    phase=result.get('phase')
    value={'result':'refused','phase':phase if type(phase) is str and phase in allowed else 'guard'}
    failed=result.get('failed_flow_phase')
    if type(failed) is str and failed in ci_smoke_runtime.STEP_LABELS:
        value['failed_flow_phase']=failed
        for row in ci_smoke_runtime.safe_steps(result.get('steps')):
            if row['flow']==failed:value['status']=row['status']
    stack_phase=result.get('stack_phase')
    if type(stack_phase) is str and stack_phase in ci_smoke.STACK_PHASES:value['stack_phase']=stack_phase
    value.update(ci_smoke_stack.safe_command_evidence(result.get('command')))
    return value

def main():
    phase='guard';result=None
    try:
        require(len(sys.argv)==1);guarded_context(os.environ)
        token=os.environ['GH_TOKEN']
        api=github_read.GitHubReadAdapter(token)
        downloader=artifact_download.ArtifactDownloader(token)
        with tempfile.TemporaryDirectory(prefix='apex-historical-validation-',dir='/tmp') as directory:
            work=Path(directory).resolve();work.chmod(0o700)
            try:result=validate(dict(os.environ),work,read=api.read,download=downloader.download)
            except ValidationError as cause:result=cause.safe_evidence
        phase='persist';ci_smoke.persist_result(result,os.environ)
        if result['result']!='passed':
            print(json.dumps(console_failure(result),sort_keys=True),file=sys.stderr)
            return 1
        print('Historical CI validation passed; native_release_eligible=false; storage_verified=false')
        return 0
    except Exception as cause:
        print(json.dumps(diagnostic(cause,phase),sort_keys=True),file=sys.stderr)
        return 1

if __name__=='__main__':sys.exit(main())
