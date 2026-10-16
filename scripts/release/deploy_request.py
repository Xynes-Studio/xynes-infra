#!/usr/bin/env python3
"""Bounded deployment data intake for a root-owned SSH forced command.

The privileged command supplies a literal stage; stdin cannot choose it. Use
receive(0, stage='qa') for the SSH-owned stdin pipe, never SSH_ORIGINAL_COMMAND,
environment variables or caller-selected arguments. This module executes no
commands and grants no deployment authority: native artifact acquisition,
source binding, signature checks, fresh stage approval and serialized mutation
must follow in the trusted host integration. A first-attempt reference is not
replay protection; deployment state must independently reject repeated work.
"""
import json
import math
import os
import re
import select
import time

MAX_BYTES = 4096
MAX_ID = 9007199254740991
ERROR = 'Deployment request refused'
STAGES = ('qa', 'staging', 'prod')


class RequestError(ValueError):
    """Fixed diagnostic contains no request content, path or OS error."""


def _require(condition):
    if not condition:
        raise RequestError(ERROR)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _constant(_):
    raise RequestError(ERROR)


def _keys(value, keys):
    _require(type(value) is dict and set(value) == keys)


def _positive(value):
    return type(value) is int and 0 < value <= MAX_ID


def parse(raw, *, stage):
    """Return newly parsed request data plus the separately trusted fixed stage."""
    try:
        _require(type(stage) is str and stage in STAGES
                 and type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        _keys(value, {'schema_version', 'artifact_kind', 'controller', 'publication', 'deployment'})
        _require(type(value['schema_version']) is int and value['schema_version'] == 1
                 and value['artifact_kind'] == 'apex-deployment-request')
        controller = value['controller']
        _keys(controller, {'repository', 'commit', 'workflow', 'run_id', 'run_attempt'})
        _require(controller['repository'] == 'Xynes-Studio/xynes-infra'
                 and controller['workflow'] == 'apex-build.yml'
                 and type(controller['commit']) is str
                 and re.fullmatch(r'[a-f0-9]{40}', controller['commit'])
                 and _positive(controller['run_id'])
                 and type(controller['run_attempt']) is int and controller['run_attempt'] == 1)
        publication = value['publication']
        _keys(publication, {'id', 'digest'})
        _require(_positive(publication['id']) and type(publication['digest']) is str
                 and re.fullmatch(r'sha256:[a-f0-9]{64}', publication['digest']))
        deployment = value['deployment']
        _keys(deployment, {'run_id','run_attempt','job_id'})
        _require(_positive(deployment['run_id']) and deployment['run_id'] != controller['run_id']
                 and _positive(deployment['job_id']) and type(deployment['run_attempt']) is int
                 and deployment['run_attempt'] == 1)
        return {**value, 'stage': stage}
    except Exception:
        raise RequestError(ERROR) from None


def receive(fd=0, *, stage, timeout_seconds=5):
    """Read exactly one EOF-terminated request from trusted SSH stdin.

    One monotonic deadline bounds the entire read, including slow trickles or a
    sender withholding EOF after valid JSON. Only MAX_BYTES+1 bytes are read.
    The caller owns the descriptor; this function neither closes it nor changes
    its flags. fd and timeout are trusted integration settings, never stdin data.
    """
    try:
        _require(type(stage) is str and stage in STAGES
                 and type(fd) is int and 0 <= fd <= 2147483647
                 and type(timeout_seconds) in (int, float)
                 and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 30)
        deadline = time.monotonic() + timeout_seconds
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            _require(remaining > 0)
            ready, _, _ = select.select([fd], [], [], remaining)
            _require(ready and time.monotonic() < deadline)
            chunk = os.read(fd, MAX_BYTES + 1 - len(raw))
            if not chunk:
                return parse(bytes(raw), stage=stage)
            raw.extend(chunk)
            _require(len(raw) <= MAX_BYTES)
    except Exception:
        raise RequestError(ERROR) from None
