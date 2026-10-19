#!/usr/bin/env python3
"""Injected GHCR OCI publishing transport; no CLI, admission, credential read or retry."""
import base64
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tarfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

try:
    from . import image_build, registry_policy, release_manifest as records
except ImportError:
    import image_build
    import registry_policy
    import release_manifest as records

ORIGIN = 'https://ghcr.io'
CONFIG_CDN_HOST = 'pkg-containers.githubusercontent.com'
MAX_SIGNED_URL = 8192
MAX_REQUESTS = 512
TOTAL_SECONDS = 300
TOKEN_SECONDS = 300
TOKEN_RESPONSE_BYTES = 64 * 1024
RESPONSE_BYTES = 256 * 1024
WORKLOADS = image_build.SHARED_BUILD + image_build.source_context.APPS
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9._=/\-]{16,16384}')


class RegistryError(ValueError):
    """Never attach request URLs, challenge/token/body bytes or underlying exceptions."""


def require(condition):
    if not condition:
        raise RegistryError('Registry transport refused: unsafe, incomplete or mismatched operation')


@dataclass(frozen=True, repr=False)
class GitHubPublisherToken:
    """In-memory GITHUB_TOKEN from the protected publishing job; not an approval."""
    actor: str
    token: str


@dataclass(frozen=True, repr=False)
class Response:
    status: int
    headers: dict
    body: bytes = b''


def signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def headers(response):
    require(isinstance(response, Response) and type(response.status) is int and isinstance(response.headers, dict))
    result = {}
    for name, value in response.headers.items():
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}', name)
                and isinstance(value, str) and len(value) <= 8192
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))
        key = name.lower()
        require(key not in result)
        result[key] = value
    return result


def check_metadata(values, digest, size, mandatory=False):
    if mandatory or 'docker-content-digest' in values:
        require(values.get('docker-content-digest') == digest)
    if 'content-length' in values:
        value = values['content-length']
        require(re.fullmatch(r'[0-9]{1,12}', value) and int(value) == size)


def validate_challenge(value, repository):
    require(isinstance(value, str) and value.startswith('Bearer '))
    raw = value[len('Bearer '):]
    fields = {}
    while raw:
        match = re.match(r'\s*([a-z_]+)="([^"\\\r\n]*)"\s*(?:,\s*|$)', raw)
        require(match is not None and match.end() > 0)
        key, item = match.groups()
        require(key not in fields and key in ('realm', 'service', 'scope'))
        fields[key] = item
        raw = raw[match.end():]
    require(fields.get('realm') == ORIGIN + '/token' and fields.get('service') == 'ghcr.io')
    if 'scope' in fields:
        parts = fields['scope'].split(':')
        require(len(parts) == 3 and parts[0] == 'repository' and parts[1] == repository)
        actions = parts[2].split(',')
        require(actions and len(actions) == len(set(actions)) and set(actions) <= {'pull', 'push'})


def upload_url(location, repository, digest):
    require(isinstance(location, str) and 0 < len(location) <= 4096
            and not any(ord(c) < 33 or ord(c) > 126 for c in location))
    if location.startswith('/'):
        require(not location.startswith('//'))
        location = ORIGIN + location
    parsed = urlsplit(location)
    require(parsed.scheme == 'https' and parsed.netloc == 'ghcr.io' and not parsed.fragment
            and not parsed.username and not parsed.password)
    prefix = '/v2/' + repository + '/blobs/uploads/'
    require(parsed.path.startswith(prefix))
    identifier = parsed.path[len(prefix):]
    require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}', identifier) and identifier not in ('.', '..'))
    values = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    require(len(values) <= 1)
    if values:
        key, value = values[0]
        require(key == '_state' and 0 < len(value) <= 2048
                and re.fullmatch(r'[A-Za-z0-9._=-]+', value))
    query = parsed.query + ('&' if parsed.query else '') + urlencode({'digest': digest})
    return ORIGIN + parsed.path + '?' + query


