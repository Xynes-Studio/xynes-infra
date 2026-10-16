#!/usr/bin/env python3
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import registry_policy


class RegistryPolicyTests(unittest.TestCase):
    def test_user_selected_private_repo_linked_registry_is_exact(self):
        value=registry_policy.read_policy(ROOT/'infra/release/registry-policy.json')
        self.assertEqual(value,registry_policy.EXPECTED)
        self.assertNotIn('token',value);self.assertEqual(value['server_pull_authentication']['scopes'],['read:packages'])
    def test_other_namespace_public_visibility_write_scope_and_credentials_refused(self):
        with tempfile.TemporaryDirectory(prefix='apex-registry-policy-') as tmp:
            path=Path(tmp)/'policy.json'
            for mutation in [lambda x:x.update(registry_prefix='attacker.invalid/owner'),lambda x:x.update(visibility='public'),lambda x:x.update(token='DO-NOT-EXPOSE'),lambda x:x['server_pull_authentication'].update(scopes=['write:packages']),lambda x:x.update(package_repository='other/infra'),lambda x:x.update(schema_version=True)]:
                value=copy.deepcopy(registry_policy.EXPECTED);mutation(value);path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):registry_policy.read_policy(path)
    def test_nonobject_policy_refuses_cleanly(self):
        with tempfile.TemporaryDirectory(prefix='apex-registry-policy-') as tmp:
            path=Path(tmp)/'policy.json'
            for value in [None, [], 'invalid']:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):registry_policy.read_policy(path)

    def test_duplicate_fields_and_symlink_refused(self):
        with tempfile.TemporaryDirectory(prefix='apex-registry-policy-') as tmp:
            path=Path(tmp)/'policy.json';path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):registry_policy.read_policy(path)
            path.write_text(json.dumps(registry_policy.EXPECTED));link=Path(tmp)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):registry_policy.read_policy(link)

if __name__=='__main__':unittest.main()
