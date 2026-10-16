#!/usr/bin/env python3
"""Acquire hash-bound native Actions artifacts as data, never executable source."""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import zipfile
try:
    from . import image_build, release_manifest as records
except ImportError:
    import image_build
    import release_manifest as records

REPOSITORY_ID = 1415400917
PREFIX = '/repos/Xynes-Studio/xynes-infra/actions/artifacts/'
MAX_TOTAL = 32 * 1024 ** 3
MAX_JSON = 4 * 1024 ** 2
MAX_RATIO = 1000
ADMISSION_FILES = ('controller.json', 'source-manifest.json', 'public-inputs.json', 'source-checks.json')
BUILD_FILES = ('image-receipts.json',) + tuple('archives/shared--' + name + '.oci.tar' for name in image_build.SHARED_BUILD) + tuple('archives/' + stage + '--' + name + '.oci.tar' for stage in records.STAGES for name in records.APPS)
PUBLICATION_FILES = ('published-receipt.json',)
MIGRATION_FILES = ('migration-bundle.tar',)
FILES_BY_KIND = {'admission': ADMISSION_FILES, 'build': BUILD_FILES, 'publication': PUBLICATION_FILES, 'migration': MIGRATION_FILES}
ZIP_LIMITS = {'publication': 8*1024**2, 'migration': 40*1024**2}
MIGRATION_LIMIT = 32*1024**2
ERROR = 'Artifact handoff refused: invalid or incomplete bundle'


class HandoffError(ValueError):
    """Contains no rejected input, path, response or signed download URL."""


def require(condition):
    if not condition:
        raise HandoffError(ERROR)


def metadata(value, kind, reference, controller):
    require(isinstance(value, dict))
    artifact_id = reference['id']
    url = 'https://api.github.com' + PREFIX + str(artifact_id)
    require(type(value.get('id')) is int and value['id'] == artifact_id
            and value.get('digest') == reference['digest'] and value.get('expired') is False
            and type(value.get('size_in_bytes')) is int and 0 < value['size_in_bytes'] <= ZIP_LIMITS.get(kind, MAX_TOTAL)
            and value.get('name') == 'apex-' + kind + '-' + str(controller['run_id']) + '-1'
            and value.get('url') == url and value.get('archive_download_url') == url + '/zip')
    run = value.get('workflow_run')
    require(isinstance(run, dict) and type(run.get('id')) is int and run['id'] == controller['run_id']
            and type(run.get('repository_id')) is int and run['repository_id'] == REPOSITORY_ID
            and type(run.get('head_repository_id')) is int and run['head_repository_id'] == REPOSITORY_ID
            and run.get('head_branch') == 'main' and run.get('head_sha') == controller['commit'])
    return copy.deepcopy(value)


def same_file(before, after):
    return os.path.samestat(before, after) and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)


def central_directory(file, size, expected_count):
    # Bound the central directory before ZipFile allocates it. ZIP64 permits
    # large OCI artifacts, but not arbitrary counts, metadata or multi-disk ZIPs.
    require(size >= 22)
    file.seek(max(0, size - 65557)); tail = file.read(65557)
    at = tail.rfind(b'PK\x05\x06'); require(at >= 0 and at + 22 == len(tail))
    end = size - len(tail) + at
    _, disk, start_disk, disk_count, count, length, offset, comment = struct.unpack('<4s4H2IH', tail[at:at+22])
    require(disk == start_disk == comment == 0 and disk_count == count)
    central_end = end
    if count == 65535 or length == 0xffffffff or offset == 0xffffffff:
        require(end >= 20); file.seek(end-20); locator = file.read(20)
        signature, zip_disk, location, disks = struct.unpack('<4sIQI', locator)
        require(signature == b'PK\x06\x07' and zip_disk == 0 and disks == 1 and 0 <= location < end-20)
        file.seek(location); raw = file.read(56); require(len(raw) == 56)
        signature, record_size, _, _, zip_disk, start_disk, disk_count, count, length, offset = struct.unpack('<4sQ2H2I4Q', raw)
        require(signature == b'PK\x06\x06' and record_size == 44 and location+56 == end-20
                and zip_disk == start_disk == 0 and disk_count == count)
        central_end = location
    require(count == expected_count and 0 < length <= 65536 and offset >= 0 and offset + length == central_end)
    return offset


