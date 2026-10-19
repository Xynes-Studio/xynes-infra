#!/usr/bin/env python3
"""Injected GHCR transport tests; no network, Docker, provider or credential reads."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import image_build
import registry_policy

SPEC = importlib.util.spec_from_file_location('image_build_tests', ROOT / 'scripts/test/image-build.test.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
TOKEN = 'ghs_DO_NOT_PRINT_PUBLISHER_SECRET_123456789'
BEARER = 'DO_NOT_PRINT_BEARER_SECRET_123456789'
STATE = 'DO_NOT_PRINT_UPLOAD_STATE'


def module():
    path = ROOT / 'scripts/release/registry_io.py'
    assert path.exists(), 'registry transport implementation is missing'
    spec = importlib.util.spec_from_file_location('registry_io', path)
    value = importlib.util.module_from_spec(spec)
    sys.modules['registry_io'] = value
    spec.loader.exec_module(value)
    return value


class Clock:
    value = 0.0
    def __call__(self): return self.value


class Registry:
    """Content-addressed mock with actual OCI bytes and bounded stream reads."""
    def __init__(self, api, repository, files, digest, clock):
        self.api, self.repository, self.files, self.digest, self.clock = api, repository, files, digest, clock
        self.calls = []
        self.blobs = {}
        self.manifest = None
        self.location = f'https://ghcr.io/v2/{repository}/blobs/uploads/upload-1?_state={STATE}'
        self.challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        self.expires = 300
        self.override = None
        self.on_request = None
        self.fail_put = False
        self.tamper_subject = False
        self.tamper_config = False
        self.subject_type = image_build.OCI_MANIFEST
        self.read_chunk = 19
        self.config_redirect = None
        self.cdn_status = 200
        self.cdn_body = None

    def response(self, status, headers=None, body=b''):
        return self.api.Response(status, headers or {}, body)

    def request(self, method, url, *, headers, body, timeout, max_response_bytes, follow_redirects):
        self.assert_safe(method, url, timeout, max_response_bytes, follow_redirects)
        self.calls.append((method, url, dict(headers)))
        if self.on_request: self.on_request(method, url, body)
        if self.override:
            response = self.override(method, url)
            if response is not None: return response
        parsed = urlsplit(url)
        path = parsed.path
        if parsed.netloc == 'pkg-containers.githubusercontent.com':
            assert method == 'GET' and headers == {} and body == b''
            assert url == self.config_redirect
            config_digest = json.loads(self.files['blobs/sha256/' + self.digest[7:]])['config']['digest']
            data = self.blobs[config_digest] if self.cdn_body is None else self.cdn_body
            return self.response(self.cdn_status, {'Content-Length': str(len(data)), 'Location': self.config_redirect}, data)
        if path in ('/v2/', '/v2/' + self.repository + '/tags/list'):
            self.assert_no_auth(headers)
            if path != '/v2/': assert parse_qs(parsed.query) == {'n': ['1']}
            return self.response(401, {'WWW-Authenticate': self.challenge})
        if path == '/token':
            expected = 'Basic ' + base64.b64encode(('fixture-actor:' + TOKEN).encode()).decode()
            assert headers['Authorization'] == expected
            assert parse_qs(parsed.query) == {'service': ['ghcr.io'], 'scope': [f'repository:{self.repository}:pull,push']}
            return self.response(200, {'Content-Type': 'application/json'}, json.dumps({'token': BEARER, 'expires_in': self.expires}).encode())
        assert headers['Authorization'] == 'Bearer ' + BEARER
        base = '/v2/' + self.repository
        if method == 'HEAD' and path.startswith(base + '/blobs/sha256:'):
            digest = path.rsplit('/', 1)[-1]
            if digest not in self.blobs: return self.response(404)
            return self.response(200, {'Docker-Content-Digest': digest, 'Content-Length': str(len(self.blobs[digest]))})
        if method == 'POST' and path == base + '/blobs/uploads/':
            assert body == b''
            return self.response(202, {'Location': self.location})
        if method == 'PUT' and path.startswith(base + '/blobs/uploads/'):
            if self.fail_put: raise RuntimeError('secret=' + TOKEN + ' location=' + self.location)
            assert parse_qs(parsed.query)['_state'] == [STATE]
            digest = parse_qs(parsed.query)['digest'][0]
            chunks = []
            while True:
                chunk = body.read(self.read_chunk)
                if not chunk: break
                chunks.append(chunk)
            data = b''.join(chunks)
            assert headers['Content-Length'] == str(len(data))
            assert digest == 'sha256:' + hashlib.sha256(data).hexdigest()
            self.blobs[digest] = data
            return self.response(201, {'Docker-Content-Digest': digest})
        if method == 'PUT' and path == base + '/manifests/' + self.digest:
            assert headers['Content-Type'] == image_build.OCI_MANIFEST
            assert isinstance(body, bytes)
            assert self.digest == 'sha256:' + hashlib.sha256(body).hexdigest()
            self.manifest = body
            return self.response(201, {'Docker-Content-Digest': self.digest})
        if method == 'GET' and path == base + '/manifests/' + self.digest:
            data = b'tampered' if self.tamper_subject else self.manifest
            return self.response(200, {'Docker-Content-Digest': self.digest, 'Content-Type': self.subject_type}, data)
        if method == 'GET' and path.startswith(base + '/blobs/sha256:'):
            digest = path.rsplit('/', 1)[-1]
            if self.config_redirect is not None:
                return self.response(307, {'Location': self.config_redirect})
            data = b'tampered-config' if self.tamper_config else self.blobs[digest]
            return self.response(200, {'Content-Type': 'application/octet-stream', 'Docker-Content-Digest': digest}, data)
        raise AssertionError('unexpected protocol method/path')

    def assert_safe(self, method, url, timeout, limit, redirects):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.netloc in ('ghcr.io', 'pkg-containers.githubusercontent.com')
        assert method in ('GET', 'HEAD', 'POST', 'PUT')
        assert 0 < timeout <= 120 and limit <= 256 * 1024 and redirects is False
    def assert_no_auth(self, headers): assert 'Authorization' not in headers


class RegistryIOTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-registry-io-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'image.oci.tar'
        manifest, public, controller = FIXTURE.inputs()
        self.task = image_build.build_tasks(manifest, public, controller, 'planning')[0]
        self.files, self.digest = FIXTURE.archive(self.path, self.task['labels'])
        self.repository = 'xynes-studio/apex/' + self.task['workload']
        self.reference = 'ghcr.io/' + self.repository + '@' + self.digest
        self.api = module()
        self.clock = Clock()
        self.registry = Registry(self.api, self.repository, self.files, self.digest, self.clock)
        self.credential = self.api.GitHubPublisherToken('fixture-actor', TOKEN)
        self.diagnostics = io.StringIO()
        capture = contextlib.redirect_stderr(self.diagnostics)
        capture.__enter__(); self.addCleanup(capture.__exit__, None, None, None)

    def publish(self, reference=None, policy=None):
        return self.api.publish_oci(self.path, self.task, reference or self.reference,
                                    policy=copy.deepcopy(policy or registry_policy.EXPECTED), credential=self.credential,
                                    transport=self.registry, monotonic=self.clock)

    def refused(self, action=None):
        with self.assertRaises(self.api.RegistryError) as caught: (action or self.publish)()
        text = str(caught.exception) + repr(caught.exception)
        for secret in (TOKEN, BEARER, STATE): self.assertNotIn(secret, text)

    def test_exact_bytes_publish_and_registry_subject_config_verified_without_approval(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured): result = self.publish()
        self.assertEqual(captured.getvalue(), '')
        self.assertEqual(result['image'], self.reference)
        self.assertEqual(result['manifest_digest'], self.digest)
        self.assertTrue(result['published']); self.assertTrue(result['registry_content_verified'])
        self.assertFalse(result['attested']); self.assertFalse(result['trust_verified']); self.assertFalse(result['deployable'])
        self.assertEqual(self.registry.manifest, self.files['blobs/sha256/' + self.digest[7:]])
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertNotIn(BEARER, json.dumps(result)); self.assertNotIn(STATE, json.dumps(result))

    def test_repository_probe_handles_observed_ghcr_root_placeholder_without_relaxing_scope(self):
        endpoint = '/v2/' + self.repository + '/tags/list'
        def observed(method, url):
            parsed = urlsplit(url)
            if parsed.path == '/v2/':
                return self.registry.response(401, {'WWW-Authenticate': self.registry.challenge + ',scope="repository:user/image:pull"'})
            if parsed.path == endpoint:
                return self.registry.response(401, {'WWW-Authenticate': self.registry.challenge + ',scope="repository:' + self.repository + ':pull"'})
        self.registry.override = observed
        result = self.publish()
        self.assertTrue(result['registry_content_verified'])
        self.assertEqual(self.registry.calls[0], ('GET', 'https://ghcr.io' + endpoint + '?n=1', {}))
        self.assertFalse(any(urlsplit(url).path == '/v2/' for _, url, _ in self.registry.calls))
        token_calls = [(method, url, headers) for method, url, headers in self.registry.calls if urlsplit(url).path == '/token']
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(parse_qs(urlsplit(token_calls[0][1]).query), {'service': ['ghcr.io'], 'scope': ['repository:' + self.repository + ':pull,push']})

    def test_registry_diagnostics_use_only_fixed_phases_in_protocol_order(self):
        self.publish()
        events = [json.loads(line) for line in self.diagnostics.getvalue().splitlines()]
        manifest = json.loads(self.files['blobs/sha256/' + self.digest[7:]])
        count = len({b['digest'] for b in [manifest['config']] + manifest['layers']})
        phases = ['registry-challenge', 'registry-token'] + ['registry-blob-upload'] * count + ['registry-manifest-upload', 'registry-manifest-readback', 'registry-config-readback']
        self.assertEqual(events, [{'artifact_kind': 'apex-build-diagnostic', 'phase': phase, 'status': status} for phase in phases for status in ('start', 'success')])

    def test_registry_failure_diagnostics_never_echo_credentials_urls_or_exceptions(self):
        self.registry.fail_put = True
        self.refused()
        raw = self.diagnostics.getvalue()
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([(e['phase'], e['status']) for e in events[-2:]], [('registry-blob-upload', 'start'), ('registry-blob-upload', 'failure')])
        for private in (TOKEN, BEARER, STATE, self.registry.location, 'secret='):
            self.assertNotIn(private, raw)

    def test_foreign_challenge_diagnostic_fails_before_token_exchange(self):
        self.registry.challenge = 'Bearer realm="https://attacker.invalid/token",service="ghcr.io"'
        self.refused()
        events = [json.loads(line) for line in self.diagnostics.getvalue().splitlines()]
        self.assertEqual([(e['phase'], e['status']) for e in events], [('registry-challenge', 'start'), ('registry-challenge', 'failure')])
        self.assertEqual(len(self.registry.calls), 1)
        self.assertNotIn('attacker.invalid', self.diagnostics.getvalue())

    def test_ghcr_config_blob_redirect_downloads_once_without_any_credentials_or_extra_headers(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/ghcr1/blobs/config?signature=' + STATE
        try:
            result = self.publish()
        except self.api.RegistryError:
            self.fail('observed legitimate GHCR config redirect is unsupported')
        cdn = [(method, url, headers) for method, url, headers in self.registry.calls if urlsplit(url).netloc == 'pkg-containers.githubusercontent.com']
        self.assertEqual(cdn, [('GET', self.registry.config_redirect, {})])
        self.assertTrue(result['registry_content_verified'])
        self.assertFalse(result['trust_verified']); self.assertFalse(result['deployable'])
        self.assertNotIn(STATE, json.dumps(result))

    def test_config_redirect_rejects_foreign_ambiguous_or_unbounded_cdn_urls_before_download(self):
        for location in ['https://attacker.invalid/config?signature=' + STATE,
                         'http://pkg-containers.githubusercontent.com/config',
                         '//pkg-containers.githubusercontent.com/config',
                         'https://ghcr.io/config',
                         'https://pkg-containers.githubusercontent.com.attacker.invalid/config',
                         'https://PKG-CONTAINERS.GITHUBUSERCONTENT.COM/config',
                         'https://pkg-containers.githubusercontent.com:443/config',
                         'https://user:password@pkg-containers.githubusercontent.com/config',
                         'https://pkg-containers.githubusercontent.com/config#fragment',
                         'https://pkg-containers.githubusercontent.com/config#',
                         'https://pkg-containers.githubusercontent.com/?signature=' + 'x' * 8192,
                         'https://pkg-containers.githubusercontent.com/config\nHeader:injected',
                         'https://pkg-containers.githubusercontent.com/config%0d%0aHeader',
                         'https://pkg-containers.githubusercontent.com',
                         'https://pkg-containers.githubusercontent.com//ambiguous/config']:
            with self.subTest(location_category=location.split('://')[0]):
                self.registry.calls = []
                self.registry.config_redirect = location
                self.refused()
                self.assertFalse(any(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _, url, _ in self.registry.calls))

    def test_cdn_download_never_follows_second_redirect_retries_or_challenges(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        for status in [301, 302, 303, 307, 308, 401, 403, 429, 500]:
            self.registry.calls = []
            self.registry.cdn_status = status
            self.refused()
            cdn = [(method, url, headers) for method, url, headers in self.registry.calls if urlsplit(url).netloc == 'pkg-containers.githubusercontent.com']
            self.assertEqual(cdn, [('GET', self.registry.config_redirect, {})])

    def test_only_config_307_with_location_can_enter_cdn_exception(self):
        location = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        for status, header in [(301, {'Location': location}), (302, {'Location': location}),
                               (303, {'Location': location}), (308, {'Location': location}),
                               (307, {})]:
            self.registry.calls = []
            self.registry.override = lambda m,u,status=status,header=header: self.registry.response(status,header) if m=='GET' and '/blobs/sha256:' in urlsplit(u).path else None
            self.refused()
            self.assertFalse(any(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _,url,_ in self.registry.calls))

    def test_expired_token_between_registry_redirect_and_cdn_prevents_download(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        def expire(method,url,body):
            if method=='GET' and urlsplit(url).netloc=='ghcr.io' and '/blobs/sha256:' in urlsplit(url).path:
                self.clock.value = 301
        self.registry.on_request = expire
        self.refused()
        self.assertFalse(any(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _,url,_ in self.registry.calls))

    def test_cdn_config_content_and_response_bound_remain_exact(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        for data in [b'tampered-config', b'x' * (256 * 1024 + 1)]:
            self.registry.cdn_body = data
            self.refused()

    def test_cdn_hop_reuses_token_deadline_and_archive_mutation_checks(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        for change in ['deadline', 'mutation']:
            self.registry.calls = []
            self.clock.value = 0
            before = self.path.read_bytes()
            def intervene(method, url, body):
                if urlsplit(url).netloc == 'pkg-containers.githubusercontent.com':
                    if change == 'deadline':
                        self.clock.value = 301
                    else:
                        with self.path.open('r+b') as file:
                            file.seek(1024);file.write(b'mutation')
            self.registry.on_request = intervene
            self.refused()
            self.path.write_bytes(before)
            self.assertEqual(sum(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _, url, _ in self.registry.calls), 1)

    def test_cdn_exception_text_or_signed_url_never_appears_in_failure(self):
        self.registry.config_redirect = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        def explode(method, url, body):
            if urlsplit(url).netloc == 'pkg-containers.githubusercontent.com':
                raise RuntimeError(TOKEN + BEARER + self.registry.config_redirect)
        self.registry.on_request = explode
        self.refused()
        self.assertEqual(sum(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _, url, _ in self.registry.calls), 1)

    def test_manifest_and_write_redirects_to_valid_cdn_remain_refused(self):
        location = 'https://pkg-containers.githubusercontent.com/config?signature=' + STATE
        for method, suffix in [('GET', '/manifests/'), ('PUT', '/manifests/'), ('PUT', '/blobs/uploads/')]:
            self.registry.calls = []
            self.registry.blobs = {}
            self.registry.override = lambda m,u,method=method,suffix=suffix: self.registry.response(307,{'Location':location}) if m==method and suffix in urlsplit(u).path else None
            self.refused()
            self.assertFalse(any(urlsplit(url).netloc == 'pkg-containers.githubusercontent.com' for _, url, _ in self.registry.calls))

    def test_wrong_namespace_local_id_tag_or_wrong_manifest_refused_before_credentials(self):
        for reference in ['sha256:' + 'a' * 64, 'ghcr.io/attacker/apex/xynes-gateway@' + self.digest,
                          'ghcr.io/' + self.repository + ':latest', self.reference.replace(self.digest, 'sha256:' + 'a' * 64),
                          self.reference.replace('xynes-gateway', 'xynes-accounts-service')]:
            self.refused(lambda: self.publish(reference)); self.assertEqual(self.registry.calls, [])

    def test_wrong_registry_policy_and_unknown_workload_refused(self):
        policy = copy.deepcopy(registry_policy.EXPECTED); policy['visibility'] = 'public'
        self.refused(lambda: self.publish(policy=policy)); self.assertEqual(self.registry.calls, [])
        self.task['workload'] = 'attacker'; self.refused(); self.assertEqual(self.registry.calls, [])

    def test_malformed_or_foreign_auth_challenge_never_receives_publisher_token(self):
        for challenge in ['Bearer realm="https://attacker.invalid/token",service="ghcr.io"',
                          'Bearer realm="http://ghcr.io/token",service="ghcr.io"',
                          'Bearer realm="https://ghcr.io/token?redirect=evil",service="ghcr.io"',
                          'Bearer realm="https://ghcr.io/token",service="other"',
                          'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:attacker/app:push"',
                          'Bearer realm="https://ghcr.io/token",realm="https://ghcr.io/token",service="ghcr.io"']:
            self.registry.calls = []; self.registry.challenge = challenge; self.refused()
            self.assertEqual(len(self.registry.calls), 1)

    def test_same_repository_pull_challenge_uses_only_the_fixed_publisher_repo_scope(self):
        self.registry.challenge += ',scope="repository:' + self.repository + ':pull"'
        self.publish()
        self.assertEqual(parse_qs(urlsplit(self.registry.calls[1][1]).query)['scope'], ['repository:' + self.repository + ':pull,push'])

    def test_no_redirects_at_any_registry_or_token_operation(self):
        for method, suffix in [('GET', '/v2/'), ('GET', '/token'), ('HEAD', '/blobs/'), ('POST', '/blobs/uploads/'), ('PUT', '/blobs/uploads/')]:
            self.registry.calls = []
            self.registry.override = lambda m, u, method=method, suffix=suffix: self.registry.response(307, {'Location': 'https://attacker.invalid/' + TOKEN}) if m == method and suffix in urlsplit(u).path else None
            self.refused()
            self.assertTrue(all(urlsplit(u).netloc == 'ghcr.io' for _, u, _ in self.registry.calls))

    def test_upload_location_host_protocol_path_and_query_boundaries(self):
        for location in ['https://attacker.invalid/upload', 'http://ghcr.io/v2/' + self.repository + '/blobs/uploads/id',
                         '//ghcr.io/v2/' + self.repository + '/blobs/uploads/id',
                         'https://ghcr.io/v2/attacker/app/blobs/uploads/id',
                         'https://ghcr.io/v2/' + self.repository + '/blobs/uploads/%2e%2e',
                         self.registry.location + '&redirect=evil', self.registry.location + '&_state=duplicate',
                         self.registry.location + '&digest=sha256%3A' + 'a' * 64,
                         self.registry.location + '#fragment']:
            self.registry.calls = []; self.registry.location = location; self.refused()
            self.assertFalse(any(m == 'PUT' for m, _, _ in self.registry.calls))

    def test_relative_same_repository_upload_location_is_supported(self):
        self.registry.location = urlsplit(self.registry.location).path + '?_state=' + STATE
        self.publish()

    def test_put_network_failure_is_not_retried_or_echoed(self):
        self.registry.fail_put = True; self.refused()
        self.assertEqual(sum(m == 'PUT' for m, _, _ in self.registry.calls), 1)

    def test_existing_blob_head_digest_and_size_must_match(self):
        for headers in [{'Docker-Content-Digest': 'sha256:' + 'a' * 64}, {'Content-Length': '999999'}]:
            self.registry.calls = []
            self.registry.override = lambda m, u, h=headers: self.registry.response(200, h) if m == 'HEAD' else None
            self.refused(); self.assertFalse(any(m in ('POST', 'PUT') for m, _, _ in self.registry.calls))

    def test_postpublish_manifest_and_config_tampering_and_wrong_media_refused(self):
        for field, value in [('tamper_subject', True), ('tamper_config', True), ('subject_type', 'application/vnd.docker.distribution.manifest.v2+json')]:
            setattr(self.registry, field, value); self.refused(); setattr(self.registry, field, False if isinstance(value, bool) else image_build.OCI_MANIFEST)

    def test_token_expiry_and_total_deadline_fail_before_next_operation(self):
        self.registry.expires = 1
        self.registry.on_request = lambda m, u, b: setattr(self.clock, 'value', 2) if m == 'HEAD' else None
        self.refused(); self.assertFalse(any(m == 'POST' for m, _, _ in self.registry.calls))

    def test_long_registry_expiry_is_clamped_to_short_local_use(self):
        self.registry.expires = 3600
        self.registry.on_request = lambda m, u, b: setattr(self.clock, 'value', 301) if m == 'HEAD' else None
        self.refused()

    def test_oversize_token_response_and_invalid_expiry_refused(self):
        for body in [b'x' * (64 * 1024 + 1), json.dumps({'token': BEARER, 'expires_in': True}).encode(), json.dumps({'token': BEARER, 'expires_in': 0}).encode()]:
            self.registry.override = lambda m, u, b=body: self.registry.response(200, {'Content-Type': 'application/json'}, b) if urlsplit(u).path == '/token' else None
            self.refused()

    def test_archive_mutation_after_inspection_prevents_payload_upload(self):
        def mutate(method, url, body):
            if urlsplit(url).path == '/token':
                with self.path.open('r+b') as file: file.seek(1024); file.write(b'changed')
        self.registry.on_request = mutate; self.refused()
        self.assertFalse(any(m in ('POST', 'PUT') for m, _, _ in self.registry.calls))

    def test_upload_stream_requires_full_consumption_and_verified_hash(self):
        self.registry.override = lambda m, u: self.registry.response(201, {'Docker-Content-Digest': self.digest}) if m == 'PUT' and '/blobs/uploads/' in u else None
        self.refused()

    def test_existing_verified_blobs_are_not_uploaded_again(self):
        image = json.loads(self.files['blobs/sha256/' + self.digest[7:]])
        for item in [image['config']] + image['layers']:
            self.registry.blobs[item['digest']] = self.files['blobs/sha256/' + item['digest'][7:]]
        self.publish()
        self.assertFalse(any(m == 'POST' for m, _, _ in self.registry.calls))
        self.assertEqual(sum(m == 'PUT' for m, _, _ in self.registry.calls), 1)

    def test_empty_upload_success_response_length_is_not_blob_size(self):
        original = self.registry.request
        def request(method, url, **kwargs):
            result = original(method, url, **kwargs)
            if method == 'PUT' and '/blobs/uploads/' in url:
                return self.registry.response(201, {**result.headers, 'Content-Length': '0'})
            return result
        self.registry.request = request
        self.assertTrue(self.publish()['registry_content_verified'])

    def test_server_digest_mismatch_on_uploaded_blob_refused(self):
        # Consume the streaming bytes fully, then return a mismatched server digest.
        original = self.registry.request
        def request(method, url, **kwargs):
            result = original(method, url, **kwargs)
            if method == 'PUT' and '/blobs/uploads/' in url:
                return self.registry.response(201, {'Docker-Content-Digest': 'sha256:' + 'a' * 64})
            return result
        self.registry.request = request
        self.refused()
        self.assertFalse(any(m == 'PUT' and '/manifests/' in u for m, u, _ in self.registry.calls))

    def test_local_oci_platform_or_label_tamper_refused_before_credentials(self):
        FIXTURE.archive(self.path, self.task['labels'], architecture='arm64')
        self.refused(); self.assertEqual(self.registry.calls, [])

    def test_stream_mutation_is_detected_even_if_timestamp_is_restored(self):
        before = self.path.stat()
        def mutate(method, url, body):
            if method == 'PUT' and '/blobs/uploads/' in url:
                with self.path.open('r+b') as file:
                    file.seek(1024); file.write(b'changed')
                import os
                os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.registry.on_request = mutate
        self.refused()
        self.assertEqual(sum(m == 'PUT' for m, _, _ in self.registry.calls), 1)

    def test_unauthorized_rate_limit_and_server_error_never_retry(self):
        for status in [401, 403, 429, 500]:
            self.registry.calls = []
            self.registry.override = lambda m, u, status=status: self.registry.response(status, body=(TOKEN + STATE).encode()) if m == 'POST' else None
            self.refused()
            self.assertEqual(sum(m == 'POST' for m, _, _ in self.registry.calls), 1)

    def test_partial_or_oversized_read_requests_are_refused(self):
        for count in [-1, 2 * 1024 * 1024]:
            def attempt(method, url, body, count=count):
                if method == 'PUT' and '/blobs/uploads/' in url: body.read(count)
            self.registry.on_request = attempt
            self.refused()
            self.registry.calls = []

    def test_response_case_duplicate_headers_and_conflicting_tokens_are_refused(self):
        for headers, body in [({'Content-Type': 'application/json', 'content-type': 'application/json'}, {'token': BEARER}),
                              ({'Content-Type': 'application/json'}, {'token': BEARER, 'access_token': 'different-token-123456789'})]:
            self.registry.override = lambda m, u, h=headers, b=body: self.registry.response(200, h, json.dumps(b).encode()) if urlsplit(u).path == '/token' else None
            self.refused()


    def test_task_environment_cannot_echo_unvalidated_values_into_receipts(self):
        self.task['environment'] = TOKEN
        self.refused()
        self.assertEqual(self.registry.calls, [])

    def test_transport_cannot_accept_self_minted_extra_task_labels(self):
        self.task['labels']['approval_verified'] = 'true'
        self.files, self.digest = FIXTURE.archive(self.path, self.task['labels'])
        self.reference = 'ghcr.io/' + self.repository + '@' + self.digest
        self.registry.digest = self.digest
        self.registry.files = self.files
        self.refused()
        self.assertEqual(self.registry.calls, [])

    def test_frontend_stage_and_native_workload_bindings_publish_exactly(self):
        manifest, public, controller = FIXTURE.inputs()
        tasks = image_build.build_tasks(manifest, public, controller, 'planning')
        for task in [t for t in tasks if t['workload'] in ('archive-scanner', 'libreoffice') or t['environment'] is not None]:
            self.task = task
            self.files, self.digest = FIXTURE.archive(self.path, task['labels'])
            self.repository = 'xynes-studio/apex/' + task['workload']
            self.reference = 'ghcr.io/' + self.repository + '@' + self.digest
            self.registry = Registry(self.api, self.repository, self.files, self.digest, self.clock)
            result = self.publish()
            self.assertEqual(result['environment'], task['environment'])
            self.assertEqual(result['source_commit'], task['source_commit'])
            self.assertFalse(result['deployable'])

    def test_credentials_and_response_representations_do_not_echo_secrets(self):
        self.assertNotIn(TOKEN, repr(self.credential))
        self.assertNotIn(STATE, repr(self.registry.response(202, {'Location': self.registry.location})))
        for actor, token in [('actor\nAuthorization: evil', TOKEN), ('actor', TOKEN + '\n')]:
            self.refused(lambda: self.api.publish_oci(self.path, self.task, self.reference, policy=registry_policy.EXPECTED,
                           credential=self.api.GitHubPublisherToken(actor, token), transport=self.registry, monotonic=self.clock))


if __name__ == '__main__': unittest.main()