class Session:
    def __init__(self, transport, monotonic, check_file):
        require(callable(getattr(transport, 'request', None)) and callable(monotonic))
        self.transport, self.clock, self.check_file = transport, monotonic, check_file
        self.deadline = self.now() + TOTAL_SECONDS
        self.token_deadline = self.deadline
        self.bearer = None
        self.repository = None
        self.requests = 0

    def now(self):
        value = self.clock()
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
        return value

    def remaining(self):
        self.check_file()
        value = min(self.deadline, self.token_deadline) - self.now()
        require(value > 0)
        return value

    def _exchange(self, method, url, values, body, response_limit, upload=False):
        # This private operation never adds credentials or follows a redirect.
        # Its callers separately bind either the registry or the sole config CDN.
        self.requests += 1
        require(self.requests <= MAX_REQUESTS)
        timeout = min(120 if upload else 30, self.remaining())
        try:
            response = self.transport.request(method, url, headers=values, body=body,
                                              timeout=timeout, max_response_bytes=response_limit,
                                              follow_redirects=False)
        except Exception:
            raise RegistryError('Registry transport refused: request failed or incomplete') from None
        self.remaining()
        metadata = headers(response)
        require(isinstance(response.body, bytes) and len(response.body) <= response_limit)
        return response, metadata

    def request(self, method, url, *, body=b'', extra=None, response_limit=RESPONSE_BYTES, upload=False):
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.netloc == 'ghcr.io' and not parsed.fragment
                and not parsed.username and not parsed.password
                and method in ('GET', 'HEAD', 'POST', 'PUT'))
        values = dict(extra or {})
        if self.bearer is not None:
            require('Authorization' not in values)
            values['Authorization'] = 'Bearer ' + self.bearer
        response, metadata = self._exchange(method, url, values, body, response_limit, upload)
        require(not 300 <= response.status < 400)
        return response, metadata

    def config_blob(self, repository, digest):
        # Only a verified config descriptor from this session's authenticated
        # workload may enter the one-hop credential-free download exception.
        require(repository == self.repository and self.bearer is not None
                and repository.startswith('xynes-studio/apex/')
                and repository.removeprefix('xynes-studio/apex/') in WORKLOADS
                and isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest))
        url = ORIGIN + '/v2/' + repository + '/blobs/' + digest
        response, metadata = self._exchange('GET', url, {'Authorization': 'Bearer ' + self.bearer},
                                             b'', RESPONSE_BYTES)
        if response.status == 307:
            location = metadata.get('location')
            require(isinstance(location, str) and 0 < len(location) <= MAX_SIGNED_URL
                    and all(33 <= ord(c) <= 126 for c in location) and '#' not in location
                    and not re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)', location, re.I))
            target = urlsplit(location)
            require(target.scheme == 'https' and target.netloc == CONFIG_CDN_HOST
                    and target.hostname == CONFIG_CDN_HOST and target.port is None
                    and not target.username and not target.password and not target.fragment
                    and target.path.startswith('/') and not target.path.startswith('//'))
            # Never copy Authorization, Basic credentials, cookies, Accept or
            # registry response headers to the CDN. The signed URL stays in memory.
            response, metadata = self._exchange('GET', location, {}, b'', RESPONSE_BYTES)
            require(response.status == 200)  # no second hop, retry or auth challenge
        require(not 300 <= response.status < 400)
        return response, metadata

    def authenticate(self, repository, credential):
        with image_build.diagnostic('registry-challenge'):
            probe, metadata = self.request('GET', ORIGIN + '/v2/' + repository + '/tags/list?n=1', response_limit=TOKEN_RESPONSE_BYTES)
            require(probe.status in (200, 401))
            if probe.status == 401:
                validate_challenge(metadata.get('www-authenticate'), repository)
        with image_build.diagnostic('registry-token'):
            encoded = base64.b64encode((credential.actor + ':' + credential.token).encode('ascii')).decode('ascii')
            query = urlencode({'service': 'ghcr.io', 'scope': 'repository:' + repository + ':pull,push'})
            response, metadata = self.request('GET', ORIGIN + '/token?' + query,
                                              extra={'Authorization': 'Basic ' + encoded}, response_limit=TOKEN_RESPONSE_BYTES)
            require(response.status == 200 and metadata.get('content-type', '').split(';')[0].strip() == 'application/json')
            payload = records.strict_json(response.body.decode('utf8'))
            require(isinstance(payload, dict) and set(payload) <= {'token', 'access_token', 'expires_in', 'issued_at'}
                    and ('token' in payload or 'access_token' in payload))
            token = payload.get('token', payload.get('access_token'))
            require(isinstance(token, str) and TOKEN_PATTERN.fullmatch(token)
                    and ('access_token' not in payload or payload['access_token'] == token))
            expires = payload.get('expires_in', 60)
            require(type(expires) is int and 0 < expires <= 86400)
            self.token_deadline = min(self.deadline, self.now() + min(expires, TOKEN_SECONDS))
            self.bearer = token
            self.repository = repository


