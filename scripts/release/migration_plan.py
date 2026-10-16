#!/usr/bin/env python3
"""Verify exported migration bindings and plan a fresh bootstrap; never execute it."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

try:
    from . import source_context, migration_inventory as inventory_api, release_manifest as records
except ImportError:
    import source_context
    import migration_inventory as inventory_api
    import release_manifest as records

# Reviewed current inventory only. New SQL or prerequisite paths require review.
REVIEWED_FILES = {'xynes-accounts-service': ('drizzle.config.ts',
                            'drizzle/0000_add_workspaces_slug_unique_idx.sql',
                            'drizzle/meta/_journal.json',
                            'package.json',
                            'src/infra/db/schema.ts'),
 'xynes-authz-service': ('drizzle.config.ts',
                         'package.json',
                         'src/db/migrations/0000_unique_morlocks.sql',
                         'src/db/migrations/0001_accounts_schema_1_authz_user_roles_workspace_scoped.sql',
                         'src/db/migrations/meta/0000_snapshot.json',
                         'src/db/migrations/meta/_journal.json',
                         'src/db/schema.ts',
                         'src/db/seed/authz.seed.ts',
                         'src/db/seed/permissions.config.ts',
                         'src/infra/ensure-seeded.ts',
                         'src/scripts/seed.ts'),
 'xynes-cms-core': ('drizzle.config.ts',
                    'drizzle/0000_aspiring_phalanx.sql',
                    'drizzle/0001_glorious_maximus.sql',
                    'drizzle/0002_mysterious_betty_ross.sql',
                    'drizzle/0003_busy_diamondback.sql',
                    'drizzle/0004_complete_cyclops.sql',
                    'drizzle/0005_green_pixie.sql',
                    'drizzle/0006_sturdy_hobgoblin.sql',
                    'drizzle/0007_cms_entry_actor_columns.sql',
                    'drizzle/0008_cms_published_snapshot.sql',
                    'drizzle/0009_cms_publication_validation.sql',
                    'drizzle/meta/0000_snapshot.json',
                    'drizzle/meta/0001_snapshot.json',
                    'drizzle/meta/0002_snapshot.json',
                    'drizzle/meta/0003_snapshot.json',
                    'drizzle/meta/0004_snapshot.json',
                    'drizzle/meta/_journal.json',
                    'package.json',
                    'src/infra/db/migrate.ts',
                    'src/infra/db/schema.ts',
                    'src/infra/db/seed.ts',
                    'src/infra/db/seeders.ts'),
 'xynes-doc-service': ('drizzle.config.ts',
                       'drizzle/0000_complete_war_machine.sql',
                       'drizzle/meta/0000_snapshot.json',
                       'drizzle/meta/_journal.json',
                       'package.json',
                       'src/infra/db/schema.ts'),
 'xynes-gateway': ('package.json',
                   'src/data/postgresRouteRepository.ts',
                   'src/data/postgresWorkspaceApiKeyRepository.ts',
                   'src/infra/bodyLimitSetup.ts',
                   'src/infra/db.ts',
                   'src/infra/rateLimitSetup.ts'),
 'xynes-infra': ('supabase/migrations/20251210135828_create_app_schemas.sql',
                 'supabase/migrations/20251219133000_accounts_schema_1_identity_workspace_core.sql',
                 'supabase/migrations/20251229100000_create_platform_routes.sql',
                 'supabase/migrations/20251229100001_seed_platform_routes.sql',
                 'supabase/migrations/20251230100000_create_route_rate_limits.sql',
                 'supabase/migrations/20251230100001_seed_route_rate_limits.sql',
                 'supabase/migrations/20260218090000_add_me_profile_route.sql',
                 'supabase/migrations/20260218160000_add_workspace_members_route.sql',
                 'supabase/migrations/20260424090000_workspace_admin_integrations.sql',
                 'supabase/migrations/20260424103000_cms_content_entries_actor_columns.sql',
                 'supabase/migrations/20260427090000_seed_cms_dashboard_routes.sql',
                 'supabase/migrations/20260508120000_fix_workspace_domains_hostname_shape.sql',
                 'supabase/migrations/20260508130000_seed_platform_domain_regenerate_route.sql',
                 'supabase/migrations/20260508140000_add_dns_records_found.sql',
                 'supabase/migrations/20260513090000_universal_storage_platform_schema.sql',
                 'supabase/migrations/20260513120000_seed_platform_storage_routes.sql',
                 'supabase/migrations/20260513140000_promote_workspace_invites_to_infra.sql',
                 'supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql',
                 'supabase/migrations/20260528090000_storage_object_references_and_dedup_index.sql',
                 'supabase/migrations/20260528100000_storage_processing_jobs_active_unique_index.sql',
                 'supabase/migrations/20260529090000_storage_processing_jobs_payload_and_required.sql',
                 'supabase/migrations/20260601090000_workspace_invites_mail_columns.sql',
                 'supabase/migrations/20260603090000_seed_platform_invite_resend_route.sql',
                 'supabase/migrations/20261005090000_seed_cms_delivery_routes.sql',
                 'supabase/migrations/20261012090000_gateway_route_body_limits.sql'),
 'xynes-storage-service': ('drizzle.config.ts',
                           'package.json',
                           'src/infra/db/client.ts',
                           'src/infra/db/schema.ts'),
 'xynes-telemetry-service': ('drizzle.config.ts',
                             'package.json',
                             'src/db/migrations/0000_furry_rawhide_kid.sql',
                             'src/db/migrations/0001_gateway_request_logs.sql',
                             'src/db/migrations/meta/0000_snapshot.json',
                             'src/db/migrations/meta/_journal.json',
                             'src/db/schema/index.ts')}
MAX_BODY = 'supabase/migrations/20261012090000_gateway_route_body_limits.sql'
DEV_PROVIDER = 'supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql'
INITIAL_INFRA = ('supabase/migrations/20251210135828_create_app_schemas.sql',
                 'supabase/migrations/20251219133000_accounts_schema_1_identity_workspace_core.sql')
MAX_FILE_BYTES = inventory_api.MAX_BLOB


class PlanError(ValueError):
    """Refusal contains no rejected path, data, URI, SQL or credential."""


def require(condition):
    if not condition:
        raise PlanError('Migration plan refused: incomplete, unsafe or mismatched source binding')


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected))


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value))


def signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def read_owned(root, path):
    """Walk fixed export directories through no-follow descriptors, never symlinks."""
    inventory_api.safe_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    directory = os.open(root, flags | getattr(os, 'O_DIRECTORY', 0))
    try:
        for component in PurePosixPath(path).parts[:-1]:
            before = os.stat(component, dir_fd=directory, follow_symlinks=False)
            require(stat.S_ISDIR(before.st_mode))
            child = os.open(component, flags | getattr(os, 'O_DIRECTORY', 0), dir_fd=directory)
            try:
                require(signature(before) == signature(os.fstat(child)))
            except Exception:
                os.close(child)
                raise
            os.close(directory)
            directory = child
        name = PurePosixPath(path).name
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and 0 <= before.st_size <= MAX_FILE_BYTES)
        # O_NOFOLLOW alone does not prevent a regular-file -> FIFO race.
        # Open without waiting for a FIFO writer, then validate the actual
        # descriptor before fdopen/read can touch an untrusted file type.
        descriptor = os.open(name, flags | os.O_NONBLOCK, dir_fd=directory)
        try:
            opened = os.fstat(descriptor)
            require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                    and 0 <= opened.st_size <= MAX_FILE_BYTES
                    and signature(before) == signature(opened))
        except Exception:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, 'rb') as file:
            require(signature(opened) == signature(os.fstat(file.fileno())))
            data = file.read(MAX_FILE_BYTES + 1)
            require(len(data) <= MAX_FILE_BYTES
                    and signature(before) == signature(os.fstat(file.fileno()))
                    == signature(os.stat(name, dir_fd=directory, follow_symlinks=False)))
        return data
    finally:
        os.close(directory)


def validate_roots(roots):
    keys(roots, inventory_api.OWNERS)
    result = {}
    for owner, value in roots.items():
        require(isinstance(value, (str, Path)))
        root = Path(value)
        require(root.is_absolute() and len(str(root)) <= 4096)
        before = root.lstat()
        require(stat.S_ISDIR(before.st_mode) and root.resolve(strict=True) == root)
        result[owner] = (root, signature(before))
    require(len({str(root) for root, _ in result.values()}) == len(result))
    return result


def check_export_paths(root, owner):
    count = 0
    migration_root = 'supabase/migrations' if owner == 'xynes-infra' else inventory_api.MIGRATION_ROOTS.get(owner)
    def scan_error(_error):
        raise PlanError('Migration plan refused: exported path scan incomplete')
    for directory, directories, files in os.walk(root, followlinks=False, onerror=scan_error):
        relative_directory = Path(directory).relative_to(root)
        for name in directories + files:
            count += 1;require(count <= 20000)
            relative = (relative_directory / name).as_posix()
            parts = PurePosixPath(relative).parts
            in_root = migration_root is not None and (relative == migration_root or relative.startswith(migration_root + '/'))
            if in_root:
                entry = root / relative
                require(not entry.is_symlink())
                if name in files:
                    require(relative in REVIEWED_FILES[owner])
            elif owner != 'xynes-infra' and not any(part in source_context.DENIED_PARTS for part in parts):
                require(not relative.endswith('.sql') and 'migrations' not in parts and parts[0] != 'drizzle')


def plan(manifest, inventory, roots):
    for value in (manifest, inventory):
        records.bounded_tree(value)
    source_context.validate_manifest(manifest, 'planning')
    keys(inventory, {'schema_version', 'artifact_kind', 'source_manifest_sha256', 'infra_commit',
                     'source_commits', 'ownership_reference', 'migrations', 'owners', 'blockers'})
    require(type(inventory['schema_version']) is int and inventory['schema_version'] == 1
            and inventory['artifact_kind'] == 'immutable-migration-prerequisite-inventory'
            and inventory['source_manifest_sha256'] == records.canonical_sha256(manifest)
            and isinstance(inventory['infra_commit'], str) and re.fullmatch(r'[a-f0-9]{40}', inventory['infra_commit']))
    keys(inventory['source_commits'], source_context.BACKENDS)
    require(inventory['source_commits'] == {owner: manifest['sources'][owner]['commit'] for owner in source_context.BACKENDS})
    keys(inventory['migrations'], inventory_api.OWNERS);keys(inventory['owners'], inventory_api.OWNERS)
    exports = validate_roots(roots)
    bindings = {};contents = {};orders = {}
    for owner in inventory_api.OWNERS:
        files = inventory['migrations'][owner]
        require(isinstance(files, list) and len(files) == len(REVIEWED_FILES[owner]))
        by_path = {}
        for file in files:
            keys(file, {'path', 'sha256'});digest(file['sha256'])
            path = file['path'];inventory_api.safe_path(path)
            require(path in REVIEWED_FILES[owner] and path not in by_path)
            by_path[path] = file
        require(set(by_path) == set(REVIEWED_FILES[owner])
                and [file['path'] for file in files] == sorted(by_path))
        root, _ = exports[owner]
        check_export_paths(root, owner)
        data = {}
        for path, file in by_path.items():
            payload = read_owned(root, path)
            require(hashlib.sha256(payload).hexdigest() == file['sha256'])
            payload.decode('utf8')  # reject invalid text before publishing any plan
            if path.endswith('.json'):
                inventory_api.json_blob(payload)
            data[path] = payload
        metadata = inventory['owners'][owner]
        keys(metadata, {'schemas', 'migration_root', 'sql_order', 'prerequisites'})
        migration_root = 'supabase/migrations' if owner == 'xynes-infra' else inventory_api.MIGRATION_ROOTS.get(owner)
        prerequisites = [] if owner == 'xynes-infra' else sorted(inventory_api.PREREQUISITES[owner])
        require(metadata['schemas'] == inventory_api.SCHEMAS[owner]
                and metadata['migration_root'] == migration_root and metadata['prerequisites'] == prerequisites)
        sql = sorted(path for path in by_path if path.endswith('.sql'))
        if owner == 'xynes-infra':
            order = sql
        elif migration_root:
            order = inventory_api.journal_order(data[migration_root + '/meta/_journal.json'], migration_root, data)
            require(set(order) == set(sql))
        else:
            require(not sql);order = []
        require(metadata['sql_order'] == order)
        orders[owner] = order;bindings[owner] = by_path;contents[owner] = data
    reference = inventory['ownership_reference']
    keys(reference, {'path', 'sha256'});digest(reference['sha256'])
    require(reference['path'] == 'docs/DATABASE.md')
    require(hashlib.sha256(read_owned(exports['xynes-infra'][0], reference['path'])).hexdigest() == reference['sha256'])
    expected_blockers = [{'owner':'xynes-infra','code':'cross-owner-order-and-seed-review-required'},
                         {'owner':'xynes-infra','code':'dev-only-provider-seed'},
                         {'owner':'xynes-infra','code':'hosted-ledger-and-adoption-review-required'}]
    telemetry = inventory_api.json_blob(contents['xynes-telemetry-service']['package.json'])
    require(isinstance(telemetry, dict) and isinstance(telemetry.get('scripts', {}), dict))
    if any('drizzle-kit push' in command for command in telemetry.get('scripts', {}).values() if isinstance(command, str)):
        expected_blockers.append({'owner':'xynes-telemetry-service','code':'push-is-not-an-ordered-migration-runner'})
    require(inventory['blockers'] == sorted(expected_blockers, key=lambda item:(item['owner'],item['code'])))
    source_commits = {'xynes-infra':inventory['infra_commit'], **inventory['source_commits']}
    def unit(owner, path):
        return {'owner':owner, **bindings[owner][path], 'source_commit':source_commits[owner]}
    units = [unit('xynes-telemetry-service', path) for path in orders['xynes-telemetry-service']]
    units += [unit('xynes-infra', path) for path in INITIAL_INFRA]
    for owner in ['xynes-authz-service','xynes-doc-service','xynes-cms-core','xynes-accounts-service']:
        units += [unit(owner, path) for path in orders[owner]]
    units += [unit('xynes-infra', path) for path in orders['xynes-infra'] if path not in INITIAL_INFRA + (DEV_PROVIDER,)]
    require(units[-1]['path'] == MAX_BODY)
    seeds = []
    for owner, path, function, condition in [
        ('xynes-authz-service','src/db/seed/authz.seed.ts','seedAuthz','after-authz-schema-before-apps'),
        ('xynes-cms-core','src/infra/db/seeders.ts','seedAllDefaultTemplates','after-cms-schema-no-workspace-required'),
        ('xynes-cms-core','src/infra/db/seeders.ts','seedAllDefaultContentTypes','existing-workspaces-only')]:
        seeds.append({'owner':owner,'path':path,'function':function,'condition':condition,
                      'source_commit':source_commits[owner], 'bindings':[
                          dict(bindings[owner][entry]) for entry in sorted(inventory_api.PREREQUISITES[owner])]})
    for root, before in exports.values():
        require(signature(root.lstat()) == before)
    return {'schema_version':1,'artifact_kind':'immutable-fresh-migration-plan',
            'source_manifest_sha256':records.canonical_sha256(manifest),
            'inventory_sha256':records.canonical_sha256(inventory), 'source_commits':source_commits,
            'ownership_reference':dict(reference), 'units':units,
            'exclusions':[{**unit('xynes-infra',DEV_PROVIDER),'reason':'dev-only-provider-seed',
                           'environments':['qa','staging','prod']}],
            'seed_requirements':seeds,
            'required_runtime_gates':['independently-approved-source-and-inventory',
                                     'exact-direct-db-identity-and-freshness-or-reviewed-ledger',
                                     'serialized-transactional-execution-and-ledger-hash-refusal',
                                     'catalog-schema-route-and-product-verification',
                                     'stage-specific-storage-provider-provisioning'],
            'approved':False,'deployable':False,'freshness_verified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest',type=Path,required=True)
    parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--roots',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=plan(records.read_json(args.source_manifest),records.read_json(args.inventory),records.read_json(args.roots))
    except (ValueError,OSError,TypeError,KeyError,UnicodeError,RecursionError):
        print('Migration plan refused: invalid or unavailable exported source binding',file=sys.stderr)
        return 1
    print(json.dumps(result,sort_keys=True,indent=2))
    return 0


if __name__=='__main__':sys.exit(main())
