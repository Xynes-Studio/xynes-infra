#!/usr/bin/env python3
"""Temporary, inert files exercise provider handoff parsing and descriptor checks."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
GLOBAL_KEYS = ('CLOUDFLARE_DNS_API_TOKEN', 'CLOUDFLARE_ZONE_NAME', 'R2_ACCOUNT_ID',
               'B2_BACKUP_BUCKET', 'B2_KEY_ID', 'B2_APPLICATION_KEY', 'GHCR_READ_TOKEN',
               'RELEASE_POLICY_READ_TOKEN')
STAGE_FIELDS = ('R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'GOOGLE_CLIENT_ID',
                'GOOGLE_CLIENT_SECRET', 'POSTHOG_API_KEY', 'POSTHOG_HOST',
                'RESEND_API_KEY', 'RESEND_FROM_ADDRESS')


def stage_key(stage, field):
    provider, suffix = field.split('_', 1)
    return f'{provider}_{stage}_{suffix}'


def fixture():
    data = {key: 'inert-' + key.lower() for key in GLOBAL_KEYS}
    for stage in ('QA', 'STAGING', 'PROD'):
        data.update({stage_key(stage, field): 'inert-' + stage.lower() + '-' + field.lower()
                     for field in STAGE_FIELDS})
    return data


def render(data):
    return ''.join(key + '=' + value + '\n' for key, value in data.items())


class Tests(unittest.TestCase):
    def setUp(self):
        source = ROOT / 'scripts/release/provider_inputs.py'
        self.assertTrue(source.exists(), 'secure provider input reader is missing')
        spec = importlib.util.spec_from_file_location('provider_inputs', source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private-inputs.env'
        self.values = fixture()
        self.write(render(self.values))

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')
        self.path.chmod(0o600)

    def refused(self, callback):
        with self.assertRaises(self.module.ProviderInputError) as caught:
            callback()
        self.assertEqual(str(caught.exception), 'Provider inputs refused')
        self.assertNotIn(str(self.path), repr(caught.exception))
        for value in self.values.values():
            self.assertNotIn(value, str(caught.exception))

    def test_complete_file_explicit_access_and_redacted_wrapper(self):
        inputs = self.module.load(self.path)
        self.assertIsNone(inputs.require_complete())
        self.assertEqual(len(self.values), 35)
        for key, value in self.values.items():
            self.assertEqual(inputs.get(key), value)
            self.assertNotIn(value, repr(inputs))
        self.refused(lambda: inputs.get('not-a-declared-name'))
        self.refused(lambda: inputs.get([]))

    def test_comments_quotes_and_empty_values_do_not_execute_or_expand(self):
        self.write('# inert fixture\n\nCLOUDFLARE_ZONE_NAME="xynes.invalid" # zone\n'
                   "RESEND_QA_FROM_ADDRESS='QA Sender <sender@qa.invalid>'\n"
                   'POSTHOG_QA_HOST=https://posthog.invalid # host\nR2_ACCOUNT_ID=\n')
        inputs = self.module.load(self.path)
        self.assertEqual(inputs.get('CLOUDFLARE_ZONE_NAME'), 'xynes.invalid')
        self.assertEqual(inputs.get('RESEND_QA_FROM_ADDRESS'), 'QA Sender <sender@qa.invalid>')
        self.assertEqual(inputs.get('POSTHOG_QA_HOST'), 'https://posthog.invalid')
        self.assertEqual(inputs.get('R2_ACCOUNT_ID'), '')
        self.refused(inputs.require_complete)
        self.refused(lambda: inputs.get('GHCR_READ_TOKEN'))

    def test_each_stage_maps_only_its_runtime_names(self):
        inputs = self.module.load(self.path)
        for stage in ('QA', 'STAGING', 'PROD'):
            view = inputs.stage(stage.lower())
            expected = {
                'R2_ACCOUNT_ID': self.values['R2_ACCOUNT_ID'],
                'R2_BUCKET': self.values[f'R2_{stage}_BUCKET'],
                f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{stage}_ACCESS_KEY_ID': self.values[f'R2_{stage}_ACCESS_KEY_ID'],
                f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{stage}_SECRET_ACCESS_KEY': self.values[f'R2_{stage}_SECRET_ACCESS_KEY'],
                'GOOGLE_CLIENT_ID': self.values[f'GOOGLE_{stage}_CLIENT_ID'],
                'GOOGLE_CLIENT_SECRET': self.values[f'GOOGLE_{stage}_CLIENT_SECRET'],
                'POSTHOG_API_KEY': self.values[f'POSTHOG_{stage}_API_KEY'],
                'POSTHOG_HOST': self.values[f'POSTHOG_{stage}_HOST'],
                'MAIL_RESEND_SECRET_REF': 'secret://xynes/mail/resend-' + stage.lower(),
                f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{stage}_API_KEY': self.values[f'RESEND_{stage}_API_KEY'],
                f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{stage}_FROM_ADDRESS': self.values[f'RESEND_{stage}_FROM_ADDRESS'],
                'SMTP_HOST': 'smtp.resend.com', 'SMTP_PORT': '465', 'SMTP_USER': 'resend',
                'SMTP_PASS': self.values[f'RESEND_{stage}_API_KEY'],
                'SMTP_ADMIN_EMAIL': self.values[f'RESEND_{stage}_FROM_ADDRESS'],
            }
            for key, value in expected.items():
                self.assertEqual(view.get(key), value)
            for key in ('CLOUDFLARE_DNS_API_TOKEN', 'GHCR_READ_TOKEN', 'B2_APPLICATION_KEY',
                        'RELEASE_POLICY_READ_TOKEN', 'R2_QA_ACCESS_KEY_ID',
                        f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{"PROD" if stage != "PROD" else "QA"}_API_KEY'):
                self.refused(lambda: view.get(key))
            for value in self.values.values():
                self.assertNotIn(value, repr(view))
        for bad in ('dev', 'QA', '', None, [], 'prod\n'):
            self.refused(lambda: inputs.stage(bad))

    def test_stage_can_be_ready_while_other_stages_and_global_access_are_not(self):
        data = {key: 'replace-with-credential' for key in self.values}
        data['R2_ACCOUNT_ID'] = self.values['R2_ACCOUNT_ID']
        for field in STAGE_FIELDS:
            key = stage_key('QA', field)
            data[key] = self.values[key]
        self.write(render(data))
        inputs = self.module.load(self.path)
        self.assertEqual(inputs.stage('qa').get('SMTP_PASS'), self.values['RESEND_QA_API_KEY'])
        self.refused(inputs.require_complete)
        self.refused(lambda: inputs.stage('staging'))
        self.refused(lambda: inputs.stage('prod'))
        del data['POSTHOG_QA_HOST']
        self.write(render(data))
        self.refused(lambda: self.module.load(self.path).stage('qa'))

    def test_missing_empty_and_placeholder_values_are_not_complete(self):
        for bad in ('', "''", 'replace-with-key', 'REPLACE-WITH-KEY', '" replace-with-key "'):
            data = dict(self.values); data['GHCR_READ_TOKEN'] = bad
            self.write(render(data))
            self.refused(self.module.load(self.path).require_complete)
        data = dict(self.values); del data['R2_ACCOUNT_ID']
        self.write(render(data))
        self.refused(self.module.load(self.path).require_complete)
        self.refused(lambda: self.module.load(self.path).stage('qa'))

    def test_provider_values_have_no_local_token_format_or_length_requirement(self):
        self.write(render({key: 'a' for key in self.values}))
        inputs = self.module.load(self.path)
        self.assertIsNone(inputs.require_complete())
        self.assertEqual(inputs.stage('prod').get('SMTP_PASS'), 'a')

    def test_legacy_cloudflare_dns_fields_do_not_gate_runtime_providers(self):
        data = fixture()
        for key in ('CLOUDFLARE_DNS_API_TOKEN','CLOUDFLARE_ZONE_NAME'):data[key]='replace-with-unused-dns'
        self.write(render(data))
        inputs = self.module.load(self.path)
        self.assertIsNone(inputs.require_complete())
        self.assertEqual(inputs.stage('qa').get('R2_ACCOUNT_ID'),data['R2_ACCOUNT_ID'])
        for key in ('CLOUDFLARE_DNS_API_TOKEN','CLOUDFLARE_ZONE_NAME'):del data[key]
        self.write(render(data))
        self.assertIsNone(self.module.load(self.path).require_complete())

    def test_alternate_dns_handoff_fields_are_inert_for_runtime_credentials(self):
        data = fixture()
        del data['CLOUDFLARE_DNS_API_TOKEN'];del data['CLOUDFLARE_ZONE_NAME']
        data['GODADDY_DNS_API_TOKEN']='inert-dns-token'
        data['DNS_PROVIDER']='godaddy'
        self.write(render(data))
        inputs = self.module.load(self.path)
        self.assertIsNone(inputs.require_complete())
        for stage in ('qa','staging','prod'):
            view=inputs.stage(stage)
            self.refused(lambda: view.get('GODADDY_DNS_API_TOKEN'))
            self.refused(lambda: view.get('DNS_PROVIDER'))
        self.assertNotIn('inert-dns-token',repr(inputs))

    def test_duplicate_unknown_interpolation_controls_and_ambiguous_syntax_refuse(self):
        invalid = ['UNKNOWN=inert', 'export R2_ACCOUNT_ID=inert',
                   'R2_ACCOUNT_ID=inert\nR2_ACCOUNT_ID=other', 'R2_ACCOUNT_ID',
                   ' R2_ACCOUNT_ID=inert', 'R2_ACCOUNT_ID =inert',
                   'R2_ACCOUNT_ID=$HOME', 'R2_ACCOUNT_ID=${HOME}',
                   'R2_ACCOUNT_ID=$(touch /tmp/inert)', 'R2_ACCOUNT_ID=`inert`',
                   'R2_ACCOUNT_ID="unterminated', "R2_ACCOUNT_ID='unterminated",
                   'R2_ACCOUNT_ID="inert"junk', 'R2_ACCOUNT_ID=inert"ambiguous',
                   'R2_ACCOUNT_ID=inert words', 'R2_ACCOUNT_ID="inert\\n"',
                   'R2_ACCOUNT_ID=inert\x00', 'R2_ACCOUNT_ID=inert\t',
                   'R2_ACCOUNT_ID=inert\r\n', 'R2_ACCOUNT_ID="inert\x7f"',
                   'R2_ACCOUNT_ID="inert\u202e"', '\ufeffR2_ACCOUNT_ID=inert',
                   '# comment $HOME', '# comment\x00']
        for text in invalid:
            with self.subTest(case=invalid.index(text)):
                self.write(text)
                self.refused(lambda: self.module.load(self.path))
        self.path.write_bytes(b'R2_ACCOUNT_ID=\xff\n')
        self.refused(lambda: self.module.load(self.path))

    def test_symlink_hardlink_fifo_directory_missing_and_wrong_uid_refuse(self):
        original = self.path.with_name('original.env'); self.path.rename(original)
        self.path.symlink_to(original)
        self.refused(lambda: self.module.load(self.path)); self.path.unlink()
        os.link(original, self.path)
        self.refused(lambda: self.module.load(self.path)); self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.refused(lambda: self.module.load(self.path)); self.path.unlink()
        self.path.mkdir()
        self.refused(lambda: self.module.load(self.path)); self.path.rmdir()
        self.refused(lambda: self.module.load(self.path))
        original.rename(self.path)
        with patch.object(self.module.os, 'getuid', return_value=os.getuid() + 1):
            self.refused(lambda: self.module.load(self.path))

    def test_exact_permissions_and_size_are_enforced(self):
        for mode in (0o400, 0o640, 0o644, 0o700, 0o1600):
            self.path.chmod(mode)
            self.refused(lambda: self.module.load(self.path))
        self.write('#' + 'a' * 65535)
        self.module.load(self.path)
        self.write('#' + 'a' * 65536)
        self.refused(lambda: self.module.load(self.path))

    def test_real_read_mutation_and_path_replacement_refuse(self):
        real_read = os.read
        for mutation in ('content', 'replacement', 'permission', 'hardlink'):
            with self.subTest(mutation=mutation):
                self.write(render(self.values)); mutated = False
                def reading(fd, size):
                    nonlocal mutated
                    data = real_read(fd, size)
                    if not mutated:
                        mutated = True
                        if mutation == 'content':
                            with self.path.open('a') as out: out.write('# appended\n')
                        elif mutation == 'replacement':
                            replacement = self.path.with_name('replacement.env')
                            replacement.write_text(render(self.values)); replacement.chmod(0o600)
                            replacement.replace(self.path)
                        elif mutation == 'permission': self.path.chmod(0o640)
                        else: os.link(self.path, self.path.with_name('extra-link'))
                    return data
                with patch.object(self.module.os, 'read', side_effect=reading):
                    self.refused(lambda: self.module.load(self.path))
                extra = self.path.with_name('extra-link')
                if extra.exists(): extra.unlink()

    def test_read_errors_are_redacted_and_open_descriptor_is_closed(self):
        real_open, real_close = os.open, os.close
        opened, closed = [], []
        def opening(*args, **kwargs):
            fd = real_open(*args, **kwargs); opened.append(fd); return fd
        def closing(fd):
            closed.append(fd); return real_close(fd)
        with patch.object(self.module.os, 'open', side_effect=opening), \
             patch.object(self.module.os, 'close', side_effect=closing), \
             patch.object(self.module.os, 'read', side_effect=OSError(self.values['GHCR_READ_TOKEN'])):
            self.refused(lambda: self.module.load(self.path))
        self.assertEqual(opened, closed)
        self.assertEqual(len(opened), 1)


if __name__ == '__main__':
    unittest.main()
