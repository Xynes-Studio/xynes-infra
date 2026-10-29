#!/usr/bin/env python3
"""Real filesystem and process tests for durable host release state."""
import copy
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/release/host_release_state.py'
sys.path.insert(0, str(MODULE.parent))


def load():
    spec = importlib.util.spec_from_file_location('host_release_state', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contend(path, stage, queue):
    module = load()
    try:
        with module.ReleaseState(Path(path), stage=stage, owner_uid=os.getuid()):
            queue.put('acquired')
    except module.StateError:
        queue.put('refused')


class Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), 'host release state implementation is missing')
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'state'
        self.path.mkdir(mode=0o700)
        self.request = {'schema_version': 1, 'artifact_kind': 'apex-deployment-request',
            'controller': {'repository': 'Xynes-Studio/xynes-infra', 'commit': 'a'*40,
                           'workflow': 'apex-build.yml', 'run_id': 123, 'run_attempt': 1},
            'publication': {'id': 456, 'digest': 'sha256:'+'b'*64},
            'deployment': {'run_id': 789, 'run_attempt': 1, 'job_id': 901}}
        self.source = 'sha256:'+'c'*64

    def state(self, stage='qa', path=None):
        return self.module.ReleaseState(path or self.path, stage=stage, owner_uid=os.getuid())

    def begin(self, state, request=None, source=None):
        return state.begin(self.request if request is None else request,
                           source_sha256=source or self.source, started_at=100)

    def smoke(self, stage='qa', request=None, source=None):
        return {'stage': stage, 'publication': (request or self.request)['publication'],
                'source_sha256': source or self.source, 'result': 'passed', 'observed_at': 90000}

    def soak(self, stage='staging'):
        return {'stage': stage, 'publication': self.request['publication'],
                'source_sha256': self.source, 'result': 'passed',
                'started_at': 90001, 'completed_at': 176401}

    def refuse(self, callback):
        with self.assertRaises(self.module.StateError) as caught:
            callback()
        self.assertEqual(str(caught.exception), 'Host release state refused')

    def test_accept_and_failure_preserve_current_previous_and_forward_only_history(self):
        with self.state() as state:
            self.assertEqual(state.read_accepted(), {'current': None, 'previous': None})
            token = self.begin(state)
            state.accept(token, smoke=self.smoke(), accepted_at=90001)
            first = state.read_accepted()['current']
        request = copy.deepcopy(self.request)
        request['deployment'].update(run_id=790, job_id=902)
        request['publication'] = {'id': 457, 'digest': 'sha256:'+'d'*64}
        with self.state() as state:
            token = self.begin(state, request)
            state.fail(token, reason='smoke', failed_at=90002)
            self.assertEqual(state.read_accepted(), {'current': first, 'previous': None})
            self.assertEqual(state.read_pending(), None)
            self.refuse(lambda: self.begin(state, request))
        request['deployment'].update(run_id=791, job_id=903)
        request['publication'] = {'id': 458, 'digest': 'sha256:'+'e'*64}
        with self.state() as state:
            token = self.begin(state, request)
            state.accept(token, smoke=self.smoke(request=request), accepted_at=90003)
            accepted = state.read_accepted()
            self.assertEqual(accepted['previous'], first)
            self.assertEqual(accepted['current']['request']['publication'], request['publication'])
            returned = state.read_accepted()
            returned['current']['request']['publication']['id'] = 1
            self.assertEqual(state.read_accepted(), accepted)
        for file in self.path.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_pending_crash_is_recoverable_but_never_automatically_reexecuted(self):
        with self.state() as state:
            token = self.begin(state)
        with self.state() as state:
            self.assertEqual(state.read_pending()['token'], token)
            self.refuse(lambda: self.begin(state))
            state.fail(token, reason='interrupted', failed_at=101)
        with self.state() as state:
            self.refuse(lambda: self.begin(state))
            self.assertIsNone(state.read_pending())

    def test_process_contention_serializes_even_different_stages(self):
        context = multiprocessing.get_context('spawn')
        with self.state() as state:
            for stage in ('qa', 'staging', 'prod'):
                queue = context.Queue()
                child = context.Process(target=contend, args=(str(self.path), stage, queue))
                child.start()
                child.join(5)
                self.assertFalse(child.is_alive())
                self.assertEqual(child.exitcode, 0)
                self.assertEqual(queue.get(timeout=1), 'refused')
                queue.close()
        with self.state('prod'):
            pass

    def test_staging_accepts_smoke_then_records_a_bound_completed_soak(self):
        with self.state('staging') as state:
            token = self.begin(state)
            state.accept(token, smoke=self.smoke('staging'), accepted_at=90001)
            self.assertIsNone(state.read_accepted()['current']['soak'])
            self.refuse(lambda: state.read_staging_for_promotion(publication=self.request['publication'], source_sha256=self.source, controller=self.request['controller']))
            bad = self.soak(); bad['completed_at'] -= 1
            self.refuse(lambda: state.record_soak(token, soak=bad, recorded_at=176402))
            for field, value in [('result', 'failed'), ('stage', 'prod'), ('source_sha256', 'sha256:'+'f'*64),
                                  ('publication', {'id': 1, 'digest': 'sha256:'+'b'*64})]:
                bad = self.soak(); bad[field] = value
                self.refuse(lambda: state.record_soak(token, soak=bad, recorded_at=176402))
            state.record_soak(token, soak=self.soak(), recorded_at=176402)
            self.assertEqual(state.read_accepted()['current']['soak'], self.soak())
            self.refuse(lambda: state.record_soak(token, soak=self.soak(), recorded_at=176403))

    def test_replay_collisions_and_unknown_request_authority_are_refused(self):
        with self.state() as state:
            token = self.begin(state)
            state.fail(token, reason='runtime', failed_at=101)
            for section, field in [('deployment', 'run_id'), ('deployment', 'job_id'), ('publication', 'id')]:
                request = copy.deepcopy(self.request)
                request['deployment'].update(run_id=800, job_id=950)
                request['publication'] = {'id': 500, 'digest': 'sha256:'+'d'*64}
                request[section][field] = self.request[section][field]
                self.refuse(lambda: self.begin(state, request))
            for field in ('stage', 'path', 'approved', 'signature_verified'):
                request = {**self.request, field: True}
                self.refuse(lambda: self.begin(state, request))

    def test_invalid_smoke_cannot_accept_and_terminal_records_cannot_change(self):
        with self.state() as state:
            token = self.begin(state)
            for field, value in [('result', 'failed'), ('stage', 'prod'), ('approved', True), ('observed_at', True)]:
                smoke = self.smoke(); smoke[field] = value
                self.refuse(lambda: state.accept(token, smoke=smoke, accepted_at=90001))
            self.refuse(lambda: state.fail(token, reason='/secret/path', failed_at=101))
            state.accept(token, smoke=self.smoke(), accepted_at=90001)
            self.refuse(lambda: state.fail(token, reason='runtime', failed_at=90002))
            self.refuse(lambda: state.accept(token, smoke=self.smoke(), accepted_at=90002))

    def test_smoke_storage_verification_flag_is_bounded_by_stage(self):
        with self.state('staging') as state:
            token = self.begin(state)
            smoke = self.smoke('staging'); smoke['storage_verified'] = False
            self.refuse(lambda: state.accept(token, smoke=smoke, accepted_at=90001))
            smoke['storage_verified'] = True
            state.accept(token, smoke=smoke, accepted_at=90001)
        os.remove(self.path / 'staging.json') if (self.path / 'staging.json').exists() else None
        with self.state() as state:
            token = self.begin(state)
            for bad in (1, None, 'false'):
                smoke = self.smoke(); smoke['storage_verified'] = bad
                self.refuse(lambda: state.accept(token, smoke=smoke, accepted_at=90001))
            smoke = self.smoke(); smoke['storage_verified'] = False
            state.accept(token, smoke=smoke, accepted_at=90001)
            self.assertIs(state.read_accepted()['current']['smoke']['storage_verified'], False)

    def test_lock_and_ledger_permissions_links_unknown_entries_and_stage_crossing(self):
        for stage in ('dev', '../prod', 'QA', None, True):
            self.refuse(lambda: self.state(stage).__enter__())
        os.chmod(self.path, 0o750)
        self.refuse(lambda: self.state().__enter__())
        os.chmod(self.path, 0o700)
        for name in ('host.lock', 'qa.lock', 'qa.json', 'unknown'):
            path = self.path/name
            path.symlink_to('/etc/passwd')
            self.refuse(lambda: self.state().__enter__())
            path.unlink()
        with self.state() as state:
            self.begin(state)
        ledger = self.path/'qa.json'
        original = ledger.read_bytes()
        data = json.loads(original); data['stage'] = 'prod'
        ledger.write_text(json.dumps(data))
        self.refuse(lambda: self.state().__enter__())
        ledger.write_bytes(original)
        for mode in (0o644, 0o640, 0o400):
            os.chmod(ledger, mode)
            self.refuse(lambda: self.state().__enter__())
        os.chmod(ledger, 0o600)
        linked = self.path/'prod.json'; os.link(ledger, linked)
        self.refuse(lambda: self.state().__enter__())
        linked.unlink()
        sibling = self.path.parent/'alias'; sibling.symlink_to(self.path, target_is_directory=True)
        self.refuse(lambda: self.state(path=sibling).__enter__())

    def test_atomic_write_partial_writes_and_failures_leave_prior_pointer_intact(self):
        with self.state() as state:
            token = self.begin(state)
            original = (self.path/'qa.json').read_bytes()
            real_write = os.write
            with patch.object(self.module.os, 'write', side_effect=lambda fd, raw: real_write(fd, raw[:7])):
                state.fail(token, reason='runtime', failed_at=101)
            self.assertNotEqual((self.path/'qa.json').read_bytes(), original)
        request = copy.deepcopy(self.request)
        request['deployment'].update(run_id=800, job_id=950)
        request['publication'] = {'id': 500, 'digest': 'sha256:'+'d'*64}
        for syscall in ('write', 'fsync', 'replace'):
            original = (self.path/'qa.json').read_bytes()
            with self.state() as state:
                with patch.object(self.module.os, syscall, side_effect=OSError('SECRET')):
                    self.refuse(lambda: self.begin(state, request))
            self.assertEqual((self.path/'qa.json').read_bytes(), original)
            self.assertFalse(any(p.name.endswith('.tmp') for p in self.path.iterdir()))

    def test_bound_ledger_duplicates_corruption_and_methods_outside_lock(self):
        state = self.state()
        self.refuse(state.read_accepted)
        with state:
            self.begin(state)
        self.refuse(state.read_accepted)
        path = self.path/'qa.json'
        original = path.read_bytes()
        for raw in (b'{}', original.replace(b'"stage":"qa"', b'"stage":"qa","stage":"qa"'),
                    original+b' trailing', b' '* (self.module.MAX_BYTES+1)):
            path.write_bytes(raw)
            self.refuse(lambda: self.state().__enter__())
        path.write_bytes(original)

    def test_staging_promotion_read_is_exact_and_inside_the_host_lock(self):
        with self.state('prod') as state:
            self.refuse(lambda: state.read_staging_for_promotion(controller=self.request['controller'], publication=self.request['publication'], source_sha256=self.source))
        with self.state('staging') as state:
            token = self.begin(state)
            state.accept(token, smoke=self.smoke('staging'), accepted_at=90001)
            state.record_soak(token, soak=self.soak(), recorded_at=176402)
        with self.state('prod') as state:
            record = state.read_staging_for_promotion(controller=self.request['controller'], publication=self.request['publication'], source_sha256=self.source)
            self.assertEqual(record['token'], token)
            self.refuse(lambda: state.read_staging_for_promotion(controller=self.request['controller'], publication=self.request['publication'], source_sha256='sha256:'+'d'*64))
            self.refuse(lambda: state.read_staging_for_promotion(controller=self.request['controller'], publication={'id': 999, 'digest': 'sha256:'+'b'*64}, source_sha256=self.source))

    def test_directory_sync_failure_poisoning_requires_reopen_and_reconciliation(self):
        real_sync = os.fsync
        with self.state() as state:
            def sync(fd):
                if fd == state.fd:
                    raise OSError('private failure detail')
                return real_sync(fd)
            with patch.object(self.module.os, 'fsync', side_effect=sync):
                self.refuse(lambda: self.begin(state))
            self.refuse(state.read_pending)
            self.refuse(lambda: self.begin(state))
        with self.state() as state:
            pending = state.read_pending()
            self.assertIsNotNone(pending)
            state.fail(pending['token'], reason='interrupted', failed_at=101)

    def test_nested_entry_does_not_release_an_existing_host_lock(self):
        with self.state() as state:
            self.refuse(state.__enter__)
            self.assertEqual(state.read_accepted(), {'current': None, 'previous': None})

    def test_history_limit_refuses_new_work_without_eviction(self):
        with self.state() as state:
            token = self.begin(state)
            state.fail(token, reason='runtime', failed_at=101)
            request = copy.deepcopy(self.request)
            request['deployment'].update(run_id=800, job_id=950)
            request['publication'] = {'id': 500, 'digest': 'sha256:'+'d'*64}
            original = (self.path/'qa.json').read_bytes()
            with patch.object(self.module, 'MAX_ATTEMPTS', 1):
                self.refuse(lambda: self.begin(state, request))
            self.assertEqual((self.path/'qa.json').read_bytes(), original)

    def test_publication_promotes_across_stages_but_deployment_identity_cannot(self):
        with self.state() as state:
            token = self.begin(state)
            state.accept(token, smoke=self.smoke(), accepted_at=90001)
        with self.state('staging') as state:
            self.refuse(lambda: self.begin(state))
            request = copy.deepcopy(self.request)
            request['deployment'].update(run_id=800, job_id=950)
            token = self.begin(state, request)
            state.accept(token, smoke=self.smoke('staging'), accepted_at=90001)
            self.assertEqual(state.read_accepted()['current']['request']['publication'], self.request['publication'])

    def test_external_ledger_replacement_invalidates_cached_release(self):
        with self.state() as state:
            token = self.begin(state)
            state.accept(token, smoke=self.smoke(), accepted_at=90001)
            alternate = self.path.parent/'replacement'
            alternate.write_bytes((self.path/'qa.json').read_bytes())
            alternate.chmod(0o600)
            alternate.replace(self.path/'qa.json')
            self.refuse(state.read_accepted)

    def test_soak_and_promotion_refuse_when_staging_has_unreconciled_pending_work(self):
        with self.state('staging') as state:
            token = self.begin(state)
            state.accept(token, smoke=self.smoke('staging'), accepted_at=90001)
            state.record_soak(token, soak=self.soak(), recorded_at=176402)
            request = copy.deepcopy(self.request)
            request['deployment'].update(run_id=800, job_id=950)
            request['publication'] = {'id': 500, 'digest': 'sha256:'+'d'*64}
            self.begin(state, request)
        with self.state('prod') as state:
            self.refuse(lambda: state.read_staging_for_promotion(publication=self.request['publication'], source_sha256=self.source, controller=self.request['controller']))


if __name__ == '__main__':
    unittest.main()
