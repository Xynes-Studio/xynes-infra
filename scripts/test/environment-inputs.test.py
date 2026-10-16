#!/usr/bin/env python3
"""Exercise stage isolation and real HMAC JWTs using only private temp fixtures."""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import provider_inputs


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/environment_inputs.py'
        self.assertTrue(path.exists(), 'stage environment preparation is missing')
        spec = importlib.util.spec_from_file_location('environment_inputs', path)
        self.module = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name);self.directory.chmod(0o700)
        path = self.directory / 'providers.env'
        self.providers = {key: 'inert-' + key.lower() for key in provider_inputs.ALLOWED_KEYS}
        for stage in ('QA', 'STAGING', 'PROD'):
            self.providers[f'POSTHOG_{stage}_HOST'] = 'https://eu.i.posthog.com'
            self.providers[f'RESEND_{stage}_FROM_ADDRESS'] = f'sender-{stage.lower()}@mail.example.invalid'
        path.write_text(''.join(k + '=' + v + '\n' for k,v in self.providers.items()));path.chmod(0o600)
        self.inputs = provider_inputs.load(path)

    def refused(self, callback):
        with self.assertRaises(self.module.EnvironmentInputError) as caught:callback()
        self.assertEqual(str(caught.exception), 'Environment inputs refused')
        self.assertNotIn(str(self.directory), repr(caught.exception))
        for value in self.providers.values():self.assertNotIn(value, str(caught.exception))

    def test_three_stage_urls_provider_aliases_and_independent_generated_secrets(self):
        results = [self.module.prepare(stage, self.inputs, now=1791810000) for stage in ('qa','staging','prod')]
        for stage, result in zip(('qa','staging','prod'), results):
            namespace = 'erp.xynes.com' if stage == 'prod' else stage + '.xynes.com'
            self.assertEqual(result.get('XYNES_ENV'), stage)
            self.assertEqual(result.get('NEXT_PUBLIC_API_URL'), 'https://api.' + namespace)
            self.assertEqual(result.get('NEXT_PUBLIC_AUTH_APP_URL'), 'https://auth.' + namespace)
            self.assertEqual(result.get('NEXT_PUBLIC_CMS_APP_URL'), 'https://cms.' + namespace)
            self.assertEqual(result.get('NEXT_PUBLIC_SUPABASE_URL'), 'https://supabase.' + namespace)
            self.assertEqual(result.get('JWT_ISSUER'), 'https://supabase.' + namespace + '/auth/v1')
            self.assertEqual(result.get('AUTH_COOKIE_DOMAIN'), '.' + namespace)
            self.assertEqual(result.get('GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI'), 'https://supabase.' + namespace + '/auth/v1/callback')
            # Pinned Auth routes: signup/server callback, OAuth client callback,
            # and ForgotPasswordForm's reset-password target.
            self.assertEqual(result.get('ADDITIONAL_REDIRECT_URLS').split(','),
                             ['https://auth.' + namespace + path for path in ('/callback','/callback/client','/reset-password')])
            self.assertEqual(result.get('MAIL_API_KEY'), self.providers[f'RESEND_{stage.upper()}_API_KEY'])
            self.assertEqual(result.get('SMTP_PASS'), result.get('MAIL_API_KEY'))
            self.assertEqual(result.get('STORAGE_ACCESS_KEY_ID'), self.providers[f'R2_{stage.upper()}_ACCESS_KEY_ID'])
            self.assertEqual(result.get('NEXT_PUBLIC_SUPABASE_ANON_KEY'), result.get('SUPABASE_ANON_KEY'))
            self.assertEqual(result.get('POSTHOG_HOST'), 'https://eu.i.posthog.com')
            self.assertEqual(result.get('FEATURE_FLAGS_DEBUG'), 'false')
            self.assertEqual(result.get('XYNES_INTERNAL_IDENTITIES_DIR'), f'/srv/xynes/{stage}/identities')
            for forbidden in (
                'GHCR_READ_TOKEN',
                'RELEASE_POLICY_READ_TOKEN',
                'CLOUDFLARE_DNS_API_TOKEN',
                'B2_APPLICATION_KEY',
                'NEXT_PUBLIC_POSTHOG_API_KEY',
            ):
                self.refused(lambda: result.get(forbidden))
            for value in self.providers.values():self.assertNotIn(value, repr(result))
        for key in ('POSTGRES_PASSWORD','JWT_SECRET','TELEMETRY_IP_HASH_SALT','SUPABASE_SERVICE_ROLE_KEY'):
            self.assertEqual(len({result.get(key) for result in results}), 3)

    def test_hosted_templates_match_the_deployment_dns_and_cookie_boundaries(self):
        for stage in ('qa','staging','prod'):
            with self.subTest(stage=stage):
                namespace='erp.xynes.com' if stage=='prod' else stage+'.xynes.com'
                path=ROOT/'infra/release/templates'/('.env.'+stage+'.template')
                values=dict(line.split('=',1) for line in path.read_text().splitlines()
                            if line and not line.startswith('#') and '=' in line)
                for app,key in (('api','NEXT_PUBLIC_GATEWAY_URL'),('auth','NEXT_PUBLIC_AUTH_APP_URL'),
                                ('cms','NEXT_PUBLIC_CMS_APP_URL'),('auth','INVITE_BASE_URL')):
                    self.assertEqual(values[key],'https://'+app+'.'+namespace)
                self.assertEqual(values['AUTH_COOKIE_DOMAIN'],'.'+namespace)
                self.assertEqual(values['GATEWAY_CORS_ALLOWED_ORIGINS'],
                                 'https://auth.'+namespace+',https://cms.'+namespace)
                self.assertEqual(values['GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI'],
                                 'https://supabase.'+namespace+'/auth/v1/callback')

    def test_supabase_jwt_signature_roles_and_claims_are_real_and_matching(self):
        result = self.module.prepare('qa', self.inputs, now=1791810000)
        decode = lambda text: base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
        for key, role in [('SUPABASE_ANON_KEY','anon'),('SUPABASE_SERVICE_ROLE_KEY','service_role')]:
            header, claims, signature = result.get(key).split('.')
            self.assertEqual(json.loads(decode(header)), {'alg':'HS256','typ':'JWT'})
            payload = json.loads(decode(claims));self.assertEqual(payload['role'],role)
            self.assertEqual(payload['iss'],'supabase');self.assertEqual(payload['iat'],1791810000)
            self.assertGreater(payload['exp'],payload['iat'])
            self.assertEqual(decode(signature), hmac.new(result.get('JWT_SECRET').encode(), (header+'.'+claims).encode(), hashlib.sha256).digest())
        self.assertIn(result.get('POSTGRES_PASSWORD'), result.get('DATABASE_URL'))
        self.assertEqual(result.get('DATABASE_URL'), result.get('ACCOUNTS_DATABASE_URL'))

    def test_private_write_is_exclusive_and_never_overwrites_existing_settings(self):
        result = self.module.prepare('qa', self.inputs, now=1791810000)
        path = self.directory / '.env.qa'
        result.write_new(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        values = dict(line.split('=',1) for line in path.read_text().splitlines())
        self.assertEqual(values['JWT_SECRET'], result.get('JWT_SECRET'))
        self.assertEqual(values['SMTP_PASS'], result.get('MAIL_API_KEY'))
        before = path.read_bytes();self.refused(lambda: result.write_new(path));self.assertEqual(path.read_bytes(),before)
        link = self.directory / 'link';link.symlink_to(path)
        self.refused(lambda: result.write_new(link));self.assertEqual(path.read_bytes(),before)
        self.directory.chmod(0o755)
        self.refused(lambda: result.write_new(self.directory / 'unsafe.env'))
        self.assertFalse((self.directory / 'unsafe.env').exists())

    def test_invalid_stage_partial_provider_input_or_unsafe_values_refuse_redacted(self):
        for stage in ('dev','QA','',None):self.refused(lambda: self.module.prepare(stage,self.inputs,now=1791810000))
        for now in (-1,True,'1791810000'):self.refused(lambda: self.module.prepare('qa',self.inputs,now=now))
        partial = provider_inputs.ProviderInputs({})
        self.refused(lambda: self.module.prepare('qa',partial,now=1791810000))
        for key,value in [('POSTHOG_QA_HOST','http://insecure.invalid'),('RESEND_QA_FROM_ADDRESS','Display Name <sender@example.invalid>')]:
            data = {**self.providers,key:value}
            self.refused(lambda: self.module.prepare('qa',provider_inputs.ProviderInputs(data),now=1791810000))

    def test_write_failure_removes_only_its_partial_file_and_redacts_close_errors(self):
        result = self.module.prepare('qa',self.inputs,now=1791810000)
        path = self.directory / '.env.qa'
        with patch.object(self.module.os,'write',side_effect=OSError(self.providers['GHCR_READ_TOKEN'])):
            self.refused(lambda: result.write_new(path))
        self.assertFalse(path.exists())
        real_close = self.module.os.close
        closed = []
        def closing(fd):
            real_close(fd);closed.append(fd)
            if len(closed) == 1:raise OSError(self.providers['GHCR_READ_TOKEN'])
        with patch.object(self.module.os,'close',side_effect=closing):
            self.refused(lambda: result.write_new(path))
        self.assertEqual(len(closed),2)

    def test_replaced_output_path_is_not_accepted_or_removed(self):
        result = self.module.prepare('qa',self.inputs,now=1791810000)
        path = self.directory / '.env.qa'
        real_fsync = self.module.os.fsync
        replaced = False
        def sync(fd):
            nonlocal replaced
            real_fsync(fd)
            if not replaced:
                replaced = True
                replacement = self.directory / 'replacement'
                replacement.write_bytes(b'owned-other-fixture');replacement.chmod(0o600);replacement.replace(path)
        with patch.object(self.module.os,'fsync',side_effect=sync):
            self.refused(lambda: result.write_new(path))
        self.assertEqual(path.read_bytes(),b'owned-other-fixture')

    def test_same_inode_same_size_output_mutation_is_refused(self):
        result = self.module.prepare('qa',self.inputs,now=1791810000)
        path = self.directory / '.env.qa'
        real_fsync = self.module.os.fsync;mutated = False
        def sync(fd):
            nonlocal mutated
            real_fsync(fd)
            if not mutated:
                mutated = True
                data = path.read_bytes()
                self.assertIn(b'SMTP_SENDER_NAME=Xynes',data)
                path.write_bytes(data.replace(b'SMTP_SENDER_NAME=Xynes',b'SMTP_SENDER_NAME=Other'))
        with patch.object(self.module.os,'fsync',side_effect=sync):
            self.refused(lambda: result.write_new(path))
        self.assertFalse(path.exists())


    def saved(self, *, legacy=False):
        result = self.module.prepare('qa', self.inputs, now=1791810000)
        path = self.directory / '.env.qa';result.write_new(path)
        if legacy:
            data = '\n'.join(line for line in path.read_text().splitlines()
                             if not line.startswith(('AUTH_DATABASE_PASSWORD=', 'GOTRUE_DATABASE_URL='))) + '\n'
            path.write_text(data)
        return path, dict(line.split('=',1) for line in path.read_text().splitlines())

    def test_auth_database_credentials_are_separate_and_match_auth_role_url(self):
        result = self.module.prepare('qa', self.inputs, now=1791810000)
        password = result.get('AUTH_DATABASE_PASSWORD')
        self.assertRegex(password, r'^[a-f0-9]{64}$')
        self.assertNotEqual(password, result.get('POSTGRES_PASSWORD'))
        self.assertEqual(result.get('GOTRUE_DATABASE_URL'),
                         'postgres://supabase_auth_admin:' + password + '@supabase-db:5432/postgres')

    def test_refresh_changes_only_provider_fields_without_rotating_or_changing_urls(self):
        self.assertTrue(callable(getattr(self.module, 'refresh', None)), 'safe prepared refresh is missing')
        path,before = self.saved()
        # Preserve an operator-selected public URL rather than rebuilding defaults.
        path.write_text(path.read_text().replace(before['NEXT_PUBLIC_CMS_APP_URL'],'https://cms.custom.example.invalid'))
        before = dict(line.split('=',1) for line in path.read_text().splitlines())
        changed = dict(self.providers)
        for key in ('R2_QA_ACCESS_KEY_ID','R2_QA_SECRET_ACCESS_KEY','RESEND_QA_API_KEY',
                    'GOOGLE_QA_CLIENT_ID','GOOGLE_QA_CLIENT_SECRET','POSTHOG_QA_API_KEY'):
            changed[key] = 'updated-' + key.lower()
        with patch.object(self.module.secrets,'token_hex',side_effect=AssertionError('must retain generated keys')):
            result = self.module.refresh('qa',provider_inputs.ProviderInputs(changed),path)
        after = dict(line.split('=',1) for line in path.read_text().splitlines())
        permitted = {'STORAGE_ACCESS_KEY_ID','STORAGE_SECRET_ACCESS_KEY','MAIL_API_KEY','SMTP_PASS',
                     'GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID','GOTRUE_EXTERNAL_GOOGLE_SECRET','POSTHOG_API_KEY'}
        self.assertEqual({key for key in before if before[key] != after[key]},permitted)
        self.assertEqual(set(before),set(after))
        self.assertEqual(result.get('JWT_SECRET'),before['JWT_SECRET'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'providers.env','.env.qa'})

    def test_refresh_legacy_file_adds_auth_password_once_and_retains_all_original_settings(self):
        self.assertTrue(callable(getattr(self.module, 'refresh', None)), 'safe prepared refresh is missing')
        path,before = self.saved(legacy=True)
        result = self.module.refresh('qa',self.inputs,path)
        after = dict(line.split('=',1) for line in path.read_text().splitlines())
        self.assertEqual({key:after[key] for key in before},before)
        self.assertEqual(set(after)-set(before),{'AUTH_DATABASE_PASSWORD','GOTRUE_DATABASE_URL'})
        with patch.object(self.module.secrets,'token_hex',side_effect=AssertionError('must retain auth key')):
            again = self.module.refresh('qa',self.inputs,path)
        self.assertEqual(again.get('AUTH_DATABASE_PASSWORD'),result.get('AUTH_DATABASE_PASSWORD'))

    def test_refresh_refuses_unsafe_files_or_invalid_keys_without_overwriting(self):
        self.assertTrue(callable(getattr(self.module, 'refresh', None)), 'safe prepared refresh is missing')
        path,before = self.saved();data = path.read_bytes()
        path.chmod(0o640);self.refused(lambda:self.module.refresh('qa',self.inputs,path));path.chmod(0o600)
        hard = self.directory/'hard';os.link(path,hard)
        self.refused(lambda:self.module.refresh('qa',self.inputs,path));hard.unlink()
        path.rename(hard);path.symlink_to(hard)
        self.refused(lambda:self.module.refresh('qa',self.inputs,path));path.unlink();hard.rename(path)
        self.directory.chmod(0o750);self.refused(lambda:self.module.refresh('qa',self.inputs,path));self.directory.chmod(0o700)
        for suffix in (b'UNKNOWN_SETTING=secret\n',b'JWT_SECRET=duplicate\n',b'BAD-NAME=secret\n',
                       b'SMTP_PASS=$REPLACEMENT\n',b'SMTP_PASS=bad\x00value\n'):
            path.write_bytes(data+suffix)
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
            self.assertEqual(path.read_bytes(),data+suffix)
        path.write_bytes(data)
        self.refused(lambda:self.module.refresh('prod',self.inputs,path))
        path.write_bytes(data.replace(b'AUTH_DATABASE_PASSWORD=',b'UNKNOWN_AUTH_PASSWORD='))
        self.refused(lambda:self.module.refresh('qa',self.inputs,path))

    def test_refresh_refuses_changed_input_during_atomic_write_and_leaves_original(self):
        self.assertTrue(callable(getattr(self.module, 'refresh', None)), 'safe prepared refresh is missing')
        path,before = self.saved();data = path.read_bytes()
        real_sync = self.module.os.fsync;mutated=False
        def sync(fd):
            nonlocal mutated
            real_sync(fd)
            if not mutated:
                mutated=True
                path.write_bytes(data.replace(b'SMTP_SENDER_NAME=Xynes',b'SMTP_SENDER_NAME=Other'))
        with patch.object(self.module.os,'fsync',side_effect=sync):
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
        self.assertIn(b'SMTP_SENDER_NAME=Other',path.read_bytes())
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'providers.env','.env.qa'})

    def test_refresh_invalid_provider_input_and_write_failure_preserve_original(self):
        self.assertTrue(callable(getattr(self.module, 'refresh', None)), 'safe prepared refresh is missing')
        path,before = self.saved();data=path.read_bytes()
        self.refused(lambda:self.module.refresh('qa',provider_inputs.ProviderInputs({}),path))
        self.assertEqual(path.read_bytes(),data)
        with patch.object(self.module.os,'write',side_effect=OSError('private-fixture-value')):
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'providers.env','.env.qa'})


    def test_refresh_refuses_oversized_partial_or_mismatched_auth_settings(self):
        path,before = self.saved();data=path.read_bytes()
        variants = [b'X'*65537,
                    b'\n'.join(line for line in data.split(b'\n') if not line.startswith(b'GOTRUE_DATABASE_URL=')),
                    data.replace(b'postgres://supabase_auth_admin:',b'postgres://postgres:'),
                    data.replace(b'XYNES_ENV=qa',b'XYNES_ENV=prod'),
                    data.replace(b'AUTH_DATABASE_PASSWORD=',b'AUTH_DATABASE_PASSWORD=not-hex-')]
        for value in variants:
            with self.subTest(size=len(value)):
                path.write_bytes(value)
                self.refused(lambda:self.module.refresh('qa',self.inputs,path))
                self.assertEqual(path.read_bytes(),value)

    def test_refresh_changed_file_during_initial_read_refuses_without_removing_replacement(self):
        path,before = self.saved();data = path.read_bytes()
        real_read = self.module.os.read;changed=False
        def reading(fd,size):
            nonlocal changed
            result=real_read(fd,size)
            if not changed:
                changed=True
                replacement=self.directory/'replacement'
                replacement.write_bytes(data);replacement.chmod(0o600);replacement.replace(path)
            return result
        with patch.object(self.module.os,'read',side_effect=reading):
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'providers.env','.env.qa'})

    def test_refresh_replaced_output_is_detected_and_preserved(self):
        path,before = self.saved();real_replace=self.module.os.replace
        def replacing(*args,**kwargs):
            real_replace(*args,**kwargs)
            replacement=self.directory/'replacement'
            replacement.write_bytes(b'other-operator-file');replacement.chmod(0o600)
            real_replace(replacement,path)
        with patch.object(self.module.os,'replace',side_effect=replacing):
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
        self.assertEqual(path.read_bytes(),b'other-operator-file')
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'providers.env','.env.qa'})


    def test_refresh_revalidates_output_after_directory_sync(self):
        path,before = self.saved();real_sync=self.module.os.fsync
        def sync(fd):
            real_sync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                path.write_bytes(b'other-operator-file')
        with patch.object(self.module.os,'fsync',side_effect=sync):
            self.refused(lambda:self.module.refresh('qa',self.inputs,path))
        self.assertEqual(path.read_bytes(),b'other-operator-file')


if __name__ == '__main__':unittest.main()
