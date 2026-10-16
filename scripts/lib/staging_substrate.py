#!/usr/bin/env python3
"""Dry-run-first creation of the staging directory/network substrate only."""
import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

LIVE_ROOT = Path('/srv/xynes')
LOCK = '.apex-staging-substrate.lock'
NETWORK = 'xynes_staging_net'
DESIRED = {'staging': 0o755, 'staging/compose': 0o750, 'staging/env': 0o750, 'staging/supabase': 0o755}
LABELS = {'io.xynes.managed': 'true', 'io.xynes.owner': 'xynes-infra',
          'io.xynes.environment': 'staging', 'io.xynes.apex.substrate': '1'}
DOCKER = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'network']
FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"driver":{{json .Driver}},'
          '"scope":{{json .Scope}},"internal":{{json .Internal}},"attachable":{{json .Attachable}},'
          '"ingress":{{json .Ingress}},"ipv6":{{json .EnableIPv6}},'
          '"options":{{json .Options}},"labels":{{json .Labels}}}')
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SubstrateError(ValueError):
    """Never includes paths, command stderr, credentials or rejected values."""


def require(condition):
    if not condition:
        raise SubstrateError('Staging substrate refused: state, ownership or plan is unsafe or unavailable')


def identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': f'{stat.S_IMODE(info.st_mode):04o}'}


def same(left, right):
    return identity(left) == identity(right)


def json_read(text):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value);value[key] = item
        return value
    require(isinstance(text, str) and len(text.encode()) <= 16 * 1024)
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: require(False))
    except (ValueError, RecursionError):
        raise SubstrateError('Staging substrate refused: malformed network metadata') from None


def live_runner(argv):
    # A fixed local daemon and clean environment prevent remote Docker contexts
    # or caller-supplied CLI configuration from redirecting this host operation.
    return subprocess.run(argv, capture_output=True, text=True, timeout=15,
                          env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                               'DOCKER_CONFIG': '/run/xynes-apex-staging-unused-docker-config'})