class VerifiedBlobReader:
    """Only bounded raw blob reads; never extracts a filesystem or decompresses data."""
    def __init__(self, stream, descriptor, session):
        self.stream, self.descriptor, self.session = stream, descriptor, session
        self.remaining_bytes = descriptor['size']
        self.digest = hashlib.sha256()
        self.done = False

    def read(self, size):
        require(type(size) is int and 0 < size <= 1024 * 1024)
        self.session.remaining()
        if self.remaining_bytes == 0:
            require(self.stream.read(1) == b'')
            require('sha256:' + self.digest.hexdigest() == self.descriptor['digest'])
            self.done = True
            return b''
        data = self.stream.read(min(size, self.remaining_bytes))
        require(isinstance(data, bytes) and 0 < len(data) <= self.remaining_bytes)
        self.remaining_bytes -= len(data)
        self.digest.update(data)
        return data

    def verify_complete(self):
        require(self.remaining_bytes == 0)
        self.read(1)
        require(self.done)


def validate_task(task, policy):
    require(isinstance(policy, dict) and type(policy.get('schema_version')) is int and policy == registry_policy.EXPECTED)
    require(isinstance(task, dict) and task.get('workload') in WORKLOADS
            and task.get('platform') == 'linux/amd64' and task.get('target') == 'prod')
    require(task.get('environment') in records.STAGES if task['workload'] in image_build.source_context.APPS else task.get('environment') is None)
    for key, length in [('source_commit', 40), ('recipe_sha256', 64), ('build_inputs_sha256', 64)]:
        require(isinstance(task.get(key), str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', task[key]))
    labels = task.get('labels')
    if task['workload'] in image_build.source_context.PROCESSOR_FILES and isinstance(labels, dict) and 'com.xynes.apex.processor-content-sha256' in labels:
        require(set(labels) == {'org.opencontainers.image.source', 'com.xynes.apex.processor-content-sha256'}
                and labels['org.opencontainers.image.source'] == 'https://github.com/' + policy['package_repository'])
        digest = labels['com.xynes.apex.processor-content-sha256']
        require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest)
                and task.get('build_args') == {'SOURCE_DATE_EPOCH': str(946684800 + int(digest[:8], 16) % 315360000)})
        # Actual publish_batch tasks are independently rebuilt from the approved
        # manifest, including its verified Git file inventory/content identity.
        return
    if task['workload'] in image_build.source_context.PROCESSOR_FILES:
        require(image_build.namespace_task(task))
    require(isinstance(labels, dict) and set(labels) == {'org.opencontainers.image.source', 'org.opencontainers.image.revision',
            'com.xynes.apex.controller-commit', 'com.xynes.apex.run-id', 'com.xynes.apex.run-attempt', 'com.xynes.apex.build-inputs-sha256'})
    require(labels.get('org.opencontainers.image.source') == 'https://github.com/' + policy['package_repository']
            and labels.get('org.opencontainers.image.revision') == task['source_commit']
            and labels.get('com.xynes.apex.build-inputs-sha256') == task['build_inputs_sha256'])
    require(isinstance(labels.get('com.xynes.apex.controller-commit'), str)
            and re.fullmatch(r'[a-f0-9]{40}', labels['com.xynes.apex.controller-commit']))
    for key, maximum in [('com.xynes.apex.run-id', 9007199254740991), ('com.xynes.apex.run-attempt', 10000)]:
        value = labels.get(key)
        require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,15}', value) and int(value) <= maximum)


