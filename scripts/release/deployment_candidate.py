#!/usr/bin/env python3
"""Acquire a native artifact/signature candidate without host deployment authority.

read and download must be trusted bounded GitHubReadAdapter.read and
ArtifactDownloader.download adapters. verify must be a trusted closure/partial
of native_attestation.verify with the owned binary and scoped tokens bound by the
privileged integration, never request data. Fixtures can inject these boundaries
but cannot establish actual signatures. work is caller-owned private temporary
storage; the caller retains responsibility for its cleanup. No source execution,
Docker operation, stage approval, host installation or migration occurs here.
"""
import base64
import copy
import hashlib
import os
from pathlib import Path
import re
import stat

try:
    from . import admission, artifact_handoff, deploy_request, native_attestation
    from . import publication_record, publish_batch, publish_images, release_manifest as records
except ImportError:
    import admission, artifact_handoff, deploy_request, native_attestation
    import publication_record, publish_batch, publish_images
    import release_manifest as records

ERROR = 'Deployment candidate refused'
SOURCE_PATH = 'infra/release/source-manifest.json'
SIGNATURE_KEYS = {'artifact_kind', 'controller', 'image', 'statement_sha256',
                  'bindings_verified', 'deployable', 'native_signature_verified', 'verifier_version'}


class CandidateError(ValueError):
    """Refusals omit request bytes, provider data, local paths and verifier errors."""


def require(value):
    if not value:
        raise CandidateError(ERROR)


def repository(value):
    require(type(value) is dict and type(value.get('id')) is int
            and value['id'] == admission.REPOSITORY_ID
            and value.get('full_name') == admission.REPOSITORY)


def run_identity(value, controller):
    require(type(value) is dict)
    repository(value.get('repository'))
    repository(value.get('head_repository'))
    expected = {'id': controller['run_id'], 'run_attempt': 1,
                'path': admission.WORKFLOW, 'event': 'workflow_dispatch',
                'head_sha': controller['commit'], 'head_branch': 'main',
                'status': 'completed', 'conclusion': 'success'}
    require(all(type(value.get(key)) is type(item) and value[key] == item for key, item in expected.items())
            and admission.positive(value.get('workflow_id')))
    return {**expected, 'workflow_id': value['workflow_id']}


def main_identity(value, controller):
    require(type(value) is dict and value.get('name') == 'main'
            and value.get('protected') is True and type(value.get('commit')) is dict
            and value['commit'].get('sha') == controller['commit'])


def controller_binding(value, controller):
    records.validate_controller(value)
    require(type(value['run_attempt']) is int and value['run_attempt'] == 1
            and value == controller)


def artifact_reference(value):
    require(type(value) is dict and admission.positive(value.get('id'))
            and value.get('expired') is False and type(value.get('digest')) is str
            and re.fullmatch(r'sha256:[a-f0-9]{64}', value['digest']))
    return {'id': value['id'], 'digest': value['digest']}


def discover(value, controller, publication):
    require(type(value) is dict and type(value.get('total_count')) is int
            and 0 < value['total_count'] <= 100 and type(value.get('artifacts')) is list
            and len(value['artifacts']) == value['total_count'])
    items = value['artifacts']
    require(all(type(item) is dict and admission.positive(item.get('id')) for item in items)
            and len({item['id'] for item in items}) == len(items))
    selected = {}
    for kind in ('admission', 'publication'):
        name = f'apex-{kind}-{controller["run_id"]}-1'
        matching = [item for item in items if item.get('name') == name]
        require(len(matching) == 1)
        selected[kind] = artifact_reference(matching[0])
    require(selected['publication'] == publication
            and selected['admission']['id'] != publication['id'])
    return selected['admission']


def private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as file:
        before = os.fstat(file.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600
                and 0 < before.st_size <= records.MAX_BYTES)
        raw = file.read(records.MAX_BYTES + 1)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink,
                              s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(len(raw) == before.st_size and identity(os.fstat(file.fileno())) == identity(before)
                and identity(path.lstat()) == identity(before))
        return raw


