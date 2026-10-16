#!/usr/bin/env python3
"""Durable data-only host ledger; no deployment or admission authority.

The privileged coordinator supplies the fixed absolute state directory and stage,
never request paths. Production owner_uid is always 0 (the default); the explicit
owner override exists for unprivileged filesystem fixtures. Hold this context for
ALL host changes including rollback. Call begin only after native qualification;
accept only after real host smoke; record_soak later after real observation. Stored proofs are coordinator observations,
not signatures or independent approval. DB migrations are deliberately never
rolled back by this ledger. Failed/pending records retain accepted pointers.

At most 512 attempts are retained; exhaustion refuses writes, never evicts replay
history. A crashed pending attempt needs explicit operational reconciliation and
fail('interrupted'); it is never resumed automatically. A durability error poisons
the context: reacquire and inspect authoritative state before any further action.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

try:
    from . import deploy_request
except ImportError:
    import deploy_request

ERROR = 'Host release state refused'
MAX_BYTES = 1024 * 1024
MAX_ATTEMPTS = 512
STAGES = ('qa', 'staging', 'prod')


class StateError(ValueError):
    """Fixed redacted refusal."""


def require(value):
    if not value:
        raise StateError(ERROR)


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys))


def timestamp(value):
    require(type(value) is int and 0 <= value <= 9007199254740991)


def digest(value):
    require(type(value) is str and re.fullmatch(r'sha256:[a-f0-9]{64}', value))


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


class ReleaseState:
    def __init__(self, directory, *, stage, owner_uid=0):
        self.directory, self.stage, self.owner_uid = directory, stage, owner_uid
        self.fd = None
        self.locks = []
        self.data = None
        self.poisoned = False
        self.ledger_identity = None
        self.loaded = False

    def _info(self, info):
        require(stat.S_ISREG(info.st_mode) and info.st_uid == self.owner_uid
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)

    def _ledger_info(self):
        try:
            return identity(os.stat(self.stage+'.json', dir_fd=self.fd, follow_symlinks=False))
        except FileNotFoundError:
            return None

    def _stable(self):
        require(self.fd is not None and not self.poisoned)
        info, path = os.fstat(self.fd), self.directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner_uid
                and stat.S_IMODE(info.st_mode) == 0o700
                and (info.st_dev, info.st_ino) == (path.st_dev, path.st_ino)
                and self.directory.resolve(strict=True) == self.directory)
        if self.loaded:
            require(self._ledger_info() == self.ledger_identity)
        for name, descriptor in self.locks:
            info = os.fstat(descriptor)
            self._info(info)
            path = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            require((info.st_dev, info.st_ino) == (path.st_dev, path.st_ino))

    def __enter__(self):
        require(self.fd is None)
        try:
            require(self.fd is None and isinstance(self.directory, Path)
                    and self.directory.is_absolute() and self.stage in STAGES
                    and type(self.stage) is str and type(self.owner_uid) is int and self.owner_uid >= 0
                    and self.directory.resolve(strict=True) == self.directory)
            self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._stable()
            # All stage operations share a host lock; stage lock also fences future
            # stage-local integrations. Neither lock blocks or gets deleted.
            for name in ('host.lock', self.stage+'.lock'):
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                                     | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
                self.locks.append((name, descriptor))
                self._info(os.fstat(descriptor))
                require(os.fstat(descriptor).st_size == 0)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._stable()
            allowed = {'host.lock', *(stage+'.lock' for stage in STAGES), *(stage+'.json' for stage in STAGES)}
            for name in os.listdir(self.fd):
                require(name in allowed)
                self._info(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
            self.data = self._read()
            self.ledger_identity = self._ledger_info()
            self.loaded = True
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise StateError(ERROR) from None

    def __exit__(self, *args):
        for _, descriptor in reversed(self.locks):
            os.close(descriptor)
        self.locks = []
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.data = None
        self.loaded = False

    def _request(self, value, stage=None):
        stage = self.stage if stage is None else stage
        require(type(value) is dict)
        parsed = deploy_request.parse(encode(value), stage=stage)
        require({key: item for key, item in parsed.items() if key != 'stage'} == value)

    def _proof(self, value, record, *, soak=False, stage=None):
        stage = self.stage if stage is None else stage
        keys = {'stage', 'publication', 'source_sha256', 'result'} | (
            {'started_at', 'completed_at'} if soak else {'observed_at'})
        exact(value, keys)
        require(value['stage'] == stage and encode(value['publication']) == encode(record['request']['publication'])
                and value['source_sha256'] == record['source_sha256'] and value['result'] == 'passed')
        if soak:
            timestamp(value['started_at']); timestamp(value['completed_at'])
            require(value['started_at'] >= record['started_at']
                    and value['completed_at']-value['started_at'] >= 86400
                    and value['started_at'] >= record['ended_at']
                    and value['completed_at'] <= record['soak_recorded_at'])
        else:
            timestamp(value['observed_at'])
            require(record['started_at'] <= value['observed_at'] <= record['ended_at'])

    def _validate(self, value, stage=None):
        stage = self.stage if stage is None else stage
        exact(value, {'schema_version', 'stage', 'current', 'previous', 'attempts'})
        require(type(value['schema_version']) is int and value['schema_version'] == 1
                and value['stage'] == stage and type(value['attempts']) is list
                and len(value['attempts']) <= MAX_ATTEMPTS)
        tokens, runs, jobs, publications = set(), set(), set(), set()
        accepted, pending = [], 0
        for record in value['attempts']:
            exact(record, {'token', 'request', 'source_sha256', 'status', 'started_at', 'ended_at',
                           'smoke', 'soak', 'soak_recorded_at', 'failure_reason'})
            self._request(record['request'], stage); digest(record['source_sha256']); timestamp(record['started_at'])
            token = hashlib.sha256(encode({'request': record['request'], 'source_sha256': record['source_sha256']})).hexdigest()
            require(record['token'] == token and token not in tokens)
            tokens.add(token)
            run, job = record['request']['deployment']['run_id'], record['request']['deployment']['job_id']
            publication = record['request']['publication']['id']
            require(run not in runs and job not in jobs and publication not in publications)
            runs.add(run); jobs.add(job); publications.add(publication)
            if record['status'] == 'pending':
                pending += 1
                require(record['ended_at'] is None and record['smoke'] is None and record['soak'] is None
                        and record['failure_reason'] is None and record['soak_recorded_at'] is None)
            else:
                timestamp(record['ended_at']); require(record['ended_at'] >= record['started_at'])
                if record['status'] == 'accepted':
                    require(record['failure_reason'] is None)
                    self._proof(record['smoke'], record, stage=stage)
                    if record['soak'] is not None:
                        require(stage in ('qa', 'staging'))
                        timestamp(record['soak_recorded_at'])
                        self._proof(record['soak'], record, soak=True, stage=stage)
                    else:
                        require(record['soak_recorded_at'] is None)
                    accepted.append(token)
                else:
                    require(record['status'] == 'failed' and record['smoke'] is None and record['soak'] is None
                            and record['soak_recorded_at'] is None
                            and record['failure_reason'] in ('smoke', 'migration', 'runtime', 'interrupted'))
        require(pending <= 1 and (not pending or value['attempts'][-1]['status'] == 'pending'))
        require(value['current'] == (accepted[-1] if accepted else None)
                and value['previous'] == (accepted[-2] if len(accepted) > 1 else None))

    def _read(self, stage=None):
        stage = self.stage if stage is None else stage
        require(stage in STAGES)
        try:
            descriptor = os.open(stage+'.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return {'schema_version': 1, 'stage': stage, 'current': None, 'previous': None, 'attempts': []}
        try:
            info = os.fstat(descriptor); self._info(info)
            require(0 < info.st_size <= MAX_BYTES)
            raw = bytearray()
            while len(raw) <= MAX_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_BYTES+1-len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            require(len(raw) == info.st_size and identity(os.fstat(descriptor)) == identity(info)
                    and identity(os.stat(stage+'.json', dir_fd=self.fd, follow_symlinks=False)) == identity(info))
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False))
            self._validate(value, stage)
            return value
        finally:
            os.close(descriptor)

    def _write(self, value):
        self._stable(); self._validate(value)
        require(self._read() == self.data)
        raw = encode(value); require(len(raw) <= MAX_BYTES)
        name = '.'+self.stage+'.'+uuid.uuid4().hex+'.tmp'
        descriptor, temporary_info = None, None
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
            temporary_info = os.fstat(descriptor); self._info(temporary_info)
            remaining = memoryview(raw)
            while remaining:
                count = os.write(descriptor, remaining); require(count > 0)
                remaining = remaining[count:]
            os.fsync(descriptor)
            info = os.fstat(descriptor); self._info(info)
            require(info.st_size == len(raw) and identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) == identity(info))
            self._stable()
            os.replace(name, self.stage+'.json', src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
            self.data = copy.deepcopy(value)
            self.ledger_identity = self._ledger_info()
        except Exception:
            self.poisoned = True
            raise StateError(ERROR) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_info is not None:
                try:
                    info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (temporary_info.st_dev, temporary_info.st_ino):
                        os.unlink(name, dir_fd=self.fd)
                except FileNotFoundError:
                    pass

    def _active(self, token):
        self._stable()
        require(type(token) is str and re.fullmatch(r'[a-f0-9]{64}', token))
        record = next((item for item in self.data['attempts'] if item['token'] == token), None)
        require(record is not None and record['status'] == 'pending')
        return copy.deepcopy(record)

    def begin(self, request, *, source_sha256, started_at):
        try:
            self._stable(); self._request(request); digest(source_sha256); timestamp(started_at)
            value = copy.deepcopy(self.data)
            for other in STAGES:
                if other != self.stage:
                    for prior in self._read(other)['attempts']:
                        require(prior['request']['deployment']['run_id'] != request['deployment']['run_id']
                                and prior['request']['deployment']['job_id'] != request['deployment']['job_id'])
            require(len(value['attempts']) < MAX_ATTEMPTS and not self.read_pending())
            record = {'request': copy.deepcopy(request), 'source_sha256': source_sha256, 'status': 'pending',
                      'started_at': started_at, 'ended_at': None, 'smoke': None, 'soak': None, 'soak_recorded_at': None, 'failure_reason': None}
            record['token'] = hashlib.sha256(encode({'request': request, 'source_sha256': source_sha256})).hexdigest()
            value['attempts'].append(record)
            self._write(value)
            return record['token']
        except Exception:
            raise StateError(ERROR) from None

    def accept(self, token, *, smoke, accepted_at):
        try:
            record = self._active(token)
            record.update(status='accepted', ended_at=accepted_at, smoke=copy.deepcopy(smoke))
            value = copy.deepcopy(self.data)
            value['attempts'][-1] = record
            value['previous'], value['current'] = value['current'], token
            self._write(value)
        except Exception:
            raise StateError(ERROR) from None

    def fail(self, token, *, reason, failed_at):
        try:
            record = self._active(token)
            record.update(status='failed', ended_at=failed_at, failure_reason=reason)
            value = copy.deepcopy(self.data); value['attempts'][-1] = record
            self._write(value)
        except Exception:
            raise StateError(ERROR) from None

    def read_accepted(self):
        try:
            self._stable()
            return {key: copy.deepcopy(next((item for item in self.data['attempts'] if item['token'] == self.data[key]), None))
                    for key in ('current', 'previous')}
        except Exception:
            raise StateError(ERROR) from None

    def read_pending(self):
        try:
            self._stable()
            return copy.deepcopy(next((item for item in self.data['attempts'] if item['status'] == 'pending'), None))
        except Exception:
            raise StateError(ERROR) from None

    def read_staging_for_promotion(self, *, publication, source_sha256, controller):
        """Match the current 24h-soaked staging release while holding the host lock.

        This data check does not grant production authority: fresh native main,
        release identity and independent production approval are still required.
        """
        try:
            self._stable(); digest(source_sha256)
            exact(publication, {'id', 'digest'})
            require(deploy_request._positive(publication['id']))
            digest(publication['digest'])
            value = self._read('staging')
            current = next((item for item in value['attempts'] if item['token'] == value['current']), None)
            require(not any(item['status'] == 'pending' for item in value['attempts'])
                    and current is not None and current['status'] == 'accepted'
                    and encode(current['request']['publication']) == encode(publication)
                    and current['source_sha256'] == source_sha256 and current['soak'] is not None
                    and encode(current['request']['controller']) == encode(controller))
            return copy.deepcopy(current)
        except Exception:
            raise StateError(ERROR) from None

    def record_soak(self, token, *, soak, recorded_at):
        """Attach a real coordinator observation to the still-current QA/staging release.

        The lock is held only for this atomic update, not throughout the 24h wait.
        Caller request data must never be passed as a soak observation.
        """
        try:
            self._stable()
            require(self.stage in ('qa', 'staging') and token == self.data['current'] and not self.read_pending())
            value = copy.deepcopy(self.data)
            record = next((item for item in value['attempts'] if item['token'] == token), None)
            require(record is not None and record['status'] == 'accepted' and record['soak'] is None)
            record['soak'], record['soak_recorded_at'] = copy.deepcopy(soak), recorded_at
            self._write(value)
        except Exception:
            raise StateError(ERROR) from None
