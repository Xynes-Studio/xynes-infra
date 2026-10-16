#!/usr/bin/env python3
"""Read a protected local provider handoff without evaluation or diagnostics."""
import os
import re
import stat
from types import MappingProxyType
import unicodedata

MAX_BYTES = 64 * 1024
STAGES = ('qa', 'staging', 'prod')
GLOBAL_KEYS = frozenset(('CLOUDFLARE_DNS_API_TOKEN', 'CLOUDFLARE_ZONE_NAME', 'R2_ACCOUNT_ID',
                         'B2_BACKUP_BUCKET', 'B2_KEY_ID', 'B2_APPLICATION_KEY',
                         'GHCR_READ_TOKEN', 'RELEASE_POLICY_READ_TOKEN'))
STAGE_FIELDS = ('R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'GOOGLE_CLIENT_ID',
                'GOOGLE_CLIENT_SECRET', 'POSTHOG_API_KEY', 'POSTHOG_HOST',
                'RESEND_API_KEY', 'RESEND_FROM_ADDRESS')


def _stage_key(stage, field):
    provider, suffix = field.split('_', 1)
    return f'{provider}_{stage.upper()}_{suffix}'


# Retain compatibility with the existing private handoff, but authoritative DNS
# lives on a separate HestiaCP VPS. These legacy fields confer no DNS authority
# and must not gate the Cloudflare R2 or other runtime provider credentials.
LEGACY_DNS_KEYS = frozenset(('CLOUDFLARE_DNS_API_TOKEN','CLOUDFLARE_ZONE_NAME',
                            'GODADDY_DNS_API_TOKEN','DNS_PROVIDER'))
ALLOWED_KEYS = GLOBAL_KEYS | LEGACY_DNS_KEYS | frozenset(_stage_key(stage, field)
                                                       for stage in STAGES for field in STAGE_FIELDS)


class ProviderInputError(ValueError):
    """Refusals never contain input values, names, paths or underlying errors."""


def _require(condition):
    if not condition:
        raise ProviderInputError('Provider inputs refused')


def _complete(value):
    return isinstance(value, str) and bool(value.strip()) and 'replace-with' not in value.lower()


class StageInputs:
    """Explicit access to one stage's runtime mapping, with a redacted repr."""
    __slots__ = ('_values',)

    def __init__(self, values):
        self._values = MappingProxyType(dict(values))

    def __repr__(self):
        return '<StageInputs redacted>'

    def get(self, name):
        _require(isinstance(name, str) and name in self._values)
        return self._values[name]


class ProviderInputs:
    """No iteration or serialization API; callers request declared keys only."""
    __slots__ = ('_values',)

    def __init__(self, values):
        self._values = MappingProxyType(dict(values))

    def __repr__(self):
        return '<ProviderInputs redacted>'

    def get(self, name):
        _require(isinstance(name, str) and name in ALLOWED_KEYS and name in self._values)
        return self._values[name]

    def require_complete(self):
        _require(all(_complete(self._values.get(key)) for key in ALLOWED_KEYS - LEGACY_DNS_KEYS))

    def stage(self, stage):
        _require(isinstance(stage, str) and stage in STAGES)
        fields = {field: self._values.get(_stage_key(stage, field)) for field in STAGE_FIELDS}
        _require(_complete(self._values.get('R2_ACCOUNT_ID'))
                 and all(_complete(value) for value in fields.values()))
        prefix = stage.upper()
        return StageInputs({
            'R2_ACCOUNT_ID': self._values['R2_ACCOUNT_ID'],
            'R2_BUCKET': fields['R2_BUCKET'],
            f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{prefix}_ACCESS_KEY_ID': fields['R2_ACCESS_KEY_ID'],
            f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{prefix}_SECRET_ACCESS_KEY': fields['R2_SECRET_ACCESS_KEY'],
            'GOOGLE_CLIENT_ID': fields['GOOGLE_CLIENT_ID'],
            'GOOGLE_CLIENT_SECRET': fields['GOOGLE_CLIENT_SECRET'],
            'POSTHOG_API_KEY': fields['POSTHOG_API_KEY'], 'POSTHOG_HOST': fields['POSTHOG_HOST'],
            'MAIL_RESEND_SECRET_REF': 'secret://xynes/mail/resend-' + stage,
            f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{prefix}_API_KEY': fields['RESEND_API_KEY'],
            f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{prefix}_FROM_ADDRESS': fields['RESEND_FROM_ADDRESS'],
            'SMTP_HOST': 'smtp.resend.com', 'SMTP_PORT': '465', 'SMTP_USER': 'resend',
            'SMTP_PASS': fields['RESEND_API_KEY'], 'SMTP_ADMIN_EMAIL': fields['RESEND_FROM_ADDRESS'],
        })


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_file(info):
    _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
             and info.st_uid == os.getuid() and info.st_nlink == 1
             and 0 <= info.st_size <= MAX_BYTES)


def _parse(data):
    text = data.decode('utf-8')
    _require(all(character == '\n' or not unicodedata.category(character).startswith('C')
                 for character in text))
    _require(not any(character in text for character in ('$','`','\\')))
    values = {}
    for line in text.split('\n'):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', line)
        _require(match is not None)
        name, raw = match.groups()
        _require(name in ALLOWED_KEYS and name not in values)
        raw = raw.strip(' ')
        if raw.startswith(('"', "'")):
            quote = raw[0]
            end = raw.find(quote, 1)
            _require(end >= 1)
            remainder = raw[end + 1:]
            _require(not remainder or remainder.strip(' ') == ''
                     or (remainder.startswith(' ') and remainder.lstrip(' ').startswith('#')))
            value = raw[1:end]
        else:
            match = re.fullmatch(r'([^\s\'"#]*)(?: +(?:#.*)?)?', raw)
            _require(match is not None)
            value = match.group(1)
        values[name] = value
    return ProviderInputs(values)


def load(path):
    """Single-link, current-user, mode-0600 input; revalidate after bounded read."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        initial = os.fstat(fd)
        _safe_file(initial)
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(fd, min(8192, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        _require(size <= MAX_BYTES and size == initial.st_size)
        final = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        _safe_file(final)
        _safe_file(current)
        _require(_identity(initial) == _identity(final) == _identity(current))
        return _parse(b''.join(chunks))
    except Exception:
        raise ProviderInputError('Provider inputs refused') from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                raise ProviderInputError('Provider inputs refused') from None
