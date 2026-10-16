#!/usr/bin/env python3
"""Qualify complete OCI batches before any app-byte write, then publish by digest.

The coordinator supplies trusted admission and native package-metadata readers.
Missing packages may be bootstrapped by a trusted namespace callback only after
complete archive validation; app images may never create unqualified packages.
No CLI, credential read, network adapter, attestation or deployment operations.
"""
import copy
from pathlib import Path

try:
    from . import admission, image_build, registry_io, registry_policy, release_manifest as records
except ImportError:
    import admission
    import image_build
    import registry_io
    import registry_policy
    import release_manifest as records

BUILD_KEYS = {'schema_version', 'artifact_kind', 'controller', 'source_manifest_sha256',
              'images', 'external_images', 'published', 'deployable'}
ADMISSION_KEYS = {'repository', 'commit', 'workflow', 'run_id', 'run_attempt', 'admitted',
                  'repository_id', 'job_id', 'workflow_blob', 'environment_id', 'reviewer_id',
                  'checks', 'source_approval', 'evidence_sha256'}
PACKAGE_KEYS = {'id', 'name', 'package_type', 'visibility', 'owner', 'repository'}


class PublishError(ValueError):
    """Never reveal rejected input, provider metadata, credential or callback errors."""


def require(value):
    if not value:
        raise PublishError('Complete image publication refused')


def qualify_admission(value, controller, *, source_sha256=None):
    records.bounded_tree(value)
    require(isinstance(value, dict) and set(value) == ADMISSION_KEYS
            and all(value.get(k) == v for k, v in controller.items())
            and value['admitted'] is True and value['repository_id'] == admission.REPOSITORY_ID
            and controller['run_attempt'] == 1)
    for key in ('job_id', 'environment_id', 'reviewer_id'):
        require(admission.positive(value[key]))
    require(isinstance(value['workflow_blob'], str) and records.SHA.fullmatch(value['workflow_blob']))
    source=value['source_approval']
    require(type(source) is dict and set(source)=={'approval_reference','pull_number','head_commit','merge_commit',
            'tree_sha','reviewer_id','review_id','source_manifest_sha256'}
            and all(admission.positive(source.get(k)) for k in ('pull_number','reviewer_id','review_id'))
            and source['approval_reference']=='https://github.com/'+admission.REPOSITORY+'/pull/'+str(source['pull_number'])
            and source['merge_commit']==controller['commit'] and source['reviewer_id']==value['reviewer_id']
            and all(type(source.get(k)) is str and records.SHA.fullmatch(source[k]) for k in ('head_commit','merge_commit','tree_sha'))
            and type(source.get('source_manifest_sha256')) is str and records.HASH.fullmatch(source['source_manifest_sha256'])
            and (source_sha256 is None or source['source_manifest_sha256']==source_sha256))
    checks = value['checks']
    require(isinstance(checks, list) and len(checks) == len(admission.CHECKS)
            and {c.get('name') for c in checks if isinstance(c, dict)} == set(admission.CHECKS))
    for check in checks:
        require(isinstance(check, dict) and set(check) ==
                {'name', 'check_id', 'run_id', 'job_id', 'run_attempt', 'workflow_id'}
                and all(admission.positive(check[k]) for k in check if k != 'name'))
    require(value['evidence_sha256'] == records.canonical_sha256(
        {k: v for k, v in value.items() if k != 'evidence_sha256'}))
    return copy.deepcopy(value)


def qualify_package(value, workload):
    records.bounded_tree(value)
    require(isinstance(value, dict) and set(value) == PACKAGE_KEYS and admission.positive(value['id'])
            and value['name'] == 'apex/' + workload and value['package_type'] == 'container'
            and value['visibility'] == 'private')
    owner, repository = value['owner'], value['repository']
    require(isinstance(owner, dict) and set(owner) == {'login', 'type'}
            and isinstance(owner['login'], str) and owner['login'].casefold() == 'xynes-studio'
            and owner['type'] == 'Organization')
    require(isinstance(repository, dict) and set(repository) == {'id', 'full_name'}
            and repository['id'] == admission.REPOSITORY_ID and repository['full_name'] == admission.REPOSITORY)
    return copy.deepcopy(value)