class RootTree:
    def __init__(self, substrate):
        self.substrate = substrate
        self.chain = []

    def __enter__(self):
        try:
            fd = os.open('/', DIRECTORY_FLAGS)
            self.chain.append((None, None, fd, os.fstat(fd)))
            for part in self.substrate.root.parts[1:]:
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
                next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
                self.chain.append((fd, part, next_fd, os.fstat(next_fd)))
                require(stat.S_ISDIR(before.st_mode) and same(before, os.fstat(next_fd)))
                fd = next_fd
            self.fd = fd
            info = os.fstat(fd)
            require(info.st_uid == self.substrate.uid and info.st_gid == self.substrate.root_gid
                    and stat.S_IMODE(info.st_mode) == 0o755)
            if self.substrate.root == LIVE_ROOT:
                require(self.substrate.uid == 0 and self.substrate.root_gid == 0)
                for _, _, opened, _ in self.chain:
                    info = os.fstat(opened)
                    require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022)
            self.check()
            return self
        except (OSError, ValueError):
            self.__exit__(None, None, None)
            raise SubstrateError('Staging substrate refused: unsafe root path or ownership') from None

    def __exit__(self, *_):
        for _, _, fd, _ in reversed(self.chain):os.close(fd)
        self.chain.clear()

    def check(self):
        for parent, name, fd, original in self.chain:
            current = os.fstat(fd)
            require(stat.S_ISDIR(current.st_mode) and same(current, original))
            if parent is not None:
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require(stat.S_ISDIR(entry.st_mode) and same(entry, current))

    def check_links(self, links):
        self.check()
        for parent, name, fd in links:
            info = os.fstat(fd)
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISDIR(entry.st_mode) and same(entry, info))

    def expected(self, info, relative):
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.substrate.uid
                and info.st_gid == self.substrate.gid and stat.S_IMODE(info.st_mode) == DESIRED[relative])

    @contextmanager
    def target(self, relative, missing=False):
        require(relative == '' or relative in DESIRED)
        fd = self.fd;links = [];prefix = ''
        try:
            for name in relative.split('/') if relative else []:
                prefix = (prefix + '/' if prefix else '') + name
                try:before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    require(missing);yield None;return
                require(stat.S_ISDIR(before.st_mode))
                opened = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                links.append((fd, name, opened))
                current = os.fstat(opened)
                require(same(before, current));self.expected(current, prefix)
                fd = opened
            self.check_links(links)
            yield fd
            self.check_links(links)
        finally:
            for _, _, opened in reversed(links):os.close(opened)

    def records(self):
        result = []
        for relative in DESIRED:
            with self.target(relative, missing=True) as fd:
                info = os.fstat(fd) if fd is not None else None
                # Bind the dry-run snapshot to change time too: Linux can reuse
                # an inode after deletion. Keep live FD identity stable while
                # our own mkdir operations change ancestor directory metadata.
                snapshot = {**identity(info), 'ctime_ns': info.st_ctime_ns} if info is not None else {}
                result.append({'path': relative, 'exists': fd is not None, **snapshot})
        return result

    def validate_lock(self, fd=None):
        try:entry = os.stat(LOCK, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            require(fd is None);return
        require(stat.S_ISREG(entry.st_mode) and entry.st_uid == self.substrate.uid
                and entry.st_gid == self.substrate.root_gid and stat.S_IMODE(entry.st_mode) == 0o600
                and entry.st_nlink == 1 and entry.st_size == 0)
        if fd is not None:require(same(entry, os.fstat(fd)))
        self.check()

    @contextmanager
    def lock(self):
        fd = None
        try:
            self.check();self.validate_lock()
            try:
                fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.fd)
                require(os.fstat(fd).st_uid == self.substrate.uid)
                os.fchown(fd, self.substrate.uid, self.substrate.root_gid)
                os.fchmod(fd, 0o600);os.fsync(fd);os.fsync(self.fd)
            except FileExistsError:
                fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.fd)
            self.validate_lock(fd)
            try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:raise SubstrateError('Staging substrate refused: owned lock is unavailable') from None
            self.validate_lock(fd)
            yield fd
            self.validate_lock(fd)
        finally:
            if fd is not None:os.close(fd)  # Never unlink a lock or any resource.

    def mkdir(self, relative, lock_fd):
        parent, _, name = relative.rpartition('/')
        with self.target(parent) as parent_fd:
            self.validate_lock(lock_fd)
            try:os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                with self.target(relative):pass  # Re-read; never repair existing metadata.
                return
            opened = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                info = os.fstat(opened)
                entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                require(stat.S_ISDIR(info.st_mode) and same(info, entry) and info.st_uid == self.substrate.uid
                        and not stat.S_IMODE(info.st_mode) & 0o077)
                os.fchown(opened, self.substrate.uid, self.substrate.gid)
                os.fchmod(opened, DESIRED[relative]);os.fsync(opened);os.fsync(parent_fd)
                self.expected(os.fstat(opened), relative)
                require(same(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), os.fstat(opened)))
                self.validate_lock(lock_fd)
            finally:os.close(opened)


