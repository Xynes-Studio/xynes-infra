#!/usr/bin/env python3
"""Protected Actions publisher; native proof and complete data precede writes."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

try:
    from . import admission, artifact_download, artifact_handoff, github_read, publish_batch, registry_https, registry_io, registry_policy, registry_namespace, release_manifest as records
except ImportError:
    import admission, artifact_download, artifact_handoff, github_read, publish_batch, registry_https, registry_io, registry_policy, registry_namespace
    import release_manifest as records

ROOT=Path(__file__).resolve().parents[2]


class PublisherError(ValueError):pass


def require(value):
    if not value:raise PublisherError('Protected image publisher refused')


def controller_from(env):
    expected={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':admission.REPOSITORY,'GITHUB_REF':'refs/heads/main',
              'GITHUB_REF_PROTECTED':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1',
              'GITHUB_JOB':'publish-images','GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com',
              'GITHUB_WORKFLOW_REF':admission.REPOSITORY+'/'+admission.WORKFLOW+'@refs/heads/main',
              'APEX_REGISTRY_PREFIX':registry_policy.EXPECTED['registry_prefix']}
    require(all(env.get(k)==v for k,v in expected.items()))
    require(isinstance(env.get('GH_TOKEN'),str) and env['GH_TOKEN'].startswith('ghs_')
            and registry_io.TOKEN_PATTERN.fullmatch(env['GH_TOKEN']))
    require(isinstance(env.get('GITHUB_ACTOR'),str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?',env['GITHUB_ACTOR']))
    run=env.get('GITHUB_RUN_ID');require(isinstance(run,str) and re.fullmatch('[1-9][0-9]{0,15}',run))
    controller={'repository':admission.REPOSITORY,'commit':env.get('GITHUB_SHA'),'workflow':'apex-build.yml',
                'run_id':int(run),'run_attempt':1}
    records.validate_controller(controller)
    return controller


def qualified_sources(value,manifest):
    require(isinstance(value,dict) and set(value)=={'checks_verified','approved','deployable','sources'}
            and value['checks_verified'] is True and value['approved'] is False and value['deployable'] is False)
    sources=value['sources'];require(isinstance(sources,list) and len(sources)==len(manifest['sources']))
    catalog=records.read_json(ROOT/'infra/release/source-check-catalog.json');seen=set()
    for source in sources:
        require(isinstance(source,dict) and set(source)=={'repository','commit','checks'})
        key=source['repository'].removeprefix('Xynes-Studio/') if isinstance(source.get('repository'),str) else ''
        require(key not in seen and key in manifest['sources'] and source['repository']==manifest['sources'][key]['repository']
                and source['commit']==manifest['sources'][key]['commit'])
        seen.add(key);checks=source['checks'];expected=catalog[key]
        require(isinstance(checks,list) and len(checks)==len(expected)
                and {c.get('name') for c in checks if isinstance(c,dict)}==set(expected))
        for check in checks:
            require(isinstance(check,dict) and set(check)=={'name','check_id','job_id','run_id','run_attempt','workflow_id','workflow_path','workflow_blob'}
                    and all(admission.positive(check[k]) for k in ('check_id','job_id','run_id','run_attempt','workflow_id'))
                    and check['workflow_path']==expected[check['name']] and isinstance(check['workflow_blob'],str)
                    and records.SHA.fullmatch(check['workflow_blob']))


def package(read,workload):
    value=read('/orgs/Xynes-Studio/packages/container/apex%2F'+workload)
    return package_value(value)


def package_value(value):
    if value is None:return None
    require(isinstance(value,dict) and isinstance(value.get('owner'),dict) and isinstance(value.get('repository'),dict))
    return {**{key:value.get(key) for key in ('id','name','package_type','visibility')},
            'owner':{key:value['owner'].get(key) for key in ('login','type')},
            'repository':{key:value['repository'].get(key) for key in ('id','full_name')}}


def run(env,work,*,read,read_policy,read_graphql,download,transport,read_namespace=None,publish=registry_io.publish_oci):
    """Injected callbacks are trusted I/O boundaries. main always supplies real adapters."""
    try:
        controller=controller_from(env);work=Path(work)
        require(work.is_absolute() and work.is_dir() and not work.is_symlink() and work.resolve()==work)
        jobs=read(admission.PREFIX+f'/actions/runs/{controller["run_id"]}/attempts/1/jobs?per_page=100&page=1')
        require(isinstance(jobs,dict) and isinstance(jobs.get('jobs'),list) and type(jobs.get('total_count')) is int
                and 0<jobs['total_count']<=100 and len(jobs['jobs'])==jobs['total_count'])
        candidates=[j for j in jobs['jobs'] if isinstance(j,dict) and j.get('name')=='publish-images']
        require(len(candidates)==1 and admission.positive(candidates[0].get('id')));job_id=candidates[0]['id']
        gate=lambda bound: admission.validate_admission(bound,job_id,read_policy,github_read.strict_json,read_graphql)
        # Admission here prevents unauthorized downloads; the batch repeats it
        # immediately before each application upload, including fresh approvals.
        gate(controller)
        intake=artifact_handoff.acquire(kind='admission',reference=records.strict_json(env['APEX_ADMISSION_RECEIPT']),
            controller=controller,read=read,download=download,output=work/'admission')
        require(records.read_json(intake/'controller.json')==controller)
        manifest=records.read_json(intake/'source-manifest.json');public=records.read_json(intake/'public-inputs.json')
        qualified_sources(records.read_json(intake/'source-checks.json'),manifest)
        build=artifact_handoff.acquire(kind='build',reference=records.strict_json(env['APEX_BUILD_RECEIPT']),
            controller=controller,read=read,download=download,output=work/'build')
        credential=registry_io.GitHubPublisherToken(env['GITHUB_ACTOR'],env['GH_TOKEN'])
        return publish_batch.publish_batch(manifest=manifest,public=public,controller=controller,
            build_receipt=records.read_json(build/'image-receipts.json'),archives=build/'archives',admission_check=gate,
            package_proof=lambda workload:package(read,workload),credential=credential,
            transport=transport,publish=publish,
            namespace_init=lambda:registry_namespace.qualify(controller=controller,
                read_package=read_namespace or (lambda workload:package(read,workload)),admission_check=gate,
                credential=credential,transport=transport,output=work/'namespace-qualification',publish=publish))
    except Exception:
        raise PublisherError('Protected image publisher refused') from None


def persist_publication(result,env):
    """Preserve the complete data-only receipt and exact verified image subjects.

    The workflow uploads only this file with the pinned artifact action. It must
    hand off its native artifact ID/digest to deployment; this file is not an
    attestation and does not itself grant deployment authority.
    """
    try:
        require(isinstance(result,dict) and result.get('artifact_kind')=='content-verified-image-publications'
                and result.get('published') is True and result.get('visibility_verified') is True
                and result.get('attested') is False and result.get('deployable') is False)
        require(result.get('receipt_sha256')==records.canonical_sha256({k:v for k,v in result.items() if k!='receipt_sha256'}))
        records.validate_controller(result['controller']);require(result['controller']==controller_from(env))
        require(isinstance(result.get('images'),list) and len(result['images'])==15)
        subjects=[]
        for image in result['images']:
            require(isinstance(image,dict) and image.get('published') is True and image.get('visibility_verified') is True)
            name,digest=image['image'].rsplit('@',1)
            require(name==registry_policy.EXPECTED['registry_prefix']+'/'+image['workload']
                    and image['workload'] in registry_io.WORKLOADS and re.fullmatch('sha256:[a-f0-9]{64}',digest)
                    and digest==image['manifest_digest'])
            subjects.append({'name':name,'digest':digest})
        require(len({(s['name'],s['digest']) for s in subjects})==15)
        base=Path(env['RUNNER_TEMP']);require(base.is_absolute() and base.resolve()==base and not base.is_symlink())
        info=base.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o022)
        directory=base/'apex-publications';directory.mkdir(mode=0o700)
        path=directory/'published-receipt.json'
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        with os.fdopen(descriptor,'w') as output:
            json.dump(result,output,sort_keys=True);output.write('\n');output.flush();os.fsync(output.fileno())
        descriptor=os.open(env['GITHUB_OUTPUT'],os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(descriptor,'wb') as output:
            info=os.fstat(output.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                                                 and info.st_nlink==1 and info.st_size<=4*1024*1024 and not stat.S_IMODE(info.st_mode)&0o022)
            output.write(('subjects='+json.dumps(subjects,separators=(',',':'))+'\n').encode());output.flush();os.fsync(output.fileno())
        return path
    except Exception:
        raise PublisherError('Protected image publisher refused') from None


def main():
    try:
        require(len(sys.argv)==1)
        # Separate policy metadata capability from the package-writing job token.
        api=github_read.GitHubReadAdapter(os.environ.get('GH_TOKEN',''))
        policy=github_read.GitHubReadAdapter(os.environ.get('GH_POLICY_READ_TOKEN',''))
        downloader=artifact_download.ArtifactDownloader(os.environ.get('GH_TOKEN',''))
        with tempfile.TemporaryDirectory(prefix='apex-protected-publisher-') as directory:
            result=run(os.environ,Path(directory).resolve(),read=api.read,read_policy=policy.read,read_graphql=policy.read_graphql,
                       download=downloader.download,transport=registry_https.HTTPSRegistryTransport(),
                       read_namespace=lambda workload:package_value(api.package_metadata(workload)))
            persist_publication(result,os.environ)
            # This is content/visibility evidence only; signing and host trust
            # verification remain separate required pipeline operations.
            print(json.dumps({'published':result['published'],'images':len(result['images']),
                              'receipt_sha256':result['receipt_sha256'],'attested':False,'deployable':False}))
        return 0
    except Exception:
        print('Protected image publisher refused',file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
