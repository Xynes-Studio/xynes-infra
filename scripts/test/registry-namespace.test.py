#!/usr/bin/env python3
"""Native package proofs and real, zero-layer OCI namespace qualification fixtures."""
import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import image_build
import registry_io
import release_manifest as records


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/registry_namespace.py'
        self.assertTrue(path.is_file(), 'Inert namespace qualification is missing')
        spec = importlib.util.spec_from_file_location('registry_namespace', path)
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-namespace-'); self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name).resolve() / 'new'
        self.controller = dict(repository='Xynes-Studio/xynes-infra', commit='1' * 40,
                               workflow='apex-build.yml', run_id=123, run_attempt=1)
        self.packages = {}; self.writes = []; self.events = []; self.gates = 0
        self.credential = registry_io.GitHubPublisherToken('fixture-actor', 'ghs_INERT_FIXTURE_NOT_A_REAL_TOKEN_123456789')
        self.transport = SimpleNamespace(request=lambda *a, **k: self.fail('Live transport forbidden'))

    def proof(self, workload):
        return dict(id=list(registry_io.WORKLOADS).index(workload) + 1, name='apex/' + workload,
                    package_type='container', visibility='private', owner=dict(login='Xynes-Studio', type='Organization'),
                    repository=dict(id=1415400917, full_name='Xynes-Studio/xynes-infra'))

    def read(self, workload):
        self.events.append('read:' + workload)
        return copy.deepcopy(self.packages.get(workload))

    def admission(self, controller):
        self.gates += 1; self.events.append('admission')
        value = {**controller, 'admitted': True, 'repository_id': 1415400917, 'job_id': 200,
                 'workflow_blob': 'a' * 40, 'environment_id': 300, 'reviewer_id': 8,
                 'checks': [dict(name=n, check_id=600+i, run_id=400+i, job_id=500+i, run_attempt=1,
                                 workflow_id=700+i) for i, n in enumerate(['lint','test','coverage','build','typecheck','security'])]}
        value['source_approval']=dict(approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1',pull_number=1,head_commit='b'*40,merge_commit=controller['commit'],tree_sha='c'*40,reviewer_id=8,review_id=901,source_manifest_sha256='d'*64)
        value['evidence_sha256'] = records.canonical_sha256(value)
        return value

    def publish(self, path, task, image, **kwargs):
        self.assertIs(kwargs['credential'], self.credential); self.assertIs(kwargs['transport'], self.transport)
        registry_io.validate_task(task, kwargs['policy'])
        local = image_build.inspect_oci(path, task)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        with tarfile.open(path) as archive:
            values = {m.name: json.load(archive.extractfile(m)) for m in archive if m.isfile()}
        index = values['index.json']; self.assertEqual(len(index['manifests']), 1)
        manifest = values['blobs/sha256/' + index['manifests'][0]['digest'][7:]]
        config = values['blobs/sha256/' + manifest['config']['digest'][7:]]
        self.assertEqual(manifest['layers'], []); self.assertEqual(config['rootfs'], {'type': 'layers', 'diff_ids': []})
        self.assertEqual(set(config['config']), {'Labels'})
        self.assertEqual(config['config']['Labels'], {**task['labels'], 'com.xynes.apex.artifact-kind': 'namespace-qualification-only'})
        self.assertEqual(len(values), 4); self.assertLess(path.stat().st_size, 20000)
        self.assertEqual(task['source_commit'], self.controller['commit'])
        self.assertEqual(task['recipe_sha256'], self.module.RECIPE_SHA256)
        self.assertEqual(task['build_inputs_sha256'], records.canonical_sha256({
            'artifact_kind': 'namespace-qualification-only', 'controller': self.controller,
            'workload': task['workload'], 'recipe_sha256': self.module.RECIPE_SHA256, 'platform': 'linux/amd64'}))
        self.assertEqual(task['environment'], 'qa' if task['workload'] in image_build.source_context.APPS else None)
        self.assertEqual(image, 'ghcr.io/xynes-studio/apex/' + task['workload'] + '@' + local['manifest_digest'])
        self.assertEqual(self.events[-1], 'admission')
        self.writes.append(copy.deepcopy(task)); self.events.append('write:' + task['workload'])
        self.packages[task['workload']] = self.proof(task['workload'])
        return {**local, 'image': image, 'published': True, 'registry_content_verified': True,
                'visibility_verified': False, 'attested': False, 'trust_verified': False, 'deployable': False}

    def run_qualify(self, **overrides):
        arguments = dict(controller=self.controller, read_package=self.read, admission_check=self.admission,
                         credential=self.credential, transport=self.transport, output=self.output, publish=self.publish)
        arguments.update(overrides); return self.module.qualify(**arguments)

    def test_missing_namespaces_use_only_real_inert_archives_after_complete_preflight(self):
        result = self.run_qualify()
        self.assertEqual(set(result), set(registry_io.WORKLOADS)); self.assertEqual(len(result), 11)
        self.assertEqual(len(self.writes), 11)
        first = next(i for i, e in enumerate(self.events) if e.startswith('write:'))
        self.assertTrue({'read:' + w for w in registry_io.WORKLOADS} <= set(self.events[:first]))
        self.assertEqual({p.name for p in self.output.iterdir()}, {w + '.oci.tar' for w in registry_io.WORKLOADS})
        for workload, proof in result.items(): self.assertEqual(proof, self.proof(workload))

    def test_existing_private_packages_are_never_mutated(self):
        self.packages = {w: self.proof(w) for w in registry_io.WORKLOADS}
        self.assertEqual(self.run_qualify(), self.packages); self.assertEqual(self.writes, [])

    def test_last_preflight_bad_or_duplicate_package_prevents_every_write(self):
        last = registry_io.WORKLOADS[-1]
        for mode in ['public', 'foreign', 'owner', 'extra', 'duplicate', 'error']:
            with self.subTest(mode=mode):
                self.packages = {w: self.proof(w) for w in registry_io.WORKLOADS}
                value = self.packages[last]
                if mode == 'public': value['visibility'] = 'public'
                elif mode == 'foreign': value['repository']['id'] = 2
                elif mode == 'owner': value['owner']['login'] = 'other'
                elif mode == 'extra': value['approved'] = True
                elif mode == 'duplicate': value['id'] = 1
                def read(workload):
                    if mode == 'error' and workload == last: raise RuntimeError('PRIVATE_SENTINEL')
                    return self.read(workload)
                with self.assertRaises(self.module.NamespaceError) as caught: self.run_qualify(read_package=read)
                self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception)); self.assertEqual(self.writes, [])

    def test_fresh_admission_revocation_stops_second_inert_write(self):
        def check(controller):
            if self.writes: raise RuntimeError('PRIVATE_SENTINEL')
            return self.admission(controller)
        with self.assertRaises(self.module.NamespaceError): self.run_qualify(admission_check=check)
        self.assertEqual(len(self.writes), 1)

    def test_late_missing_public_or_foreign_proof_stops_after_inert_only(self):
        for mode in ['missing', 'public', 'foreign']:
            with self.subTest(mode=mode):
                self.output = Path(self.tmp.name).resolve() / mode; self.packages = {}; self.writes = []
                def read(workload):
                    value = self.read(workload)
                    if self.writes and value:
                        if mode == 'missing': return None
                        value['visibility' if mode == 'public' else 'repository'] = 'public' if mode == 'public' else dict(id=1,full_name='other/repo')
                    return value
                with self.assertRaises(self.module.NamespaceError): self.run_qualify(read_package=read)
                self.assertEqual(len(self.writes), 1)

    def test_controller_and_serialized_approval_cannot_authorize_writes(self):
        for mode in ['rerun', 'badrepo', 'boolean', 'hash']:
            with self.subTest(mode=mode):
                controller = copy.deepcopy(self.controller)
                if mode == 'rerun': controller['run_attempt'] = 2
                elif mode == 'badrepo': controller['repository'] = 'other/repo'
                def check(value):
                    if mode == 'boolean': return {'approved': True}
                    proof = self.admission(value)
                    if mode == 'hash': proof['evidence_sha256'] = '0' * 64
                    return proof
                with self.assertRaises(self.module.NamespaceError): self.run_qualify(controller=controller, admission_check=check)
                self.assertEqual(self.writes, [])

    def test_existing_output_symlink_and_relative_paths_never_overwrite(self):
        self.output.mkdir(); sentinel = self.output / 'customer'; sentinel.write_bytes(b'preserve')
        symlink = Path(self.tmp.name) / 'link'; symlink.symlink_to(self.output, target_is_directory=True)
        for output in [self.output, symlink, Path('relative')]:
            with self.assertRaises(self.module.NamespaceError): self.run_qualify(output=output)
        self.assertEqual(sentinel.read_bytes(), b'preserve'); self.assertEqual(self.writes, [])

    def test_failed_content_verification_returns_no_proofs_and_never_logs_callbacks(self):
        def publish(*args, **kwargs):
            value = self.publish(*args, **kwargs); value['registry_content_verified'] = False; return value
        stdout = io.StringIO(); stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(self.module.NamespaceError): self.run_qualify(publish=publish)
        self.assertEqual(len(self.writes), 1); self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')

    def test_numeric_result_flags_are_not_verified_boolean_proofs(self):
        def publish(*args, **kwargs):
            value = self.publish(*args, **kwargs); value['published'] = 1; return value
        with self.assertRaises(self.module.NamespaceError): self.run_qualify(publish=publish)
        self.assertEqual(len(self.writes), 1)

    def test_newly_visible_private_namespace_is_preserved_without_write(self):
        counts = {}
        def read(workload):
            counts[workload] = counts.get(workload, 0) + 1
            if counts[workload] == 2: self.packages[workload] = self.proof(workload)
            return self.read(workload)
        self.assertEqual(len(self.run_qualify(read_package=read)), 11)
        self.assertEqual(self.writes, [])


if __name__ == '__main__': unittest.main()
