#!/usr/bin/env python3
"""Bind migration and DB prerequisite blobs; never run SQL, TypeScript or a DB tool."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

try:
    from . import source_context, release_manifest
except ImportError:
    import source_context
    import release_manifest

OWNERS = ('xynes-infra',) + source_context.BACKENDS
MIGRATION_ROOTS = {
    'xynes-accounts-service': 'drizzle',
    'xynes-authz-service': 'src/db/migrations',
    'xynes-cms-core': 'drizzle',
    'xynes-doc-service': 'drizzle',
    'xynes-telemetry-service': 'src/db/migrations',
}
# A closed source binding, not a declaration that these files may be executed.
PREREQUISITES = {
    'xynes-gateway': ('package.json', 'src/infra/db.ts', 'src/infra/bodyLimitSetup.ts',
                     'src/infra/rateLimitSetup.ts', 'src/data/postgresRouteRepository.ts',
                     'src/data/postgresWorkspaceApiKeyRepository.ts'),
    'xynes-accounts-service': ('package.json', 'drizzle.config.ts', 'src/infra/db/schema.ts'),
    'xynes-authz-service': ('package.json', 'drizzle.config.ts', 'src/db/schema.ts',
                            'src/scripts/seed.ts', 'src/db/seed/authz.seed.ts',
                            'src/db/seed/permissions.config.ts', 'src/infra/ensure-seeded.ts'),
    'xynes-cms-core': ('package.json', 'drizzle.config.ts', 'src/infra/db/schema.ts', 'src/infra/db/migrate.ts',
                          'src/infra/db/seed.ts', 'src/infra/db/seeders.ts'),
    'xynes-doc-service': ('package.json', 'drizzle.config.ts', 'src/infra/db/schema.ts'),
    'xynes-telemetry-service': ('package.json', 'drizzle.config.ts', 'src/db/schema/index.ts'),
    'xynes-storage-service': ('package.json', 'drizzle.config.ts', 'src/infra/db/schema.ts', 'src/infra/db/client.ts'),
}
SCHEMAS = {'xynes-infra': ['identity', 'platform', 'comments'],
           'xynes-gateway': [], 'xynes-accounts-service': [],
           'xynes-authz-service': ['authz'], 'xynes-cms-core': ['cms', 'cms_migration_backups'],
           'xynes-doc-service': ['docs'], 'xynes-telemetry-service': ['telemetry'],
           'xynes-storage-service': []}
MAX_BLOB = 1024 * 1024


class InventoryError(ValueError):
    """A sanitized failure is never a partial inventory."""


def require(condition):
    if not condition:
        raise InventoryError('Migration inventory refused: missing, unsafe or unreviewed source binding')


def safe_path(path):
    require(isinstance(path, str) and 0 < len(path) <= 255 and re.fullmatch(r'[A-Za-z0-9._/-]+', path))
    parts = PurePosixPath(path).parts
    require(not path.startswith('/') and PurePosixPath(path).as_posix() == path
            and all(p not in ('.', '..') and not p.startswith('.env') for p in parts))


def tree(repo, commit):
    require(isinstance(commit, str) and re.fullmatch(r'[a-f0-9]{40}', commit))
    require(source_context.git(repo, 'cat-file', '-t', commit).strip() == b'commit')
    entries = {}
    for row in source_context.git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if row:
            metadata, raw_path = row.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            path = raw_path.decode('utf-8')
            require(path not in entries)
            entries[path] = (mode, kind, oid)
    require(len(entries) <= 20000)
    return entries


def blob(repo, entries, path):
    safe_path(path)
    require(path in entries)
    mode, kind, oid = entries[path]
    require(mode in ('100644', '100755') and kind == 'blob')
    require(int(source_context.git(repo, 'cat-file', '-s', oid)) <= MAX_BLOB)
    data = source_context.git(repo, 'cat-file', 'blob', oid)
    require(len(data) <= MAX_BLOB)
    return data


def json_blob(data):
    require(len(data) <= MAX_BLOB)
    try:
        return release_manifest.strict_json(data.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise InventoryError('Migration inventory refused: malformed metadata') from None


def journal_order(data, root, files):
    journal = json_blob(data)
    require(isinstance(journal, dict) and set(journal) == {'version', 'dialect', 'entries'}
            and journal['version'] == '7' and journal['dialect'] == 'postgresql'
            and isinstance(journal['entries'], list) and 0 < len(journal['entries']) <= 512)
    result = []
    seen = set()
    previous_time = 0
    for index, entry in enumerate(journal['entries']):
        require(isinstance(entry, dict) and set(entry) == {'idx', 'version', 'when', 'tag', 'breakpoints'})
        tag = entry['tag']
        require(type(entry['idx']) is int and entry['idx'] == index and entry['version'] == '7'
                and type(entry['when']) is int and previous_time < entry['when'] <= 9007199254740991
                and type(entry['breakpoints']) is bool
                and isinstance(tag, str) and re.fullmatch(r'[0-9]{4}_[A-Za-z0-9_]+', tag)
                and tag.startswith(f'{index:04d}_') and tag not in seen)
        path = root + '/' + tag + '.sql'
        require(path in files)
        result.append(path)
        seen.add(tag)
        previous_time = entry['when']
    return result


def binding(path, data):
    return {'path': path, 'sha256': hashlib.sha256(data).hexdigest()}


def migration_path(root, path):
    relative = path[len(root) + 1:]
    return (re.fullmatch(r'[0-9]{4}_[A-Za-z0-9_]+\.sql', relative) is not None
            or relative == 'meta/_journal.json'
            or re.fullmatch(r'meta/[0-9]{4}_snapshot\.json', relative) is not None)


def inventory(manifest, repositories, infra_commit):
    """Read exact Git objects. Output .migrations fits release_manifest.validate_artifacts.

    sql_order is per-owner journal/filename order only; there is no global apply plan.
    Blockers are obligations for a later reviewed hosted runner, never approvals.
    """
    source_context.validate_manifest(manifest, 'planning')
    require(isinstance(repositories, dict) and set(repositories) == set(OWNERS))
    require(isinstance(infra_commit, str) and re.fullmatch(r'[a-f0-9]{40}', infra_commit))
    migrations = {}
    owners = {}
    blockers = [{'owner': 'xynes-infra', 'code': 'hosted-ledger-and-adoption-review-required'},
                {'owner': 'xynes-infra', 'code': 'cross-owner-order-and-seed-review-required'}]
    repo = repositories['xynes-infra']
    entries = tree(repo, infra_commit)
    sql_paths = sorted(p for p in entries if p.startswith('supabase/migrations/'))
    require(0 < len(sql_paths) <= 512)
    versions = set()
    infra_files = []
    for path in sql_paths:
        safe_path(path)
        match = re.fullmatch(r'supabase/migrations/([0-9]{14})_[A-Za-z0-9_]+\.sql', path)
        require(match is not None and match[1] not in versions)
        versions.add(match[1])
        infra_files.append(binding(path, blob(repo, entries, path)))
    ownership_reference = binding('docs/DATABASE.md', blob(repo, entries, 'docs/DATABASE.md'))
    migrations['xynes-infra'] = infra_files
    owners['xynes-infra'] = {'schemas': SCHEMAS['xynes-infra'], 'migration_root': 'supabase/migrations',
                              'sql_order': sql_paths, 'prerequisites': []}
    if 'supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql' in entries:
        blockers.append({'owner': 'xynes-infra', 'code': 'dev-only-provider-seed'})
    for owner in source_context.BACKENDS:
        repo = repositories[owner]
        commit = manifest['sources'][owner]['commit']
        entries = tree(repo, commit)
        root = MIGRATION_ROOTS.get(owner)
        selected = set(PREREQUISITES[owner])
        for path in entries:
            # Test/docs SQL is not production SQL. Unknown production SQL/roots
            # refuse instead of gaining inferred ownership or disappearing.
            parts = PurePosixPath(path).parts
            in_root = root is not None and (path == root or path.startswith(root + '/'))
            if in_root:
                safe_path(path)
                require(migration_path(root, path))
                selected.add(path)
            elif any(part in source_context.DENIED_PARTS for part in parts):
                continue
            elif path.endswith('.sql') or 'migrations' in parts or parts[0] == 'drizzle':
                require(False)
        # Reuse the context exporter, including its source allowlist and symlink
        # rejection, then require every selected path came from that exact commit.
        files = {path: data for path, _, data in source_context.tracked_files(repo, commit, owner)
                 if path in selected}
        require(selected <= set(files) and len(selected) <= 512)
        for path in selected:
            safe_path(path)
            require(len(files[path]) <= MAX_BLOB)
            if path.endswith('.json'):
                json_blob(files[path])
        if root:
            journal = root + '/meta/_journal.json'
            require(journal in selected)
            order = journal_order(files[journal], root, files)
            sql = {p for p in selected if p.endswith('.sql')}
            require(sql)
            # Distinct SQL files must not claim the same Drizzle ordinal.
            ordinals = [PurePosixPath(p).name[:4] for p in sql]
            require(len(set(ordinals)) == len(ordinals))
            if sql != set(order):
                blockers.append({'owner': owner, 'code': 'unjournaled-sql'})
        else:
            order = []
        if owner == 'xynes-telemetry-service':
            package = json_blob(files['package.json'])
            require(isinstance(package, dict) and isinstance(package.get('scripts', {}), dict))
            if any('drizzle-kit push' in command for command in package.get('scripts', {}).values()
                   if isinstance(command, str)):
                blockers.append({'owner': owner, 'code': 'push-is-not-an-ordered-migration-runner'})
        migrations[owner] = [binding(path, files[path]) for path in sorted(selected)]
        owners[owner] = {'schemas': SCHEMAS[owner], 'migration_root': root,
                         'sql_order': order, 'prerequisites': sorted(PREREQUISITES[owner])}
    # Parser conformance without creating a release, operational artifact or approval.
    release_manifest.validate_artifacts({'bundle_sha256': '0' * 64, 'binaries': {
        'xynes-idle': {'platform': 'linux/amd64', 'sha256': '0' * 64}}}, migrations)
    return {'schema_version': 1, 'artifact_kind': 'immutable-migration-prerequisite-inventory',
            'source_manifest_sha256': release_manifest.canonical_sha256(manifest),
            'infra_commit': infra_commit, 'source_commits': {
                owner: manifest['sources'][owner]['commit'] for owner in source_context.BACKENDS},
            'ownership_reference': ownership_reference, 'migrations': migrations,
            'owners': owners, 'blockers': sorted(blockers, key=lambda x: (x['owner'], x['code']))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--infra-repo', type=Path, required=True)
    parser.add_argument('--infra-commit', required=True)
    parser.add_argument('--workspace-root', type=Path, required=True,
                        help='Meta-folder containing xynes/<backend> Git repositories')
    args = parser.parse_args()
    try:
        manifest = release_manifest.read_json(args.source_manifest)
        repositories = {'xynes-infra': args.infra_repo}
        repositories.update({owner: args.workspace_root / 'xynes' / owner for owner in source_context.BACKENDS})
        result = inventory(manifest, repositories, args.infra_commit)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, UnicodeError, RecursionError, KeyError, TypeError):
        print('Migration inventory refused: invalid or unavailable immutable source binding', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
