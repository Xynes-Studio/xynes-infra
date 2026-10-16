#!/usr/bin/env python3
"""Static deployment boundary and executable bootstrap admission checks.

The parent release fixture must additionally run the pinned images and migrations.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2] / 'infra/release/auth'

class AuthRuntimeTest(unittest.TestCase):
    def compose(self):
        path = ROOT / 'compose.fragment.yml'
        self.assertTrue(path.is_file(), 'minimal hosted Auth fragment must exist')
        return json.loads(path.read_text())

    def test_only_db_and_auth_with_no_public_container_ports(self):
        document = self.compose()
        self.assertEqual(set(document['services']), {'supabase-db', 'supabase-auth'})
        for service in document['services'].values():
            self.assertNotIn('ports', service)
            self.assertNotIn('network_mode', service)
            self.assertEqual(service['platform'], 'linux/amd64')
            self.assertIn('@sha256:', service['image'])
            self.assertEqual(service['restart'], 'unless-stopped')
            self.assertEqual(service['labels']['xynes.idle.eligible'], 'false')
            self.assertIn('healthcheck', service)
            self.assertEqual(set(service['networks']), {'environment'})
            self.assertEqual(service['labels']['com.xynes.managed'], 'true')
            self.assertEqual(service['labels']['com.xynes.idle'], 'never')
            self.assertEqual(service['labels']['com.xynes.environment'], '${XYNES_ENV:?required}')

    def test_auth_uses_separate_role_and_consistent_user_token_contract(self):
        document = self.compose()
        auth = document['services']['supabase-auth']
        env = auth['environment']
        self.assertEqual(env['GOTRUE_DB_DATABASE_URL'], '${GOTRUE_DATABASE_URL:?required}')
        self.assertNotIn('POSTGRES_PASSWORD', env)
        self.assertEqual(env['GOTRUE_JWT_ISSUER'], '${JWT_ISSUER:?required}')
        self.assertEqual(env['API_EXTERNAL_URL'], '${API_EXTERNAL_URL:?required}')
        self.assertEqual(env['GOTRUE_JWT_AUD'], 'authenticated')
        self.assertEqual(env.get('GOTRUE_JWT_DEFAULT_GROUP_NAME'), 'authenticated')
        self.assertEqual(env['GOTRUE_JWT_EXP'], '3600')
        self.assertNotIn('GOTRUE_JWT_KEYS', env)  # legacy HS256 mode, same secret as gateway
        self.assertEqual(env['GOTRUE_MAILER_AUTOCONFIRM'], 'false')
        self.assertEqual(env['GOTRUE_SMTP_PORT'], '465')
        self.assertEqual(env['GOTRUE_SMTP_HOST'], 'smtp.resend.com')
        self.assertEqual(auth['depends_on']['supabase-db']['condition'], 'service_healthy')
        self.assertTrue(auth['read_only'])
        self.assertEqual(auth['cap_drop'], ['ALL'])
        self.assertEqual(auth['security_opt'], ['no-new-privileges:true'])

    def test_bootstrap_creates_no_auth_tables_or_superuser_auth_role(self):
        path = ROOT / 'bootstrap.sql'
        self.assertTrue(path.is_file(), 'reviewed bootstrap must exist')
        sql = path.read_text()
        self.assertNotRegex(sql.lower(), r'create\s+table|drop\s+|truncate\s+')
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', sql)
        self.assertIn('CREATE SCHEMA auth AUTHORIZATION supabase_auth_admin', sql)
        self.assertIn('CREATE EXTENSION pgcrypto', sql)
        self.assertIn('REVOKE CREATE ON SCHEMA public FROM PUBLIC', sql)
        self.assertIn('already has Auth state', sql)
        self.assertIn('log_min_error_statement = panic', sql)
        self.assertIn('\\getenv auth_password AUTH_DATABASE_PASSWORD', sql)
        self.assertNotIn('CREATE DATABASE', sql)
        self.assertNotIn('CREATE FUNCTION', sql)
        self.assertNotIn('CREATE ROLE anon', sql)
        self.assertNotIn('CREATE ROLE authenticated', sql)
        self.assertNotIn('CREATE ROLE service_role', sql)

    def test_bootstrap_refuses_invalid_or_shared_password_before_psql(self):
        script = ROOT / 'bootstrap.sh'
        self.assertTrue(script.is_file(), 'bootstrap admission script must exist')
        for password, database_password in [('', 'b' * 64), ('x' * 64, 'b' * 64), ('a' * 64, 'a' * 64)]:
            env = {'PATH': '/usr/bin:/bin', 'AUTH_DATABASE_PASSWORD': password,
                   'POSTGRES_PASSWORD': database_password, 'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'postgres'}
            result = subprocess.run(['/bin/sh', str(script)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, 'Auth bootstrap inputs refused\n')
            self.assertNotIn(password if password else '<no password>', result.stdout + result.stderr)

    def test_mail_templates_support_otp_and_recovery_without_external_assets(self):
        env = self.compose()['services']['supabase-auth']['environment']
        for kind in ('confirmation', 'magic_link', 'recovery', 'invite', 'email_change', 'reauthentication'):
            path = ROOT / 'templates' / (kind + '.html')
            self.assertTrue(path.is_file(), 'reviewed mail template must exist: ' + kind)
            html = path.read_text()
            self.assertIn('{{ .Token }}', html)
            self.assertNotRegex(html.lower(), r'<script|<img|<iframe|https?://')
            self.assertNotIn('.TokenHash', html)
            key = 'GOTRUE_MAILER_TEMPLATES_' + kind.upper()
            self.assertEqual(env[key], 'http://caddy:8090/' + kind + '.html')
            if kind in ('recovery', 'invite', 'email_change'):
                self.assertIn('{{ .ConfirmationURL }}', html)
        suggestion = (ROOT / 'templates.Caddyfile').read_text()
        self.assertIn('http://:8090', suggestion)
        self.assertNotIn('auto_https off', suggestion)
        self.assertNotIn('log {', suggestion)
        self.assertIn('file_server', suggestion)
        self.assertIn('Cache-Control "no-store"', suggestion)

if __name__ == '__main__':
    unittest.main()
