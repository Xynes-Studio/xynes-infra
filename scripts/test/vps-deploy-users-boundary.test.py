#!/usr/bin/env python3
"""Real filesystem gate tests; no accounts, permissions or host config mutate."""
from pathlib import Path
import base64
import os
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'setup/vps-deploy-users.sh'
MARKER = '/etc/xynes/deploy-boundary.json'
ERROR = 'Apex deployment boundary is installed; legacy deploy-user apply refused.'
HARNESS = r'''
script="$1"; fixture="$2"; requested_mode="$3"
set --
. "$script"
MODE="$requested_mode"; LAYOUT_ROOT="$fixture"; ONLY_ENV=dev
DEV_PUBKEY=fixture-public-key; QA_PUBKEY=fixture-public-key; PROD_PUBKEY=fixture-public-key
# Trusted inert replacements prevent any legacy host action. The original main
# and its gate, argument/state handling and filesystem tests remain executable.
require_root_for_apply() { printf 'ROOT_PREFLIGHT\n'; }
preflight_xynes_group_exists() { printf 'GROUP_PREFLIGHT\n'; }
preflight_srv_env_exists() { return 0; }
converge_env() { printf 'CONVERGE:%s\n' "$1"; }
main
'''


class BoundaryGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='apex-legacy-gate-')
        self.root = Path(self.temporary.name)
        self.marker = self.root / 'boundary.json'
        self.script = self.root / 'vps-deploy-users.sh'
        source = SOURCE.read_text()
        # Map only the fixed marker into an offline fixture. Production exposes
        # no argument/environment override for its canonical /etc marker.
        self.script.write_text(source.replace(MARKER, str(self.marker)))

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, mode='apply', *, cli=None, env=None):
        argv = ['bash', str(self.script), *cli] if cli is not None else [
            'bash', '-c', HARNESS, 'fixture', str(self.script), str(self.root), mode]
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=3,
                                  env=env)
        except subprocess.TimeoutExpired:
            self.fail('The boundary gate failed to refuse before a blocking file read')

    def refused(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(ERROR, result.stderr)
        for mutation in ('CONVERGE:', 'ROOT_PREFLIGHT', 'GROUP_PREFLIGHT'):
            self.assertNotIn(mutation, result.stdout + result.stderr)
        return result

    def test_regular_marker_refuses_before_any_host_preflight(self):
        self.marker.write_bytes(b'opaque-canary-not-json')
        result = self.refused()
        self.assertNotIn('opaque-canary', result.stdout + result.stderr)
        self.assertEqual(self.marker.read_bytes(), b'opaque-canary-not-json')

    def test_empty_marker_refuses(self):
        self.marker.touch()
        self.refused()

    def test_dangling_symlink_refuses_without_reading_target(self):
        self.marker.symlink_to(self.root / 'missing-target')
        self.refused()
        self.assertTrue(self.marker.is_symlink())

    def test_symlink_to_existing_file_refuses(self):
        target = self.root / 'private-target'
        target.write_bytes(b'private-canary')
        target.chmod(0)
        self.marker.symlink_to(target)
        result = self.refused()
        self.assertNotIn('private-canary', result.stdout + result.stderr)

    def test_fifo_and_directory_markers_refuse_without_read(self):
        os.mkfifo(self.marker)
        self.refused()
        self.marker.unlink()
        self.marker.mkdir()
        self.refused()

    def test_actual_cli_refuses_before_reading_public_key_file(self):
        self.marker.touch()
        public_key = self.root / 'blocking-key-file'
        os.mkfifo(public_key)
        result = self.invoke(cli=['--only', 'dev', '--dev-pubkey-file', str(public_key)])
        self.assertEqual(result.returncode, 1)
        self.assertIn(ERROR, result.stderr)

    def test_preview_and_help_remain_available_with_boundary(self):
        self.marker.symlink_to(self.root / 'missing')
        preview = self.invoke('dry-run')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn('CONVERGE:dev', preview.stdout)
        self.assertNotIn(ERROR, preview.stderr)
        help_result = self.invoke(cli=['--help'])
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('USAGE:', help_result.stdout)

    def test_actual_cli_gate_covers_every_stage_and_srv_root_override(self):
        self.marker.touch()
        for stage in ('dev', 'qa', 'prod'):
            result = self.invoke(cli=['--only', stage, '--srv-root', str(self.root)])
            self.assertEqual(result.returncode, 1)
            self.assertIn(ERROR, result.stderr)

    def test_actual_dry_run_previews_without_changing_fixture(self):
        self.marker.write_bytes(b'opaque-canary')
        raw = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + bytes(range(32))
        key = 'ssh-ed25519 ' + base64.b64encode(raw).decode('ascii')
        result = self.invoke(cli=['--dry-run', '--only', 'dev', '--srv-root',
                                  str(self.root), '--dev-pubkey', key])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('WOULD:', result.stdout)
        self.assertNotIn(ERROR, result.stderr)
        self.assertEqual(self.marker.read_bytes(), b'opaque-canary')
        self.assertEqual(set(path.name for path in self.root.iterdir()),
                         {'boundary.json', 'vps-deploy-users.sh'})

    def test_absent_marker_preserves_legacy_flow_and_env_cannot_redirect_gate(self):
        absent = self.invoke()
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn('CONVERGE:dev', absent.stdout)
        self.marker.touch()
        env = dict(os.environ, APEX_DEPLOY_BOUNDARY='/does/not/exist',
                   XYNES_DEPLOY_BOUNDARY='/does/not/exist', MODE='dry-run')
        result = self.invoke(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(ERROR, result.stderr)


if __name__ == '__main__':
    unittest.main()