def _publish(path, task, expected_image, policy, credential, transport, monotonic):
    validate_task(task, policy)
    require(isinstance(credential, GitHubPublisherToken)
            and isinstance(credential.actor, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?', credential.actor)
            and isinstance(credential.token, str) and TOKEN_PATTERN.fullmatch(credential.token))
    path = Path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    local = image_build.inspect_oci(path, task)
    repository = policy['registry_prefix'].removeprefix('ghcr.io/') + '/' + task['workload']
    require(expected_image == 'ghcr.io/' + repository + '@' + local['manifest_digest'])
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as file:
        def check_file():
            require(signature(before) == signature(path.lstat()) == signature(os.fstat(file.fileno())))
        check_file()
        file.seek(0)
        actual_hash, actual_size = image_build.hash_stream(file, image_build.MAX_ARCHIVE)
        require(actual_hash == local['archive_sha256'] and actual_size == local['archive_size'])
        check_file()
        file.seek(0)
        with tarfile.open(fileobj=image_build.BoundedReader(file), mode='r:') as archive:
            manifest_member = archive.getmember('blobs/sha256/' + local['manifest_digest'][7:])
            manifest_bytes = archive.extractfile(manifest_member).read(RESPONSE_BYTES + 1)
            require(len(manifest_bytes) <= RESPONSE_BYTES and 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest() == local['manifest_digest'])
            manifest = records.strict_json(manifest_bytes.decode('utf8'))
            config_descriptor = manifest['config']
            config_stream = archive.extractfile(archive.getmember('blobs/sha256/' + config_descriptor['digest'][7:]))
            config_bytes = config_stream.read(RESPONSE_BYTES + 1)
            require(len(config_bytes) <= RESPONSE_BYTES and 'sha256:' + hashlib.sha256(config_bytes).hexdigest() == local['config_digest'])
            session = Session(transport, monotonic, check_file)
            session.authenticate(repository, credential)
            base = ORIGIN + '/v2/' + repository
            seen = set()
            for blob in [config_descriptor] + manifest['layers']:
                if blob['digest'] in seen:
                    continue
                seen.add(blob['digest'])
                with image_build.diagnostic('registry-blob-upload'):
                    response, metadata = session.request('HEAD', base + '/blobs/' + blob['digest'])
                    if response.status == 200:
                        check_metadata(metadata, blob['digest'], blob['size'])
                        continue
                    require(response.status == 404)
                    started, metadata = session.request('POST', base + '/blobs/uploads/', extra={'Content-Length': '0'})
                    require(started.status == 202)
                    location = upload_url(metadata.get('location'), repository, blob['digest'])
                    stream = archive.extractfile(archive.getmember('blobs/sha256/' + blob['digest'][7:]))
                    try:
                        reader = VerifiedBlobReader(stream, blob, session)
                        uploaded, metadata = session.request('PUT', location, body=reader, upload=True,
                             extra={'Content-Type': 'application/octet-stream', 'Content-Length': str(blob['size'])})
                        reader.verify_complete()
                        require(uploaded.status == 201)
                        # PUT Content-Length measures its response body, unlike blob HEAD.
                        require(metadata.get('docker-content-digest') == blob['digest'])
                    finally:
                        stream.close()
            subject_url = base + '/manifests/' + local['manifest_digest']
            with image_build.diagnostic('registry-manifest-upload'):
                uploaded, metadata = session.request('PUT', subject_url, body=manifest_bytes,
                     extra={'Content-Type': image_build.OCI_MANIFEST, 'Content-Length': str(len(manifest_bytes))})
                require(uploaded.status == 201 and metadata.get('docker-content-digest') == local['manifest_digest'])
            with image_build.diagnostic('registry-manifest-readback'):
                subject, metadata = session.request('GET', subject_url, extra={'Accept': image_build.OCI_MANIFEST})
                require(subject.status == 200 and metadata.get('content-type', '').split(';')[0].strip() == image_build.OCI_MANIFEST
                        and subject.body == manifest_bytes)
                check_metadata(metadata, local['manifest_digest'], len(manifest_bytes), mandatory=True)
            with image_build.diagnostic('registry-config-readback'):
                remote_config, metadata = session.config_blob(repository, local['config_digest'])
                require(remote_config.status == 200 and remote_config.body == config_bytes)
                check_metadata(metadata, local['config_digest'], len(config_bytes))
                config = records.strict_json(remote_config.body.decode('utf8'))
                require(config.get('os') == 'linux' and config.get('architecture') == 'amd64'
                        and all(config['config']['Labels'].get(k) == v for k, v in task['labels'].items()))
            check_file()
    return {**local, 'image': expected_image, 'published': True, 'registry_content_verified': True,
            'visibility_verified': False, 'attested': False, 'trust_verified': False, 'deployable': False}


def publish_oci(path, task, expected_image, *, policy, credential, transport, monotonic=time.monotonic):
    """Caller admits the job; injected HTTPS transport must bound reads and never retry."""
    try:
        return _publish(path, task, expected_image, policy, credential, transport, monotonic)
    except Exception:
        raise RegistryError('Registry transport refused: unsafe, incomplete or mismatched operation') from None
