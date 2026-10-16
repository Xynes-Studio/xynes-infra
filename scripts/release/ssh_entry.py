#!/usr/bin/env python3
"""Unprivileged root-owned sshd ForceCommand entry; no privileged executor here.

Install with a fixed isolated interpreter and root-owned imports. sshd must deny
PTYs/forwarding and run this entry for the three deployment accounts. sudoers
must authorize only each account's exact literal-stage command; the separately
installed root executor must recheck SUDO_USER, enforce its own deadline/lock,
verify native candidates/fresh approval and record acceptance or rollback.

An unprivileged entry cannot guarantee termination of an already privileged
child. Output bounds/timeouts cause entry refusal; they are not cancellation or
rollback evidence. Never install this entry before the complete root boundary.
"""
import json
import math
import os
import pwd
import selectors
import signal
import subprocess
import sys
import time

try:
    from . import deploy_request
except ImportError:
    import deploy_request

ERROR = 'SSH deployment entry refused'
REQUEST_TIMEOUT = 5
COMMAND_TIMEOUT = 3600
MAX_OUTPUT = 64 * 1024
ACCOUNTS = {'xynes_qa_deploy': 'qa', 'xynes_staging_deploy': 'staging', 'xynes_prod_deploy': 'prod'}
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}


class EntryError(ValueError):
    """Only the fixed diagnostic crosses this unprivileged boundary."""


def require(value):
    if not value:
        raise EntryError(ERROR)


def bounded_process(argv, *, data, env, cwd, timeout_seconds, max_output):
    """Trusted fixed-argv execution, bounded combined pipes and one deadline.

    argv/env/cwd/limits come only from run(), never from the SSH request. Direct
    invocation by fixtures uses inert processes to exercise pipe/deadline logic.
    Privileged execution must independently handle cancellation and recovery.
    """
    process = None
    selector = selectors.DefaultSelector()
    success = False
    try:
        require(type(argv) is list and 0 < len(argv) <= 128
                and all(type(arg) is str and 0 < len(arg) <= 4096 and '\0' not in arg for arg in argv)
                and type(data) is bytes and 0 < len(data) <= deploy_request.MAX_BYTES
                and type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                and 0 < timeout_seconds <= COMMAND_TIMEOUT
                and type(max_output) is int and 0 < max_output <= MAX_OUTPUT)
        deadline = time.monotonic() + timeout_seconds
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=cwd, close_fds=True, start_new_session=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_WRITE if stream is process.stdin else selectors.EVENT_READ)
        sent = received = 0
        output, errors = bytearray(), bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            for key, _ in selector.select(min(0.2, remaining)):
                require(time.monotonic() < deadline)
                if key.fileobj is process.stdin:
                    count = os.write(key.fd, data[sent:])
                    require(count > 0)
                    sent += count
                    if sent == len(data):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, min(65536, max_output + 1 - received))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    received += len(chunk)
                    require(received <= max_output)
                    (output if key.fileobj is process.stdout else errors).extend(chunk)
        remaining = deadline - time.monotonic()
        require(remaining > 0)
        code = process.wait(timeout=remaining)
        success = True
        return code, bytes(output), bytes(errors)
    except Exception:
        raise EntryError(ERROR) from None
    finally:
        selector.close()
        if process is not None:
            if not success:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def run(fd=0, *, process=None):
    """Validate OS identity/stdin, then invoke only the account's fixed command."""
    try:
        uid = os.getuid()
        require(type(uid) is int and uid > 0 and os.geteuid() == uid
                and os.environ.get('SSH_ORIGINAL_COMMAND') == 'apex-deploy'
                and 'SSH_TTY' not in os.environ
                and not any(os.isatty(descriptor) for descriptor in (fd, 1, 2)))
        name = pwd.getpwuid(uid).pw_name
        require(type(name) is str and name in ACCOUNTS
                and (process is None or callable(process)))
        stage = ACCOUNTS[name]
        request = deploy_request.receive(fd, stage=stage, timeout_seconds=REQUEST_TIMEOUT)
        data = (json.dumps({key: request[key] for key in ('schema_version', 'artifact_kind', 'controller', 'publication', 'deployment')},
                           sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
        response = (bounded_process if process is None else process)(
            ['/usr/bin/sudo', '-n', '--', '/usr/local/libexec/xynes-deploy-root', stage],
            data=data, env=dict(ENVIRONMENT), cwd='/', timeout_seconds=COMMAND_TIMEOUT, max_output=MAX_OUTPUT)
        require(type(response) is tuple and len(response) == 3)
        code, output, errors = response
        require(type(code) is int and code == 0 and type(output) is bytes and type(errors) is bytes
                and len(output) + len(errors) <= MAX_OUTPUT)
        # Do not interpret or forward arbitrary privileged stdout/stderr as
        # deployment evidence. The root executor stores separate signed receipts.
        return {'artifact_kind': 'apex-ssh-entry-result', 'stage': stage,
                'root_command_completed': True, 'deployable': False}
    except Exception:
        raise EntryError(ERROR) from None


def main():
    try:
        require(len(sys.argv) == 1)
        result = run()
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        return 0
    except Exception:
        print(ERROR, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
