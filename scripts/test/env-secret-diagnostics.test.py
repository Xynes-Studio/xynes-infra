#!/usr/bin/env python3
"""Actual shell diagnostics plus extracted helper races on real temporary files."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().with_name('env-templates-no-secrets.test.sh')


class Tests(unittest.TestCase):
    def fixture(self, base):
        root = base / 'xynes' / 'xynes-infra'
        (root / 'infra/release/templates').mkdir(parents=True)
        for name in ['.env.example', 'infra/release/templates/.env.dev.template',
                     'infra/release/templates/.env.qa.template', 'infra/release/templates/.env.staging.template', 'infra/release/templates/.env.prod.template']:
            (root / name).write_text('# placeholder only\n')
        return root

    def run_gate(self, value, optional=None, target='.env.example'):
        with tempfile.TemporaryDirectory(prefix='apex-template-safe-') as tmp:
            base = Path(tmp).resolve();root = self.fixture(base)
            (root / target).write_text(value)
            if optional:
                path = base / 'xynes/xynes-authz-service/.env.example'
                path.parent.mkdir()
                optional(path)
            return subprocess.run(['bash', str(TOOL)], env={**os.environ, 'INFRA_DIR': str(root)},
                                  text=True, capture_output=True, timeout=20)

    def test_staging_template_is_required_and_scanned_without_raw_diagnostics(self):
        marker = 'ghp_' + 'C' * 36
        p = self.run_gate('TOKEN=' + marker + '\n', target='infra/release/templates/.env.staging.template')
        self.assertNotEqual(p.returncode, 0, 'staging template was omitted from the secret gate')
        self.assertNotIn(marker, p.stdout + p.stderr)

    def test_candidate_never_appears_in_diagnostics(self):
        marker = 'ghp_' + 'A' * 36
        p = self.run_gate('PROVIDER_TOKEN=' + marker + '\n')
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(marker not in p.stdout + p.stderr, 'secret candidate appeared in diagnostic output')

    def test_postgres_password_pattern_actually_rejects_real_shaped_credentials(self):
        marker = 'fixture-only-unsafe-password'
        p = self.run_gate('DATABASE_URL=postgresql://postgres:' + marker + '@db.internal/postgres\n')
        self.assertNotEqual(p.returncode, 0, 'Postgres pattern accepted unsafe credentials')
        self.assertTrue(marker not in p.stdout + p.stderr, 'password candidate appeared in diagnostic output')

    def test_documented_placeholder_and_absent_optional_templates_pass(self):
        self.assertEqual(self.run_gate('DATABASE_URL=postgresql://postgres:REPLACE_ME@db.internal/postgres\n').returncode, 0)

    def test_existing_optional_fifo_is_unsafe_not_missing(self):
        p = self.run_gate('# placeholder only\n', os.mkfifo)
        self.assertNotEqual(p.returncode, 0, 'existing optional FIFO was silently treated as missing')
        self.assertIn('Secret check refused', p.stdout)

    def test_dangling_optional_symlink_is_unsafe_not_missing(self):
        p = self.run_gate('# placeholder only\n', lambda path: path.symlink_to(path.parent / 'absent-private'))
        self.assertNotEqual(p.returncode, 0, 'dangling optional symlink was silently treated as missing')
        self.assertIn('Secret check refused', p.stdout)

    def test_existing_optional_directory_is_unsafe_not_missing(self):
        p = self.run_gate('# placeholder only\n', lambda path: path.mkdir())
        self.assertNotEqual(p.returncode, 0, 'optional directory was silently treated as missing')

    def test_valid_optional_template_is_scanned_without_raw_diagnostics(self):
        marker = 'github_pat_' + 'B' * 50
        p = self.run_gate('# placeholder only\n', lambda path: path.write_text('TOKEN=' + marker + '\n'))
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(marker not in p.stdout + p.stderr, 'optional credential appeared in diagnostic output')

    def helper(self, mutation=None, setup=None):
        """Run the exact embedded reader. Injection only changes actual test files
        immediately before its file open; it doesn't emulate reader decisions."""
        body = TOOL.read_text().split("<<'PY_MATCH'\n", 1)[1].split('\nPY_MATCH\n', 1)[0]
        wrapper = '''import io, os, pathlib, sys
source = sys.stdin.read()
root = pathlib.Path.cwd()
path = root / '.env.example'
original_open, original_io, original_stat = os.open, io.open, os.stat
stat_count = 0
triggered = False
grew = False
mode = sys.argv[3]
def race(value):
    global triggered
    if triggered or not isinstance(value, (str, bytes, os.PathLike)) or pathlib.Path(value).name != '.env.example':
        return
    triggered = True
    if mode == 'fifo':
        path.unlink();os.mkfifo(path)
    elif mode == 'symlink':
        path.unlink();path.symlink_to(root / 'outside-private')
    elif mode == 'large':
        path.write_bytes(b'x' * (1024 * 1024 + 1))
    elif mode == 'replace':
        path.unlink();path.write_text('# different inode, same source size!\\n')
    elif mode == 'grow-during-read':
        pass

def opened(value, flags, *args, **kwargs):
    race(value)
    result = original_open(value, flags, *args, **kwargs)
    return result

def io_opened(value, *args, **kwargs):
    global grew
    race(value)
    result = original_io(value, *args, **kwargs)
    if mode in ('grow-during-read', 'unlink-during-read') and not grew:
        grew = True
        if mode == 'grow-during-read':
            path.write_bytes(b'x' * (1024 * 1024 + 1))
        else:
            path.unlink()
    return result
def statted(value, *args, **kwargs):
    global stat_count
    if isinstance(value, (str, os.PathLike)) and pathlib.Path(value).name == '.env.example':
        stat_count += 1
        if mode == 'unlink-before-final-stat' and stat_count == 2:
            path.unlink()
    return original_stat(value, *args, **kwargs)
os.open, io.open, os.stat = opened, io_opened, statted
exec(compile(source, '<template-reader>', 'exec'))
'''
        with tempfile.TemporaryDirectory(prefix='apex-template-reader-') as tmp:
            root = Path(tmp).resolve()
            path = root / '.env.example';path.write_text('# placeholder only\n')
            marker = 'ghp_' + 'C' * 36
            (root / 'outside-private').write_text('TOKEN=' + marker + '\n')
            if setup:
                setup(path)
            try:
                p = subprocess.run([sys.executable, '-c', wrapper, 'ghp_[A-Za-z0-9]{30,}', '.env.example', mutation or 'none'],
                                   input=body, cwd=root, text=True, capture_output=True, timeout=2)
            except subprocess.TimeoutExpired:
                p = None
            self.assertIsNotNone(p, 'template reader blocked on an unsafe file instead of refusing')
            self.assertTrue(marker not in p.stdout + p.stderr, 'reader leaked private target values')
            return p

    def test_preopen_fifo_race_refuses_without_blocking(self):
        self.assertEqual(self.helper('fifo').returncode, 2)

    def test_preopen_symlink_race_refuses_instead_of_scanning_outside(self):
        self.assertEqual(self.helper('symlink').returncode, 2)

    def test_preopen_growth_cannot_bypass_size_guard(self):
        self.assertEqual(self.helper('large').returncode, 2)

    def test_growth_after_open_cannot_cause_an_unbounded_read(self):
        self.assertEqual(self.helper('grow-during-read').returncode, 2)

    def test_disappearance_after_open_is_unsafe_not_optional_missing(self):
        self.assertEqual(self.helper('unlink-during-read').returncode, 2)

    def test_disappearance_at_final_path_check_is_unsafe_not_missing(self):
        self.assertEqual(self.helper('unlink-before-final-stat').returncode, 2)

    def test_preopen_replacement_identity_refuses(self):
        self.assertEqual(self.helper('replace').returncode, 2)

    def test_oversized_initial_input_refuses(self):
        self.assertEqual(self.helper(setup=lambda p: p.write_bytes(b'x' * (1024 * 1024 + 1))).returncode, 2)

    def test_reader_missing_status_is_distinct_from_unsafe_or_matching(self):
        self.assertEqual(self.helper(setup=lambda p: p.unlink()).returncode, 3)
        self.assertEqual(self.helper().returncode, 0)
        self.assertEqual(self.helper(setup=lambda p: p.write_text('TOKEN=' + 'ghp_' + 'D' * 36)).returncode, 1)


if __name__ == '__main__':
    unittest.main()