def source_bytes(value):
    require(type(value) is dict and value.get('type') == 'file'
            and value.get('path') == SOURCE_PATH and value.get('encoding') == 'base64'
            and type(value.get('size')) is int and 0 < value['size'] <= records.MAX_BYTES
            and type(value.get('sha')) is str and records.SHA.fullmatch(value['sha'])
            and type(value.get('content')) is str and len(value['content']) <= 350000)
    encoded = value['content'].replace('\n', '')
    raw = base64.b64decode(encoded, validate=True)
    require(len(raw) == value['size'] and base64.b64encode(raw).decode() == encoded
            and hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() == value['sha'])
    return raw


def package(read, workload):
    value = read('/orgs/Xynes-Studio/packages/container/apex%2F' + workload)
    # Native responses include incidental metadata. Project only the exact
    # closed package contract before invoking the existing qualification gate.
    require(type(value) is dict and type(value.get('owner')) is dict
            and type(value.get('repository')) is dict)
    projected = {key: value.get(key) for key in ('id', 'name', 'package_type', 'visibility')}
    projected['owner'] = {key: value['owner'].get(key) for key in ('login', 'type')}
    projected['repository'] = {key: value['repository'].get(key) for key in ('id', 'full_name')}
    require(type(projected['repository']['id']) is int)
    return publish_batch.qualify_package(projected, workload)


def signature(value, image, controller):
    records.bounded_tree(value)
    require(type(value) is dict and set(value) == SIGNATURE_KEYS
            and value['artifact_kind'] == 'native-attestation-result-bindings'
            and value['controller'] == controller and value['image'] == image
            and value['bindings_verified'] is True and value['native_signature_verified'] is True
            and value['deployable'] is False and value['verifier_version'] == native_attestation.VERSION)
    records.validate_controller(value['controller'])
    require(type(value['controller']['run_attempt']) is int and value['controller']['run_attempt'] == 1
            and type(value['statement_sha256']) is list and 0 < len(value['statement_sha256']) <= 5)
    for digest in value['statement_sha256']:
        records.hash_value(digest)
    return copy.deepcopy(value)


def acquire(raw, *, stage, work, read, download, verify):
    """Return data-only proof; fresh stage admission and host state remain required."""
    try:
        request = deploy_request.parse(raw, stage=stage)
        controller, publication = request['controller'], request['publication']
        require(all(callable(callback) for callback in (read, download, verify))
                and isinstance(work, Path) and work.is_absolute() and work.resolve(strict=True) == work)
        info = work.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                and not (work / 'admission').exists() and not (work / 'admission').is_symlink()
                and not (work / 'publication').exists() and not (work / 'publication').is_symlink())
        repo = read(admission.PREFIX)
        repository(repo)
        require(repo.get('private') is False and repo.get('fork') is False and repo.get('default_branch') == 'main')
        run_path = admission.PREFIX + f'/actions/runs/{controller["run_id"]}'
        initial_run = run_identity(read(run_path), controller)
        main_identity(read(admission.PREFIX + '/branches/main'), controller)
        workflow = read(admission.PREFIX + f'/actions/workflows/{initial_run["workflow_id"]}')
        require(type(workflow) is dict and type(workflow.get('id')) is int
                and workflow['id'] == initial_run['workflow_id']
                and workflow.get('path') == admission.WORKFLOW and workflow.get('state') == 'active')
        reference = discover(read(run_path + '/artifacts?per_page=100&page=1'), controller, publication)
        intake = artifact_handoff.acquire(kind='admission', reference=reference, controller=controller,
            read=read, download=download, output=work/'admission')
        controller_binding(records.read_json(intake/'controller.json'), controller)
        manifest_raw = private_bytes(intake/SOURCE_PATH.rsplit('/', 1)[1])
        require(manifest_raw == source_bytes(read(admission.PREFIX + '/contents/' + SOURCE_PATH + '?ref=' + controller['commit'])))
        manifest = records.strict_json(manifest_raw.decode('utf-8'))
        public = records.read_json(intake/'public-inputs.json')
        publish_images.qualified_sources(records.read_json(intake/'source-checks.json'), manifest)
        bundle = artifact_handoff.acquire(kind='publication', reference=publication, controller=controller,
            read=read, download=download, output=work/'publication')
        published = records.read_json(bundle/'published-receipt.json')
        require(type(published) is dict)
        controller_binding(published.get('controller'), controller)
        receipt = publication_record.qualify(receipt=published,
            manifest=manifest, public=public, controller=controller)
        images = receipt['images']
        require(len({image['image'] for image in images}) == 15)
        packages = {}
        for image in images:
            workload = image['workload']
            if workload not in packages:
                packages[workload] = package(read, workload)
            require(packages[workload]['id'] == image['package_id'])
        signatures = [signature(verify(image=image['image'], controller=copy.deepcopy(controller)),
                                image['image'], controller) for image in images]
        for workload, expected in packages.items():
            require(package(read, workload) == expected)
        result = {'schema_version': 1, 'artifact_kind': 'verified-deployment-candidate',
                  'stage': stage, 'controller': copy.deepcopy(controller),
                  'admission': reference, 'publication': copy.deepcopy(publication),
                  'source_manifest_sha256': receipt['source_manifest_sha256'],
                  'receipt_sha256': receipt['receipt_sha256'], 'signatures': signatures,
                  'stage_approved': False, 'deployable': False}
        records.bounded_tree(result)
        require(run_identity(read(run_path), controller) == initial_run)
        main_identity(read(admission.PREFIX + '/branches/main'), controller)
        return result
    except Exception:
        raise CandidateError(ERROR) from None