def publish_batch(*, manifest, public, controller, build_receipt, archives, admission_check,
                  package_proof, credential, transport, publish=registry_io.publish_oci,
                  namespace_init=None):
    """Callbacks are trusted live proof providers, never serialized approval inputs."""
    try:
        require(all(callable(c) for c in (admission_check, package_proof, publish)))
        require(namespace_init is None or callable(namespace_init))
        # Freeze caller-owned structures before invoking any proof or publisher callback.
        manifest, public, controller, build_receipt = copy.deepcopy((manifest, public, controller, build_receipt))
        records.bounded_tree(build_receipt)
        tasks = image_build.build_tasks(manifest, public, controller, 'release')
        require(len(tasks) == 15 and isinstance(build_receipt, dict) and set(build_receipt) == BUILD_KEYS
                and type(build_receipt['schema_version']) is int and build_receipt['schema_version'] == 1
                and build_receipt['artifact_kind'] == 'local-oci-build-receipts'
                and build_receipt['controller'] == controller
                and build_receipt['source_manifest_sha256'] == records.canonical_sha256(manifest)
                and build_receipt['external_images'] == manifest['external_images']
                and build_receipt['published'] is False and build_receipt['deployable'] is False)
        images = build_receipt['images']
        require(isinstance(images, list) and len(images) == 15)
        by_key = {}
        for image in images:
            require(isinstance(image, dict) and isinstance(image.get('workload'), str))
            key = (image['workload'], image.get('environment'))
            require(key not in by_key); by_key[key] = image
        archives = Path(archives)
        require(archives.is_absolute() and archives.is_dir() and not archives.is_symlink()
                and archives.resolve() == archives)
        names = {task['id'] + '.oci.tar' for task in tasks}
        require({p.name for p in archives.iterdir()} == names)
        prepared = []
        for task in tasks:
            path = archives / (task['id'] + '.oci.tar')
            local = image_build.inspect_oci(path, task)
            require(by_key.get((task['workload'], task['environment'])) == local)
            prepared.append((task, path, local))
        require(isinstance(credential, registry_io.GitHubPublisherToken)
                and callable(getattr(transport, 'request', None)))
        # No package write can occur before the entire archive and namespace inventory passes.
        qualify_admission(admission_check(copy.deepcopy(controller)), controller,source_sha256=records.canonical_sha256(manifest))
        if namespace_init is not None:
            namespace_init()
        packages, package_ids = {}, set()
        for task in tasks:
            workload = task['workload']
            if workload not in packages:
                proof = qualify_package(package_proof(workload), workload)
                require(proof['id'] not in package_ids); package_ids.add(proof['id']); packages[workload] = proof
        published, gates = [], []
        for task, path, local in prepared:
            # Reinspect current bytes before the fresh remote proof and final admission.
            require(image_build.inspect_oci(path, task) == local)
            proof = qualify_package(package_proof(task['workload']), task['workload'])
            require(proof == packages[task['workload']])
            gate = qualify_admission(admission_check(copy.deepcopy(controller)), controller,source_sha256=records.canonical_sha256(manifest))
            image = registry_policy.EXPECTED['registry_prefix'] + '/' + task['workload'] + '@' + local['manifest_digest']
            result = publish(path, copy.deepcopy(task), image, policy=copy.deepcopy(registry_policy.EXPECTED),
                             credential=credential, transport=transport)
            expected = {**local, 'image': image, 'published': True, 'registry_content_verified': True,
                        'visibility_verified': False, 'attested': False, 'trust_verified': False, 'deployable': False}
            records.bounded_tree(result)
            require(isinstance(result, dict) and result == expected
                    and all(result[key] is True for key in ('published', 'registry_content_verified'))
                    and all(result[key] is False for key in ('visibility_verified', 'attested', 'trust_verified', 'deployable')))
            # Visibility evidence is independent of content transport and comes from the native reader.
            proof = qualify_package(package_proof(task['workload']), task['workload'])
            require(proof == packages[task['workload']])
            published.append({**result, 'visibility_verified': True, 'package_id': proof['id']})
            gates.append(gate['evidence_sha256'])
        result = {'schema_version': 1, 'artifact_kind': 'content-verified-image-publications',
                  'controller': controller, 'source_manifest_sha256': records.canonical_sha256(manifest),
                  'build_receipt_sha256': records.canonical_sha256(build_receipt), 'images': published,
                  'external_images': manifest['external_images'], 'admission_evidence_sha256': gates,
                  'published': True, 'registry_content_verified': True, 'visibility_verified': True,
                  'attested': False, 'trust_verified': False, 'deployable': False}
        result['receipt_sha256'] = records.canonical_sha256(result)
        return result
    except Exception:
        raise PublishError('Complete image publication refused') from None
