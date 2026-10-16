#!/usr/bin/env python3
"""Real inert ZIP/publication data; injected signature proof is not a signature."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import attestation_binding
import native_attestation
import release_manifest as records

PREFIX = '/repos/Xynes-Studio/xynes-infra'
ERROR = 'Deployment candidate refused'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/deployment_candidate.py'
        self.assertTrue(path.is_file(), 'deployment candidate acquisition is missing')
        self.module = load('deployment_candidate', path)
        self.fixture = load('publication_fixture', ROOT / 'scripts/test/publish-batch.test.py').Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.receipt = self.fixture.run_batch()
        self.controller = copy.deepcopy(self.fixture.controller)
        self.manifest = copy.deepcopy(self.fixture.manifest)
        self.public = copy.deepcopy(self.fixture.public)
        self.manifest_raw = (json.dumps(self.manifest, indent=2) + '\n').encode()
        self.signature_fixture = load('signature_fixture', ROOT / 'scripts/test/attestation-binding.test.py').Tests()
        self.signature_fixture.setUp()
        self.addCleanup(self.signature_fixture.doCleanups)
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-candidate-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.work = self.base
        self.attempts = 0
        self.reads, self.downloads, self.verifications = [], [], []
        self.repo = {'id': 1415400917, 'full_name': self.controller['repository'],
                     'private': False, 'fork': False, 'default_branch': 'main'}
        self.run = {'id': self.controller['run_id'], 'run_attempt': 1, 'workflow_id': 500,
                    'path': '.github/workflows/apex-build.yml', 'event': 'workflow_dispatch',
                    'head_sha': self.controller['commit'], 'head_branch': 'main',
                    'status': 'completed', 'conclusion': 'success',
                    'repository': copy.deepcopy(self.repo), 'head_repository': copy.deepcopy(self.repo)}
        self.branch = {'name': 'main', 'protected': True, 'commit': {'sha': self.controller['commit']}}
        self.workflow = {'id': 500, 'path': '.github/workflows/apex-build.yml', 'state': 'active'}
        self.catalog = records.read_json(ROOT / 'infra/release/source-check-catalog.json')
        self.checks = {'checks_verified': True, 'approved': False, 'deployable': False, 'sources': []}
        for name, source in self.manifest['sources'].items():
            checks = [dict(name=check, check_id=600+i, job_id=700+i, run_id=800+i,
                           run_attempt=1, workflow_id=900+i, workflow_path=workflow, workflow_blob='a'*40)
                      for i, (check, workflow) in enumerate(self.catalog[name].items())]
            self.checks['sources'].append(dict(repository=source['repository'], commit=source['commit'], checks=checks))
        self.refresh_bundles()

    def zip(self, files):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return out.getvalue()

    def refresh_bundles(self):
        self.data = {
            99: self.zip({'controller.json': json.dumps(self.controller).encode(),
                          'source-manifest.json': self.manifest_raw,
                          'public-inputs.json': json.dumps(self.public).encode(),
                          'source-checks.json': json.dumps(self.checks).encode()}),
            456: self.zip({'published-receipt.json': json.dumps(self.receipt).encode()}),
        }
        self.metas = {}
        for aid, kind in ((99, 'admission'), (456, 'publication')):
            url = 'https://api.github.com' + PREFIX + '/actions/artifacts/' + str(aid)
            self.metas[aid] = dict(id=aid, name=f'apex-{kind}-{self.controller["run_id"]}-1',
                digest='sha256:' + hashlib.sha256(self.data[aid]).hexdigest(), expired=False,
                size_in_bytes=len(self.data[aid]), url=url, archive_download_url=url+'/zip',
                workflow_run=dict(id=self.controller['run_id'], repository_id=1415400917,
                                  head_repository_id=1415400917, head_branch='main', head_sha=self.controller['commit']))
        self.listing = {'total_count': 2, 'artifacts': list(copy.deepcopy(self.metas).values())}
        self.request = dict(schema_version=1, artifact_kind='apex-deployment-request',
                            controller=copy.deepcopy(self.controller),
                            publication={'id': 456, 'digest': self.metas[456]['digest']},
                            deployment={'run_id':789,'run_attempt':1,'job_id':901})
        self.content = dict(type='file', path='infra/release/source-manifest.json', encoding='base64',
                            size=len(self.manifest_raw), content=base64.b64encode(self.manifest_raw).decode(),
                            sha=hashlib.sha1(f'blob {len(self.manifest_raw)}\0'.encode()+self.manifest_raw).hexdigest())

    def read(self, path):
        self.reads.append(path)
        if path == PREFIX:
            result = self.repo
        elif path == PREFIX + '/branches/main':
            result = self.branch
        elif path == PREFIX + f'/actions/runs/{self.controller["run_id"]}':
            result = self.run
        elif path == PREFIX + '/actions/workflows/500':
            result = self.workflow
        elif path == PREFIX + f'/actions/runs/{self.controller["run_id"]}/artifacts?per_page=100&page=1':
            result = self.listing
        elif path == PREFIX + '/contents/infra/release/source-manifest.json?ref=' + self.controller['commit']:
            result = self.content
        elif path.startswith(PREFIX + '/actions/artifacts/'):
            result = self.metas[int(path.rsplit('/', 1)[1])]
        elif path.startswith('/orgs/Xynes-Studio/packages/container/apex%2F'):
            result = self.fixture.package(path.split('%2F')[1])
            result.update(created_at='incidental native API field')
        else:
            raise AssertionError('unexpected API path: ' + path)
        return copy.deepcopy(result)

    def download(self, aid, destination, limit):
        self.downloads.append(aid)
        self.assertEqual(limit, len(self.data[aid]))
        destination.write_bytes(self.data[aid])

    def verify(self, *, image, controller):
        self.verifications.append(image)
        self.assertEqual(controller, self.controller)
        result = copy.deepcopy(self.signature_fixture.result)
        old = json.dumps(result).replace('a'*40, controller['commit']).replace('/runs/100/', f'/runs/{controller["run_id"]}/')
        result = json.loads(old)
        name, digest = image.rsplit('@sha256:', 1)
        result['verificationResult']['statement']['subject'] = [{'name': name, 'digest': {'sha256': digest}}]
        proof = attestation_binding.qualify_native_result(json.dumps([result]).encode(), image, controller)
        return {**proof, 'native_signature_verified': True, 'verifier_version': native_attestation.VERSION}

    def acquire(self, **overrides):
        self.attempts += 1
        self.work = self.base / ('attempt-' + str(self.attempts))
        self.work.mkdir(mode=0o700)
        arguments = dict(raw=json.dumps(self.request).encode(), stage='qa', work=self.work,
                         read=self.read, download=self.download, verify=self.verify)
        arguments.update(overrides)
        return self.module.acquire(**arguments)

    def refused(self, **overrides):
        with self.assertRaises(self.module.CandidateError) as caught:
            self.acquire(**overrides)
        self.assertEqual(str(caught.exception), ERROR)

    def test_complete_inert_artifacts_bind_all_fifteen_images_without_stage_authority(self):
        result = self.acquire(stage='staging')
        self.assertEqual(result['stage'], 'staging')
        self.assertEqual(result['controller'], self.controller)
        self.assertEqual(result['receipt_sha256'], self.receipt['receipt_sha256'])
        self.assertEqual(result['source_manifest_sha256'], records.canonical_sha256(self.manifest))
        self.assertEqual(result['publication'], self.request['publication'])
        self.assertEqual(result['admission'], {'id': 99, 'digest': self.metas[99]['digest']})
        self.assertEqual([p['image'] for p in result['signatures']], [i['image'] for i in self.receipt['images']])
        self.assertEqual(len(self.verifications), 15)
        self.assertFalse(result['deployable'])
        self.assertFalse(result['stage_approved'])
        self.assertEqual(self.reads[-2:], [PREFIX+f'/actions/runs/{self.controller["run_id"]}', PREFIX+'/branches/main'])
        for name, expected in (('admission', 4), ('publication', 1)):
            directory = self.work / name
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            files = list(directory.iterdir())
            self.assertEqual(len(files), expected)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))

    def test_caller_stage_and_extra_request_fields_refuse_before_reads(self):
        self.request['stage'] = 'prod'
        self.refused()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.downloads, [])

    def test_native_run_repository_workflow_attempt_state_and_main_are_required(self):
        for key, value in (('id', 999), ('run_attempt', 2), ('workflow_id', True),
                           ('path', '.github/workflows/other.yml'), ('event', 'push'),
                           ('head_sha', '0'*40), ('head_branch', 'develop'),
                           ('status', 'in_progress'), ('conclusion', 'failure')):
            with self.subTest(key=key):
                saved = self.run[key]
                self.run[key] = value
                self.refused()
                self.run[key] = saved
        for section in ('repository', 'head_repository'):
            for key, value in (('id', 1113600873), ('full_name', 'other/repo')):
                saved = self.run[section][key]
                self.run[section][key] = value
                self.refused()
                self.run[section][key] = saved
        self.branch['commit']['sha'] = '0'*40
        self.refused()
        self.assertEqual(self.downloads, [])

    def test_repository_identity_visibility_default_branch_and_workflow_are_required(self):
        for key, value in (('id', 1), ('full_name', 'other/repo'), ('private', True),
                           ('fork', True), ('default_branch', 'develop')):
            saved = self.repo[key]
            self.repo[key] = value
            self.refused()
            self.repo[key] = saved
        self.workflow['state'] = 'disabled_manually'
        self.refused()
        self.assertEqual(self.downloads, [])

    def test_missing_duplicate_expired_or_unbounded_artifact_listing_is_refused(self):
        original = copy.deepcopy(self.listing)
        mutations = [lambda r: r.update(total_count=101), lambda r: r.update(total_count=True),
                     lambda r: r['artifacts'].pop(0),
                     lambda r: r['artifacts'].append(copy.deepcopy(r['artifacts'][0])),
                     lambda r: r['artifacts'][0].update(expired=True),
                     lambda r: r['artifacts'][0].update(digest='invalid'),
                     lambda r: r['artifacts'][0].update(name='apex-admission-123-2'),
                     lambda r: r['artifacts'][1].update(id=999)]
        for mutate in mutations:
            self.listing = copy.deepcopy(original)
            mutate(self.listing)
            if self.listing['total_count'] == 2:
                self.listing['total_count'] = len(self.listing['artifacts'])
            self.refused()
        self.assertEqual(self.downloads, [])

    def test_tampered_native_zip_and_wrong_run_metadata_are_refused(self):
        self.data[99] = self.data[99][:-1] + b'X'
        self.refused()
        self.refresh_bundles()
        self.metas[456]['workflow_run']['id'] += 1
        self.refused()
        self.assertEqual(self.verifications, [])

    def test_source_manifest_is_bound_to_exact_native_commit_bytes_and_git_blob_hash(self):
        for mode in ('bytes', 'hash', 'size', 'path', 'encoding', 'content'):
            original = copy.deepcopy(self.content)
            if mode == 'bytes':
                raw = self.manifest_raw + b'\n'
                self.content.update(size=len(raw), content=base64.b64encode(raw).decode(),
                                    sha=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest())
            elif mode == 'hash':
                self.content['sha'] = '0'*40
            elif mode == 'content':
                self.content['content'] = 'not-base64'
            else:
                self.content[mode] = {'size': True, 'path': 'other', 'encoding': 'none'}[mode]
            self.refused()
            self.content = original
        self.assertEqual(self.verifications, [])

    def test_admission_controller_public_maps_and_source_check_inventory_are_required(self):
        for mode in ('controller', 'public', 'checks'):
            self.refresh_bundles()
            files = {'controller.json': json.dumps(self.controller).encode(),
                     'source-manifest.json': self.manifest_raw,
                     'public-inputs.json': json.dumps(self.public).encode(),
                     'source-checks.json': json.dumps(self.checks).encode()}
            if mode == 'controller':
                changed = {**self.controller, 'run_id': 999}
                files['controller.json'] = json.dumps(changed).encode()
            elif mode == 'public':
                changed = copy.deepcopy(self.public)
                changed['qa'] = copy.deepcopy(changed['prod'])
                files['public-inputs.json'] = json.dumps(changed).encode()
            else:
                files['source-checks.json'] = b'{"checks_verified":true,"approved":false,"deployable":false,"sources":[]}'
            self.data[99] = self.zip(files)
            meta = self.metas[99]
            meta.update(size_in_bytes=len(self.data[99]), digest='sha256:'+hashlib.sha256(self.data[99]).hexdigest())
            self.listing['artifacts'][0] = copy.deepcopy(meta)
            self.refused()
        self.assertEqual(self.verifications, [])

    def test_complete_publication_hash_inventory_and_bindings_are_required(self):
        original = copy.deepcopy(self.receipt)
        for mode in ('hash', 'missing', 'controller', 'image', 'flag'):
            self.receipt = copy.deepcopy(original)
            if mode == 'hash':
                self.receipt['receipt_sha256'] = '0'*64
            elif mode == 'missing':
                self.receipt['images'].pop()
            elif mode == 'controller':
                self.receipt['controller']['run_id'] += 1
            elif mode == 'image':
                self.receipt['images'][-1]['environment'] = 'qa'
            else:
                self.receipt['deployable'] = True
            if mode != 'hash':
                self.receipt['receipt_sha256'] = records.canonical_sha256({k:v for k,v in self.receipt.items() if k!='receipt_sha256'})
            self.refresh_bundles()
            self.refused()
        self.assertEqual(self.verifications, [])

    def test_private_packages_must_match_native_receipt_ids_and_owned_repository(self):
        for mode in ('public', 'id', 'repository', 'missing'):
            def read(path):
                value = self.read(path)
                if path.startswith('/orgs/'):
                    if mode == 'public': value['visibility'] = 'public'
                    elif mode == 'id': value['id'] += 100
                    elif mode == 'repository': value['repository']['id'] = 1
                    else: return None
                return value
            self.refused(read=read)
        self.assertEqual(self.verifications, [])

    def test_failed_or_wrong_signature_proof_never_yields_a_candidate(self):
        for mode in ('exception', 'false', 'wrongimage', 'wrongcontroller', 'version', 'extra'):
            def verify(**arguments):
                proof = self.verify(**arguments)
                if mode == 'exception': raise RuntimeError('private provider response')
                elif mode == 'false': proof['native_signature_verified'] = False
                elif mode == 'wrongimage': proof['image'] += '-wrong'
                elif mode == 'wrongcontroller': proof['controller']['run_attempt'] = 2
                elif mode == 'version': proof['verifier_version'] = 'unowned'
                else: proof['approved'] = True
                return proof
            self.refused(verify=verify)

    def test_artifact_controller_boolean_cannot_impersonate_attempt_one(self):
        original = copy.deepcopy(self.receipt)
        for kind in ('admission', 'publication'):
            self.receipt = copy.deepcopy(original)
            if kind == 'publication':
                self.receipt['controller']['run_attempt'] = True
                self.receipt['receipt_sha256'] = records.canonical_sha256({k:v for k,v in self.receipt.items() if k!='receipt_sha256'})
            self.refresh_bundles()
            if kind == 'admission':
                files = {'controller.json': json.dumps({**self.controller, 'run_attempt': True}).encode(),
                         'source-manifest.json': self.manifest_raw,
                         'public-inputs.json': json.dumps(self.public).encode(),
                         'source-checks.json': json.dumps(self.checks).encode()}
                self.data[99] = self.zip(files)
                self.metas[99].update(size_in_bytes=len(self.data[99]), digest='sha256:'+hashlib.sha256(self.data[99]).hexdigest())
                self.listing['artifacts'][0] = copy.deepcopy(self.metas[99])
            self.refused()

    def test_late_native_state_drift_is_rechecked_after_all_signatures(self):
        for mode in ('run', 'main', 'package'):
            def read(path):
                value = self.read(path)
                if len(self.verifications) == 15:
                    if mode == 'run' and path == PREFIX+f'/actions/runs/{self.controller["run_id"]}': value['run_attempt'] = 2
                    elif mode == 'main' and path == PREFIX+'/branches/main': value['commit']['sha'] = '0'*40
                    elif mode == 'package' and path.startswith('/orgs/'): value['visibility'] = 'public'
                return value
            self.refused(read=read)
            self.verifications.clear()


if __name__ == '__main__':
    unittest.main()
