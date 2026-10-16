#!/usr/bin/env python3
"""Prepare deny-by-default image contexts from immutable Git objects, never a checkout."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

INFRA = Path(__file__).resolve().parents[2]
BACKENDS = ('xynes-gateway', 'xynes-accounts-service', 'xynes-authz-service',
            'xynes-cms-core', 'xynes-doc-service', 'xynes-telemetry-service', 'xynes-storage-service')
APPS = ('xynes-auth-app', 'xynes-cms-console-web')
LIBS = ('xynes-auth-sdk', 'xynes-i18n', 'lumia-ds')
SOURCES = BACKENDS + APPS + LIBS
EXTERNAL_IMAGE_PREFIXES = {'clamav-freshclam': 'clamav/clamav:',
                          'supabase-db': 'postgres:17-alpine@sha256:',
                          'supabase-auth': 'supabase/gotrue:v2.'}
INFRA_RECIPES = {name: 'infra/images/' + name + '.Dockerfile' for name in BACKENDS[:6]}
INFRA_RECIPES['xynes-auth-app'] = 'infra/images/auth-app.Dockerfile'
INFRA_RECIPES['xynes-cms-console-web'] = 'infra/images/cms-console-web.Dockerfile'
PROCESSOR_FILES = {
    'archive-scanner': {'go.mod', 'archive.go', 'server.go', 'archive_test.go',
                        'gateway_test.go', 'more_test.go', 'server_test.go', 'coverage-gate.py'},
    'libreoffice': {'package.json', 'bun.lock', 'tsconfig.json', '.eslintrc.cjs',
                   'src/convert.ts', 'src/errors.ts', 'src/health.ts', 'src/parse-request.ts',
                   'src/safe-mime.ts', 'src/shim.ts', 'src/soffice-runner.ts',
                   'tests/_test-helpers.ts', 'tests/config.test.ts', 'tests/convert.test.ts',
                   'tests/errors.test.ts', 'tests/health.test.ts', 'tests/parse-request.test.ts',
                   'tests/safe-mime.test.ts', 'tests/shim-boot.test.ts', 'tests/shim.test.ts',
                   'tests/soffice-runner.test.ts'},
}
SHA = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'[a-f0-9]{64}')
IMAGE = re.compile(r'[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}')
COMMON_FILES = {'package.json', 'bun.lock', 'bun.lockb', 'pnpm-lock.yaml', 'pnpm-workspace.yaml',
                'Dockerfile', 'Dockerfile.dockerignore', '.dockerignore', 'LICENSE', 'index.ts',
                'proxy.ts', 'middleware.ts', 'mdx-components.js', 'components.json'}
CONFIG = re.compile(r'(?:tsconfig[^/]*\.json|next(?:-env\.d\.ts|\.config\.(?:mjs|js|ts))|'
                    r'(?:postcss|tailwind|tsup|drizzle|eslint)\.config\.(?:js|cjs|mjs|ts))')
DIRECTORIES = {'src', 'scripts', 'drizzle'}
FRONTEND_DIRECTORIES = DIRECTORIES | {'app', 'pages', 'public', 'messages', 'messages.meta', 'styles'}
DENIED_PARTS = {'.git', '.github', '.claude', '.entire', '.agents', '.codex', 'node_modules',
                '.pnpm-store', '.next', '.turbo', '.vercel', 'dist', 'coverage', 'test', 'tests',
                '__tests__', '__mocks__', 'e2e', 'playwright-report', 'test-results', 'docs',
                '.DS_Store', '.ssh', '.aws', '.tmp', 'secrets'}
URL_KEYS = {'NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_APP_URL',
            'NEXT_PUBLIC_AUTH_APP_URL'}
REQUIRED = URL_KEYS | {'NEXT_PUBLIC_SUPABASE_ANON_KEY', 'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS'}
AUTH_EXTRA = {'NEXT_PUBLIC_CONSOLE_URL', 'NEXT_PUBLIC_CMS_CONSOLE_URL'}
OPTIONAL = {'NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE'}


class ContextError(ValueError):
    """Sanitized refusal; never attach source values or credentials."""


def require(condition, message):
    if not condition:
        raise ContextError(message)


def exact_keys(value, expected, label):
    require(isinstance(value, dict) and set(value) == set(expected), 'Invalid ' + label + ' fields')


def read_json(path):
    # Duplicate fields must not mean one thing to a reviewer and another to this builder.
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique_pairs)


def validate_manifest(manifest, purpose):
    exact_keys(manifest, {'schema_version', 'status', 'approval_reference', 'sources', 'recipes',
                          'base_images', 'tools', 'processors', 'external_images'}, 'manifest')
    require(type(manifest['schema_version']) is int and manifest['schema_version'] == 1, 'Invalid schema version')
    require(manifest['status'] in ('planning', 'approved'), 'Invalid approval status')
    approval = manifest['approval_reference']
    require(approval is None or (isinstance(approval, str) and re.fullmatch(r'https://github\.com/Xynes-Studio/xynes-infra/(?:pull|issues)/[1-9][0-9]*', approval)), 'Invalid approval reference')
    if manifest['status'] == 'approved' or purpose == 'release':
        require(manifest['status'] == 'approved' and approval is not None,
                'Release preparation requires approved sources and an independent approval reference')
    exact_keys(manifest['sources'], SOURCES, 'source inventory')
    for name, source in manifest['sources'].items():
        exact_keys(source, {'repository', 'commit'}, 'source')
        require(source['repository'] == 'Xynes-Studio/' + name, 'Unknown source repository')
        require(isinstance(source['commit'], str) and SHA.fullmatch(source['commit']), 'Source must pin a full immutable commit')
    exact_keys(manifest['recipes'], BACKENDS + APPS, 'recipe inventory')
    for name, recipe in manifest['recipes'].items():
        exact_keys(recipe, {'source', 'path', 'sha256', 'target'}, 'recipe')
        source = recipe['source']
        require(source == name or (name in INFRA_RECIPES and source == 'xynes-infra'), 'Unknown recipe owner')
        expected = INFRA_RECIPES[name] if source == 'xynes-infra' else 'Dockerfile'
        require(recipe['path'] == expected and recipe['target'] == 'prod', 'Unknown recipe path or target')
        require(isinstance(recipe['sha256'], str) and DIGEST.fullmatch(recipe['sha256']), 'Missing recipe digest')
    bases = manifest['base_images']
    require(isinstance(bases, list) and bases and len(set(bases)) == len(bases), 'Invalid base image inventory')
    require(all(isinstance(x, str) and IMAGE.fullmatch(x) for x in bases), 'Every base image requires a digest')
    exact_keys(manifest['processors'], PROCESSOR_FILES, 'processor inventory')
    for name, processor in manifest['processors'].items():
        exact_keys(processor, {'source', 'context_prefix', 'recipe', 'sha256', 'target', 'platform', 'files', 'base_images'}, 'processor')
        require(processor['source'] == 'xynes-storage-service'
                and processor['context_prefix'] == 'sidecars/' + name
                and processor['recipe'] == 'infra/images/' + name + '.Dockerfile'
                and processor['target'] == 'prod' and processor['platform'] == 'linux/amd64', 'Unknown processor boundary')
        require(isinstance(processor['sha256'], str) and DIGEST.fullmatch(processor['sha256']), 'Missing processor recipe digest')
        require(isinstance(processor['base_images'], list) and processor['base_images']
                and processor['base_images'] == sorted(set(processor['base_images']))
                and all(base in bases for base in processor['base_images']), 'Invalid processor base inventory')
        files = processor['files']
        require(isinstance(files, list) and len(files) == len(PROCESSOR_FILES[name]), 'Missing processor file inventory')
        paths = []
        for file in files:
            exact_keys(file, {'path', 'mode', 'sha256'}, 'processor file')
            require(file['path'] in PROCESSOR_FILES[name] and file['mode'] in ('100644', '100755')
                    and isinstance(file['sha256'], str) and DIGEST.fullmatch(file['sha256']), 'Invalid processor file')
            paths.append(file['path'])
        require(paths == sorted(PROCESSOR_FILES[name]), 'Processor file inventory is not exact and sorted')
    exact_keys(manifest['external_images'], EXTERNAL_IMAGE_PREFIXES, 'external image inventory')
    for name, prefix in EXTERNAL_IMAGE_PREFIXES.items():
        external = manifest['external_images'][name]
        exact_keys(external, {'image', 'platform'}, 'external image')
        require(isinstance(external['image'], str) and IMAGE.fullmatch(external['image'])
                and external['image'].startswith(prefix) and external['platform'] == 'linux/amd64', 'Unpinned or unknown external image')
    exact_keys(manifest['tools'], {'pnpm', 'lumia_pnpm'}, 'tool')
    require(manifest['tools'] == {'pnpm': '10.33.0', 'lumia_pnpm': '10.23.0'}, 'Unreviewed package manager version')


def public_url(value, purpose):
    require(isinstance(value, str) and 0 < len(value) < 2048 and not any(ord(c) < 33 for c in value), 'Invalid public URL')
    url = urlsplit(value)
    require(url.scheme in ('https', 'http') and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment, 'Invalid public URL')
    if url.scheme == 'http':
        require(purpose == 'planning' and url.hostname in ('localhost', '127.0.0.1', '::1'), 'Hosted public URLs require HTTPS')
    try:
        url.port
    except ValueError:
        raise ContextError('Invalid public URL port') from None


def public_key(value, purpose):
    require(isinstance(value, str) and 0 < len(value) < 4096, 'Invalid public Supabase key')
    if purpose == 'planning' and value == 'fixture-public-key':
        return
    if re.fullmatch(r'sb_publishable_[A-Za-z0-9_-]{20,}', value):
        return
    try:
        parts = value.split('.')
        require(len(parts) == 3 and all(re.fullmatch(r'[A-Za-z0-9_-]+', p) for p in parts), 'Invalid public Supabase key')
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        require(isinstance(payload, dict) and payload.get('role') == 'anon', 'Only public anon Supabase keys may enter an image')
    except (ValueError, TypeError):
        raise ContextError('Invalid public Supabase key') from None


def validate_public_inputs(inputs, purpose):
    exact_keys(inputs, APPS, 'public app input')
    for name, values in inputs.items():
        required = REQUIRED | (AUTH_EXTRA if name == 'xynes-auth-app' else set())
        require(isinstance(values, dict) and required <= set(values) <= required | OPTIONAL, 'Missing or unknown public build field')
        for key, value in values.items():
            require(isinstance(value, str) and '\n' not in value and '\r' not in value and '\x00' not in value, 'Invalid public build value')
            if key in URL_KEYS | AUTH_EXTRA:
                public_url(value, purpose)
            elif key == 'NEXT_PUBLIC_SUPABASE_ANON_KEY':
                public_key(value, purpose)
            elif key == 'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS':
                require(0 < len(value) < 4096, 'Invalid redirect domains')
                for host in value.split(','):
                    require(re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]{1,5})?', host), 'Invalid redirect domain')
                    public_url(('http://' if host.split(':')[0] in ('localhost', '127.0.0.1') else 'https://') + host, purpose)
            else:
                flags = json.loads(value or '{}')
                require(isinstance(flags, dict) and len(flags) <= 30 and all(re.fullmatch(r'[a-z][a-z0-9_]{0,79}', k) and isinstance(v, bool) for k, v in flags.items()), 'Invalid feature flag override')


    for key in ('NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_KEY', 'NEXT_PUBLIC_AUTH_APP_URL', 'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS'):
        require(inputs[APPS[0]][key] == inputs[APPS[1]][key], 'Frontend environment inputs disagree')
    require(inputs[APPS[0]]['NEXT_PUBLIC_APP_URL'] == inputs[APPS[0]]['NEXT_PUBLIC_AUTH_APP_URL'], 'Auth app origin mismatch')
    require(inputs[APPS[0]]['NEXT_PUBLIC_CONSOLE_URL'] == inputs[APPS[1]]['NEXT_PUBLIC_APP_URL']
            and inputs[APPS[0]]['NEXT_PUBLIC_CMS_CONSOLE_URL'] == inputs[APPS[1]]['NEXT_PUBLIC_APP_URL'], 'CMS app origin mismatch')


def git(repo, *args):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args], capture_output=True,
                            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
    require(result.returncode == 0, 'Pinned source object unavailable; fetch its exact repository before preparation')
    return result.stdout


def safe_source_file(name, path):
    parts = PurePosixPath(path).parts
    require(parts and not path.startswith('/') and all(p not in ('', '.', '..') for p in parts)
            and not any(ord(c) < 32 for c in path), 'Unsafe tracked source path')
    if any(p in DENIED_PARTS or p.startswith('.env') for p in parts):
        return False
    if any(re.search(r'(?:\.(?:test|spec)\.[^/]+$|\.(?:pem|key|p12|pfx|age|dump|bak|log|tsbuildinfo)$)', p, re.I) for p in parts):
        return False
    if len(parts) == 1:
        return path in COMMON_FILES or CONFIG.fullmatch(path) is not None
    if name == 'lumia-ds':
        if path in {'packages/cli/bin/lib/svg-import.js', 'packages/icons/svgr.config.js'}:
            return True
        if path.startswith('packages/icons/svg/') and path.endswith('.svg'):
            return True
        if len(parts) < 3 or parts[0] != 'packages':
            return False
        subpath = '/'.join(parts[2:])
        return safe_source_file('xynes-auth-sdk', subpath) or (len(parts) == 3 and parts[-1].endswith('.css'))
    return parts[0] in (DIRECTORIES if name in BACKENDS else FRONTEND_DIRECTORIES)


def tracked_files(repo, commit, name, processor=None):
    require(git(repo, 'cat-file', '-t', commit).strip() == b'commit', 'Source object must be a commit')
    entries = git(repo, 'ls-tree', '-rz', commit).split(b'\0')
    selected = []
    for entry in entries:
        if not entry:
            continue
        metadata, encoded_path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        path = encoded_path.decode('utf-8')
        prefix = 'sidecars/' + processor + '/' if processor else ''
        allowed = (path.startswith(prefix) and path[len(prefix):] in PROCESSOR_FILES[processor]) if processor else safe_source_file(name, path)
        if allowed:
            require(kind == 'blob' and mode in ('100644', '100755'), 'Selected source symlinks and submodules are refused')
            selected.append((path, mode, oid))
    if processor:
        prefix = 'sidecars/' + processor + '/'
        require({p[len(prefix):] for p, _, _ in selected} == PROCESSOR_FILES[processor], 'Missing reviewed processor source/test file')
    else:
        require(any(p == 'package.json' for p, _, _ in selected), 'Missing tracked package manifest')
    # Batch exact blob IDs avoids export-subst/export-ignore and never reads working-tree bytes.
    process = subprocess.Popen(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
    try:
        for path, mode, oid in selected:
            process.stdin.write((oid + '\n').encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            require(len(header) == 3 and header[1] == 'blob', 'Source blob unavailable')
            size = int(header[2])
            require(size <= 32 * 1024 * 1024, 'Tracked file exceeds source-context limit')
            contents = process.stdout.read(size)
            require(len(contents) == size and process.stdout.read(1) == b'\n', 'Incomplete source blob')
            yield path, mode, contents
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()


def validate_recipe(contents, recipe, bases):
    require(hashlib.sha256(contents).hexdigest() == recipe['sha256'], 'Recipe digest mismatch')
    stages = set()
    used = set()
    for line in contents.decode().splitlines():
        match = re.match(r'FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$', line, re.I)
        if re.match(r'\s*FROM\b', line, re.I):
            require(match is not None, 'Unsupported base declaration syntax')
        if match:
            base, stage = match.groups()
            require(base in stages or base in bases, 'Recipe uses an unpinned or undeclared base image')
            if base in bases:
                used.add(base)
            if stage:
                stages.add(stage)
    require('prod' in stages and used, 'Recipe has no pinned production target')
    return sorted(used)


def prepare(args):
    require(args.environment in ('qa', 'staging', 'prod'), 'Unknown hosted environment')
    manifest = read_json(args.manifest)
    validate_manifest(manifest, args.purpose)
    inputs = read_json(args.public_inputs)
    validate_public_inputs(inputs, args.purpose)
    output = args.output.absolute()
    require(not output.exists() and not output.is_symlink(), 'Output already exists; use a new empty destination')
    require(output.parent.is_dir() and not output.parent.is_symlink(), 'Output parent must exist and be a real directory')
    workspace = args.workspace_root.resolve(strict=True)
    work = Path(tempfile.mkdtemp(prefix='.source-context-', dir=output.parent))
    record = {'schema_version': 1, 'artifact_kind': 'git-source-image-contexts',
              'deployable': False, 'status': manifest['status'], 'purpose': args.purpose,
              'approval_reference': manifest['approval_reference'], 'environment': args.environment,
              'sources': manifest['sources'], 'recipes': manifest['recipes'], 'base_images': manifest['base_images'],
              'processors': manifest['processors'], 'external_images': manifest['external_images'],
              'tools': manifest['tools'], 'public_inputs': inputs, 'files': {}}
    try:
        for name, source in manifest['sources'].items():
            repo = workspace / ('xynes' if name in BACKENDS else 'xynes-front-end') / name
            require(repo.is_dir() and repo.resolve().is_relative_to(workspace), 'Source repository missing or outside workspace')
            destination = work / ('backends/' + name if name in BACKENDS else 'frontend/' + name)
            for path, mode, contents in tracked_files(repo, source['commit'], name):
                require(not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[A-Z0-9]{16}|xynes_live_[a-f0-9]{64}|sb_secret_[A-Za-z0-9_-]{20,}', contents), 'Tracked secret material is refused')
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
                target.chmod(0o755 if mode == '100755' else 0o644)
                record['files'][str(target.relative_to(work))] = hashlib.sha256(contents).hexdigest()
        for name, recipe in manifest['recipes'].items():
            if recipe['source'] == 'xynes-infra':
                contents = (INFRA / recipe['path']).read_bytes()
                path = (work / 'backends' / name / 'Dockerfile') if name in BACKENDS else work / 'frontend' / Path(recipe['path']).name
                path.write_bytes(contents)
                if name == 'xynes-auth-app':
                    ignore = (INFRA / 'infra/images/auth-app.Dockerfile.dockerignore').read_bytes()
                    ignore_path = work / 'frontend/auth-app.Dockerfile.dockerignore'
                    ignore_path.write_bytes(ignore)
                    record['files'][str(ignore_path.relative_to(work))] = hashlib.sha256(ignore).hexdigest()
                record['files'][str(path.relative_to(work))] = hashlib.sha256(contents).hexdigest()
            else:
                path = work / ('backends/' + name if name in BACKENDS else 'frontend/' + name) / 'Dockerfile'
                contents = path.read_bytes()
            validate_recipe(contents, recipe, manifest['base_images'])
        for name, processor in manifest['processors'].items():
            repo = workspace / 'xynes/xynes-storage-service'
            destination = work / 'processors' / name
            prefix = processor['context_prefix'] + '/'
            actual_files = []
            for path, mode, contents in tracked_files(repo, manifest['sources']['xynes-storage-service']['commit'], 'xynes-storage-service', name):
                require(not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[A-Z0-9]{16}|xynes_live_[a-f0-9]{64}|sb_secret_[A-Za-z0-9_-]{20,}', contents), 'Tracked processor secret material is refused')
                target = destination / path[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
                target.chmod(0o755 if mode == '100755' else 0o644)
                record['files'][str(target.relative_to(work))] = hashlib.sha256(contents).hexdigest()
                actual_files.append({'path': path[len(prefix):], 'mode': mode, 'sha256': hashlib.sha256(contents).hexdigest()})
            require(sorted(actual_files, key=lambda file: file['path']) == processor['files'], 'Processor content inventory mismatch')
            contents = (INFRA / processor['recipe']).read_bytes()
            require(validate_recipe(contents, processor, manifest['base_images']) == processor['base_images'], 'Processor base inventory mismatch')
            target = destination / (name + '.Dockerfile')
            target.write_bytes(contents)
            record['files'][str(target.relative_to(work))] = hashlib.sha256(contents).hexdigest()
        (work / 'source-record.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        # All checks pass before publication; never leave half a context on failure.
        os.rename(work, output)
    except BaseException:
        shutil.rmtree(work)
        raise
    print('Prepared immutable Git source contexts; image builds and release acceptance remain required.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--workspace-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--environment', required=True)
    parser.add_argument('--public-inputs', type=Path, required=True)
    parser.add_argument('--purpose', choices=('planning', 'release'), default='planning')
    args = parser.parse_args()
    try:
        prepare(args)
    except (ContextError, OSError, ValueError, UnicodeError):
        # Input paths, failed git stderr and malformed JSON may contain secrets.
        print('Source context refused: invalid, unavailable or unapproved input. No context published.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
