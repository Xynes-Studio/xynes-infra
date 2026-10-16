#!/usr/bin/env python3
"""Unprivileged entry fixtures; no sudo or privileged host executor is invoked."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ERROR = 'SSH deployment entry refused'


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/ssh_entry.py'
        self.assertTrue(path.is_file(), 'unprivileged forced-command entry is missing')
        sys.path.insert(0, str(ROOT / 'scripts/release'))
        self.addCleanup(sys.path.remove, str(ROOT / 'scripts/release'))
        spec = importlib.util.spec_from_file_location('ssh_entry', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.request = dict(schema_version=1, artifact_kind='apex-deployment-request',
            controller=dict(repository='Xynes-Studio/xynes-infra', commit='a'*40,
                            workflow='apex-build.yml', run_id=123, run_attempt=1),
            publication=dict(id=456, digest='sha256:'+'b'*64),
            deployment=dict(run_id=789,run_attempt=1,job_id=901))
        self.calls = []

    def process(self, argv, *, data, env, cwd, timeout_seconds, max_output):
        self.calls.append(dict(argv=argv, data=data, env=env, cwd=cwd,
                               timeout_seconds=timeout_seconds, max_output=max_output))
        return (0, b'root diagnostic must never be forwarded', b'private-value')

    def invoke(self, *, name='xynes_qa_deploy', command='apex-deploy', raw=None,
            env=None, tty=False, uid=1005, euid=1005, process=None, eof=True):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, json.dumps(self.request).encode() if raw is None else raw)
            if eof:
                os.close(write_fd)
                write_fd = None
            environment = {} if command is None else {'SSH_ORIGINAL_COMMAND': command}
            environment.update(env or {})
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(self.module.os, 'getuid', return_value=uid), \
                 patch.object(self.module.os, 'geteuid', return_value=euid), \
                 patch.object(self.module.os, 'isatty', return_value=tty), \
                 patch.object(self.module.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name=name)):
                return self.module.run(read_fd, process=process or self.process)
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def refused(self, **overrides):
        with self.assertRaises(self.module.EntryError) as caught:
            self.invoke(**overrides)
        self.assertEqual(str(caught.exception), ERROR)

    def test_os_account_alone_selects_literal_stage_and_fixed_sudo_argv(self):
        for stage in ('qa', 'staging', 'prod'):
            result = self.invoke(name='xynes_'+stage+'_deploy')
            call = self.calls[-1]
            self.assertEqual(call['argv'], ['/usr/bin/sudo', '-n', '--',
                                           '/usr/local/libexec/xynes-deploy-root', stage])
            self.assertEqual(json.loads(call['data']), self.request)
            self.assertEqual(call['data'], (json.dumps(self.request, sort_keys=True, separators=(',', ':'))+'\n').encode())
            self.assertEqual(call['cwd'], '/')
            self.assertEqual(call['env'], {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
            self.assertEqual(result, {'artifact_kind':'apex-ssh-entry-result', 'stage':stage,
                                      'root_command_completed':True, 'deployable':False})

    def test_caller_environment_cannot_choose_stage_executable_or_privileged_settings(self):
        result = self.invoke(env={'USER':'xynes_prod_deploy','LOGNAME':'xynes_prod_deploy',
            'STAGE':'prod','APEX_STAGE':'prod','PATH':'/caller/bin','PYTHONPATH':'/caller',
            'LD_PRELOAD':'/caller/injected.so','SUDO_USER':'root','HOME':'/caller/home'})
        self.assertEqual(result['stage'], 'qa')
        self.assertEqual(self.calls[0]['argv'][-1], 'qa')
        self.assertNotIn('/caller', str(self.calls[0]['env']))

    def test_nonexact_original_commands_and_unknown_or_elevated_callers_are_refused(self):
        for command in (None, '', 'apex-deploy qa', 'apex-deploy;id', 'apex-deploy\n',
                        '/usr/local/libexec/xynes-deploy-root prod', 'sh', 'scp -t /tmp'):
            self.refused(command=command)
        for name in ('root','xynes','xynes_dev_deploy','xynes_qa_deploy;sh','xynes_QA_deploy',''):
            self.refused(name=name)
        self.refused(uid=0, euid=0)
        self.refused(uid=1005, euid=0)
        self.assertEqual(self.calls, [])

    def test_pty_or_ssh_tty_is_refused_before_sudo(self):
        self.refused(tty=True)
        for value in ('/dev/pts/1', ''):
            self.refused(env={'SSH_TTY':value})
        self.assertEqual(self.calls, [])

    def test_unknown_request_fields_cross_stage_injection_overflow_and_retries_never_invoke_sudo(self):
        attacks = [json.dumps({**self.request, 'stage':'prod'}).encode(),
                   json.dumps({**self.request, 'command':'id'}).encode(),
                   json.dumps({**self.request, 'env':{'DATABASE_URL':'caller'}}).encode(),
                   json.dumps({**self.request, 'path':'/etc/shadow'}).encode(),
                   b'X'*(self.module.deploy_request.MAX_BYTES+1),
                   json.dumps(self.request).encode().replace(b'"run_attempt": 1', b'"run_attempt": 2'),
                   json.dumps(self.request).encode()+b'{}']
        for raw in attacks:
            self.refused(raw=raw)
        self.assertEqual(self.calls, [])

    def test_incomplete_request_and_missing_eof_have_a_fixed_intake_deadline(self):
        with patch.object(self.module, 'REQUEST_TIMEOUT', 0.05):
            for raw in (b'', json.dumps(self.request).encode()):
                started = time.monotonic()
                self.refused(raw=raw, eof=False)
                self.assertLess(time.monotonic()-started, 0.5)
        self.assertEqual(self.calls, [])

    def test_process_failure_oversized_output_and_callback_errors_are_redacted(self):
        for response in ((1,b'private-value',b'private-value'), (-15,b'',b'private-value'),
                         (True,b'',b''), (0,'wrong-type',b''),
                         (0,b'X'*(self.module.MAX_OUTPUT+1),b''),
                         (0,b'X'*self.module.MAX_OUTPUT,b'X'), (0,b'',b'X'*(self.module.MAX_OUTPUT+1))):
            self.refused(process=lambda *args, **kwargs: response)
        def failed(*args, **kwargs):
            raise RuntimeError('private-value')
        self.refused(process=failed)

    def test_native_process_adapter_bounds_actual_stdout_stderr_and_deadline(self):
        environment = {'PATH':os.defpath, 'LANG':'C.UTF-8'}
        def native(code, *, timeout=1, limit=128):
            return self.module.bounded_process([sys.executable,'-I','-c',code], data=b'input',
                env=environment, cwd='/', timeout_seconds=timeout, max_output=limit)
        code, out, err = native('import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(d); sys.stderr.write("note")')
        self.assertEqual((code,out,err), (0,b'input',b'note'))
        for code in ('import sys; sys.stdout.write("X"*129)',
                     'import sys; sys.stdout.write("X"*100); sys.stderr.write("X"*100)'):
            with self.assertRaises(self.module.EntryError) as caught:
                native(code)
            self.assertEqual(str(caught.exception), ERROR)
        with self.assertRaises(self.module.EntryError) as caught:
            native('import time; time.sleep(1)', timeout=0.05)
        self.assertEqual(str(caught.exception), ERROR)
        with self.assertRaises(self.module.EntryError) as caught:
            self.module.bounded_process(['/nonexistent-apex-test-command'], data=b'input',
                env=environment, cwd='/', timeout_seconds=1, max_output=128)
        self.assertEqual(str(caught.exception), ERROR)

    def test_main_rejects_caller_arguments_and_never_echoes_root_diagnostics(self):
        with patch.object(self.module.sys, 'argv', ['ssh_entry.py','prod']), \
             patch.object(self.module, 'run') as run, \
             patch.object(self.module.sys, 'stdout') as out, \
             patch.object(self.module.sys, 'stderr') as errors:
            self.assertEqual(self.module.main(), 1)
            run.assert_not_called()
            out.write.assert_not_called()
            self.assertEqual(''.join(call.args[0] for call in errors.write.call_args_list), ERROR+'\n')


if __name__ == '__main__':
    unittest.main()
