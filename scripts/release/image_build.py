#!/usr/bin/env python3
"""Build fifteen credential-free OCI archives; never publish, attest or deploy."""
import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zlib

try:
    from . import source_context, release_manifest as records
except ImportError:
    import source_context
    import release_manifest as records

SHARED_BUILD = source_context.BACKENDS + ('archive-scanner', 'libreoffice')
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER_TYPES = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+gzip'}
MAX_ARCHIVE = 2 * 1024 ** 3
MAX_EXPANDED = 4 * 1024 ** 3
NAMESPACE_KIND = 'namespace-qualification-only'
NAMESPACE_RECIPE = hashlib.sha256(b'xynes-apex-namespace-v1:linux/amd64;zero-layers;labels-only').hexdigest()


class BuildError(ValueError):
    """Refusals contain no input, credential or build log bytes."""


def require(condition):
    if not condition:
        raise BuildError('Image build refused: unsafe or mismatched input/artifact')


def namespace_task(task):
    """Recognize the exact internal zero-layer namespace recipe, never app bytes."""
    try:
        require(set(task) == {'id', 'workload', 'environment', 'platform', 'target', 'source_commit',
                             'recipe_sha256', 'build_inputs_sha256', 'labels'})
        name = task['workload']; labels = task['labels']
        require(name in SHARED_BUILD + source_context.APPS and task['id'] == 'namespace--' + name
                and task['recipe_sha256'] == NAMESPACE_RECIPE and task['platform'] == 'linux/amd64'
                and task['target'] == 'prod' and task['environment'] == ('qa' if name in source_context.APPS else None))
        require(set(labels) == {'org.opencontainers.image.source', 'org.opencontainers.image.revision',
            'com.xynes.apex.controller-commit', 'com.xynes.apex.run-id', 'com.xynes.apex.run-attempt',
            'com.xynes.apex.build-inputs-sha256'})
        require(all(isinstance(labels[key], str) and re.fullmatch(r'[1-9][0-9]*', labels[key]) for key in
                    ('com.xynes.apex.run-id', 'com.xynes.apex.run-attempt')))
        controller = {'repository': 'Xynes-Studio/xynes-infra', 'commit': labels['com.xynes.apex.controller-commit'],
            'workflow': 'apex-build.yml', 'run_id': int(labels['com.xynes.apex.run-id']),
            'run_attempt': int(labels['com.xynes.apex.run-attempt'])}
        records.validate_controller(controller)
        expected = records.canonical_sha256({'artifact_kind': NAMESPACE_KIND, 'controller': controller,
            'workload': name, 'recipe_sha256': NAMESPACE_RECIPE, 'platform': 'linux/amd64'})
        require(task['source_commit'] == controller['commit'] and task['build_inputs_sha256'] == expected
            and labels['com.xynes.apex.build-inputs-sha256'] == expected
            and labels['org.opencontainers.image.revision'] == controller['commit']
            and labels['org.opencontainers.image.source'] == 'https://github.com/' + controller['repository'])
        return True
    except (ValueError, KeyError, TypeError):
        return False


