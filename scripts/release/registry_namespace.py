#!/usr/bin/env python3
"""Create missing GHCR namespaces with inert OCI metadata; never publish app bytes.

The coordinator supplies authenticated native metadata and live admission readers.
None means only authenticated API 404. No network, credentials, CLI or retries here.
Existing namespaces and output paths are never mutated or deleted.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile

try:
    from . import image_build, publish_batch, registry_io, registry_policy, release_manifest as records
except ImportError:
    import image_build
    import publish_batch
    import registry_io
    import registry_policy
    import release_manifest as records

KIND = 'namespace-qualification-only'
RECIPE_SHA256 = hashlib.sha256(b'xynes-apex-namespace-v1:linux/amd64;zero-layers;labels-only').hexdigest()


class NamespaceError(ValueError):
    """Refusals never expose native metadata, credentials or callback errors."""


def require(value):
    if not value:
        raise NamespaceError('Registry namespace qualification refused')


def _task(controller, workload):
    inputs = {'artifact_kind': KIND, 'controller': controller, 'workload': workload,
              'recipe_sha256': RECIPE_SHA256, 'platform': 'linux/amd64'}
    digest = records.canonical_sha256(inputs)
    return {'id': 'namespace--' + workload, 'workload': workload,
            'environment': 'qa' if workload in image_build.source_context.APPS else None,
            'platform': 'linux/amd64', 'target': 'prod', 'source_commit': controller['commit'],
            'recipe_sha256': RECIPE_SHA256, 'build_inputs_sha256': digest,
            'labels': {'org.opencontainers.image.source': 'https://github.com/' + controller['repository'],
                       'org.opencontainers.image.revision': controller['commit'],
                       'com.xynes.apex.controller-commit': controller['commit'],
                       'com.xynes.apex.run-id': str(controller['run_id']),
                       'com.xynes.apex.run-attempt': str(controller['run_attempt']),
                       'com.xynes.apex.build-inputs-sha256': digest}}


def _archive(path, task):
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')
    def descriptor(data, media_type):
        return {'mediaType': media_type, 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data)}
    config = encode({'architecture': 'amd64', 'os': 'linux',
                     'config': {'Labels': {**task['labels'], 'com.xynes.apex.artifact-kind': KIND}},
                     'rootfs': {'type': 'layers', 'diff_ids': []}})
    config_ref = descriptor(config, image_build.OCI_CONFIG)
    manifest = encode({'schemaVersion': 2, 'mediaType': image_build.OCI_MANIFEST,
                       'config': config_ref, 'layers': []})
    manifest_ref = descriptor(manifest, image_build.OCI_MANIFEST)
    members = {'oci-layout': encode({'imageLayoutVersion': '1.0.0'}),
               'index.json': encode({'schemaVersion': 2, 'manifests': [manifest_ref]}),
               'blobs/sha256/' + config_ref['digest'][7:]: config,
               'blobs/sha256/' + manifest_ref['digest'][7:]: manifest}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as file:
        os.fchmod(file.fileno(), 0o600)
        with tarfile.open(fileobj=file, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, data in members.items():
                item = tarfile.TarInfo(name); item.size = len(data); item.mode = 0o600
                archive.addfile(item, io.BytesIO(data))
        file.flush(); os.fsync(file.fileno())
    return image_build.inspect_oci(path, task)


def qualify(*, controller, read_package, admission_check, credential, transport, output,
            publish=registry_io.publish_oci):
    """Return exactly eleven native private-package proofs, never app-image receipts."""
    try:
        require(all(callable(c) for c in (read_package, admission_check, publish)))
        controller = copy.deepcopy(controller); records.validate_controller(controller)
        require(controller['run_attempt'] == 1 and len(registry_io.WORKLOADS) == 11
                and len(set(registry_io.WORKLOADS)) == 11)
        require(isinstance(credential, registry_io.GitHubPublisherToken)
                and callable(getattr(transport, 'request', None)))
        output = Path(output)
        require(output.is_absolute() and not output.exists() and not output.is_symlink()
                and output.parent.is_dir() and output.parent.resolve() == output.parent)
        packages, ids = {}, set()
        for workload in registry_io.WORKLOADS:
            proof = read_package(workload)
            if proof is not None:
                proof = publish_batch.qualify_package(proof, workload)
                require(proof['id'] not in ids); ids.add(proof['id'])
            packages[workload] = proof
        publish_batch.qualify_admission(admission_check(copy.deepcopy(controller)), controller)
        output.mkdir(mode=0o700); os.chmod(output, 0o700)
        directory = output.lstat()
        prepared = {}
        for workload, proof in packages.items():
            if proof is None:
                task = _task(controller, workload); path = output / (workload + '.oci.tar')
                registry_io.validate_task(task, registry_policy.EXPECTED)
                prepared[workload] = (task, path, _archive(path, task))
        for workload, (task, path, local) in prepared.items():
            require(output.resolve() == output and os.path.samestat(directory, output.lstat())
                    and stat.S_IMODE(output.stat().st_mode) == 0o700)
            # A namespace that appeared since preflight must already be private and
            # linked correctly; it is never overwritten by this qualification run.
            proof = read_package(workload)
            if proof is None:
                require(image_build.inspect_oci(path, task) == local)
                publish_batch.qualify_admission(admission_check(copy.deepcopy(controller)), controller)
                image = registry_policy.EXPECTED['registry_prefix'] + '/' + workload + '@' + local['manifest_digest']
                result = publish(path, copy.deepcopy(task), image, policy=copy.deepcopy(registry_policy.EXPECTED),
                                 credential=credential, transport=transport)
                records.bounded_tree(result)
                require(result == {**local, 'image': image, 'published': True, 'registry_content_verified': True,
                                   'visibility_verified': False, 'attested': False, 'trust_verified': False, 'deployable': False}
                        and all(result[k] is True for k in ('published', 'registry_content_verified'))
                        and all(result[k] is False for k in ('visibility_verified', 'attested', 'trust_verified', 'deployable')))
                require(image_build.inspect_oci(path, task) == local)
                proof = read_package(workload)
            proof = publish_batch.qualify_package(proof, workload)
            require(proof['id'] not in ids); ids.add(proof['id']); packages[workload] = proof
        # Final native readback prevents returning a stale partial/private inventory.
        for workload, expected in packages.items():
            require(publish_batch.qualify_package(read_package(workload), workload) == expected)
        return copy.deepcopy(packages)
    except Exception:
        raise NamespaceError('Registry namespace qualification refused') from None