def acquire_migration(*, controller, manifest_bytes, work, read, download):
    """Acquire the build's exact migration data after candidate verification.

    The privileged coordinator supplies its current verified controller/manifest
    and private work. Native artifact metadata and bytes are checked afresh;
    extraction independently reconstructs the reviewed forty-unit SQL plan.
    This data operation supplies no image trust, stage approval, DB freshness,
    backup or permission to execute SQL. The coordinator retains those gates.
    """
    try:
        try:
            from . import migration_transport
        except ImportError:
            import migration_transport
        records.validate_controller(controller)
        require(type(controller['run_attempt']) is int and controller['run_attempt']==1
                and type(manifest_bytes) is bytes and 0<len(manifest_bytes)<=records.MAX_BYTES
                and all(callable(callback) for callback in (read,download))
                and isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True)==work)
        info=work.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid()
                and stat.S_IMODE(info.st_mode)==0o700)
        repo=read(admission.PREFIX);repository(repo)
        require(repo.get('private') is False and repo.get('fork') is False and repo.get('default_branch')=='main')
        run_path=admission.PREFIX+f'/actions/runs/{controller["run_id"]}'
        initial=run_identity(read(run_path),controller)
        main_identity(read(admission.PREFIX+'/branches/main'),controller)
        workflow=read(admission.PREFIX+f'/actions/workflows/{initial["workflow_id"]}')
        require(type(workflow) is dict and type(workflow.get('id')) is int and workflow['id']==initial['workflow_id']
                and workflow.get('path')==admission.WORKFLOW and workflow.get('state')=='active')
        source_path=admission.PREFIX+'/contents/'+SOURCE_PATH+'?ref='+controller['commit']
        require(manifest_bytes==source_bytes(read(source_path)))
        listing=read(run_path+'/artifacts?per_page=100&page=1')
        require(type(listing) is dict and type(listing.get('total_count')) is int
                and 0<listing['total_count']<=100 and type(listing.get('artifacts')) is list
                and len(listing['artifacts'])==listing['total_count']
                and all(type(item) is dict and admission.positive(item.get('id')) for item in listing['artifacts'])
                and len({item['id'] for item in listing['artifacts']})==listing['total_count'])
        selected=[item for item in listing['artifacts'] if item.get('name')==f'apex-migration-{controller["run_id"]}-1']
        require(len(selected)==1);reference=artifact_reference(selected[0])
        intake=artifact_handoff.acquire(kind='migration',reference=reference,controller=controller,
                read=read,download=download,output=work/'migration-artifact')
        bundle=intake/'migration-bundle.tar'
        raw=migration_transport.private_bytes(bundle,os.getuid())
        verified=migration_transport.extract(bundle=bundle,expected_sha256=hashlib.sha256(raw).hexdigest(),
                manifest_bytes=manifest_bytes,controller=controller,work=work)
        require(run_identity(read(run_path),controller)==initial)
        main_identity(read(admission.PREFIX+'/branches/main'),controller)
        require(manifest_bytes==source_bytes(read(source_path)))
        return verified
    except Exception:
        raise CandidateError(ERROR) from None
