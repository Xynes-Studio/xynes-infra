#!/usr/bin/env python3
"""Bounded, verified, read-only GitHub HTTP; no credential reads or diagnostics."""
import http.client
import json
import math
import re
import ssl
import time
from urllib.parse import unquote, urlsplit

try:
    from . import admission, registry_io
except ImportError:
    import admission
    import registry_io

MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 15
API_VERSION = '2022-11-28'
USER_AGENT = 'xynes-apex-controller'


class GitHubReadError(ValueError):
    """Every refusal is independent of token, URL, provider data and cause."""


def require(value):
    if not value:
        raise GitHubReadError('Bounded GitHub read refused')


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result); result[key] = value
        return result
    def floating(value):
        number = float(value); require(math.isfinite(number)); return number
    value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_float=floating,
                       parse_constant=lambda _: require(False))
    require(isinstance(value, (dict, list)))
    pending = [(value, 0)]; nodes = 0
    while pending:
        item, depth = pending.pop(); nodes += 1
        require(nodes <= 100000 and depth <= 20)
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def allowed_path(path):
    require(isinstance(path, str) and 0 < len(path) <= 4096 and path.startswith('/')
            and not path.startswith('//') and all(33 <= ord(c) <= 126 for c in path))
    require(not re.search(r'%(?![A-Fa-f0-9]{2})', path))
    parsed = urlsplit(path)
    require(not parsed.scheme and not parsed.netloc and not parsed.fragment)
    decoded = unquote(path, errors='strict')
    require(all(33 <= ord(c) <= 126 for c in decoded) and '%' not in decoded and '\\' not in decoded)
    require(all(part not in ('.', '..') for part in unquote(parsed.path).split('/')))
    if parsed.path == admission.PREFIX or parsed.path.startswith(admission.PREFIX + '/'):
        return
    require(not parsed.query)
    if parsed.path == '/users/' + admission.REVIEWER:
        return
    package = re.fullmatch(r'/orgs/Xynes-Studio/packages/container/apex%2[Ff]([a-z0-9-]+)', parsed.path)
    require(package and package.group(1) in registry_io.WORKLOADS)


class DeadlineReader:
    """Bound header lines and chunk framing between individual buffered reads."""
    def __init__(self, buffered, sock, deadline):
        self._buffered, self._sock, self._deadline = buffered, sock, deadline

    def _remaining(self):
        remaining = self._deadline - time.monotonic(); require(remaining > 0)
        self._sock.settimeout(remaining)

    def read1(self, size):
        require(type(size) is int and 0 <= size <= 65537)
        self._remaining()
        data = self._buffered.read1(size)
        require(isinstance(data, bytes) and len(data) <= size)
        self._remaining()
        return data

    def read(self, size):
        # HTTPResponse uses read() for exact chunk delimiters. Never delegate a
        # multi-recv buffered read whose inactivity timeout could reset forever.
        require(type(size) is int and 0 <= size <= 65537)
        chunks, received = [], 0
        while received < size:
            data = self.read1(size - received)
            if not data:
                break
            chunks.append(data); received += len(data)
        return b''.join(chunks)

    def readline(self, size):
        require(type(size) is int and 0 <= size <= 65537)
        line = bytearray()
        while len(line) < size:
            data = self.read1(1)
            if not data:
                break
            line.extend(data)
            if data == b'\n':
                break
        return bytes(line)

    def close(self):
        self._buffered.close()

    def flush(self):
        self._buffered.flush()


class GitHubReadAdapter:
    """A token remains in memory; injected factories are trusted transport boundaries."""
    def __init__(self, token, *, connection_factory=http.client.HTTPSConnection):
        require(isinstance(token, str) and registry_io.TOKEN_PATTERN.fullmatch(token)
                and callable(connection_factory))
        self._token = token
        self._factory = connection_factory

    def __repr__(self):
        return 'GitHubReadAdapter()'

    def read(self, path):
        return self._request('GET', path, b'')

    def package_metadata(self, workload):
        require(isinstance(workload,str) and workload in registry_io.WORKLOADS)
        return self._request('GET','/orgs/Xynes-Studio/packages/container/apex%2F'+workload,b'',allow_missing=True)

    def read_graphql(self, query):
        require(isinstance(query, str) and query == admission.NO_BYPASS_QUERY)
        return self._request('POST', '/graphql', json.dumps({'query': query}).encode())

    def _request(self, method, path, body, *, allow_missing=False):
        connection = None
        try:
            require(method in ('GET', 'POST'))
            require(type(allow_missing) is bool and (not allow_missing or method=='GET'
                    and path in {'/orgs/Xynes-Studio/packages/container/apex%2F'+name for name in registry_io.WORKLOADS}))
            if method == 'GET':
                allowed_path(path); require(body == b'')
            else:
                require(path == '/graphql' and body == json.dumps({'query': admission.NO_BYPASS_QUERY}).encode())
            context = ssl.create_default_context()
            require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED)
            deadline = time.monotonic() + TIMEOUT
            connection = self._factory('api.github.com', timeout=TIMEOUT, context=context)
            def deadline_response(sock, *args, **kwargs):
                response = http.client.HTTPResponse(sock, *args, **kwargs)
                response.fp = DeadlineReader(response.fp, sock, deadline)
                return response
            connection.response_class = deadline_response
            def remaining():
                timeout = deadline - time.monotonic(); require(timeout > 0)
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
            connection.connect(); remaining()
            headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/vnd.github+json',
                       'User-Agent': USER_AGENT, 'X-GitHub-Api-Version': API_VERSION, 'Accept-Encoding': 'identity'}
            if method == 'POST':
                headers['Content-Type'] = 'application/json'
            connection.request(method, path, body=body, headers=headers)
            remaining(); response = connection.getresponse()
            if allow_missing and type(response.status) is int and response.status==404:
                return None
            require(type(response.status) is int and response.status == 200)
            pairs = response.getheaders(); require(isinstance(pairs, list) and len(pairs) <= 100)
            metadata = {}
            for name, value in pairs:
                require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}', name)
                        and name.lower() not in metadata and isinstance(value, str) and len(value) <= 8192
                        and all(32 <= ord(c) <= 126 for c in value))
                metadata[name.lower()] = value
            require(metadata.get('content-type', '').split(';', 1)[0].strip()
                    in ('application/json', 'application/vnd.github+json')
                    and metadata.get('content-encoding', 'identity').lower() == 'identity')
            length = metadata.get('content-length')
            if length is not None:
                require(re.fullmatch(r'[0-9]{1,12}', length) and int(length) <= MAX_BODY)
            chunks, received = [], 0
            while True:
                remaining(); limit = min(65536, MAX_BODY + 1 - received)
                chunk = response.read1(limit)
                require(isinstance(chunk, bytes) and len(chunk) <= limit)
                if not chunk:
                    break
                received += len(chunk); require(received <= MAX_BODY); chunks.append(chunk)
            require(length is None or int(length) == received)
            return strict_json(b''.join(chunks))
        except Exception:
            raise GitHubReadError('Bounded GitHub read refused') from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    raise GitHubReadError('Bounded GitHub read refused') from None
