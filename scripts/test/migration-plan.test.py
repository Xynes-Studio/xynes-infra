#!/usr/bin/env python3
"""Source-bound planning fixtures. No SQL, Git, app-source or database execution."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import release_manifest as records
import migration_inventory as inventory_api
TOOL = ROOT / 'scripts/release/migration_plan.py'
FILES = {'xynes-accounts-service': ['drizzle.config.ts',
                            'drizzle/0000_add_workspaces_slug_unique_idx.sql',
                            'drizzle/meta/_journal.json',
                            'package.json',
                            'src/infra/db/schema.ts'],
 'xynes-authz-service': ['drizzle.config.ts',
                         'package.json',
                         'src/db/migrations/0000_unique_morlocks.sql',
                         'src/db/migrations/0001_accounts_schema_1_authz_user_roles_workspace_scoped.sql',
                         'src/db/migrations/meta/0000_snapshot.json',
                         'src/db/migrations/meta/_journal.json',
                         'src/db/schema.ts',
                         'src/db/seed/authz.seed.ts',
                         'src/db/seed/permissions.config.ts',
                         'src/infra/ensure-seeded.ts',
                         'src/scripts/seed.ts'],
 'xynes-cms-core': ['drizzle.config.ts',
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
                    'src/infra/db/seeders.ts'],
 'xynes-doc-service': ['drizzle.config.ts',
                       'drizzle/0000_complete_war_machine.sql',
                       'drizzle/meta/0000_snapshot.json',
                       'drizzle/meta/_journal.json',
                       'package.json',
                       'src/infra/db/schema.ts'],
 'xynes-gateway': ['package.json',
                   'src/data/postgresRouteRepository.ts',
                   'src/data/postgresWorkspaceApiKeyRepository.ts',
                   'src/infra/bodyLimitSetup.ts',
                   'src/infra/db.ts',
                   'src/infra/rateLimitSetup.ts'],
 'xynes-infra': ['supabase/migrations/20251210135828_create_app_schemas.sql',
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
                 'supabase/migrations/20261012090000_gateway_route_body_limits.sql'],
 'xynes-storage-service': ['drizzle.config.ts',
                           'package.json',
                           'src/infra/db/client.ts',
                           'src/infra/db/schema.ts'],
 'xynes-telemetry-service': ['drizzle.config.ts',
                             'package.json',
                             'src/db/migrations/0000_furry_rawhide_kid.sql',
                             'src/db/migrations/0001_gateway_request_logs.sql',
                             'src/db/migrations/meta/0000_snapshot.json',
                             'src/db/migrations/meta/_journal.json',
                             'src/db/schema/index.ts']}


def module():
    if not TOOL.is_file():
        raise AssertionError('immutable migration planning implementation is missing')
    spec = importlib.util.spec_from_file_location('migration_plan', TOOL)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='apex-migration-plan-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.manifest = records.read_json(ROOT / 'infra/release/source-manifest.json')
        self.manifest['status'] = 'planning';self.manifest['approval_reference'] = None
        self.roots = {owner: self.base / owner for owner in FILES}
        self.inventory = {'schema_version': 1, 'artifact_kind': 'immutable-migration-prerequisite-inventory',
            'source_manifest_sha256': records.canonical_sha256(self.manifest), 'infra_commit': 'a'*40,
            'source_commits': {owner: self.manifest['sources'][owner]['commit'] for owner in inventory_api.source_context.BACKENDS},
            'ownership_reference': {'path': 'docs/DATABASE.md', 'sha256': ''}, 'migrations': {}, 'owners': {},
            'blockers': [{'owner':'xynes-infra','code':'cross-owner-order-and-seed-review-required'},
                         {'owner':'xynes-infra','code':'dev-only-provider-seed'},
                         {'owner':'xynes-infra','code':'hosted-ledger-and-adoption-review-required'},
                         {'owner':'xynes-telemetry-service','code':'push-is-not-an-ordered-migration-runner'}]}
        for owner, paths in FILES.items():
            self.roots[owner].mkdir()
            files = []
            sql_order = sorted(path for path in paths if path.endswith('.sql'))
            migration_root = 'supabase/migrations' if owner == 'xynes-infra' else inventory_api.MIGRATION_ROOTS.get(owner)
            for path in paths:
                if path.endswith('_journal.json'):
                    data = json.dumps({'version':'7','dialect':'postgresql','entries':[
                        {'idx':i,'version':'7','when':i+1,'tag':Path(sql).stem,'breakpoints':True} for i,sql in enumerate(sql_order)]}).encode()
                elif path == 'package.json':
                    data = json.dumps({'scripts':{'db:migrate':'drizzle-kit push'} if owner=='xynes-telemetry-service' else {}}).encode()
                elif path.endswith('.json'):data = b'{}'
                elif path.endswith('.sql'):data = b'-- inert fixture only\nSELECT 1;\n'
                else:data = b'// source prerequisite fixture, never executed\n'
                self.write(owner,path,data)
                files.append({'path':path,'sha256':hashlib.sha256(data).hexdigest()})
            self.inventory['migrations'][owner] = sorted(files,key=lambda x:x['path'])
            self.inventory['owners'][owner] = {'schemas':inventory_api.SCHEMAS[owner], 'migration_root':migration_root,
                'sql_order':sql_order,'prerequisites':[] if owner=='xynes-infra' else sorted(inventory_api.PREREQUISITES[owner])}
        data = b'Fixture ownership reference, no execution authorization.\n'
        self.write('xynes-infra','docs/DATABASE.md',data)
        self.inventory['ownership_reference']['sha256'] = hashlib.sha256(data).hexdigest()

    def write(self,owner,path,data):
        p=self.roots[owner]/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)

    def call(self,manifest=None,inventory=None,roots=None):
        return module().plan(self.manifest if manifest is None else manifest,
                             self.inventory if inventory is None else inventory,
                             self.roots if roots is None else roots)

    def test_returns_complete_deterministic_plan_in_exact_fresh_order_without_authorization(self):
        result = self.call()
        self.assertEqual(result,self.call())
        self.assertFalse(result['approved']);self.assertFalse(result['deployable']);self.assertFalse(result['freshness_verified'])
        self.assertEqual(result['source_manifest_sha256'],records.canonical_sha256(self.manifest))
        units=result['units'];self.assertEqual(len(units),40)
        self.assertEqual([(u['owner'],u['path']) for u in units[:4]],[
            ('xynes-telemetry-service','src/db/migrations/0000_furry_rawhide_kid.sql'),
            ('xynes-telemetry-service','src/db/migrations/0001_gateway_request_logs.sql'),
            ('xynes-infra','supabase/migrations/20251210135828_create_app_schemas.sql'),
            ('xynes-infra','supabase/migrations/20251219133000_accounts_schema_1_identity_workspace_core.sql')])
        self.assertEqual([u['owner'] for u in units[4:18]],['xynes-authz-service']*2+['xynes-doc-service']+['xynes-cms-core']*10+['xynes-accounts-service'])
        self.assertEqual(units[-1]['path'],'supabase/migrations/20261012090000_gateway_route_body_limits.sql')
        for unit in units:
            self.assertEqual(set(unit),{'owner','path','sha256','source_commit'})
            self.assertEqual(unit['source_commit'],self.inventory['infra_commit'] if unit['owner']=='xynes-infra' else self.manifest['sources'][unit['owner']]['commit'])
        self.assertEqual(len(result['exclusions']),1)
        self.assertEqual(result['exclusions'][0]['path'],'supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql')
        self.assertEqual({s['function'] for s in result['seed_requirements']},{'seedAuthz','seedAllDefaultTemplates','seedAllDefaultContentTypes'})
        self.assertNotIn(str(self.base),json.dumps(result))

    def test_returned_bindings_do_not_alias_caller_inventory(self):
        result=self.call();before=copy.deepcopy(result)
        self.inventory['ownership_reference']['sha256']='b'*64
        self.inventory['migrations']['xynes-authz-service'][0]['sha256']='b'*64
        self.assertEqual(result,before)

    def test_unknown_inventory_fields_owners_metadata_paths_and_blocks_refused(self):
        mutations=[lambda x:x.update(approved=True),
            lambda x:x['migrations'].update(foreign=[]),
            lambda x:x['owners']['xynes-cms-core'].update(approved=True),
            lambda x:x['owners']['xynes-cms-core'].update(schemas=['public']),
            lambda x:x['owners']['xynes-cms-core'].update(migration_root='other'),
            lambda x:x['owners']['xynes-cms-core'].update(prerequisites=[]),
            lambda x:x['blockers'].append({'owner':'xynes-infra','code':'unreviewed'}),
            lambda x:x['blockers'].pop()]
        for mutation in mutations:
            with self.subTest(category=mutation.__code__.co_firstlineno):
                inventory=copy.deepcopy(self.inventory);mutation(inventory)
                with self.assertRaises(ValueError):self.call(inventory=inventory)

    def test_inventory_commits_manifest_digest_and_dependency_inventory_must_match(self):
        mutations=[lambda x:x.update(source_manifest_sha256='b'*64),
            lambda x:x.update(infra_commit='main'),
            lambda x:x['source_commits'].update(**{'xynes-doc-service':'b'*40}),
            lambda x:x['source_commits'].pop('xynes-doc-service')]
        for mutation in mutations:
            inventory=copy.deepcopy(self.inventory);mutation(inventory)
            with self.assertRaises(ValueError):self.call(inventory=inventory)
        manifest=copy.deepcopy(self.manifest);manifest['sources']['lumia-ds']['repository']='foreign/lumia-ds'
        with self.assertRaises(ValueError):self.call(manifest=manifest)

    def test_missing_tampered_or_duplicate_any_owner_file_refused_before_plan(self):
        for owner in FILES:
            path=FILES[owner][0];p=self.roots[owner]/path;before=p.read_bytes()
            with self.subTest(owner=owner):
                p.write_bytes(before+b'tampered')
                with self.assertRaises(ValueError):self.call()
                p.write_bytes(before)
                p.rename(p.with_name(p.name+'.saved'))
                with self.assertRaises((ValueError,OSError)):self.call()
                p.with_name(p.name+'.saved').rename(p)
                inventory=copy.deepcopy(self.inventory);inventory['migrations'][owner][1]=copy.deepcopy(inventory['migrations'][owner][0])
                with self.assertRaises(ValueError):self.call(inventory=inventory)

    def test_unknown_metadata_export_or_unlisted_sql_cannot_disappear(self):
        for owner,path in [('xynes-doc-service','drizzle/meta/0001_snapshot.json'),
                           ('xynes-doc-service','drizzle/docs/0001_extra.sql'),
                           ('xynes-storage-service','src/db/migrations/0000_extra.sql'),
                           ('xynes-infra','supabase/migrations/20271012090000_extra.sql')]:
            with self.subTest(owner=owner):
                self.write(owner,path,b'{}' if path.endswith('.json') else b'SELECT 1;')
                with self.assertRaises(ValueError):self.call()
                (self.roots[owner]/path).unlink()

    def test_journal_reordering_and_recomputed_hash_or_duplicate_json_refused(self):
        owner='xynes-cms-core';path='drizzle/meta/_journal.json'
        original=(self.roots[owner]/path).read_bytes()
        for category in ['reverse','duplicate','version','idx','when','tag']:
            inventory=copy.deepcopy(self.inventory);j=json.loads(original)
            if category=='reverse':j['entries'].reverse()
            elif category=='duplicate':
                data=original.replace(b'"version": "7"',b'"version": "7", "version": "7"',1)
            else:
                j['entries'][0][category]={'version':'6','idx':True,'when':True,'tag':'../escape'}[category]
            if category!='duplicate':data=json.dumps(j).encode()
            self.write(owner,path,data)
            for file in inventory['migrations'][owner]:
                if file['path']==path:file['sha256']=hashlib.sha256(data).hexdigest()
            with self.assertRaises(ValueError):self.call(inventory=inventory)
        self.write(owner,path,original)

    def test_advertised_sql_order_must_equal_verified_journal_and_filename_order(self):
        for owner in ['xynes-infra','xynes-authz-service','xynes-cms-core','xynes-telemetry-service']:
            inventory=copy.deepcopy(self.inventory);inventory['owners'][owner]['sql_order'].reverse()
            with self.assertRaises(ValueError):self.call(inventory=inventory)

    def test_schema_foreign_path_traversal_and_source_entry_fields_refused(self):
        for path in ['../outside.sql','/tmp/owned.sql','drizzle/../outside.sql','drizzle//0000.sql']:
            inventory=copy.deepcopy(self.inventory);inventory['migrations']['xynes-doc-service'][0]['path']=path
            with self.assertRaises(ValueError):self.call(inventory=inventory)
        inventory=copy.deepcopy(self.inventory);inventory['migrations']['xynes-doc-service'][0]['source_commit']='b'*40
        with self.assertRaises(ValueError):self.call(inventory=inventory)

    def test_roots_are_closed_absolute_real_and_unambiguous(self):
        for roots in [{}, {**self.roots,'foreign':self.base}, {k:v for k,v in self.roots.items() if k!='xynes-gateway'},
                      {**self.roots,'xynes-gateway':Path('relative')},
                      {**self.roots,'xynes-gateway':self.roots['xynes-doc-service']}]:
            with self.assertRaises(ValueError):self.call(roots=roots)
        alias=self.base/'alias';alias.symlink_to(self.roots['xynes-gateway'],target_is_directory=True)
        with self.assertRaises(ValueError):self.call(roots={**self.roots,'xynes-gateway':alias})

    def test_export_scan_error_cannot_hide_unknown_migration_files(self):
        planner=module()
        def inaccessible(_root,followlinks=False,onerror=None):
            if onerror is not None:onerror(PermissionError('DO-NOT-PRINT inaccessible export'))
            return iter(())
        with mock.patch.object(planner.os,'walk',side_effect=inaccessible):
            with self.assertRaises(ValueError):planner.plan(self.manifest,self.inventory,self.roots)

    def test_file_and_parent_symlinks_or_hardlinks_refused(self):
        owner='xynes-gateway';p=self.roots[owner]/'package.json';saved=p.with_name('package.saved');p.rename(saved);p.symlink_to(saved)
        with self.assertRaises(ValueError):self.call()
        p.unlink();saved.rename(p);os.link(p,self.base/'hardlink')
        with self.assertRaises(ValueError):self.call()
        (self.base/'hardlink').unlink()
        directory=self.roots[owner]/'src';moved=self.base/'moved-src';directory.rename(moved);directory.symlink_to(moved,target_is_directory=True)
        with self.assertRaises(ValueError):self.call()

    def test_fifo_substitution_between_stat_and_open_refuses_without_blocking(self):
        # A real FIFO with no writer makes a blocking O_RDONLY open hang.
        # Bound the child so the RED regression never hangs the test runner.
        script = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import migration_plan as planner
root = Path(sys.argv[2])
target = root / 'race.sql'
target.write_bytes(b'-- regular before stat\nSELECT 1;\n')
original_open = planner.os.open
opened_flags = []
def swap(name, flags, *args, **kwargs):
    if name == 'race.sql':
        target.unlink()
        planner.os.mkfifo(target)
        opened_flags.append(flags)
    return original_open(name, flags, *args, **kwargs)
planner.os.open = swap
try:
    planner.read_owned(root, 'race.sql')
except planner.PlanError:
    assert opened_flags and opened_flags[0] & planner.os.O_NONBLOCK
    print('FIFO refused before read')
else:
    raise AssertionError('FIFO substitution was accepted')
"""
        try:
            result = subprocess.run([sys.executable, '-c', script,
                                     str(ROOT / 'scripts/release'), str(self.base)],
                                    capture_output=True, text=True, timeout=2,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        except subprocess.TimeoutExpired:
            self.fail('file open blocked after real FIFO substitution')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'FIFO refused before read')

    def test_ownership_reference_file_hash_and_fixed_path_required(self):
        inventory=copy.deepcopy(self.inventory);inventory['ownership_reference']['path']='docs/other.md'
        with self.assertRaises(ValueError):self.call(inventory=inventory)
        self.write('xynes-infra','docs/DATABASE.md',b'changed ownership contract')
        with self.assertRaises(ValueError):self.call()

    def test_oversized_or_non_utf8_prerequisite_refused_even_with_recomputed_hash(self):
        owner='xynes-storage-service';path='src/infra/db/client.ts'
        for payload in [b'x'*(1024*1024+1),b'\xff']:
            inventory=copy.deepcopy(self.inventory);self.write(owner,path,payload)
            for file in inventory['migrations'][owner]:
                if file['path']==path:file['sha256']=hashlib.sha256(payload).hexdigest()
            with self.assertRaises(ValueError):self.call(inventory=inventory)

    def test_cli_publishes_complete_plan_only_and_sanitizes_invalid_input(self):
        arguments=[]
        for name,value in [('source-manifest',self.manifest),('inventory',self.inventory),('roots',{k:str(v) for k,v in self.roots.items()})]:
            path=self.base/(name+'.json');path.write_text(json.dumps(value));arguments.extend(['--'+name,str(path)])
        result=subprocess.run([sys.executable,str(TOOL),*arguments],capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout),self.call())
        inventory=self.base/'inventory.json';inventory.write_text('{"secret":"DO-NOT-PRINT","secret":"duplicate"}')
        result=subprocess.run([sys.executable,str(TOOL),*arguments],capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,1);self.assertEqual(result.stdout,'');self.assertNotIn('DO-NOT-PRINT',result.stderr)

    def test_missing_max_body_append_is_not_a_valid_plan(self):
        path='supabase/migrations/20261012090000_gateway_route_body_limits.sql'
        self.inventory['migrations']['xynes-infra']=[f for f in self.inventory['migrations']['xynes-infra'] if f['path']!=path]
        self.inventory['owners']['xynes-infra']['sql_order'].remove(path)
        with self.assertRaises(ValueError):self.call()


if __name__ == '__main__':unittest.main()
