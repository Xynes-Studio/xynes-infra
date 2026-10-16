#!/usr/bin/env python3
"""Read the protected controller's registry choice; never resolve credentials."""
from pathlib import Path
try:
    from . import release_manifest as records
except ImportError:
    import release_manifest as records

EXPECTED = {'schema_version': 1, 'registry_prefix': 'ghcr.io/xynes-studio/apex',
            'package_repository': 'Xynes-Studio/xynes-infra', 'visibility': 'private',
            'publish_authentication': 'github-actions-token',
            'server_pull_authentication': {'kind': 'github-classic-pat', 'scopes': ['read:packages']}}


def read_policy(path):
    value = records.read_json(Path(path))
    records.require(isinstance(value, dict) and type(value.get('schema_version')) is int and value == EXPECTED)
    return value
