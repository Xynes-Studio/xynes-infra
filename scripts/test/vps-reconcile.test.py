#!/usr/bin/env python3
"""Executable offline tests: owned temp roots and fake read-only host commands."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'scripts/setup/vps-reconcile.sh'
LABELS = {'io.xynes.managed': 'true', 'io.xynes.owner': 'xynes-infra',
          'io.xynes.environment': 'qa', 'io.xynes.cleanup': 'eligible'}
OLD = '2026-01-01T00:00:00Z'
NOW = '2026-10-10T00:00:00Z'
IMAGE = 'sha256:' + 'a' * 64
PREVIOUS = 'sha256:' + 'b' * 64


def fixture():
    return {'schema_version': 1, 'captured_at': NOW, 'root': '/srv/xynes',
            'runtime': {'os': 'Ubuntu 22.04.5', 'kernel': '5.15', 'docker': '29.5.3', 'compose': '5.1.4'},
            'resources': [], 'paths': [], 'users': [], 'xynes_gid': 1001,
            'retention': {'verified': True, 'keep_days': 30, 'current_images': [IMAGE], 'previous_images': [PREVIOUS]},
            'collection_errors': []}


def resource(kind='image', **extra):
    return {'kind': kind, 'id': ('sha256:' + 'c' * 64) if kind == 'image' else 'c' * 64,
            'name': 'old-qa', 'created_at': OLD, 'labels': copy.deepcopy(LABELS),
            'running': False, 'image_id': None, 'references': [], 'size_bytes': 100, **extra}


class ReconcilerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-reconcile-test-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def run_tool(self, *args, env=None):
        self.assertTrue(TOOL.is_file(), 'read-only reconciler CLI is missing')
        return subprocess.run(['bash', str(TOOL), *map(str, args)], env=env,
                              text=True, capture_output=True, timeout=15)

    def plan(self, value):
        path = self.home / 'inventory.json'
        path.write_text(json.dumps(value))
        out = self.run_tool('--inventory', path, '--dry-run')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_missing_staging_and_env_directory_drift_have_precise_nonrecursive_plan(self):
        value = fixture()
        value['paths'] = [{'path': 'qa/env', 'exists': True, 'kind': 'directory', 'uid': 1005, 'gid': 1001, 'mode': '0750', 'symlink': False}]
        result = self.plan(value)
        drift = [x for x in result['actions'] if x.get('path') == 'qa/env'][0]
        self.assertEqual(drift['operation'], 'set_directory_metadata')
        self.assertEqual(drift['uid'], 0)
        self.assertFalse(drift['recursive'])
        self.assertTrue(any(x.get('path') == 'staging/env' and x['operation'] == 'create_directory' for x in result['actions']))
        self.assertTrue(any(x['operation'] == 'create_network' and x['name'] == 'xynes_staging_net' for x in result['actions']))
        self.assertTrue(any(x['operation'] == 'provision_constrained_deploy_identity' for x in result['actions']))
        self.assertFalse(result['apply_supported'])

    def test_dry_run_repeat_is_byte_identical_and_has_content_digest(self):
        value = fixture()
        first, second = self.plan(value), self.plan(value)
        self.assertEqual(first, second)
        digest = first.pop('plan_sha256')
        self.assertEqual(digest, hashlib.sha256(json.dumps(first, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def test_docker_nanosecond_creation_timestamp_is_accepted_without_losing_retention_boundary(self):
        value = fixture()
        value['resources'] = [resource(created_at='2026-06-11T07:50:34.965467603Z')]
        result = self.plan(value)
        self.assertEqual(len(result['cleanup_candidates']), 1)

    def test_converged_metadata_has_no_repeated_staging_or_owner_actions(self):
        value = fixture()
        value['paths'] = [{'path': p, 'exists': True, 'kind': 'directory', 'uid': 0, 'gid': 1001,
                           'mode': m, 'symlink': False} for p, m in [('staging', '0755'), ('staging/compose', '0750'),
                          ('staging/env', '0750'), ('staging/supabase', '0755'), ('dev/env', '0750'), ('qa/env', '0750'), ('prod/env', '0750')]]
        value['users'] = [{'name': 'xynes_staging_deploy', 'uid': 1008, 'gid': 1001, 'docker_member': False}]
        value['resources'] = [resource('network', name='xynes_staging_net')]
        result = self.plan(value)
        self.assertEqual(result['actions'], [])

    def test_unreferenced_owned_old_image_requires_individual_approval(self):
        value = fixture(); value['resources'] = [resource()]
        result = self.plan(value)
        self.assertEqual(len(result['cleanup_candidates']), 1)
        self.assertTrue(result['cleanup_candidates'][0]['requires_candidate_approval'])
        self.assertFalse(any('delete' in a['operation'] or 'prune' in a['operation'] for a in result['actions']))

    def test_active_current_previous_referenced_unknown_and_recent_images_preserved(self):
        for replacement in [{'id': IMAGE}, {'id': PREVIOUS}, {'references': ['d' * 64]},
                            {'labels': {}}, {'labels': {**LABELS, 'io.xynes.owner': 'unknown'}},
                            {'created_at': '2026-10-09T00:00:00Z'}, {'created_at': '2027-01-01T00:00:00Z'}]:
            with self.subTest(replacement=replacement):
                value = fixture(); value['resources'] = [resource(**replacement)]
                self.assertEqual(self.plan(value)['cleanup_candidates'], [])

    def test_running_container_or_retained_container_image_cannot_be_cleanup(self):
        for replacement in [{'running': True}, {'image_id': IMAGE}, {'image_id': PREVIOUS}]:
            value = fixture(); value['resources'] = [resource('container', **replacement)]
            self.assertEqual(self.plan(value)['cleanup_candidates'], [])

    def test_stopped_container_references_always_protect_its_image(self):
        value = fixture(); image = resource()
        value['resources'] = [image, resource('container', id='d' * 64, image_id=image['id'], labels={})]
        self.assertEqual(self.plan(value)['cleanup_candidates'], [])

    def test_volumes_certs_secrets_backups_and_environment_networks_always_preserved(self):
        for kind in ['volume', 'data', 'certificate', 'secret', 'backup']:
            value = fixture(); value['resources'] = [resource(kind)]
            self.assertEqual(self.plan(value)['cleanup_candidates'], [])
        for name in ['caddy_net', 'xynes_dev_net', 'xynes_qa_net', 'xynes_prod_net', 'xynes_staging_net']:
            value = fixture(); value['resources'] = [resource('network', name=name)]
            self.assertEqual(self.plan(value)['cleanup_candidates'], [])

    def test_cleanup_fails_closed_without_retention_or_complete_collection(self):
        for change in [{'retention': {'verified': False, 'keep_days': 30, 'current_images': [], 'previous_images': []}},
                       {'collection_errors': ['docker-images-unavailable']}]:
            value = fixture(); value.update(change); value['resources'] = [resource()]
            self.assertEqual(self.plan(value)['cleanup_candidates'], [])

    def test_root_escape_symlink_and_ambiguous_paths_rejected_without_echoing_secret(self):
        for bad in ['../outside', 'qa/../env', '/etc', 'qa//env', 'qa/env/private-key']:
            value = fixture(); value['paths'] = [{'path': bad, 'exists': False}]
            path = self.home / 'bad.json'; path.write_text(json.dumps(value))
            out = self.run_tool('--inventory', path, '--dry-run')
            self.assertNotEqual(out.returncode, 0)
        value = fixture();value['paths'] = [{'path': 'qa/env', 'exists': True, 'kind': 'directory', 'uid': 0, 'gid': 1001, 'mode': '0750', 'symlink': True}]
        path.write_text(json.dumps(value));out = self.run_tool('--inventory', path, '--dry-run')
        self.assertNotEqual(out.returncode, 0)

    def test_duplicate_ids_invalid_labels_dates_and_schema_refused(self):
        cases = []
        x=fixture();x['resources']=[resource(),resource()];cases.append(x)
        x=fixture();x['resources']=[resource(created_at='not-a-date')];cases.append(x)
        x=fixture();x['resources']=[resource(id='bad;$(secret)')];cases.append(x)
        x=fixture();x['schema_version']=2;cases.append(x)
        x=fixture();x['retention']['keep_days']=0;cases.append(x)
        x=fixture();x['resources']=[resource(labels={**LABELS,'io.xynes.environment':'qa;secret'})];cases.append(x)
        for value in cases:
            path=self.home/'bad.json';path.write_text(json.dumps(value))
            out=self.run_tool('--inventory',path,'--dry-run')
            self.assertNotEqual(out.returncode,0)
            self.assertNotIn('$(secret)',out.stderr)

    def test_apply_or_deletion_options_refused_even_for_root(self):
        for flag in ['--apply', '--prune', '--delete', '--approve-candidate']:
            out = self.run_tool(flag)
            self.assertNotEqual(out.returncode, 0)

    def fake_host(self):
        binpath = self.home/'bin';binpath.mkdir()
        state = self.home/'host.json';state.write_text(json.dumps({'image': IMAGE, 'marker': 'DO-NOT-PRINT-private-value'}))
        fake = '''#!/usr/bin/env python3
import os,json,sys
from pathlib import Path
s=json.loads(Path(os.environ['FAKE_HOST']).read_text());args=sys.argv[1:];name=Path(sys.argv[0]).name
with open(os.environ['COMMAND_LOG'],'a') as f:f.write(name+' '+repr(args)+'\\n')
if name=='docker':
 if args[:1]==['version']:print('29.5.3')
 elif args[:2]==['compose','version']:print('5.1.4')
 elif args[:2]==['container','ls']:print('a'*64)
 elif args[:2]==['image','ls']:print(s['image'])
 elif args[:2]==['network','ls']:print('b'*64)
 elif args[:2]==['volume','ls']:print('database-volume')
 elif args[:2]==['container','inspect']:print(json.dumps({'id':'a'*64,'name':'/qa-worker','image_id':s['image'],'running':True,'created_at':'2026-01-01T00:00:00Z','labels':{'password':s['marker']},'mounts':[{'Type':'volume','Name':'database-volume','Source':'/srv/xynes/qa/supabase/db','Destination':'/data'}],'networks':{'xynes_qa_net':{}},'Env':['PASSWORD='+s['marker']]}))
 elif args[:2]==['image','inspect']:
  if '.Config.Labels' in args[args.index('--format')+1]:sys.exit(1) # Real Docker missing Labels map entry.
  print(json.dumps({'id':s['image'],'created_at':'2026-01-01T00:00:00Z','labels':{'secret':s['marker']},'size_bytes':100,'digests':[]}))
 elif args[:2]==['network','inspect']:print(json.dumps({'id':'b'*64,'name':'xynes_qa_net','created_at':'2026-01-01T00:00:00Z','labels':{},'containers':{'a'*64:{}}}))
 elif args[:2]==['volume','inspect']:print(json.dumps({'name':'database-volume','created_at':'2026-01-01T00:00:00Z','labels':{},'Mountpoint':'/data/'+s['marker']}))
 else:sys.exit(9)
elif name=='getent':
 if args==['group','xynes']:print('xynes:x:1001:')
 elif args[0]=='passwd':sys.exit(2)
 else:sys.exit(2)
elif name=='systemctl':print('maintenance.timer')
else:sys.exit(9)
'''
        for name in ['docker', 'getent', 'systemctl']:
            p=binpath/name;p.write_text(fake);p.chmod(0o755)
        env=os.environ.copy();env.update(PATH=str(binpath)+os.pathsep+env['PATH'],FAKE_HOST=str(state),COMMAND_LOG=str(self.home/'commands.log'))
        return env

    def test_live_collector_is_sanitized_nonmutating_and_marks_unverified_retention(self):
        env = self.fake_host();tree=self.home/'srv';tree.mkdir();(tree/'qa').mkdir();(tree/'qa/env').mkdir()
        envfile=tree/'qa/env/.env.qa';envfile.write_text('PASSWORD=DO-NOT-PRINT-private-value\n');envfile.chmod(0o640)
        before={p.relative_to(tree).as_posix(): (p.stat().st_mode,p.stat().st_uid,p.stat().st_gid) for p in tree.rglob('*')}
        out=self.run_tool('--collect','--root',tree,env=env)
        self.assertEqual(out.returncode,0,out.stderr);self.assertNotIn('DO-NOT-PRINT-private-value',out.stdout+out.stderr)
        data=json.loads(out.stdout);self.assertFalse(data['retention']['verified'])
        self.assertEqual(data['collection_errors'], [], 'valid unlabeled image must remain inventoried')
        self.assertEqual(len([r for r in data['resources'] if r['kind']=='image']), 1)
        self.assertEqual(len([r for r in data['resources'] if r['kind']=='container']),1)
        after={p.relative_to(tree).as_posix(): (p.stat().st_mode,p.stat().st_uid,p.stat().st_gid) for p in tree.rglob('*')}
        self.assertEqual(before,after);self.assertEqual(envfile.read_text(),'PASSWORD=DO-NOT-PRINT-private-value\n')
        log=(self.home/'commands.log').read_text()
        for forbidden in ['prune',' rm ', 'start', 'stop', 'pull', 'passwd', 'useradd', 'chmod', 'chown']:
            if forbidden=='passwd':continue # getent passwd is a read-only lookup.
            self.assertNotIn(forbidden,log)
        self.assertEqual(data['runtime']['docker'],'29.5.3')
        self.assertEqual(self.plan(data)['cleanup_candidates'],[])

    def test_collector_refuses_symlink_root_and_symlink_in_managed_path(self):
        env=self.fake_host();tree=self.home/'srv';tree.mkdir();link=self.home/'link';link.symlink_to(tree,target_is_directory=True)
        self.assertNotEqual(self.run_tool('--collect','--root',link,env=env).returncode,0)
        (tree/'qa').mkdir();(tree/'qa/env').symlink_to(self.home,target_is_directory=True)
        self.assertNotEqual(self.run_tool('--collect','--root',tree,env=env).returncode,0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