class Substrate:
    def __init__(self, root=LIVE_ROOT, uid=0, gid=1003, root_gid=0, runner=live_runner, euid=os.geteuid):
        self.root = Path(root)
        require(self.root.is_absolute() and '..' not in self.root.parts
                and all(type(x) is int and 0 <= x <= 2147483647 for x in (uid, gid, root_gid)))
        self.uid, self.gid, self.root_gid, self.runner, self.euid = uid, gid, root_gid, runner, euid

    def command(self, argv):
        try:
            result = self.runner(argv)
            require(type(result.returncode) is int and result.returncode == 0
                    and isinstance(result.stdout, str) and len(result.stdout.encode()) <= 16 * 1024)
            return result.stdout
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise SubstrateError('Staging substrate refused: local network operation unavailable') from None

    def network(self):
        raw = self.command(DOCKER + ['ls', '--no-trunc', '--filter', 'name=^' + NETWORK + '$', '--format',
                                    '{"id":{{json .ID}},"name":{{json .Name}}}'])
        lines = raw.splitlines();require(len(lines) <= 1)
        if not lines:return {'exists': False}
        entry = json_read(lines[0])
        require(isinstance(entry, dict) and set(entry) == {'id', 'name'} and entry['name'] == NETWORK
                and isinstance(entry['id'], str) and re.fullmatch(r'[a-f0-9]{64}', entry['id']))
        value = json_read(self.command(DOCKER + ['inspect', '--format', FORMAT, entry['id']]))
        require(isinstance(value, dict) and set(value) == {'id', 'name', 'driver', 'scope', 'internal',
                 'attachable', 'ingress', 'ipv6', 'options', 'labels'} and value['id'] == entry['id']
                and value['name'] == NETWORK and value['driver'] == 'bridge' and value['scope'] == 'local'
                and all(value[k] is False for k in ('internal', 'attachable', 'ingress', 'ipv6'))
                and value['options'] in ({}, None) and value['labels'] == LABELS)
        value['options'] = {};return {'exists': True, **value}

    def _plan(self, tree):
        tree.check();tree.validate_lock()
        paths = tree.records();net = self.network()
        tree.check();tree.validate_lock();require(paths == tree.records())
        actions = [{'operation': 'mkdir', 'path': row['path'], 'uid': self.uid, 'gid': self.gid,
                    'mode': f'{DESIRED[row["path"]]:04o}'} for row in paths if not row['exists']]
        if not net['exists']:actions.append({'operation': 'create_network', 'name': NETWORK, 'labels': LABELS.copy()})
        value = {'schema_version': 1, 'scope': 'staging-substrate', 'root': str(self.root),
                 'root_identity': identity(os.fstat(tree.fd)), 'uid': self.uid, 'gid': self.gid,
                 'paths': paths, 'network': net, 'actions': actions}
        value['plan_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return value

    def plan(self):
        try:
            with RootTree(self) as tree:return self._plan(tree)
        except (OSError, ValueError):raise SubstrateError('Staging substrate refused: unsafe or unavailable current state') from None

    def apply(self, plan_sha256):
        require(self.euid() == 0 and isinstance(plan_sha256, str) and re.fullmatch(r'[a-f0-9]{64}', plan_sha256))
        try:
            with RootTree(self) as tree, tree.lock() as lock_fd:
                fresh = self._plan(tree);require(fresh['plan_sha256'] == plan_sha256)
                for action in fresh['actions']:
                    tree.check();tree.validate_lock(lock_fd)
                    if action['operation'] == 'mkdir':tree.mkdir(action['path'], lock_fd)
                    else:
                        # Re-read immediately before creation; a concurrently owned
                        # network can converge, but unowned/different metadata refuses.
                        if not self.network()['exists']:
                            args = DOCKER + ['create', '--driver', 'bridge', '--internal=false',
                                             '--attachable=false', '--ipv6=false']
                            for key, value in sorted(LABELS.items()):args += ['--label', key + '=' + value]
                            created = self.command(args + [NETWORK]).strip()
                            require(re.fullmatch(r'[a-f0-9]{64}', created))
                            require(self.network()['id'] == created)
                        tree.check();tree.validate_lock(lock_fd)
                after = self._plan(tree);require(not after['actions'])
                return {'schema_version': 1, 'scope': 'staging-substrate', 'status': 'converged',
                        'applied_plan_sha256': plan_sha256, 'current_plan_sha256': after['plan_sha256']}
        except (OSError, ValueError):raise SubstrateError('Staging substrate refused: partial resources preserved for review or retry') from None


class Parser(argparse.ArgumentParser):
    def error(self, _):raise SubstrateError('Staging substrate refused: invalid arguments')


def main():
    try:
        parser = Parser(prog='staging-substrate', description=__doc__)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_true');mode.add_argument('--apply', action='store_true')
        parser.add_argument('--plan-sha256')
        args = parser.parse_args()
        require(bool(args.apply) == bool(args.plan_sha256))
        if args.apply:require(os.geteuid() == 0 and re.fullmatch(r'[a-f0-9]{64}', args.plan_sha256))
        require(grp.getgrnam('xynes').gr_gid == 1003)
        instance = Substrate()
        result = instance.apply(args.plan_sha256) if args.apply else instance.plan()
        print(json.dumps(result, sort_keys=True, indent=2));return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('Staging substrate refused: invalid arguments or unsafe/unavailable state; partial resources preserved', file=sys.stderr)
        return 1


if __name__ == '__main__':sys.exit(main())
