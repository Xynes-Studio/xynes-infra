#!/usr/bin/env python3
"""Bind actual successful native verifier output; data alone grants no authority."""
import copy
import re

try:
    from . import admission, registry_policy, release_manifest as records
except ImportError:
    import admission
    import registry_policy
    import release_manifest as records

ISSUER='https://token.actions.githubusercontent.com'
PREDICATE='https://slsa.dev/provenance/v1'
MAX_OUTPUT=256*1024


class AttestationError(ValueError):
    """Fixed diagnostics; never expose rejected subjects, tokens or verifier output."""


def require(value):
    if not value:raise AttestationError('Attestation verification refused')


def context(image,controller):
    records.validate_controller(controller);require(controller['run_attempt']==1)
    require(isinstance(image,str) and len(image)<=512)
    match=re.fullmatch(re.escape(registry_policy.EXPECTED['registry_prefix'])+
                       r'/(xynes-[a-z-]+|archive-scanner|libreoffice)@sha256:([a-f0-9]{64})',image)
    require(match is not None)
    try:
        from . import registry_io
    except ImportError:
        import registry_io
    require(match[1] in registry_io.WORKLOADS)
    repository='https://github.com/'+admission.REPOSITORY
    workflow=repository+'/'+admission.WORKFLOW+'@refs/heads/main'
    invocation=repository+'/actions/runs/'+str(controller['run_id'])+'/attempts/1'
    return repository,workflow,invocation,image.rsplit('@',1)[0],match[2]


def verification_args(image,controller):
    """Arguments for the owned pinned binary; no configurable trust/source flags."""
    try:
        _,workflow,_,_,_=context(image,controller)
        return ['attestation','verify','oci://'+image,'--repo',admission.REPOSITORY,
                '--hostname','github.com','--cert-identity',workflow,'--cert-oidc-issuer',ISSUER,
                '--source-ref','refs/heads/main','--source-digest',controller['commit'],
                '--signer-digest',controller['commit'],'--deny-self-hosted-runners',
                '--predicate-type',PREDICATE,'--digest-alg','sha256','--limit','5','--format','json']
    except Exception:raise AttestationError('Attestation verification refused') from None


def qualify_native_result(raw,image,controller):
    """Check successful pinned-gh output, not an unverified bundle or approval flag.

    Only the trusted native process adapter may supply these bytes in deployment.
    This function does not verify signatures or establish fresh stage admission.
    Its result explicitly remains non-deployable.
    """
    try:
        repository,workflow,invocation,name,digest=context(image,controller)
        require(isinstance(raw,bytes) and 0<len(raw)<=MAX_OUTPUT)
        values=records.strict_json(raw);require(isinstance(values,list) and 0<len(values)<=5)
        expected={'issuer':ISSUER,'subjectAlternativeName':workflow,'buildSignerURI':workflow,
                  'buildSignerDigest':controller['commit'],'sourceRepositoryURI':repository,
                  'sourceRepositoryDigest':controller['commit'],'sourceRepositoryRef':'refs/heads/main',
                  'sourceRepositoryIdentifier':str(admission.REPOSITORY_ID),
                  'sourceRepositoryOwnerURI':'https://github.com/Xynes-Studio',
                  'runnerEnvironment':'github-hosted','buildConfigURI':workflow,
                  'buildConfigDigest':controller['commit'],'buildTrigger':'workflow_dispatch',
                  'runInvocationURI':invocation,'sourceRepositoryVisibilityAtSigning':'public'}
        statements=[]
        for value in values:
            require(isinstance(value,dict) and isinstance(value.get('verificationResult'),dict))
            verified=value['verificationResult'];certificate=verified['signature']['certificate']
            require(isinstance(certificate,dict) and all(certificate.get(k)==v for k,v in expected.items()))
            timestamps=verified['verifiedTimestamps']
            require(isinstance(timestamps,list) and timestamps
                    and all(isinstance(t,dict) and isinstance(t.get('timestamp'),str) and t['timestamp'] for t in timestamps))
            statement=verified['statement']
            require(statement['_type']=='https://in-toto.io/Statement/v1' and statement['predicateType']==PREDICATE
                    and statement['subject']==[{'name':name,'digest':{'sha256':digest}}])
            predicate=statement['predicate'];definition=predicate['buildDefinition'];details=predicate['runDetails']
            require(definition['buildType']=='https://actions.github.io/buildtypes/workflow/v1'
                    and definition['externalParameters']['workflow']=={'path':admission.WORKFLOW,'ref':'refs/heads/main','repository':repository})
            github=definition['internalParameters']['github']
            require(isinstance(github,dict) and github.get('event_name')=='workflow_dispatch'
                    and github.get('repository_id')==str(admission.REPOSITORY_ID)
                    and github.get('runner_environment')=='github-hosted')
            require(definition['resolvedDependencies']==[{'digest':{'gitCommit':controller['commit']},'uri':'git+'+repository+'@refs/heads/main'}]
                    and details['builder']['id']==workflow and details['metadata']['invocationId']==invocation)
            statements.append(records.canonical_sha256(statement))
        return {'artifact_kind':'native-attestation-result-bindings','controller':copy.deepcopy(controller),
                'image':image,'statement_sha256':statements,'bindings_verified':True,'deployable':False}
    except Exception:raise AttestationError('Attestation verification refused') from None
