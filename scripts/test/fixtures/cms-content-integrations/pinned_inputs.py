"""Read immutable artifact inputs without trusting a mutable checkout or shell."""
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


def load_pinned_file(repository, revision, relative_path):
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An immutable full commit revision is required")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in relative_path:
        raise ValueError("A safe repository-relative input path is required")
    root = Path(repository).resolve()
    file = (root / relative_path).resolve()
    if not file.is_relative_to(root):
        raise ValueError("Input path escapes the repository")
    result = subprocess.run(["git", "show", f"{revision}:{relative_path}"], cwd=root, capture_output=True)
    if result.returncode:
        raise ValueError("Input file is unavailable at the pinned revision")
    try:
        current = file.read_bytes()
    except OSError as error:
        raise ValueError("Pinned input is absent from the checkout") from error
    if current != result.stdout:
        raise ValueError("Working input differs from pinned revision")
    return result.stdout


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate contract property")
        result[key] = value
    return result


def compare_contracts(canonical_bytes, mirror_bytes):
    try:
        canonical = json.loads(canonical_bytes, object_pairs_hook=_object)
        mirror = json.loads(mirror_bytes, object_pairs_hook=_object)
        if not isinstance(canonical, dict) or not isinstance(mirror, dict):
            raise ValueError("Contract root must be an object")
        encoded = [json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) for value in (canonical, mirror)]
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid canonical contract or generated mirror") from error
    if encoded[0] != encoded[1]:
        raise ValueError("Generated mirror differs from canonical contract")
