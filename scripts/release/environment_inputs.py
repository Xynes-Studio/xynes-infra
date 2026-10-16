#!/usr/bin/env python3
"""Prepare private stage settings; no network, host mutations or diagnostics."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import unicodedata
from types import MappingProxyType

try:
    from . import provider_inputs
except ImportError:
    import provider_inputs

ENVIRONMENT_KEYS = frozenset((
    'XYNES_ENV', 'POSTGRES_PASSWORD', 'DATABASE_URL',
    'ACCOUNTS_DATABASE_URL', 'JWT_SECRET', 'JWT_ISSUER',
    'JWT_AUDIENCE', 'JWT_MAX_TOKEN_LIFETIME_SECONDS', 'TELEMETRY_IP_HASH_SALT',
    'SUPABASE_URL', 'SUPABASE_PUBLIC_URL', 'SUPABASE_ANON_KEY',
    'SUPABASE_SERVICE_ROLE_KEY', 'ANON_KEY', 'SERVICE_ROLE_KEY',
    'API_EXTERNAL_URL', 'SITE_URL', 'ADDITIONAL_REDIRECT_URLS',
    'INVITE_BASE_URL', 'GATEWAY_CORS_ALLOWED_ORIGINS', 'NEXT_PUBLIC_API_URL',
    'NEXT_PUBLIC_GATEWAY_URL', 'NEXT_PUBLIC_AUTH_APP_URL', 'NEXT_PUBLIC_CMS_APP_URL',
    'NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_KEY', 'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS',
    'AUTH_COOKIE_DOMAIN', 'AUTH_COOKIE_SAMESITE', 'AUTH_COOKIE_SECURE',
    'POSTHOG_API_KEY', 'POSTHOG_HOST', 'FEATURE_FLAGS_DEBUG',
    'MAIL_PROVIDER', 'MAIL_API_KEY', 'MAIL_FROM_ADDRESS',
    'MAIL_RESEND_SECRET_REF', 'R2_ACCOUNT_ID', 'R2_BUCKET',
    'R2_REGION', 'STORAGE_ACCESS_KEY_ID', 'STORAGE_SECRET_ACCESS_KEY',
    'GOTRUE_EXTERNAL_GOOGLE_ENABLED', 'GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID', 'GOTRUE_EXTERNAL_GOOGLE_SECRET',
    'GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI', 'XYNES_INTERNAL_IDENTITIES_DIR', 'SMTP_HOST',
    'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS',
    'SMTP_ADMIN_EMAIL', 'SMTP_SENDER_NAME', 'AUTH_DATABASE_PASSWORD',
    'GOTRUE_DATABASE_URL',
))
AUTH_KEYS = frozenset(('AUTH_DATABASE_PASSWORD', 'GOTRUE_DATABASE_URL'))


class EnvironmentInputError(ValueError):
    """Only fixed, redacted refusals cross this boundary."""


def require(value):
    if not value:
        raise EnvironmentInputError('Environment inputs refused')


def token(secret, role, now):
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    claims = encode(json.dumps({'iss':'supabase','role':role,'iat':now,
                                'exp':now + 3650 * 86400}, separators=(',',':')).encode())
    payload = header + '.' + claims
    return payload + '.' + encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


class EnvironmentInputs:
    __slots__ = ('_stage', '_values')

    def __init__(self, stage, values):
        self._stage, self._values = stage, MappingProxyType(dict(values))

    def __repr__(self):
        return '<EnvironmentInputs redacted>'

    def get(self, name):
        require(isinstance(name,str) and name in self._values)
        return self._values[name]

    def write_new(self, path):
        """Exclusive local mode-0600 write into an owned mode-0700 directory.

        Refuse existing settings rather than silently rotating deployed keys.
        Host installation/0640 ownership is a separate coordinated operation.
        """
        directory = descriptor = None
        created = None
        try:
            path = Path(path)
            require(path.is_absolute() and path.name == '.env.' + self._stage)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(directory)
            require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid())
            data = ''.join(key + '=' + value + '\n' for key,value in self._values.items()).encode()
            require(len(data) <= 65536)
            descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=directory)
            info = os.fstat(descriptor);created = (info.st_dev,info.st_ino)
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_uid == os.getuid() and info.st_nlink == 1)
            position = 0
            while position < len(data):
                written = os.write(descriptor,data[position:]);require(written > 0);position += written
            os.fsync(descriptor)
            final = os.fstat(descriptor)
            current = os.stat(path.name,dir_fd=directory,follow_symlinks=False)
            require((current.st_dev,current.st_ino) == created
                    and stat.S_ISREG(current.st_mode) and stat.S_IMODE(current.st_mode) == 0o600
                    and current.st_uid == os.getuid() and current.st_nlink == 1
                    and current.st_size == final.st_size == len(data))
            identity = lambda info: (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
                                     info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            os.lseek(descriptor,0,os.SEEK_SET)
            chunks,read_size = [],0
            while read_size <= len(data):
                chunk = os.read(descriptor,min(8192,len(data)+1-read_size))
                if not chunk:break
                chunks.append(chunk);read_size += len(chunk)
            require(b''.join(chunks) == data and identity(os.fstat(descriptor)) == identity(final)
                    and identity(os.stat(path.name,dir_fd=directory,follow_symlinks=False)) == identity(current))
            os.fsync(directory)
        except Exception:
            if created is not None:
                try:
                    info = os.stat(path.name,dir_fd=directory,follow_symlinks=False)
                    if (info.st_dev,info.st_ino) == created:os.unlink(path.name,dir_fd=directory)
                except OSError:pass
            raise EnvironmentInputError('Environment inputs refused') from None
        finally:
            close_failed = False
            for fd in (descriptor,directory):
                if fd is not None:
                    try:os.close(fd)
                    except OSError:close_failed = True
            if close_failed:raise EnvironmentInputError('Environment inputs refused') from None


def prepare(stage, inputs, *, now=None):
    """Fresh independent credentials; call write_new once and retain that file.

    Legacy HS256 anon/service keys match the existing gateway/Supabase contract.
    Their API-key issuer is distinct from the Auth user-token JWT_ISSUER URL.
    No image pointers or completed-deployment claims are added here.
    """
    try:
        require(stage in provider_inputs.STAGES and isinstance(inputs,provider_inputs.ProviderInputs))
        now = int(time.time()) if now is None else now
        require(type(now) is int and 0 <= now <= 4102444800)
        namespace = 'erp.xynes.com' if stage == 'prod' else stage + '.xynes.com'
        auth,cms,api,supabase = ['https://' + name + '.' + namespace for name in ('auth','cms','api','supabase')]
        password,jwt,salt,auth_password = [secrets.token_hex(32) for _ in range(4)]
        anon,service = token(jwt,'anon',now),token(jwt,'service_role',now)
        values = {
            'XYNES_ENV':stage,'POSTGRES_PASSWORD':password,
            'AUTH_DATABASE_PASSWORD':auth_password,
            'GOTRUE_DATABASE_URL':auth_database_url(auth_password),
            'DATABASE_URL':f'postgres://postgres:{password}@supabase-db:5432/postgres',
            'ACCOUNTS_DATABASE_URL':f'postgres://postgres:{password}@supabase-db:5432/postgres',
            'JWT_SECRET':jwt,'JWT_ISSUER':supabase+'/auth/v1','JWT_AUDIENCE':'authenticated',
            'JWT_MAX_TOKEN_LIFETIME_SECONDS':'3600','TELEMETRY_IP_HASH_SALT':salt,
            'SUPABASE_URL':supabase,'SUPABASE_PUBLIC_URL':supabase,
            'SUPABASE_ANON_KEY':anon,'SUPABASE_SERVICE_ROLE_KEY':service,
            'ANON_KEY':anon,'SERVICE_ROLE_KEY':service,'API_EXTERNAL_URL':supabase+'/auth/v1',
            'SITE_URL':auth,'ADDITIONAL_REDIRECT_URLS':','.join(auth+path for path in
                ('/callback','/callback/client','/reset-password')),'INVITE_BASE_URL':auth,
            'GATEWAY_CORS_ALLOWED_ORIGINS':auth+','+cms,
            'NEXT_PUBLIC_API_URL':api,'NEXT_PUBLIC_GATEWAY_URL':api,
            'NEXT_PUBLIC_AUTH_APP_URL':auth,'NEXT_PUBLIC_CMS_APP_URL':cms,
            'NEXT_PUBLIC_SUPABASE_URL':supabase,'NEXT_PUBLIC_SUPABASE_ANON_KEY':anon,
            'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS':auth.removeprefix('https://')+','+cms.removeprefix('https://'),
            'AUTH_COOKIE_DOMAIN':'.'+namespace,'AUTH_COOKIE_SAMESITE':'Lax','AUTH_COOKIE_SECURE':'true',
            'FEATURE_FLAGS_DEBUG':'false','MAIL_PROVIDER':'resend','R2_REGION':'auto',
            'GOTRUE_EXTERNAL_GOOGLE_ENABLED':'true',
            'GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI':supabase+'/auth/v1/callback',
            'XYNES_INTERNAL_IDENTITIES_DIR':f'/srv/xynes/{stage}/identities',
        }
        values.update(provider_values(stage,inputs))
        values['SMTP_SENDER_NAME'] = 'Xynes'
        validate_values(values)
        return EnvironmentInputs(stage,values)
    except Exception:
        raise EnvironmentInputError('Environment inputs refused') from None


def auth_database_url(password):
    require(isinstance(password,str) and re.fullmatch(r'[a-f0-9]{64}',password) is not None)
    return 'postgres://supabase_auth_admin:' + password + '@supabase-db:5432/postgres'


def validate_values(values):
    require(all(isinstance(value,str) and value and not any(
        c.isspace() or c in "\"'`$\\#" or unicodedata.category(c).startswith('C') for c in value)
        for value in values.values()))


def provider_values(stage, inputs):
    require(stage in provider_inputs.STAGES and isinstance(inputs,provider_inputs.ProviderInputs))
    provider = inputs.stage(stage);prefix = stage.upper()
    values = {
        'R2_ACCOUNT_ID':provider.get('R2_ACCOUNT_ID'),'R2_BUCKET':provider.get('R2_BUCKET'),
        'STORAGE_ACCESS_KEY_ID':provider.get(f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{prefix}_ACCESS_KEY_ID'),
        'STORAGE_SECRET_ACCESS_KEY':provider.get(f'STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{prefix}_SECRET_ACCESS_KEY'),
        'MAIL_API_KEY':provider.get(f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{prefix}_API_KEY'),
        'MAIL_FROM_ADDRESS':provider.get(f'MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{prefix}_FROM_ADDRESS'),
        'MAIL_RESEND_SECRET_REF':provider.get('MAIL_RESEND_SECRET_REF'),
        'GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID':provider.get('GOOGLE_CLIENT_ID'),
        'GOTRUE_EXTERNAL_GOOGLE_SECRET':provider.get('GOOGLE_CLIENT_SECRET'),
        'POSTHOG_API_KEY':provider.get('POSTHOG_API_KEY'),'POSTHOG_HOST':provider.get('POSTHOG_HOST'),
    }
    for key in ('SMTP_HOST','SMTP_PORT','SMTP_USER','SMTP_PASS','SMTP_ADMIN_EMAIL'):
        values[key] = provider.get(key)
    require(values['POSTHOG_HOST'] in ('https://eu.i.posthog.com','https://us.i.posthog.com',
                                      'https://eu.posthog.com','https://us.posthog.com','https://app.posthog.com'))
    require(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',values['MAIL_FROM_ADDRESS']))
    validate_values(values)
    return values


def identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def safe_file(info):
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.getuid() and info.st_nlink == 1 and 0 < info.st_size <= 65536)


def read_bounded(fd):
    os.lseek(fd,0,os.SEEK_SET)
    chunks,size = [],0
    while size <= 65536:
        chunk = os.read(fd,min(8192,65537-size))
        if not chunk:break
        chunks.append(chunk);size += len(chunk)
    require(size <= 65536)
    return b''.join(chunks)


def parse_existing(data, stage):
    values = {}
    for line in data.decode('utf-8').split('\n'):
        if not line:continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.+)',line)
        require(match is not None)
        key,value = match.groups()
        require(key in ENVIRONMENT_KEYS and key not in values)
        values[key] = value
    require(set(values) in (ENVIRONMENT_KEYS,ENVIRONMENT_KEYS-AUTH_KEYS)
            and values['XYNES_ENV'] == stage)
    validate_values(values)
    if AUTH_KEYS <= values.keys():
        require(values['GOTRUE_DATABASE_URL'] == auth_database_url(values['AUTH_DATABASE_PASSWORD']))
    return values


def refresh(stage, inputs, path):
    """Atomically refresh a private prepared file, preserving all generated keys.

    This accepts only the exact prepared schema (including its legacy variant).
    Existing public URLs and non-provider values remain byte-for-byte equal.
    A missing Auth-role password is generated once; an existing pair is retained.
    Host installation is deliberately a separate operation. Concurrent mutation
    detected before replacement refuses without deleting the original file.
    """
    directory = original = temporary = None
    temporary_name = temporary_identity = None
    try:
        updates = provider_values(stage,inputs)
        path = Path(path)
        require(path.is_absolute() and path.name == '.env.' + stage)
        directory = os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parent = os.fstat(directory)
        require(stat.S_IMODE(parent.st_mode) == 0o700 and parent.st_uid == os.getuid())
        original = os.open(path.name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,dir_fd=directory)
        initial = os.fstat(original);safe_file(initial)
        before = read_bounded(original)
        require(len(before) == initial.st_size and identity(os.fstat(original)) == identity(initial)
                and identity(os.stat(path.name,dir_fd=directory,follow_symlinks=False)) == identity(initial))
        values = parse_existing(before,stage)
        values.update(updates)
        if not AUTH_KEYS <= values.keys():
            password = secrets.token_hex(32)
            values.update(AUTH_DATABASE_PASSWORD=password,GOTRUE_DATABASE_URL=auth_database_url(password))
        data = ''.join(key+'='+value+'\n' for key,value in values.items()).encode()
        require(len(data) <= 65536)
        temporary_name = '.apex-env-' + secrets.token_urlsafe(18)
        temporary = os.open(temporary_name,os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                            0o600,dir_fd=directory)
        current = os.fstat(temporary);temporary_identity = (current.st_dev,current.st_ino)
        position = 0
        while position < len(data):
            written = os.write(temporary,data[position:]);require(written > 0);position += written
        os.fsync(temporary)
        final = os.fstat(temporary);safe_file(final)
        require(final.st_size == len(data) and read_bounded(temporary) == data
                and identity(os.fstat(temporary)) == identity(final)
                and identity(os.stat(temporary_name,dir_fd=directory,follow_symlinks=False)) == identity(final))
        # Re-read both the held descriptor and pathname immediately before replacing.
        require(identity(os.fstat(original)) == identity(initial) and read_bounded(original) == before
                and identity(os.fstat(original)) == identity(initial)
                and identity(os.stat(path.name,dir_fd=directory,follow_symlinks=False)) == identity(initial))
        current_parent = os.stat(path.parent,follow_symlinks=False)
        require((current_parent.st_dev,current_parent.st_ino,current_parent.st_mode,current_parent.st_uid)
                == (parent.st_dev,parent.st_ino,parent.st_mode,parent.st_uid))
        os.replace(temporary_name,path.name,src_dir_fd=directory,dst_dir_fd=directory)
        temporary_name = None
        os.fsync(directory)
        final = os.fstat(temporary);safe_file(final)
        require(read_bounded(temporary) == data and identity(os.fstat(temporary)) == identity(final)
                and identity(os.stat(path.name,dir_fd=directory,follow_symlinks=False)) == identity(final))
        return EnvironmentInputs(stage,values)
    except Exception:
        raise EnvironmentInputError('Environment inputs refused') from None
    finally:
        if temporary_name is not None and temporary_identity is not None:
            try:
                current = os.stat(temporary_name,dir_fd=directory,follow_symlinks=False)
                if (current.st_dev,current.st_ino) == temporary_identity:
                    os.unlink(temporary_name,dir_fd=directory)
            except OSError:pass
        failed = False
        for fd in (temporary,original,directory):
            if fd is not None:
                try:os.close(fd)
                except OSError:failed=True
        if failed:raise EnvironmentInputError('Environment inputs refused') from None
