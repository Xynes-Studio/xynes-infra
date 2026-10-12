#!/usr/bin/env python3
"""Local-only history scan; reviewed exact-content exceptions, never raw diagnostics."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import stat
import subprocess
import tempfile

VERSION = '8.30.1'
MAX_REPORT = 4 * 1024 * 1024
MAX_BLOB = 8 * 1024 * 1024
FIELDS = {'Description', 'StartLine', 'EndLine', 'StartColumn', 'EndColumn', 'Match',
          'Secret', 'File', 'SymlinkFile', 'Commit', 'Entropy', 'Author', 'Email',
          'Date', 'Message', 'Tags', 'RuleID', 'Fingerprint'}
EXCEPTION_FIELDS = {'rule', 'file', 'blob_sha256', 'secret_sha256', 'reason'}

def require(condition):
    if not condition:
        raise ValueError('invalid scan input')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def file_limit(maximum):
    return lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (maximum + 1, maximum + 1))

def path_ok(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(ord(c) < 32 or c in '\\:' for c in value)
            and not value.startswith('/') and value == str(PurePosixPath(value))
            and not any(p in ('', '.', '..') for p in value.split('/')))

def read_bounded(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum)
        data = stream.read(maximum + 1)
        require(len(data) <= maximum)
        return data

def run(args, env, maximum=65536, timeout=60, statuses=(0,)):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=errors, env=env, timeout=timeout, check=False,
                                preexec_fn=file_limit(maximum))
        require(result.returncode in statuses and output.tell() <= maximum and errors.tell() == 0)
        output.seek(0)
        return output.read(maximum + 1), result.returncode

def load_exceptions(path):
    if path is None:
        return set()
    rows = json.loads(read_bounded(path, MAX_REPORT))
    require(isinstance(rows, list) and len(rows) <= 10000)
    allowed = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == EXCEPTION_FIELDS)
        require(all(isinstance(value, str) for value in row.values()))
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', row['rule']) and path_ok(row['file']))
        require(all(re.fullmatch(r'[a-f0-9]{64}', row[key]) for key in ('blob_sha256', 'secret_sha256')))
        require(0 < len(row['reason'].strip()) <= 2000)
        key = tuple(row[k] for k in ('rule', 'file', 'blob_sha256', 'secret_sha256'))
        require(key not in allowed)
        allowed.add(key)
    return allowed

def scan(repo, scanner, exceptions=None):
    require(repo.is_absolute() and repo.is_dir() and scanner.is_absolute())
    info = scanner.lstat()
    require(stat.S_ISREG(info.st_mode) and os.access(scanner, os.X_OK))
    env = {'PATH': os.defpath, 'TMPDIR': tempfile.gettempdir(), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_NO_LAZY_FETCH': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    git = ['git', '--no-replace-objects', '-C', str(repo)]
    shallow, _ = run(git + ['rev-parse', '--is-shallow-repository'], env)
    require(shallow.strip() == b'false')
    head, _ = run(git + ['rev-parse', '--verify', 'HEAD^{commit}'], env)
    require(re.fullmatch(rb'[a-f0-9]{40}\n', head))
    partial, _ = run(git + ['config', '--get-regexp', r'^(extensions\.partialclone|remote\..*\.promisor)$'], env, statuses=(0, 1))
    require(not partial)
    # Git warnings can cause Gitleaks to report zero scanned commits with exit 0.
    with tempfile.TemporaryFile() as errors:
        preflight = subprocess.run(git + ['log', '--all', '-p', '--no-ext-diff', '--no-textconv'], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
            timeout=120, check=False, preexec_fn=file_limit(65536))
        require(preflight.returncode == 0 and errors.tell() == 0)
    version, _ = run([str(scanner), 'version'], env)
    require(version.strip() == VERSION.encode())
    allowed = load_exceptions(exceptions)
    with tempfile.TemporaryDirectory(prefix='apex-secret-scan-') as directory:
        directory = Path(directory)
        directory.chmod(0o700)
        config = directory / 'scan.toml'; config.write_text('[extend]\nuseDefault=true\n'); config.chmod(0o600)
        ignore = directory / 'empty-ignore'; ignore.touch(mode=0o600)
        report = directory / 'report.json'; report.touch(mode=0o600)
        diagnostics = directory / 'diagnostics'; diagnostics.touch(mode=0o600)
        # Gitleaks chmods named reports to 0644; stdout into our 0600 FD avoids that.
        with report.open('wb') as output, diagnostics.open('wb') as errors:
            result = subprocess.run([str(scanner), 'git', str(repo), '--log-opts', '--all --no-ext-diff --no-textconv',
                '--config', str(config), '--gitleaks-ignore-path', str(ignore), '--ignore-gitleaks-allow',
                '--report-format', 'json', '--report-path', '-', '--no-banner', '--no-color', '--log-level', 'info',
                '--max-archive-depth', '2', '--max-decode-depth', '2', '--timeout', '300'],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                env=env, timeout=360, check=False, preexec_fn=file_limit(MAX_REPORT))
        require(result.returncode in (0, 1))
        # Verified against 8.30.1's actual summary; a zero/missing summary refuses.
        counts = re.findall(rb'\b([0-9]+) commits scanned\.', read_bounded(diagnostics, 65536))
        require(len(counts) == 1 and int(counts[0]) > 0)
        require(stat.S_IMODE(report.lstat().st_mode) == 0o600)
        rows = json.loads(read_bounded(report, MAX_REPORT))
        require(isinstance(rows, list) and len(rows) <= 10000)
        require((result.returncode == 0) == (len(rows) == 0))
        blocked = 0
        for row in rows:
            require(isinstance(row, dict) and set(row) <= FIELDS)
            require(all(isinstance(row.get(k), str) and row[k] for k in ('RuleID', 'File', 'Commit', 'Secret')))
            require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', row['RuleID']))
            require(path_ok(row['File']) and re.fullmatch(r'[a-f0-9]{40}', row['Commit']))
            require(not row.get('SymlinkFile') and len(row['Secret'].encode()) <= MAX_REPORT)
            blob, _ = run(git + ['show', f"{row['Commit']}:{row['File']}"], env, maximum=MAX_BLOB)
            secret = row['Secret'].encode()
            require(secret in blob)
            key = (row['RuleID'], row['File'], digest(blob), digest(secret))
            blocked += key not in allowed
        return len(rows), blocked

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--scanner', required=True, type=Path)
    parser.add_argument('--exceptions', type=Path)
    args = parser.parse_args()
    try:
        count, blocked = scan(args.repo, args.scanner, args.exceptions)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print('secret gate refused: invalid or incomplete scan')
        return 2
    print(f'secret gate: candidates={count}, unreviewed={blocked}')
    return 1 if blocked else 0

if __name__ == '__main__':
    raise SystemExit(main())
