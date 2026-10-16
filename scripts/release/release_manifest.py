#!/usr/bin/env python3
"""Validate a bounded image record's bindings; never authorize publication/deploy."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from urllib.parse import urlsplit

try:
    from . import source_context
except ImportError:
    import source_context

STAGES = ('qa', 'staging', 'prod')
BACKENDS = source_context.BACKENDS
APPS = source_context.APPS
SHARED = BACKENDS + ('archive-scanner', 'libreoffice') + tuple(source_context.EXTERNAL_IMAGE_PREFIXES)
OWNERS = ('xynes-infra',) + BACKENDS
SHA = re.compile(r'[a-f0-9]{40}')
HASH = re.compile(r'[a-f0-9]{64}')
PREFIX = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*')
SECRET = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|xynes_live_[a-f0-9]{64}|sb_secret_[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}')
MAX_BYTES = 256 * 1024


class RecordError(ValueError):
    """Refusal deliberately contains no rejected value, path or credential."""


def require(condition):
    if not condition:
        raise RecordError('Invalid image record or binding')


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected))


def hash_value(value):
    require(isinstance(value, str) and HASH.fullmatch(value) is not None)


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def bounded_tree(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        require(nodes <= 10000 and depth <= 20)
        if isinstance(item, dict):
            require(all(isinstance(k, str) and len(k) <= 128 and not SECRET.search(k) for k in item))
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            require(len(item) <= 8192 and not SECRET.search(item))
        else:
            require(item is None or type(item) in (bool, int))


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: require(False))
    bounded_tree(value)
    return value


def read_json(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_BYTES)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as file:
        opened = os.fstat(file.fileno())
        require(os.path.samestat(before, opened))
        data = file.read(MAX_BYTES + 1)
        after = path.lstat()
        require(len(data) <= MAX_BYTES and os.path.samestat(opened, after)
                and before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size)
    return strict_json(data.decode('utf-8'))


def validate_controller(controller):
    keys(controller, {'repository', 'commit', 'workflow', 'run_id', 'run_attempt'})
    require(controller['repository'] == 'Xynes-Studio/xynes-infra'
            and isinstance(controller['commit'], str) and SHA.fullmatch(controller['commit'])
            and controller['workflow'] == 'apex-build.yml')
    require(type(controller['run_id']) is int and 0 < controller['run_id'] <= 9007199254740991)
    require(type(controller['run_attempt']) is int and 0 < controller['run_attempt'] <= 10000)


def validate_artifacts(operational, migrations):
    keys(operational, {'bundle_sha256', 'binaries'})
    hash_value(operational['bundle_sha256'])
    keys(operational['binaries'], {'xynes-idle'})
    binary = operational['binaries']['xynes-idle']
    keys(binary, {'platform', 'sha256'})
    require(binary['platform'] == 'linux/amd64')
    hash_value(binary['sha256'])
    keys(migrations, OWNERS)
    for owner, files in migrations.items():
        require(isinstance(files, list) and len(files) <= 512)
        seen = set()
        for file in files:
            keys(file, {'path', 'sha256'})
            path = file['path']
            require(isinstance(path, str) and 0 < len(path) <= 255
                    and re.fullmatch(r'[A-Za-z0-9._/-]+', path))
            parts = PurePosixPath(path).parts
            require(PurePosixPath(path).as_posix() == path and not path.startswith('/')
                    and all(p not in ('.', '..') and not p.startswith('.env') for p in parts)
                    and path not in seen and PurePosixPath(path).suffix in ('.sql', '.json', '.ts'))
            if owner == 'xynes-infra':
                require(path.startswith('supabase/migrations/'))
            else:
                require(source_context.safe_source_file(owner, path))
            seen.add(path)
            hash_value(file['sha256'])
    # Empty lists mean only that the explicitly supplied inventory says empty.
    # This parser discovers/applies no migrations and infers no schema ownership.


def build_inputs(controller, source_manifest, name, stage=None, public_inputs=None):
    result = {'controller': controller, 'source_manifest_sha256': canonical_sha256(source_manifest)}
    if name in source_context.EXTERNAL_IMAGE_PREFIXES:
        result['external_image'] = source_manifest['external_images'][name]
        return result
    owner = name if name in BACKENDS + APPS else 'xynes-storage-service'
    recipe = source_manifest['recipes'][name] if name in BACKENDS + APPS else source_manifest['processors'][name]
    result.update(source_commit=source_manifest['sources'][owner]['commit'], recipe_sha256=recipe['sha256'],
                  base_images=source_manifest['base_images'], tools=source_manifest['tools'])
    if stage is not None:
        result.update(environment=stage, public_inputs_sha256=canonical_sha256(public_inputs[stage][name]))
    return result


def processor_content_identity(source_manifest, name):
    """Digest the complete reviewed processor build boundary, not release provenance.

    Git export independently verifies every selected path, mode and byte hash.
    Release receipts continue binding the complete source manifest/controller.
    """
    source_context.validate_manifest(source_manifest, 'planning')
    require(name in source_context.PROCESSOR_FILES)
    processor = source_manifest['processors'][name]
    return canonical_sha256({'schema_version': 1, 'workload': name, 'source': processor['source'],
        'context_prefix': processor['context_prefix'], 'files': processor['files'],
        'recipe_sha256': processor['sha256'], 'base_images': processor['base_images'],
        'target': processor['target'], 'platform': processor['platform'],
        'exporter': {'format': 'oci', 'compression': 'gzip', 'rewrite_timestamp': True,
                     'compatibility_version': 30}, 'build_args': {}})


def validate_image(image, name, controller, source_manifest, registry_prefix, stage=None, public_inputs=None):
    fields = {'image', 'platform', 'build_inputs_sha256'}
    if name not in source_context.EXTERNAL_IMAGE_PREFIXES:
        fields |= {'source_commit', 'recipe_sha256'}
    if stage is not None:
        fields.add('public_inputs_sha256')
    keys(image, fields)
    require(image['platform'] == 'linux/amd64')
    reference = image['image']
    require(isinstance(reference, str) and len(reference) <= 512)
    if name in source_context.EXTERNAL_IMAGE_PREFIXES:
        require(reference == source_manifest['external_images'][name]['image'])
    else:
        require(re.fullmatch(re.escape(registry_prefix + '/' + name) + r'@sha256:[a-f0-9]{64}', reference))
    inputs = build_inputs(controller, source_manifest, name, stage, public_inputs)
    if name not in source_context.EXTERNAL_IMAGE_PREFIXES:
        require(image['source_commit'] == inputs['source_commit'] and image['recipe_sha256'] == inputs['recipe_sha256'])
    if stage is not None:
        require(image['public_inputs_sha256'] == inputs['public_inputs_sha256'])
    require(image['build_inputs_sha256'] == canonical_sha256(inputs))


def validate_public_maps(public_inputs, purpose='release'):
    require(purpose in ('planning', 'release'))
    keys(public_inputs, STAGES)
    for inputs in public_inputs.values():
        source_context.validate_public_inputs(inputs, purpose)
        for values in inputs.values():
            if 'NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE' in values:
                strict_json(values['NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE'] or '{}')
    if purpose == 'release':
        def origin(value):
            url = urlsplit(value)
            return (url.scheme, url.hostname, url.port or 443)
        # Distinct public destinations prevent obvious cross-stage routing;
        # independent hosted checks must still prove DB/auth/provider isolation.
        for app, key in [('xynes-auth-app', 'NEXT_PUBLIC_API_URL'),
                         ('xynes-auth-app', 'NEXT_PUBLIC_SUPABASE_URL'),
                         ('xynes-auth-app', 'NEXT_PUBLIC_AUTH_APP_URL'),
                         ('xynes-cms-console-web', 'NEXT_PUBLIC_APP_URL')]:
            require(len({origin(public_inputs[stage][app][key]) for stage in STAGES}) == len(STAGES))
        stage_origins = {stage: {origin(values[key])
                         for app, values in public_inputs[stage].items()
                         for key in ('NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_AUTH_APP_URL', 'NEXT_PUBLIC_APP_URL')}
                         for stage in STAGES}
        for index, stage in enumerate(STAGES):
            require(all(stage_origins[stage].isdisjoint(stage_origins[other]) for other in STAGES[index + 1:]))
        for stage in STAGES:
            require(origin(public_inputs[stage]['xynes-auth-app']['NEXT_PUBLIC_APP_URL'])
                    != origin(public_inputs[stage]['xynes-cms-console-web']['NEXT_PUBLIC_APP_URL']))


def validate_record(record, source_manifest, public_inputs, artifact_bindings, registry_prefix):
    for value in (record, source_manifest, public_inputs, artifact_bindings):
        bounded_tree(value)
    require(isinstance(registry_prefix, str) and len(registry_prefix) <= 200 and PREFIX.fullmatch(registry_prefix))
    authority = registry_prefix.split('/')[0]
    require('.' in authority.split(':')[0])
    if ':' in authority:
        require(int(authority.split(':')[1]) <= 65535)
    source_context.validate_manifest(source_manifest, 'planning')
    validate_public_maps(public_inputs)
    keys(artifact_bindings, {'operational', 'migrations'})
    validate_artifacts(artifact_bindings['operational'], artifact_bindings['migrations'])
    keys(record, {'schema_version', 'artifact_kind', 'controller', 'source_manifest_sha256', 'images', 'operational', 'migrations'})
    require(type(record['schema_version']) is int and record['schema_version'] == 1
            and record['artifact_kind'] == 'apex-image-record')
    validate_controller(record['controller'])
    require(record['source_manifest_sha256'] == canonical_sha256(source_manifest))
    validate_artifacts(record['operational'], record['migrations'])
    require(record['operational'] == artifact_bindings['operational'] and record['migrations'] == artifact_bindings['migrations'])
    keys(record['images'], {'shared', 'frontends'})
    keys(record['images']['shared'], SHARED)
    for name, image in record['images']['shared'].items():
        validate_image(image, name, record['controller'], source_manifest, registry_prefix)
    keys(record['images']['frontends'], STAGES)
    associations = {}
    for stage, images in record['images']['frontends'].items():
        keys(images, APPS)
        for name, image in images.items():
            validate_image(image, name, record['controller'], source_manifest, registry_prefix, stage, public_inputs)
            key = (name, image['image'])
            require(key not in associations or associations[key] == image['public_inputs_sha256'])
            associations[key] = image['public_inputs_sha256']
    return canonical_sha256(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--public-inputs', type=Path, required=True)
    parser.add_argument('--artifact-bindings', type=Path, required=True)
    parser.add_argument('--registry-prefix', required=True)
    args = parser.parse_args()
    try:
        digest = validate_record(read_json(args.record), read_json(args.source_manifest), read_json(args.public_inputs),
                                 read_json(args.artifact_bindings), args.registry_prefix)
    except (ValueError, OSError, TypeError, UnicodeError, RecursionError):
        print('Release image record refused: invalid, unavailable or mismatched binding.', file=sys.stderr)
        return 1
    print(json.dumps({'artifact_kind': 'apex-image-record', 'record_sha256': digest,
                      'structurally_valid': True, 'trust_verified': False, 'deployable': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
