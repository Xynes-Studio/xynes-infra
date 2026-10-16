"""Exercise actual offline payloads and app-binding drift; never invoke live gh."""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/setup/configure-branch-protection.sh'


class SecurityPolicy(unittest.TestCase):
    def invoke(self, *args, env=None):
        return subprocess.run(['bash', str(SCRIPT), *args], env=env, text=True, capture_output=True, timeout=15)

    def test_every_offline_payload_is_bound_and_preserves_required_gates(self):
        repos = json.loads((ROOT / 'scripts/setup/repos.json').read_text())['repos']
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'gh'
            fake.write_text('#!/bin/sh\necho UNEXPECTED_GH_CALL >&2\nexit 89\n')
            fake.chmod(0o700)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH']}
            for repo in repos:
                for branch in ['main', 'develop', 'release/*']:
                    with self.subTest(repo=repo, branch=branch):
                        result = self.invoke('--print-policy', '--repo', repo, '--branch', branch, env=env)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertNotIn('UNEXPECTED_GH_CALL', result.stderr)
                        value = json.loads(result.stdout)
                        if branch == 'release/*':
                            rule = next(rule for rule in value['rules'] if rule['type'] == 'required_status_checks')
                            checks = rule['parameters']['required_status_checks']
                            self.assertTrue(rule['parameters']['strict_required_status_checks_policy'])
                            self.assertTrue(all(check['integration_id'] == 15368 for check in checks))
                            contexts = [check['context'] for check in checks]
                            self.assertEqual(value['bypass_actors'], [])
                        else:
                            contexts = value['required_status_checks']['contexts']
                            self.assertTrue(value['required_status_checks']['strict'])
                            self.assertEqual(value['required_status_checks']['checks'], [{'context': c, 'app_id': 15368} for c in contexts])
                            self.assertTrue(value['enforce_admins'])
                            self.assertTrue(value['required_pull_request_reviews']['dismiss_stale_reviews'])
                            self.assertTrue(value['required_conversation_resolution'])
                            self.assertFalse(value['allow_force_pushes'])
                            self.assertFalse(value['allow_deletions'])
                        self.assertTrue({'lint', 'test', 'coverage', 'build', 'security'} <= set(contexts))
                        self.assertEqual('typecheck' in contexts, repo != 'xynes-frontend-infra')
                        extras = {'xynes-storage-service': ['release-image-security', 'immutable-source-security', 'integration-processors', 'archive-safety'], 'lumia-ds': ['Visual Regression'], 'xynes-i18n': ['CodeQL analyze'], 'xynes-cms-console-web': ['Code and browser gates']}
                        self.assertTrue(set(extras.get(repo, [])) <= set(contexts))

    def test_print_requires_an_exact_scope(self):
        for args in [[], ['--repo', 'xynes-gateway'], ['--branch', 'main'], ['--repo', 'nonexistent', '--branch', 'main']]:
            with self.subTest(args=args):
                self.assertEqual(self.invoke('--print-policy', *args).returncode, 1)

    def test_api_docs_main_policy_uses_explicit_inventory_without_bootstrapping(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'gh'
            fake.write_text('#!/bin/sh\necho UNEXPECTED_GH_CALL >&2\nexit 89\n')
            fake.chmod(0o700)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH']}
            result = self.invoke('--print-policy', '--repos-file', str(ROOT / 'scripts/setup/ci-api-docs-repos.json'), '--repo', 'xynes-api-docs', '--branch', 'main', env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('UNEXPECTED_GH_CALL', result.stderr)
            value = json.loads(result.stdout)
            contexts = ['lint', 'test', 'coverage', 'build', 'typecheck', 'security']
            self.assertEqual(value['required_status_checks']['checks'], [{'context': c, 'app_id': 15368} for c in contexts])
            self.assertTrue(value['enforce_admins'])
            self.assertTrue(value['required_status_checks']['strict'])
            self.assertFalse(value['allow_force_pushes'])

    def signature(self, name, value, infra_reviews=False):
        match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}', SCRIPT.read_text(), re.M | re.S)
        self.assertIsNotNone(match)
        result = subprocess.run(['bash', '-c', match.group(0) + '\n' + name + (' true' if infra_reviews else '')], input=json.dumps(value), text=True, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_all_48_noninfra_policy_payloads_remain_byte_equivalent(self):
        repos = json.loads((ROOT / 'scripts/setup/repos.json').read_text())['repos']
        fingerprint = hashlib.sha256();count = 0
        for repo in repos + ['xynes-api-docs']:
            if repo == 'xynes-infra':continue
            for branch in ['main', 'develop', 'release/*']:
                args = ['--print-policy', '--repo', repo, '--branch', branch]
                if repo == 'xynes-api-docs':args += ['--repos-file', str(ROOT / 'scripts/setup/ci-api-docs-repos.json')]
                result = self.invoke(*args);self.assertEqual(result.returncode, 0, result.stderr)
                fingerprint.update((repo + '\0' + branch + '\0' + result.stdout).encode());count += 1
        self.assertEqual(count, 48)
        self.assertEqual(fingerprint.hexdigest(), '444e93352f39b2f9ed3891c5454c86f91dd9907afd3e56a26e7121166dfcbbba')

    def test_case_aliases_of_canonical_repository_cannot_bypass_infra_review_policy(self):
        pairs = [('xynes-studio', 'xynes-infra'), ('XYNES-STUDIO', 'xynes-infra'),
                 ('XyNeS-StUdIo', 'XyNeS-InFrA')]
        with tempfile.TemporaryDirectory(prefix='apex-owner-case-') as directory:
            config = Path(directory) / 'repos.json'
            for owner, slug in pairs:
                config.write_text(json.dumps({'owner': owner, 'repos': [slug]}))
                for branch in ['main', 'develop', 'release/*']:
                    with self.subTest(owner=owner, slug=slug, branch=branch):
                        p = self.invoke('--print-policy', '--repos-file', str(config), '--repo', slug, '--branch', branch)
                        self.assertEqual(p.returncode, 0, p.stderr);value = json.loads(p.stdout)
                        if branch == 'release/*':
                            review = next((r for r in value['rules'] if r['type'] == 'pull_request'), None)
                            self.assertIsNotNone(review, 'case alias bypassed infra release reviews')
                            self.assertTrue(review['parameters']['require_code_owner_review'])
                            self.assertTrue(review['parameters']['require_last_push_approval'])
                        else:
                            self.assertTrue(value['required_pull_request_reviews']['require_code_owner_reviews'], 'case alias bypassed infra codeowner review')
                            self.assertTrue(value['required_pull_request_reviews']['require_last_push_approval'])
                            self.assertEqual(value['required_pull_request_reviews']['bypass_pull_request_allowances'], {'users': [], 'teams': [], 'apps': []})

    def test_other_org_infra_slug_keeps_generic_policy(self):
        with tempfile.TemporaryDirectory(prefix='apex-other-owner-') as directory:
            config = Path(directory) / 'repos.json';config.write_text(json.dumps({'owner': 'Fixture-Other-Org', 'repos': ['xynes-infra']}))
            for branch in ['main', 'develop', 'release/*']:
                p = self.invoke('--print-policy', '--repos-file', str(config), '--repo', 'xynes-infra', '--branch', branch)
                self.assertEqual(p.returncode, 0, p.stderr);value = json.loads(p.stdout)
                if branch == 'release/*':self.assertFalse(any(r['type'] == 'pull_request' for r in value['rules']))
                else:
                    self.assertFalse(value['required_pull_request_reviews']['require_code_owner_reviews'])
                    self.assertNotIn('require_last_push_approval', value['required_pull_request_reviews'])
                    self.assertNotIn('bypass_pull_request_allowances', value['required_pull_request_reviews'])

    def test_infra_print_policy_requires_codeowners_last_push_and_zero_bypass(self):
        for branch in ['main', 'develop', 'release/*']:
            with self.subTest(branch=branch):
                p = self.invoke('--print-policy', '--repo', 'xynes-infra', '--branch', branch)
                self.assertEqual(p.returncode, 0, p.stderr)
                value = json.loads(p.stdout)
                if branch == 'release/*':
                    matches = [r for r in value['rules'] if r['type'] == 'pull_request']
                    self.assertEqual(len(matches), 1, 'infra release wildcard has no required review rule')
                    reviews = matches[0]['parameters']
                    self.assertTrue(reviews['required_review_thread_resolution'])
                    self.assertTrue(reviews.get('require_code_owner_review'), 'ruleset needs modern codeowner parameter')
                    self.assertTrue(reviews.get('dismiss_stale_reviews_on_push'), 'ruleset needs modern stale-review parameter')
                    self.assertNotIn('require_code_owner_reviews', reviews)
                    self.assertNotIn('dismiss_stale_reviews', reviews)
                    self.assertEqual(value['bypass_actors'], [])
                else:
                    reviews = value['required_pull_request_reviews']
                    self.assertTrue(reviews['require_code_owner_reviews'], 'infra policy would weaken native codeowner review')
                    self.assertTrue(reviews.get('require_last_push_approval'), 'infra policy would weaken last-push approval')
                    self.assertEqual(reviews.get('bypass_pull_request_allowances'), {'users': [], 'teams': [], 'apps': []})
                    self.assertTrue(value['enforce_admins'])
                self.assertTrue(reviews['require_code_owner_review' if branch == 'release/*' else 'require_code_owner_reviews'], 'infra policy would weaken native codeowner review')
                self.assertTrue(reviews.get('require_last_push_approval'), 'infra policy would weaken last-push approval')
                self.assertTrue(reviews['dismiss_stale_reviews_on_push' if branch == 'release/*' else 'dismiss_stale_reviews'])
                self.assertEqual(reviews['required_approving_review_count'], 1)

    def test_infra_readback_detects_missing_or_weaker_new_review_fields(self):
        for branch in ['main', 'develop']:
            payload = json.loads(self.invoke('--print-policy', '--repo', 'xynes-infra', '--branch', branch).stdout)
            desired = self.signature('branch_protection_desired_signature', payload, True)
            self.assertEqual(self.signature('branch_protection_signature', payload, True), desired)
            for field in ['require_code_owner_reviews', 'require_last_push_approval']:
                for change in [False, None]:
                    altered = json.loads(json.dumps(payload))
                    if change is None:altered['required_pull_request_reviews'].pop(field, None)
                    else:altered['required_pull_request_reviews'][field] = change
                    self.assertNotEqual(self.signature('branch_protection_signature', altered, True), desired,
                                        'stale review requirement falsely matched current infra policy')
            for kind, actor in [('users', {'login': 'fixture-actor'}), ('teams', {'slug': 'fixture-team'}), ('apps', {'slug': 'fixture-app'})]:
                altered = json.loads(json.dumps(payload))
                altered['required_pull_request_reviews'].setdefault('bypass_pull_request_allowances', {'users': [], 'teams': [], 'apps': []})[kind] = [actor]
                self.assertNotEqual(self.signature('branch_protection_signature', altered, True), desired)

    def test_infra_release_signature_compares_each_review_parameter(self):
        payload = json.loads(self.invoke('--print-policy', '--repo', 'xynes-infra', '--branch', 'release/*').stdout)
        review = next((r for r in payload['rules'] if r['type'] == 'pull_request'), None)
        self.assertIsNotNone(review, 'infra release review rule is missing')
        expected = self.signature('ruleset_signature', payload)
        for field in ['require_code_owner_review', 'require_last_push_approval', 'dismiss_stale_reviews_on_push',
                      'required_review_thread_resolution', 'required_approving_review_count']:
            for missing in [False, True]:
                altered = json.loads(json.dumps(payload))
                target = next(r for r in altered['rules'] if r['type'] == 'pull_request')['parameters']
                if missing:target.pop(field)
                else:target[field] = 0 if field == 'required_approving_review_count' else False
                self.assertNotEqual(self.signature('ruleset_signature', altered), expected)

    def test_signature_new_flags_are_opt_in_for_noninfra_existing_review_policy(self):
        payload = json.loads(self.invoke('--print-policy', '--repo', 'xynes-gateway', '--branch', 'main').stdout)
        desired = self.signature('branch_protection_desired_signature', payload)
        response = json.loads(json.dumps(payload))
        response['required_pull_request_reviews']['require_last_push_approval'] = True
        response['required_pull_request_reviews']['bypass_pull_request_allowances'] = {'users': [{'login': 'fixture-actor'}], 'teams': [], 'apps': []}
        self.assertEqual(self.signature('branch_protection_signature', response), desired,
                         'infra extension changed an undeclared noninfra review comparison')

    def audit_fixture(self, value, branch):
        with tempfile.TemporaryDirectory(prefix='apex-policy-readback-') as directory:
            base = Path(directory);metadata = base / 'response.json';metadata.write_text(json.dumps(value))
            fake = base / 'gh'
            fake.write_text("#!/usr/bin/env python3\nimport os,pathlib,sys\na=sys.argv[1:]\n"
                            "if any(x in a for x in ('PUT','POST','PATCH','DELETE')):raise SystemExit(89)\n"
                            "url=next((x for x in a if x.startswith('/repos/')), '')\n"
                            "if url.endswith('/protection') or '/rulesets/' in url:print(pathlib.Path(os.environ['POLICY_RESPONSE']).read_text())\n"
                            "elif url.endswith('/rulesets'):print('17')\n"
                            "else:print('{}')\n")
            fake.chmod(0o700)
            return self.invoke('--audit-only', '--repo', 'xynes-infra', '--branch', branch,
                               env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'POLICY_RESPONSE': str(metadata)})

    def test_real_audit_path_uses_new_fields_without_api_mutation(self):
        for branch in ['main', 'develop', 'release/*']:
            value = json.loads(self.invoke('--print-policy', '--repo', 'xynes-infra', '--branch', branch).stdout)
            self.assertEqual(self.audit_fixture(value, branch).returncode, 0)
            if branch == 'release/*':
                reviews = next((r['parameters'] for r in value['rules'] if r['type'] == 'pull_request'), None)
                self.assertIsNotNone(reviews)
            else:reviews = value['required_pull_request_reviews']
            reviews['require_last_push_approval'] = False
            result = self.audit_fixture(value, branch)
            self.assertEqual(result.returncode, 2, 'actual audit falsely accepted stale last-push policy')
            self.assertIn('DRIFT', result.stdout)

    def test_an_unbound_or_wrong_app_check_is_drift(self):
        payload = json.loads(self.invoke('--print-policy', '--repo', 'xynes-gateway', '--branch', 'develop').stdout)
        desired = self.signature('branch_protection_desired_signature', payload)
        self.assertEqual(self.signature('branch_protection_signature', payload), desired)
        for app in [None, -1, 123]:
            altered = json.loads(json.dumps(payload))
            altered['required_status_checks']['checks'][0]['app_id'] = app
            self.assertNotEqual(self.signature('branch_protection_signature', altered), desired)
        rules = json.loads(self.invoke('--print-policy', '--repo', 'xynes-gateway', '--branch', 'release/*').stdout)
        expected = self.signature('ruleset_signature', rules)
        rule = next(r for r in rules['rules'] if r['type'] == 'required_status_checks')
        rule['parameters']['required_status_checks'][0]['integration_id'] = None
        self.assertNotEqual(self.signature('ruleset_signature', rules), expected)


if __name__ == '__main__':
    unittest.main()