def build_tasks(manifest, public, controller, purpose):
    require(purpose in ('planning', 'release'))
    records.bounded_tree(manifest); records.bounded_tree(public)
    source_context.validate_manifest(manifest, purpose)
    records.validate_controller(controller)
    records.validate_public_maps(public, purpose)
    tasks = []
    for stage, names in [(None, SHARED_BUILD)] + [(stage, source_context.APPS) for stage in records.STAGES]:
        for name in names:
            owner = name if name in source_context.BACKENDS + source_context.APPS else 'xynes-storage-service'
            recipe = manifest['recipes'][name] if name in source_context.BACKENDS + source_context.APPS else manifest['processors'][name]
            inputs = records.build_inputs(controller, manifest, name, stage, public)
            context_stage = stage or 'qa'
            if name in source_context.BACKENDS:
                context = f'contexts/{context_stage}/backends/{name}'
                dockerfile = context + '/Dockerfile'
            elif name in source_context.APPS:
                context = f'contexts/{context_stage}/frontend'
                dockerfile = context + '/' + (Path(recipe['path']).name if recipe['source'] == 'xynes-infra' else name + '/Dockerfile')
            else:
                context = f'contexts/{context_stage}/processors/{name}'
                dockerfile = context + '/' + name + '.Dockerfile'
            args = {'XYNES_BUILD_VERSION': manifest['sources'][owner]['commit']}
            if stage is not None:
                args.update(public[stage][name])
            task_id = (stage or 'shared') + '--' + name
            digest = records.canonical_sha256(inputs)
            labels = {'org.opencontainers.image.source': 'https://github.com/' + controller['repository'],
                      'org.opencontainers.image.revision': manifest['sources'][owner]['commit'],
                      'com.xynes.apex.controller-commit': controller['commit'],
                      'com.xynes.apex.run-id': str(controller['run_id']),
                      'com.xynes.apex.run-attempt': str(controller['run_attempt']),
                      'com.xynes.apex.build-inputs-sha256': digest}
            if name in source_context.PROCESSOR_FILES:
                content = records.processor_content_identity(manifest, name)
                # Stable old epoch avoids wall-clock metadata and future dates.
                epoch = 946684800 + int(content[:8], 16) % 315360000
                args = {'SOURCE_DATE_EPOCH': str(epoch)}
                labels = {'org.opencontainers.image.source': 'https://github.com/' + controller['repository'],
                          'com.xynes.apex.processor-content-sha256': content}
            tasks.append({'id': task_id, 'workload': name, 'environment': stage,
                          'platform': 'linux/amd64', 'target': 'prod', 'context': context,
                          'dockerfile': dockerfile, 'source_commit': manifest['sources'][owner]['commit'],
                          'recipe_sha256': recipe['sha256'], 'build_inputs_sha256': digest, 'build_args': args,
                          'labels': labels})
    return tasks


