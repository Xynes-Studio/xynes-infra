#!/usr/bin/env python3
"""Refuse unsafe rendered deployment config without emitting its values."""
import json
from pathlib import Path
import re
import sys

SERVICES = {
    "gateway", "accounts-service", "authz-service", "cms-core", "doc-service",
    "storage-service", "telemetry-service", "auth-app", "cms-console-web",
    "clamav-clamd", "clamav-freshclam", "libreoffice-sidecar", "supabase-db", "supabase-auth",
}
DIGEST = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
SECRET_FILES = {
    "gateway-internal-request-private": "gateway-private.pem",
    "accounts-internal-request-private": "accounts-private.pem",
    "accounts-internal-request-trust": "accounts-trust.json",
    "authz-internal-request-trust": "authz-trust.json",
    "cms-internal-request-private": "cms-private.pem",
    "cms-internal-request-trust": "cms-trust.json",
    "docs-internal-request-private": "docs-private.pem",
    "docs-internal-request-trust": "docs-trust.json",
    "storage-internal-request-private": "storage-private.pem",
    "storage-internal-request-trust": "storage-trust.json",
    "telemetry-internal-request-private": "telemetry-private.pem",
    "telemetry-internal-request-trust": "telemetry-trust.json",
}
OWNER_PREFIXES = {
    "gateway": "gateway", "accounts-service": "accounts", "authz-service": "authz",
    "cms-core": "cms", "doc-service": "docs", "storage-service": "storage",
    "telemetry-service": "telemetry",
}
PROCESSOR_CONFIGS = {
    "definitions-ready", "freshclam-updater", "freshclam-health", "scanner-start", "scanner-health",
}
CONFIG_OWNERS = {
    "clamav-clamd": {"definitions-ready", "scanner-start", "scanner-health"},
    "clamav-freshclam": {"definitions-ready", "freshclam-updater", "freshclam-health"},
}
PROCESSOR_SERVICES = {"clamav-clamd", "clamav-freshclam", "libreoffice-sidecar"}
FRESHCLAM_CAPS = {"CHOWN", "FOWNER", "DAC_OVERRIDE", "SETUID", "SETGID"}
AUTH_SERVICES = {"supabase-db", "supabase-auth"}


def validate_auth(stage, services):
    db, auth = services['supabase-db'], services['supabase-auth']
    pins = json.loads((Path(__file__).resolve().parents[2] / 'infra/release/source-manifest.json').read_text())['external_images']
    reference_services = json.loads((Path(__file__).resolve().parents[2] / 'infra/release/auth/compose.fragment.yml').read_text())['services']
    for name in AUTH_SERVICES:
        spec = services[name]
        if (spec['image'] != pins[name]['image'] or spec.get('platform') != 'linux/amd64'
                or spec.get('labels', {}).get('com.xynes.idle') != 'never'
                or spec.get('restart') != 'unless-stopped'
                or spec.get('healthcheck', {}).get('disable')
                or spec.get('healthcheck', {}).get('test') != reference_services[name]['healthcheck']['test']):
            raise ValueError('unsafe Auth infrastructure')
    if db.get('user') != '70:70' or db.get('networks', {}).get('environment'):
        raise ValueError('unsafe database identity or alias')
    if auth.get('networks', {}).get('environment') != {'aliases':[f'supabase_auth_{stage}']}:
        raise ValueError('Auth alias outside environment')
    expected = [(f'/srv/xynes/{stage}/supabase/db','/var/lib/postgresql/data',False),
                (f'/srv/xynes/{stage}/compose/auth/bootstrap.sh','/docker-entrypoint-initdb.d/10-apex-auth.sh',True),
                (f'/srv/xynes/{stage}/compose/auth/bootstrap.sql','/opt/apex-auth/bootstrap.sql',True)]
    mounts = db.get('volumes', [])
    if len(mounts) != 3 or {(m.get('source'),m.get('target'),bool(m.get('read_only'))) for m in mounts} != set(expected):
        raise ValueError('database mount outside environment')
    if any(m.get('type') != 'bind' or m.get('bind',{}).get('create_host_path',False) is not False for m in mounts):
        raise ValueError('unsafe database bind')
    values = db.get('environment', {})
    if set(values) != {'POSTGRES_USER','POSTGRES_DB','POSTGRES_PASSWORD','POSTGRES_INITDB_ARGS','AUTH_DATABASE_PASSWORD'}:
        raise ValueError('unexpected database environment')
    password, auth_password = values['POSTGRES_PASSWORD'],values['AUTH_DATABASE_PASSWORD']
    if (not all(re.fullmatch(r'[a-f0-9]{64}',v) for v in (password,auth_password))
            or password == auth_password or values['POSTGRES_USER'] != 'postgres' or values['POSTGRES_DB'] != 'postgres'
            or values['POSTGRES_INITDB_ARGS'] != '--auth-host=scram-sha-256'):
        raise ValueError('unsafe database authentication')
    env = auth.get('environment', {})
    reference = reference_services['supabase-auth']['environment']
    if set(env) != set(reference):
        raise ValueError('unexpected Auth environment')
    for key, value in reference.items():
        if not value.startswith('${') and env[key] != value:
            raise ValueError('unsafe Auth setting')
    issuer = services['gateway']['environment']['JWT_ISSUER']
    if (env['GOTRUE_DB_DATABASE_URL'] != f'postgres://supabase_auth_admin:{auth_password}@supabase-db:5432/postgres'
            or env['GOTRUE_JWT_SECRET'] != services['gateway']['environment']['JWT_SECRET']
            or env['GOTRUE_JWT_ISSUER'] != issuer or env['API_EXTERNAL_URL'] != issuer
            or env['GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI'] != issuer+'/callback'
            or auth.get('depends_on',{}).get('supabase-db',{}).get('condition') != 'service_healthy'):
        raise ValueError('Auth credentials or dependencies mismatched')