def inventory(archive, file, central, expected, json_limit=MAX_JSON, blob_limit=image_build.MAX_ARCHIVE, total_limit=MAX_TOTAL):
    items = archive.infolist(); require(len(items) == len(expected)); seen = set(); total = 0
    for item in items:
        require(item.filename == item.orig_filename and item.filename in expected and item.filename not in seen)
        seen.add(item.filename)
        mode = item.external_attr >> 16
        require(not item.is_dir() and not (item.external_attr & 0x10)
                and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                and item.flag_bits & ~0x80e == 0
                and (item.compress_type != zipfile.ZIP_STORED or item.flag_bits & 6 == 0)
                and len(item.extra) <= 4096 and len(item.comment) <= 4096)
        limit = json_limit if item.filename.endswith('.json') else blob_limit
        require(type(item.file_size) is int and 0 <= item.file_size <= limit
                and type(item.compress_size) is int and 0 <= item.compress_size <= MAX_TOTAL
                and item.file_size <= max(item.compress_size, 1) * MAX_RATIO
                and (item.compress_type != zipfile.ZIP_STORED or item.compress_size == item.file_size))
        total += item.file_size; require(total <= total_limit)
    require(seen == set(expected))
    ordered = sorted(items, key=lambda item: item.header_offset)
    require(ordered[0].header_offset == 0)
    for index, item in enumerate(ordered):
        file.seek(item.header_offset); raw = file.read(30); require(len(raw) == 30)
        signature, _, flags, method, _, _, crc, compressed, expanded, name_size, extra_size = struct.unpack('<4s5H3I2H', raw)
        require(signature == b'PK\x03\x04' and flags == item.flag_bits and method == item.compress_type
                and name_size <= 255 and extra_size <= 4096)
        require(file.read(name_size) == item.filename.encode('ascii'))
        extra = file.read(extra_size); require(len(extra) == extra_size)
        if not flags & 8:
            if expanded == 0xffffffff or compressed == 0xffffffff:
                cursor = 0; zip64 = None
                while cursor < len(extra):
                    require(cursor+4 <= len(extra)); tag, length = struct.unpack('<HH', extra[cursor:cursor+4]); cursor += 4
                    require(cursor+length <= len(extra))
                    if tag == 1:require(zip64 is None); zip64 = extra[cursor:cursor+length]
                    cursor += length
                require(zip64 is not None)
                cursor = 0
                if expanded == 0xffffffff:require(len(zip64) >= cursor+8); expanded = struct.unpack('<Q', zip64[cursor:cursor+8])[0]; cursor += 8
                if compressed == 0xffffffff:require(len(zip64) >= cursor+8); compressed = struct.unpack('<Q', zip64[cursor:cursor+8])[0]
            require(crc == item.CRC and compressed == item.compress_size and expanded == item.file_size)
        data_end = item.header_offset + 30 + name_size + extra_size + item.compress_size
        next_offset = ordered[index+1].header_offset if index+1 < len(ordered) else central
        require(data_end <= next_offset)
        if flags & 8:
            size = next_offset-data_end; require(size in (12, 16, 20, 24))
            file.seek(data_end); descriptor = file.read(size)
            if size in (16, 24):require(descriptor[:4] == b'PK\x07\x08'); descriptor = descriptor[4:]
            values = struct.unpack('<III' if len(descriptor) == 12 else '<IQQ', descriptor)
            require(values == (item.CRC, item.compress_size, item.file_size))
        else:
            require(data_end == next_offset)
    return items


def cleanup(owned):
    # An inode replaced by another process is never removed, nor are unknown
    # descendants recursively traversed. Only our exact files/directories count.
    for path, identity, directory in reversed(owned):
        try:
            if os.path.samestat(path.lstat(), identity):
                path.rmdir() if directory else path.unlink()
        except OSError:
            pass


def acquire(*, kind, reference, controller, read, download, output):
    owned = []; temporary = []; success = False
    try:
        require(kind in FILES_BY_KIND)
        records.validate_controller(controller); require(controller['run_attempt'] == 1)
        require(isinstance(reference, dict) and set(reference) == {'id', 'digest'}
                and type(reference['id']) is int and 0 < reference['id'] <= 9007199254740991
                and isinstance(reference['digest'], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', reference['digest']))
        require(isinstance(output, Path) and output.is_absolute() and output.parent.resolve(strict=True) == output.parent
                and output.parent.is_dir() and not output.exists() and not output.is_symlink())
        before = metadata(read(PREFIX + str(reference['id'])), kind, reference, controller)
        temp = Path(tempfile.mkdtemp(prefix='.apex-artifact-', dir=output.parent)); temporary.append((temp, temp.lstat(), True))
        zipped = temp / 'artifact.zip'
        try:
            download(reference['id'], zipped, before['size_in_bytes'])
        finally:
            if zipped.exists() and not zipped.is_symlink():
                identity = zipped.lstat()
                if stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1:
                    temporary.append((zipped, identity, False))
        identity = zipped.lstat()
        require(stat.S_ISREG(identity.st_mode) and identity.st_nlink == 1 and identity.st_size == before['size_in_bytes'])
        descriptor = os.open(zipped, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as file:
            require(same_file(identity, os.fstat(file.fileno())))
            digest = hashlib.sha256(); size = 0
            while True:
                chunk = file.read(1024*1024)
                if not chunk:break
                size += len(chunk); require(size <= before['size_in_bytes']); digest.update(chunk)
            require(size == before['size_in_bytes'] and 'sha256:' + digest.hexdigest() == reference['digest'])
            require(metadata(read(PREFIX + str(reference['id'])), kind, reference, controller) == before)
            expected = FILES_BY_KIND[kind]
            central = central_directory(file, size, len(expected))
            with zipfile.ZipFile(file) as archive:
                items = inventory(archive, file, central, expected, 256*1024 if kind=='publication' else MAX_JSON,
                    MIGRATION_LIMIT if kind=='migration' else image_build.MAX_ARCHIVE,
                    MIGRATION_LIMIT if kind=='migration' else MAX_TOTAL)
                require(same_file(identity, os.fstat(file.fileno())) and same_file(identity, zipped.lstat()))
                output.mkdir(mode=0o700); owned.append((output, output.lstat(), True))
                if kind == 'build':
                    directory = output / 'archives'; directory.mkdir(mode=0o700); owned.append((directory, directory.lstat(), True))
                for item in items:
                    target = output / item.filename
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    owned.append((target, os.fstat(descriptor), False))
                    with os.fdopen(descriptor, 'wb') as destination, archive.open(item) as source:
                        count = 0
                        while True:
                            chunk = source.read(min(1024*1024, item.file_size-count+1))
                            if not chunk:break
                            count += len(chunk); require(count <= item.file_size); destination.write(chunk)
                        require(count == item.file_size); destination.flush(); os.fsync(destination.fileno())
                require(same_file(identity, os.fstat(file.fileno())) and same_file(identity, zipped.lstat()))
        success = True
        return output
    except Exception:
        raise HandoffError(ERROR) from None
    finally:
        if not success:cleanup(owned)
        cleanup(temporary)