def build_argv(task, root, archive, builder, docker_config):
    require(',' not in str(archive) and '\n' not in str(archive))
    require(isinstance(builder, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,63}', builder))
    # Builder and an empty Docker config are supplied by the isolated build job.
    # No caller-controlled extra arguments, registry login, SSH or secret mount.
    deterministic = ',rewrite-timestamp=true,compatibility-version=30' if task['workload'] in source_context.PROCESSOR_FILES else ''
    command = ['docker', '--config', str(docker_config), 'buildx', 'build', '--builder', builder,
               '--platform', 'linux/amd64', '--target', 'prod', '--provenance=false', '--sbom=false',
               '--output', f'type=oci,oci-mediatypes=true,compression=gzip{deterministic},dest={archive}',
               '--file', str(root / task['dockerfile'])]
    for key, value in sorted(task['build_args'].items()):
        command += ['--build-arg', key + '=' + value]
    for key, value in sorted(task['labels'].items()):
        command += ['--label', key + '=' + value]
    return command + [str(root / task['context'])]


class BoundedReader:
    """Stop tar extension records from requesting an unbounded metadata allocation."""
    def __init__(self, file): self.file = file
    def read(self, size):
        require(type(size) is int and 0 <= size <= 1024 * 1024)
        return self.file.read(size)
    def seek(self, *args): return self.file.seek(*args)
    def tell(self): return self.file.tell()


def hash_stream(stream, limit):
    digest = hashlib.sha256(); size = 0
    while True:
        data = stream.read(1024 * 1024)
        if not data: break
        size += len(data); require(size <= limit); digest.update(data)
    return digest.hexdigest(), size


def inspect_oci(path, task):
    """Inspect a complete single-amd64 OCI subset without extracting layer files."""
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= MAX_ARCHIVE)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        with os.fdopen(fd, 'rb') as file:
            opened = os.fstat(file.fileno()); require(os.path.samestat(before, opened))
            with tarfile.open(fileobj=BoundedReader(file), mode='r:') as archive:
                members = {}; blobs = {}; count = 0; total = 0
                for member in archive:
                    count += 1; require(count <= 1024)
                    name = member.name.rstrip('/') if member.isdir() else member.name
                    require(name not in members)
                    require(name in ('oci-layout', 'index.json', 'blobs', 'blobs/sha256')
                            or re.fullmatch(r'blobs/sha256/[a-f0-9]{64}', name))
                    require(member.isdir() and name in ('blobs', 'blobs/sha256') or member.isfile())
                    require(not member.issparse() and 0 <= member.size <= MAX_ARCHIVE)
                    total += member.size; require(total <= MAX_ARCHIVE)
                    members[name] = member
                    if member.isfile() and name.startswith('blobs/'):
                        digest, size = hash_stream(archive.extractfile(member), MAX_ARCHIVE)
                        require(digest == name.split('/')[-1] and size == member.size)
                        blobs['sha256:' + digest] = member
                def json_member(name):
                    require(name in members and members[name].isfile() and members[name].size <= records.MAX_BYTES)
                    return records.strict_json(archive.extractfile(members[name]).read(records.MAX_BYTES + 1).decode('utf8'))
                require(json_member('oci-layout') == {'imageLayoutVersion': '1.0.0'})
                index = json_member('index.json')
                referenced = set()
                def follow(descriptor):
                    require(isinstance(descriptor, dict) and set(descriptor) <= {'mediaType', 'digest', 'size', 'platform', 'annotations'}
                            and {'mediaType', 'digest', 'size'} <= set(descriptor))
                    digest = descriptor['digest']; require(isinstance(digest, str) and digest in blobs)
                    require(type(descriptor['size']) is int and descriptor['size'] == blobs[digest].size)
                    if 'platform' in descriptor:
                        platform = descriptor['platform']
                        require(isinstance(platform, dict) and platform.get('os') == 'linux'
                                and platform.get('architecture') == 'amd64' and not platform.get('variant'))
                    referenced.add(digest)
                    return digest, 'blobs/sha256/' + digest.split(':')[1]
                require(isinstance(index, dict) and index.get('schemaVersion') == 2)
                depth = 0
                while True:
                    require(isinstance(index.get('manifests'), list) and len(index['manifests']) == 1)
                    item = index['manifests'][0]; digest, name = follow(item)
                    media_type = item['mediaType']
                    if media_type == OCI_MANIFEST:
                        manifest_digest = digest; manifest = json_member(name); break
                    require(media_type == OCI_INDEX and depth < 3)
                    index = json_member(name); require(isinstance(index, dict) and index.get('schemaVersion') == 2 and index.get('mediaType') == OCI_INDEX)
                    depth += 1
                require(isinstance(manifest, dict) and manifest.get('schemaVersion') == 2 and manifest.get('mediaType') == OCI_MANIFEST
                        and isinstance(manifest.get('layers'), list) and len(manifest['layers']) <= 128
                        and 'artifactType' not in manifest and 'subject' not in manifest)
                config_descriptor = manifest['config']; config_digest, config_name = follow(config_descriptor)
                require(config_descriptor['mediaType'] == OCI_CONFIG)
                config = json_member(config_name)
                require(isinstance(config, dict) and config.get('os') == 'linux' and config.get('architecture') == 'amd64')
                require(isinstance(config.get('config'), dict) and isinstance(config['config'].get('Labels'), dict))
                require(all(config['config']['Labels'].get(k) == v for k, v in task['labels'].items()))
                if task['workload'] in source_context.PROCESSOR_FILES:
                    if 'com.xynes.apex.processor-content-sha256' in task['labels']:
                        require({k for k in config['config']['Labels'] if k.startswith('com.xynes.apex.')} ==
                                {'com.xynes.apex.processor-content-sha256'})
                    else:
                        require(namespace_task(task) and manifest['layers'] == []
                                and config.get('rootfs') == {'type': 'layers', 'diff_ids': []}
                                and config['config'] == {'Labels': {**task['labels'], 'com.xynes.apex.artifact-kind': NAMESPACE_KIND}})
                rootfs = config.get('rootfs'); require(isinstance(rootfs, dict) and rootfs.get('type') == 'layers'
                                                     and isinstance(rootfs.get('diff_ids'), list)
                                                     and len(rootfs['diff_ids']) == len(manifest['layers']))
                expanded = 0
                for layer, diff_id in zip(manifest['layers'], rootfs['diff_ids']):
                    layer_digest, _ = follow(layer); require(layer['mediaType'] in LAYER_TYPES)
                    stream = archive.extractfile(blobs[layer_digest])
                    if layer['mediaType'].endswith('+gzip'):
                        stream = gzip.GzipFile(fileobj=stream)
                    raw_hash, size = hash_stream(stream, MAX_EXPANDED - expanded)
                    expanded += size
                    require(diff_id == 'sha256:' + raw_hash)
                require(referenced == set(blobs))
            file.seek(0); archive_hash, archive_size = hash_stream(file, MAX_ARCHIVE)
            after = path.lstat(); final = os.fstat(file.fileno())
            require(os.path.samestat(opened, after) and os.path.samestat(opened, final)
                    and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    == (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and archive_size == before.st_size)
        return {'workload': task['workload'], 'environment': task['environment'], 'platform': 'linux/amd64',
                'manifest_digest': manifest_digest, 'config_digest': config_digest, 'archive_sha256': archive_hash,
                'archive_size': archive_size, 'source_commit': task['source_commit'],
                'recipe_sha256': task['recipe_sha256'], 'build_inputs_sha256': task['build_inputs_sha256'],
                'published': False, 'deployable': False}
    except (tarfile.TarError, OSError, EOFError, zlib.error, UnicodeError, KeyError, TypeError):
        raise BuildError('Image archive refused: malformed or unavailable content') from None


def execute(manifest, public, controller, purpose, workspace, output, builder, docker_config):
    tasks = build_tasks(manifest, public, controller, purpose)
    require(output.is_absolute() and not output.exists() and not output.is_symlink()
            and output.parent.is_dir() and output.parent.resolve() == output.parent)
    require(docker_config.is_absolute() and docker_config.is_dir() and not docker_config.is_symlink()
            and docker_config.resolve() == docker_config)
    # Build jobs may have public base-pull credentials only. Registry writers and
    # source credentials must never be inherited by this Docker invocation.
    config_file = docker_config / 'config.json'
    require(not config_file.is_symlink() and (not config_file.exists() or records.read_json(config_file) == {}))
    build_argv(tasks[0], output, output / 'archives/first.oci.tar', builder, docker_config)
    output.mkdir(mode=0o700)
    (output / 'contexts').mkdir(); (output / 'archives').mkdir(); (output / 'logs').mkdir()
    for stage in records.STAGES:
        values_path = output / f'public-{stage}.json'
        values_path.write_text(json.dumps(public[stage]))
        manifest_path = output / 'source-manifest.json'
        manifest_path.write_text(json.dumps(manifest))
        with (output / 'logs/source-preparation.log').open('a') as log, contextlib.redirect_stdout(log):
            source_context.prepare(SimpleNamespace(environment=stage, manifest=manifest_path,
                                   purpose=purpose, public_inputs=values_path, output=output / 'contexts' / stage,
                                   workspace_root=workspace))
    # Resolve every scheduled recipe before starting any expensive image build.
    for task in tasks:
        recipe = output / task['dockerfile']
        require(recipe.is_file() and not recipe.is_symlink() and recipe.stat().st_size <= 1024 * 1024)
        require(hashlib.sha256(recipe.read_bytes()).hexdigest() == task['recipe_sha256'])
    receipts = []
    # These are operational Docker client settings, not credentials. No caller
    # supplied secrets, GH_TOKEN, SSH_AUTH_SOCK or host-wide registry config.
    environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH') if k in os.environ}
    for task in tasks:
        artifact = output / 'archives' / (task['id'] + '.oci.tar')
        require(',' not in str(artifact) and '\n' not in str(artifact))
        with (output / 'logs' / (task['id'] + '.log')).open('xb') as log:
            subprocess.run(build_argv(task, output, artifact, builder, docker_config), env=environment,
                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=2400, check=True)
        receipts.append(inspect_oci(artifact, task))
    result = {'schema_version': 1, 'artifact_kind': 'local-oci-build-receipts', 'controller': controller,
              'source_manifest_sha256': records.canonical_sha256(manifest), 'images': receipts,
              'external_images': manifest['external_images'], 'published': False, 'deployable': False}
    # No complete receipt exists after a partial build or failed content check.
    with (output / '.image-receipts.tmp').open('x') as file:
        file.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
        file.flush(); os.fsync(file.fileno())
    os.replace(output / '.image-receipts.tmp', output / 'image-receipts.json')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'build'))
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--public-inputs', type=Path, required=True)
    parser.add_argument('--controller', type=Path, required=True)
    parser.add_argument('--purpose', choices=('planning', 'release'), required=True)
    parser.add_argument('--workspace-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--builder')
    parser.add_argument('--docker-config', type=Path)
    args = parser.parse_args()
    try:
        manifest = records.read_json(args.source_manifest); public = records.read_json(args.public_inputs)
        controller = records.read_json(args.controller)
        if args.operation == 'plan':
            result = {'tasks': build_tasks(manifest, public, controller, args.purpose), 'published': False, 'deployable': False}
        else:
            require(args.workspace_root is not None and args.output is not None and args.builder is not None and args.docker_config is not None)
            result = execute(manifest, public, controller, args.purpose, args.workspace_root.resolve(strict=True),
                             args.output.absolute(), args.builder, args.docker_config.absolute())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, TypeError, RecursionError, subprocess.SubprocessError):
        print('Image coordinator refused: invalid inputs or incomplete build/artifact checks.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