def validate(stage, config):
    if stage not in {"qa", "staging", "prod"}:
        raise ValueError("unsupported environment")
    if config.get("name") != f"xynes-{stage}":
        raise ValueError("wrong project")
    services = config.get("services", {})
    if set(services) != SERVICES:
        raise ValueError("unexpected workload set")
    validate_auth(stage, services)
    networks = config.get("networks", {})
    if set(networks) != {"environment", "processors", "updates"}:
        raise ValueError("unexpected network inventory")
    network = networks["environment"]
    if network.get("name") != f"xynes_{stage}_net" or not network.get("external"):
        raise ValueError("wrong network")
    for name, internal in [("processors", True), ("updates", False)]:
        network = networks[name]
        if (network.get("name") != f"xynes_{stage}_{name}_net"
                or bool(network.get("internal")) != internal or network.get("external")
                or network.get("driver") != "bridge" or network.get("driver_opts")
                or network.get("attachable") or network.get("ipam")):
            raise ValueError("unsafe processor network")
        if network.get("labels", {}).get("com.xynes.environment") != stage:
            raise ValueError("processor network outside environment")
    configs = config.get("configs", {})
    if set(configs) != PROCESSOR_CONFIGS:
        raise ValueError("unexpected processor config inventory")
    for name, source in configs.items():
        if source.get("file") != f"/srv/xynes/{stage}/compose/processors/{name}.sh" or set(source) != {"file", "name"} or source.get("name") != f"xynes-{stage}_{name}":
            raise ValueError("processor config outside release environment")
    secrets = config.get("secrets", {})
    if set(secrets) != set(SECRET_FILES):
        raise ValueError("unexpected identity sources")
    for source, basename in SECRET_FILES.items():
        if secrets[source].get("file") != f"/srv/xynes/{stage}/identities/{basename}":
            raise ValueError("identity outside environment")
    volumes = config.get("volumes", {})
    if set(volumes) != {"clamav-defs"} or volumes["clamav-defs"].get("name") != f"xynes-{stage}_clamav-defs":
        raise ValueError("unexpected persistent volumes")
    for name, spec in services.items():
        expected_uid = {"storage-service": "1001:1001", "clamav-clamd": "1000:1000", "clamav-freshclam": "0:0", "libreoffice-sidecar": "10001:10001"}.get(name)
        if name in PROCESSOR_SERVICES | {"storage-service"} and (spec.get("labels", {}).get("com.xynes.idle") != "never" or spec.get("restart") != "unless-stopped"):
            raise ValueError("background processor must remain always on")
        if name in PROCESSOR_SERVICES and any(key.startswith("XYNES_") or key == "CVD_CERTS_DIR" for key in spec.get("environment", {})):
            raise ValueError("processor runtime override")
        if expected_uid and spec.get("user") != expected_uid:
            raise ValueError("unexpected processor identity")
        if not DIGEST.fullmatch(spec.get("image", "")):
            raise ValueError("non-immutable image")
        if any(spec.get(key) for key in ["ports", "build", "privileged", "env_file", "container_name", "devices", "device_requests", "pid", "ipc", "network_mode"]):
            raise ValueError("unsafe workload configuration")
        if spec.get("labels", {}).get("com.xynes.environment") != stage:
            raise ValueError("wrong workload environment")
        if spec.get("labels", {}).get("com.xynes.managed") != "true":
            raise ValueError("unmanaged workload")
        expected_networks = ({"environment", "processors"} if name == "storage-service" else
                             {"processors"} if name in {"clamav-clamd", "libreoffice-sidecar"} else
                             {"updates"} if name == "clamav-freshclam" else {"environment"})
        if set(spec.get("networks", {})) != expected_networks:
            raise ValueError("unexpected network attachment")
        for attachment in expected_networks - {"environment"}:
            if spec["networks"][attachment]:
                raise ValueError("unexpected processor network alias or addressing")
        expected_configs = CONFIG_OWNERS.get(name, set())
        config_mounts = spec.get("configs", [])
        if len(config_mounts) != len(expected_configs) or {m.get("source") for m in config_mounts} != expected_configs:
            raise ValueError("processor script outside owner")
        for mount in config_mounts:
            if (mount.get("target") != "/opt/xynes/processors/" + mount["source"] + ".sh"
                    or mount.get("mode") != "0444" or mount.get("uid") or mount.get("gid")):
                raise ValueError("unsafe processor script target or permissions")
        if name in CONFIG_OWNERS:
            script = "scanner-start.sh" if name == "clamav-clamd" else "freshclam-updater.sh"
            health = "scanner-health.sh" if name == "clamav-clamd" else "freshclam-health.sh"
            if spec.get("entrypoint") != ["/bin/sh", "/opt/xynes/processors/" + script]:
                raise ValueError("processor startup gate bypassed")
            if spec.get("healthcheck", {}).get("test") != ["CMD", "/bin/sh", "/opt/xynes/processors/" + health] or spec.get("healthcheck", {}).get("disable"):
                raise ValueError("processor readiness gate bypassed")
            if spec.get("command") or spec.get("environment") and name == "clamav-freshclam":
                raise ValueError("unexpected updater command or settings")
        if name == "clamav-clamd" and spec.get("depends_on", {}).get("clamav-freshclam", {}).get("condition") != "service_healthy":
            raise ValueError("definitions readiness dependency bypassed")
        mounts = spec.get("volumes", [])
        if name == 'storage-service':
            generation=spec.get('labels',{}).get('com.xynes.storage-generation','')
            expected={'/run/xynes/storage-control':(f'/run/xynes/storage/{stage}/'+generation[:32],False),
                      '/run/secrets/storage-quiesce-token':(f'/etc/xynes/storage-control/{stage}/control.token',True)}
            if not re.fullmatch(r'[a-f0-9]{64}',generation) or len(mounts)!=2 or {m.get('target') for m in mounts}!=set(expected):
                raise ValueError('unqualified storage control')
            for mount in mounts:
                source,readonly=expected[mount['target']]
                if (mount.get('type')!='bind' or mount.get('source')!=source
                        or bool(mount.get('read_only',False))!=readonly
                        or type(mount.get('bind')) is not dict
                        or set(mount['bind'])-{'create_host_path'}
                        or mount['bind'].get('create_host_path',False) is not False
                        or set(mount)-{'type','source','target','read_only','bind'}):
                    raise ValueError('unsafe storage control mount')
            if (spec.get('environment',{}).get('STORAGE_QUIESCENCE_ENABLED')!='1'
                    or any(key.startswith('STORAGE_CONTROL_') for key in spec.get('environment',{}))
                    or spec.get('command') or spec.get('entrypoint') or spec.get('init') is not True):
                raise ValueError('storage control bypass')
        if name not in {'supabase-db','storage-service'} and any(volume.get("type") != "volume" for volume in mounts):
            raise ValueError("host data mount")
        if name in {"clamav-clamd", "clamav-freshclam"} and len(mounts) != 1:
            raise ValueError("missing shared definitions volume")
        if name == "clamav-freshclam" and any(mount.get("read_only") for mount in mounts):
            raise ValueError("updater definitions volume is not writable")
        if mounts and name not in {"clamav-clamd", "clamav-freshclam", "supabase-db", "storage-service"}:
            raise ValueError("persistent volume outside owner")
        if name not in {'supabase-db','storage-service'} and any(volume.get("source") != "clamav-defs" or volume.get("target") != "/var/lib/clamav" for volume in mounts):
            raise ValueError("unexpected persistent mount")
        if name == "clamav-clamd" and any(not mount.get("read_only") for mount in mounts):
            raise ValueError("scanner definitions writable")
        if not spec.get("read_only"):
            raise ValueError("writable root filesystem")
        if "ALL" not in spec.get("cap_drop", []) or "no-new-privileges:true" not in spec.get("security_opt", []):
            raise ValueError("missing hardening")
        caps = set(spec.get("cap_add", []))
        if caps != (FRESHCLAM_CAPS if name == "clamav-freshclam" else set()):
            raise ValueError("unexpected capabilities")
        prefix = OWNER_PREFIXES.get(name)
        expected_sources = {source for source in SECRET_FILES if prefix and source.startswith(prefix + "-")}
        mounts = spec.get("secrets", [])
        if {mount.get("source") for mount in mounts} != expected_sources or len(mounts) != len(expected_sources):
            raise ValueError("identity mounted outside owner")
        for mount in mounts:
            target = "internal-request-private.pem" if mount["source"].endswith("-private") else "internal-request-trust.json"
            if mount.get("target") != target:
                raise ValueError("wrong identity mount target")
        env = spec.get("environment", {})
        posthog_keys = {key for key in env if "POSTHOG" in key}
        if name != "gateway" and posthog_keys or any(key.startswith("NEXT_PUBLIC_") for key in posthog_keys):
            raise ValueError("PostHog credential outside gateway")
        if name == "gateway" and (not env.get("POSTHOG_API_KEY") or not env.get("POSTHOG_HOST")
                                  or env.get("FEATURE_FLAGS_DEBUG") != "false"
                                  or env.get("POSTHOG_DEBUG", "false") != "false"):
            raise ValueError("unsafe gateway feature flag configuration")
        if name in {"auth-app", "cms-console-web"} and env.get("NEXT_API_URL") != f"http://gateway_{stage}:4100":
            raise ValueError("Frontend server gateway outside environment")
        if env.get("INTERNAL_SERVICE_TOKEN") or env.get("INTERNAL_JWT_SIGNING_KEY"):
            raise ValueError("shared internal authentication")
        if name != "gateway" and "JWT_SECRET" in env:
            raise ValueError("credential outside owner")
        for prefix, owner in [("MAIL_CREDENTIAL_", "accounts-service"), ("STORAGE_CREDENTIAL_", "storage-service")]:
            keys = [key for key in env if key.startswith(prefix)]
            if keys and name != owner:
                raise ValueError("credential outside owner")
            expected = (f"MAIL_CREDENTIAL_XYNES__MAIL__RESEND_{stage.upper()}_" if prefix == "MAIL_CREDENTIAL_" else f"STORAGE_CREDENTIAL_XYNES__STORAGE__R2_{stage.upper()}_")
            if any(not key.startswith(expected) for key in keys):
                raise ValueError("credential from another environment")


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("stage and private rendered-config file required")
        file = Path(sys.argv[2])
        if file.stat().st_size > 1024 * 1024:
            raise ValueError("config too large")
        validate(sys.argv[1], json.loads(file.read_text()))
    except (ValueError, OSError, TypeError, AttributeError, KeyError):
        print("Hosted configuration refused", file=sys.stderr)
        return 1
    print("Hosted configuration validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
