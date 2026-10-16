#!/usr/bin/env python3
"""Exercise the deployment data boundary with bytes and actual OS pipes."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
ERROR = 'Deployment request refused'


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/deploy_request.py'
        self.assertTrue(path.is_file(), 'bounded deployment request protocol is missing')
        spec = importlib.util.spec_from_file_location('deploy_request', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.request = {
            'schema_version': 1,
            'artifact_kind': 'apex-deployment-request',
            'controller': {'repository': 'Xynes-Studio/xynes-infra', 'commit': 'a' * 40,
                           'workflow': 'apex-build.yml', 'run_id': 123, 'run_attempt': 1},
            'publication': {'id': 456, 'digest': 'sha256:' + 'b' * 64},
            'deployment': {'run_id': 789, 'run_attempt': 1, 'job_id': 901},
        }

    def raw(self, request=None):
        return json.dumps(self.request if request is None else request).encode('ascii')

    def refuse(self, raw, stage='qa'):
        with self.assertRaises(self.module.RequestError) as caught:
            self.module.parse(raw, stage=stage)
        self.assertEqual(str(caught.exception), ERROR)

    def test_stage_is_supplied_only_by_the_privileged_command(self):
        for stage in ('qa', 'staging', 'prod'):
            result = self.module.parse(self.raw(), stage=stage)
            self.assertEqual(result, {**self.request, 'stage': stage})
            self.assertEqual(self.request['controller']['run_attempt'], 1)
        for stage in ('dev', 'QA', 'prod;sh', '../prod', '', None, True, ['prod']):
            self.refuse(self.raw(), stage)
        for name in ('stage', 'environment'):
            for stage in ('qa', 'staging', 'prod'):
                self.refuse(self.raw({**self.request, name: stage}), 'qa')

    def test_no_paths_operations_commands_overrides_or_authority_flags(self):
        for value in ('/etc/shadow', '../prod', '$(id)', 'sh -c id', 'latest', True, {}, []):
            for name in ('path', 'command', 'operation', 'env', 'image', 'approved', 'deployable'):
                request = copy.deepcopy(self.request)
                request[name] = value
                self.refuse(self.raw(request))
                for section in ('controller', 'publication', 'deployment'):
                    request = copy.deepcopy(self.request)
                    request[section][name] = value
                    self.refuse(self.raw(request))

    def test_schema_is_exact_and_required_fields_cannot_be_omitted(self):
        for container in (None, 'controller', 'publication', 'deployment'):
            keys = self.request if container is None else self.request[container]
            for key in keys:
                request = copy.deepcopy(self.request)
                del (request if container is None else request[container])[key]
                self.refuse(self.raw(request))
        for value in (None, [], True, 1, 'request'):
            self.refuse(json.dumps(value).encode())
            for section in ('controller', 'publication', 'deployment'):
                request = copy.deepcopy(self.request)
                request[section] = value
                self.refuse(self.raw(request))

    def test_duplicate_keys_are_refused_even_when_values_match(self):
        raw = self.raw()
        for key, value in (('schema_version', '1'), ('run_attempt', '1'), ('id', '456')):
            needle = ('"' + key + '": ' + value).encode()
            self.refuse(raw.replace(needle, needle + b', ' + needle))
        self.refuse(raw.replace(b'"id": 456', b'"id": 456, "\\u0069d": 456'))

    def test_numeric_types_bounds_and_first_attempt_are_enforced(self):
        for section, key in ((None, 'schema_version'), ('controller', 'run_id'),
                             ('controller', 'run_attempt'), ('publication', 'id'),
                             ('deployment', 'run_id'), ('deployment', 'run_attempt'), ('deployment', 'job_id')):
            for value in (True, False, None, '1', 1.0, -1, 0, 9007199254740992):
                request = copy.deepcopy(self.request)
                (request if section is None else request[section])[key] = value
                self.refuse(self.raw(request))
        for key in ('schema_version', 'run_attempt'):
            request = copy.deepcopy(self.request)
            (request if key == 'schema_version' else request['controller'])[key] = 2
            self.refuse(self.raw(request))
        request = copy.deepcopy(self.request)
        request['controller']['run_id'] = request['publication']['id'] = 9007199254740991
        self.assertEqual(self.module.parse(self.raw(request), stage='qa')['publication']['id'], 9007199254740991)

    def test_controller_and_digest_accept_only_the_fixed_grammar(self):
        changes = [('artifact_kind', 'other'), ('controller.repository', 'other/repo'),
                   ('controller.workflow', '.github/workflows/apex-build.yml'),
                   ('controller.workflow', 'apex-build.yml\nsh'),
                   ('controller.commit', 'A' * 40), ('controller.commit', 'a' * 39),
                   ('controller.commit', 'a' * 40 + '\n'),
                   ('publication.digest', 'sha256:' + 'B' * 64),
                   ('publication.digest', 'sha256:' + 'b' * 64 + ';id'),
                   ('publication.digest', 'https://example.invalid/artifact')]
        for key, value in changes:
            request = copy.deepcopy(self.request)
            if '.' in key:
                section, field = key.split('.')
                request[section][field] = value
            else:
                request[key] = value
            self.refuse(self.raw(request))

    def test_malformed_and_oversize_bytes_have_fixed_diagnostics(self):
        for raw in (b'', b'\xff', b'\xef\xbb\xbf' + self.raw(), b'{}\0',
                    self.raw() + b'{}', b'[' * 2000 + b']' * 2000,
                    self.raw().replace(b'456', b'NaN'),
                    self.raw().replace(b'456', b'Infinity'),
                    b' ' * (self.module.MAX_BYTES + 1), self.raw().decode(), bytearray(self.raw())):
            self.refuse(raw)
        raw = self.raw()
        boundary = raw + b' ' * (self.module.MAX_BYTES - len(raw))
        self.assertEqual(self.module.parse(boundary, stage='qa')['stage'], 'qa')
        self.refuse(boundary + b' ')

    def pipe(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        return read_fd, write_fd

    def test_real_pipe_requires_eof_and_accepts_fragmented_bytes(self):
        read_fd, write_fd = self.pipe()
        def writer():
            try:
                raw = self.raw()
                for chunk in (raw[:20], raw[20:100], raw[100:]):
                    os.write(write_fd, chunk)
                    time.sleep(0.005)
            finally:
                os.close(write_fd)
        worker = threading.Thread(target=writer)
        worker.start()
        result = self.module.receive(read_fd, stage='staging', timeout_seconds=1)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {**self.request, 'stage': 'staging'})

    def test_real_pipe_cannot_stall_before_or_after_valid_json(self):
        for raw in (b'', self.raw()):
            read_fd, write_fd = self.pipe()
            try:
                if raw:
                    os.write(write_fd, raw)
                started = time.monotonic()
                with self.assertRaises(self.module.RequestError) as caught:
                    self.module.receive(read_fd, stage='qa', timeout_seconds=0.05)
                self.assertEqual(str(caught.exception), ERROR)
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                os.close(write_fd)

    def test_real_pipe_rejects_overflow_and_extra_objects(self):
        for raw in (b' ' * (self.module.MAX_BYTES + 1), self.raw() + self.raw()):
            read_fd, write_fd = self.pipe()
            os.write(write_fd, raw)
            os.close(write_fd)
            with self.assertRaises(self.module.RequestError) as caught:
                self.module.receive(read_fd, stage='prod', timeout_seconds=1)
            self.assertEqual(str(caught.exception), ERROR)

    def test_reader_descriptor_deadline_and_os_errors_are_redacted(self):
        read_fd, write_fd = self.pipe()
        os.close(write_fd)
        for fd in (-1, True, '0'):
            with self.assertRaises(self.module.RequestError) as caught:
                self.module.receive(fd, stage='qa')
            self.assertEqual(str(caught.exception), ERROR)
        for timeout in (True, 0, -1, 31, float('nan'), float('inf'), '1'):
            with self.assertRaises(self.module.RequestError) as caught:
                self.module.receive(read_fd, stage='qa', timeout_seconds=timeout)
            self.assertEqual(str(caught.exception), ERROR)
        closed_fd, write_fd = os.pipe()
        os.close(closed_fd)
        os.close(write_fd)
        with self.assertRaises(self.module.RequestError) as caught:
            self.module.receive(closed_fd, stage='qa')
        self.assertEqual(str(caught.exception), ERROR)

    def test_slow_trickle_does_not_restart_the_deadline(self):
        read_fd, write_fd = self.pipe()
        def writer():
            try:
                for _ in range(10):
                    os.write(write_fd, b' ')
                    time.sleep(0.025)
            finally:
                os.close(write_fd)
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            started = time.monotonic()
            with self.assertRaises(self.module.RequestError) as caught:
                self.module.receive(read_fd, stage='qa', timeout_seconds=0.05)
            self.assertEqual(str(caught.exception), ERROR)
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            worker.join(1)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
