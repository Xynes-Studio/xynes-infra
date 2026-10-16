#!/usr/bin/env python3
"""Render real hosted Compose with inert inputs; never start containers."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SERVICES = {
    "gateway", "accounts-service", "authz-service", "cms-core", "doc-service",
    "storage-service", "telemetry-service", "auth-app", "cms-console-web",
    "clamav-clamd", "clamav-freshclam", "libreoffice-sidecar", "supabase-db", "supabase-auth",
}
IMAGE_KEYS = [
    "GATEWAY", "ACCOUNTS", "AUTHZ", "CMS", "DOC", "STORAGE", "TELEMETRY",
    "AUTH_APP", "CMS_APP", "ARCHIVE_SCANNER", "FRESHCLAM", "LIBREOFFICE",
]


class HostedReleaseTests(unittest.TestCase):
    def test_storage_has_bounded_runtime_healthcheck_for_host_readiness(self):
        config=self.render('qa')
        health=config['services']['storage-service'].get('healthcheck',{})
        self.assertEqual(health.get('test'),['CMD','bun','-e',
            'try { const r = await fetch("http://127.0.0.1:4204/health", {signal: AbortSignal.timeout(2000)}); process.exit(r.ok ? 0 : 1); } catch { process.exit(1); }'])
        self.assertFalse(health.get('disable'))
    def test_storage_private_control_is_required_and_stage_generation_bound(self):
        config=self.render('qa');storage=config['services']['storage-service']
        self.assertEqual(storage.get('user'),'1001:1001')
        self.assertEqual(storage['environment'].get('STORAGE_QUIESCENCE_ENABLED'),'1')
        self.assertEqual(storage['labels'].get('com.xynes.storage-generation'),'b'*64)
        mounts={m['target']:m for m in storage.get('volumes',[])}
        self.assertEqual(set(mounts),{'/run/xynes/storage-control','/run/secrets/storage-quiesce-token'})
        self.assertEqual(mounts['/run/xynes/storage-control']['source'],'/run/xynes/storage/qa/'+'b'*32)
        self.assertEqual(mounts['/run/secrets/storage-quiesce-token']['source'],'/etc/xynes/storage-control/qa/control.token')
        for mount in mounts.values():
            self.assertEqual(mount['type'],'bind');self.assertFalse(mount['bind'].get('create_host_path',False))
        self.assertFalse(mounts['/run/xynes/storage-control'].get('read_only',False))
        self.assertTrue(mounts['/run/secrets/storage-quiesce-token']['read_only'])

    def test_validator_refuses_unqualified_storage_control_and_foreign_assets(self):
        config=self.render('qa')
        mutations=[lambda s:s.update(user='0:0'),lambda s:s['environment'].pop('STORAGE_QUIESCENCE_ENABLED',None),
            lambda s:s['environment'].update(STORAGE_QUIESCENCE_ENABLED='0'),
            lambda s:s['environment'].update(STORAGE_CONTROL_SOCKET='/tmp/foreign.sock'),
            lambda s:s['labels'].update({'com.xynes.storage-generation':'c'*64}),
            lambda s:s.update(volumes=[]),
            lambda s:s['volumes'][0].update(source='/run/xynes/storage/prod/'+'b'*32),
            lambda s:s['volumes'][0]['bind'].update(create_host_path=True),
            lambda s:s['volumes'][1].update(read_only=False)]
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'rendered.json'
            for mutate in mutations:
                candidate=json.loads(json.dumps(config));mutate(candidate['services']['storage-service']);file.write_text(json.dumps(candidate))
                result=subprocess.run(['python3',str(ROOT/'scripts/release/validate-hosted.py'),'qa',str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode,1)

    def render(self, stage, image_override=None):
        self.assertTrue((ROOT / "infra/compose/hosted.yml").is_file(), "hosted Compose missing")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XYNES_", "IMAGE_", "STORAGE_CONTROL_"))}
        values = {
            "XYNES_ENV": stage,
            "STORAGE_CONTROL_GENERATION": "b"*64,
            "STORAGE_CONTROL_DIRECTORY": f"/run/xynes/storage/{stage}/"+"b"*32,
            "DATABASE_URL": "postgres://postgres:"+"1"*64+"@supabase-db:5432/postgres",
            "ACCOUNTS_DATABASE_URL": "postgres://postgres:"+"1"*64+"@supabase-db:5432/postgres",
            "POSTGRES_PASSWORD":"1"*64,"AUTH_DATABASE_PASSWORD":"2"*64,
            "GOTRUE_DATABASE_URL":"postgres://supabase_auth_admin:"+"2"*64+"@supabase-db:5432/postgres",
            "API_EXTERNAL_URL":f"https://supabase.{stage}.example.invalid/auth/v1",
            "SITE_URL":f"https://auth.{stage}.example.invalid",
            "ADDITIONAL_REDIRECT_URLS":f"https://auth.{stage}.example.invalid/callback",
            "SMTP_PASS":"inert-mail-key","SMTP_ADMIN_EMAIL":f"no-reply@{stage}.example.invalid",
            "GOTRUE_EXTERNAL_GOOGLE_ENABLED":"true","GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID":"inert-google-id",
            "GOTRUE_EXTERNAL_GOOGLE_SECRET":"inert-google-secret",
            "GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI":f"https://supabase.{stage}.example.invalid/auth/v1/callback",
            "JWT_SECRET": "inert-test-only-value",
            "JWT_ISSUER": f"https://supabase.{stage}.example.invalid/auth/v1",
            "TELEMETRY_IP_HASH_SALT": "inert-telemetry-salt",
            "GATEWAY_CORS_ALLOWED_ORIGINS": f"https://auth.{stage}.example.invalid",
            "POSTHOG_API_KEY": f"inert-posthog-project-{stage}",
            "POSTHOG_HOST": "https://eu.i.posthog.com",
            "STORAGE_CREDENTIAL_REF": f"secret://xynes/storage/r2-{stage}",
            "STORAGE_ACCESS_KEY_ID": "inert-storage-id",
            "STORAGE_SECRET_ACCESS_KEY": "inert-storage-secret",
            "MAIL_RESEND_SECRET_REF": f"secret://xynes/mail/resend-{stage}",
            "MAIL_API_KEY": "inert-mail-key",
            "MAIL_FROM_ADDRESS": f"no-reply@{stage}.example.invalid",
            "INVITE_BASE_URL": f"https://auth.{stage}.example.invalid",
            "NEXT_PUBLIC_API_URL": f"https://api.{stage}.example.invalid",
            "NEXT_PUBLIC_AUTH_APP_URL": f"https://auth.{stage}.example.invalid",
            "NEXT_PUBLIC_CMS_APP_URL": f"https://cms.{stage}.example.invalid",
            "NEXT_PUBLIC_SUPABASE_URL": f"https://supabase.{stage}.example.invalid",
            "NEXT_PUBLIC_SUPABASE_ANON_KEY": "inert-anon-value",
            "NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS": f"auth.{stage}.example.invalid,cms.{stage}.example.invalid",
            "XYNES_INTERNAL_IDENTITIES_DIR": f"/srv/xynes/{stage}/identities",
        }
        for key in IMAGE_KEYS:
            values[f"IMAGE_{key}"] = f"registry.example.invalid/xynes/{key.lower()}@sha256:" + "a" * 64
        if image_override:
            values["IMAGE_GATEWAY"] = image_override
        with tempfile.TemporaryDirectory(prefix="xynes-hosted-config-") as tmp:
            env_file = Path(tmp) / "fixture.env"
            env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
            command = [
                "docker", "compose", "--env-file", str(env_file),
                "-f", str(ROOT / "infra/compose/hosted.yml"),
                "-f", str(ROOT / f"infra/compose/{stage}.yml"),
                "-f", str(ROOT / "infra/compose/internal-request-identities.yml"),
                "-f", str(ROOT / "infra/release/auth/compose.fragment.yml"),
                "config", "--format", "json",
            ]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "Compose fixture rendering failed")
            return json.loads(result.stdout)

    def test_three_stacks_are_isolated_and_runtime_only(self):
        for stage in ["qa", "staging", "prod"]:
            with self.subTest(stage=stage):
                config = self.render(stage)
                self.assertEqual(config["name"], f"xynes-{stage}")
                self.assertEqual(set(config["services"]), SERVICES)
                self.assertEqual(config["networks"]["environment"]["name"], f"xynes_{stage}_net")
                self.assertTrue(config["networks"]["environment"]["external"])
                for service, spec in config["services"].items():
                    self.assertNotIn("ports", spec, service)
                    self.assertNotIn("build", spec, service)
                    self.assertNotIn("env_file", spec, service)
                    self.assertNotIn("container_name", spec, service)
                    self.assertIn("@sha256:", spec["image"], service)
                    self.assertEqual(spec["labels"]["com.xynes.environment"], stage)
                    self.assertEqual(spec["labels"]["com.xynes.managed"], "true")
                    self.assertIn(spec["stop_grace_period"], ["60s", "1m0s"])
                    self.assertNotIn("privileged", spec)
                    for volume in spec.get("volumes", []):
                        self.assertEqual(volume["type"], "bind" if service in {"supabase-db","storage-service"} else "volume", service)
                for service in SERVICES - {"clamav-freshclam"}:
                    self.assertTrue(config["services"][service]["read_only"], service)
                    self.assertIn("ALL", config["services"][service]["cap_drop"])

    def test_database_auth_and_combined_config_are_private_and_stage_bound(self):
        for stage in ('qa','staging','prod'):
            config=self.render(stage)
            with tempfile.TemporaryDirectory() as tmp:
                file=Path(tmp)/'rendered.json';file.write_text(json.dumps(config))
                result=subprocess.run(['python3',str(ROOT/'scripts/release/validate-hosted.py'),stage,str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
            db=config['services']['supabase-db'];auth=config['services']['supabase-auth']
            self.assertEqual(db['user'],'70:70')
            self.assertEqual(db['volumes'][0]['source'],f'/srv/xynes/{stage}/supabase/db')
            self.assertEqual(auth['networks']['environment']['aliases'],[f'supabase_auth_{stage}'])
            self.assertEqual(auth['environment']['GOTRUE_JWT_SECRET'],config['services']['gateway']['environment']['JWT_SECRET'])
            self.assertEqual(auth['environment']['GOTRUE_JWT_ISSUER'],config['services']['gateway']['environment']['JWT_ISSUER'])
            self.assertEqual(auth['environment']['GOTRUE_MAILER_AUTOCONFIRM'],'false')

    def test_database_auth_validator_refuses_cross_stage_data_and_credentials(self):
        config=self.render('qa')
        mutations=[lambda c:c['services']['supabase-db']['volumes'][0].update(source='/srv/xynes/prod/supabase/db'),
                   lambda c:c['services']['supabase-db'].update(user='0:0'),
                   lambda c:c['services']['supabase-db']['environment'].update(AUTH_DATABASE_PASSWORD='1'*64),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_DB_DATABASE_URL='postgres://postgres:inert@supabase-db:5432/postgres'),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_MAILER_AUTOCONFIRM='true'),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_JWT_SECRET='different-key'),
                   lambda c:c['services']['supabase-auth']['networks']['environment'].update(aliases=['supabase_auth_prod']),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_JWT_KEYS='unreviewed'),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_JWT_DEFAULT_GROUP_NAME=''),
                   lambda c:c['services']['supabase-auth']['environment'].update(GOTRUE_SMTP_PORT='25'),
                   lambda c:c['services']['supabase-db']['healthcheck'].update(test=['CMD','echo','ready']),
                   lambda c:c['services']['supabase-auth']['healthcheck'].update(test=['CMD','echo','ready']),
                   lambda c:c['services']['supabase-db']['volumes'][0]['bind'].update(create_host_path=True)]
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'rendered.json'
            for mutate in mutations:
                candidate=json.loads(json.dumps(config));mutate(candidate);file.write_text(json.dumps(candidate))
                result=subprocess.run(['python3',str(ROOT/'scripts/release/validate-hosted.py'),'qa',str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode,1)
                self.assertNotIn('inert',result.stdout+result.stderr)

    def test_posthog_feature_flag_credentials_are_runtime_gateway_only(self):
        for stage in ["qa", "staging", "prod"]:
            config = self.render(stage)
            gateway = config["services"]["gateway"]["environment"]
            self.assertEqual(gateway.get("POSTHOG_API_KEY"), f"inert-posthog-project-{stage}")
            self.assertEqual(gateway.get("POSTHOG_HOST"), "https://eu.i.posthog.com")
            self.assertEqual(gateway.get("FEATURE_FLAGS_DEBUG"), "false")
            for service, spec in config["services"].items():
                if service != "gateway":
                    self.assertFalse(any("POSTHOG" in key for key in spec.get("environment", {})), service)

    def test_validator_refuses_posthog_key_leakage_and_debug_logging(self):
        config = self.render("qa")
        with tempfile.TemporaryDirectory(prefix="xynes-posthog-policy-") as tmp:
            file = Path(tmp) / "rendered.json"
            for service, key, value in [("auth-app", "NEXT_PUBLIC_POSTHOG_KEY", "inert-leak"),
                                        ("cms-core", "POSTHOG_API_KEY", "inert-leak"),
                                        ("gateway", "FEATURE_FLAGS_DEBUG", "true"),
                                        ("gateway", "POSTHOG_DEBUG", "true"),
                                        ("gateway", "POSTHOG_API_KEY", "")]:
                candidate = json.loads(json.dumps(config))
                candidate["services"][service]["environment"][key] = value
                file.write_text(json.dumps(candidate))
                result = subprocess.run(["python3", str(ROOT / "scripts/release/validate-hosted.py"), "qa", str(file)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, (service, key))
                self.assertNotIn("inert", result.stdout + result.stderr)

    def test_processors_have_closed_internal_network_and_updater_only_egress(self):
        for stage in ["qa", "staging", "prod"]:
            config = self.render(stage)
            self.assertEqual(set(config["networks"]), {"environment", "processors", "updates"})
            self.assertTrue(config["networks"]["processors"]["internal"])
            self.assertEqual(config["networks"]["processors"]["name"], f"xynes_{stage}_processors_net")
            self.assertEqual(config["networks"]["updates"]["name"], f"xynes_{stage}_updates_net")
            for service, spec in config["services"].items():
                expected = {"environment", "processors"} if service == "storage-service" else ({"processors"} if service in {"clamav-clamd", "libreoffice-sidecar"} else ({"updates"} if service == "clamav-freshclam" else {"environment"}))
                self.assertEqual(set(spec["networks"]), expected, service)
            scanner=config["services"]["clamav-clamd"]
            self.assertEqual(scanner["depends_on"]["clamav-freshclam"]["condition"], "service_healthy")
            self.assertIn("scanner-start.sh", " ".join(scanner["entrypoint"]))
            self.assertIn("scanner-health.sh", " ".join(scanner["healthcheck"]["test"]))
            updater=config["services"]["clamav-freshclam"]
            self.assertIn("freshclam-updater.sh", " ".join(updater["entrypoint"]))
            self.assertIn("freshclam-health.sh", " ".join(updater["healthcheck"]["test"]))
            self.assertTrue(updater["read_only"])

    def test_validator_refuses_processor_cross_environment_and_readiness_bypasses(self):
        config=self.render("qa")
        mutations = {
            "app_processor_access": lambda x: x["services"]["auth-app"]["networks"].update(processors={}),
            "scanner_app_access": lambda x: x["services"]["clamav-clamd"]["networks"].update(environment={}),
            "updater_app_access": lambda x: x["services"]["clamav-freshclam"]["networks"].update(environment={}),
            "updater_processor_access": lambda x: x["services"]["clamav-freshclam"]["networks"].update(processors={}),
            "public_processor_network": lambda x: x["networks"]["processors"].update(internal=False),
            "cross_stage_network": lambda x: x["networks"]["processors"].update(name="xynes_prod_processors_net"),
            "foreign_config": lambda x: x["configs"]["definitions-ready"].update(file="/srv/xynes/prod/compose/processors/definitions-ready.sh"),
            "wrong_startup_gate": lambda x: x["services"]["clamav-clamd"]["depends_on"]["clamav-freshclam"].update(condition="service_started"),
            "private_clamd_only": lambda x: x["services"]["clamav-clamd"]["healthcheck"].update(test=["CMD-SHELL", "echo PING | nc 127.0.0.1 3311"]),
            "no_updater_health": lambda x: x["services"]["clamav-freshclam"].pop("healthcheck"),
            "host_egress": lambda x: x["networks"]["updates"].update(external=True),
            "other_script_access": lambda x: x["services"]["auth-app"].update(configs=[{"source":"freshclam-updater","target":"/opt/xynes/processors/freshclam-updater.sh"}]),
            "processor_exec_override": lambda x: x["services"]["clamav-clamd"]["environment"].update(XYNES_SCANNER_EXECUTABLE="/tmp/unreviewed"),
            "missing_definitions_mount": lambda x: x["services"]["clamav-clamd"].update(volumes=[]),
            "readonly_updater_definitions": lambda x: x["services"]["clamav-freshclam"]["volumes"][0].update(read_only=True),
            "sleepable_processor": lambda x: x["services"]["clamav-clamd"]["labels"].update({"com.xynes.idle":"eligible"}),
        }
        with tempfile.TemporaryDirectory(prefix="xynes-processor-policy-") as tmp:
            file=Path(tmp)/"rendered.json"
            for label, mutate in mutations.items():
                candidate=json.loads(json.dumps(config));mutate(candidate);file.write_text(json.dumps(candidate))
                result=subprocess.run(["python3",str(ROOT/"scripts/release/validate-hosted.py"),"qa",str(file)],text=True,capture_output=True)
                self.assertEqual(result.returncode,1,label)
                self.assertNotIn("inert",result.stdout+result.stderr)

    def test_credentials_are_only_in_their_owner_service(self):
        config = self.render("staging")
        services = config["services"]
        self.assertEqual(services["accounts-service"]["environment"]["PORT"], "4203")
        self.assertEqual(services["gateway"]["environment"]["ACCOUNTS_SERVICE_URL"], "http://accounts-service:4203")
        for service, spec in services.items():
            env = spec.get("environment", {})
            if service != "gateway":
                self.assertNotIn("JWT_SECRET", env, service)
            if service != "accounts-service":
                self.assertFalse(any(key.startswith("MAIL_CREDENTIAL_") for key in env), service)
            if service != "storage-service":
                self.assertFalse(any(key.startswith("STORAGE_CREDENTIAL_") for key in env), service)
            self.assertFalse(env.get("INTERNAL_SERVICE_TOKEN"), service)
            self.assertFalse(env.get("INTERNAL_JWT_SIGNING_KEY"), service)
        self.assertEqual(services["authz-service"]["environment"]["AUTHZ_AUTO_SEED"], "0")
        self.assertEqual(services["storage-service"]["environment"]["STORAGE_PROCESSOR_MODE"], "live")
        self.assertEqual(services["libreoffice-sidecar"]["user"], "10001:10001")
        self.assertNotIn("secrets", services["auth-app"])
        self.assertNotIn("secrets", services["cms-console-web"])
        self.assertEqual(len(services["gateway"]["secrets"]), 1)
        self.assertEqual(services["gateway"]["networks"]["environment"]["aliases"], ["gateway_staging"])
        self.assertEqual(services["auth-app"]["networks"]["environment"]["aliases"], ["auth_app_staging"])

    def test_frontend_server_calls_use_only_their_stage_internal_gateway(self):
        validator = ROOT / "scripts/release/validate-hosted.py"
        for stage in ["qa", "staging", "prod"]:
            config = self.render(stage)
            self.assertEqual(config["services"]["cms-console-web"]["environment"].get("NEXT_API_URL"), f"http://gateway_{stage}:4100")
            self.assertEqual(config["services"]["auth-app"]["environment"].get("NEXT_API_URL"), f"http://gateway_{stage}:4100")
            with tempfile.TemporaryDirectory(prefix="xynes-cms-gateway-policy-") as tmp:
                file = Path(tmp) / "rendered.json"
                for app, endpoint in [(app, endpoint) for app in ["auth-app", "cms-console-web"] for endpoint in [None, "http://gateway_dev:4100", "https://api.example.invalid", "http://127.0.0.1:4100"]]:
                    candidate = json.loads(json.dumps(config))
                    env = candidate["services"][app]["environment"]
                    if endpoint is None:env.pop("NEXT_API_URL", None)
                    else:env["NEXT_API_URL"] = endpoint
                    file.write_text(json.dumps(candidate))
                    result = subprocess.run(["python3", str(validator), stage, str(file)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, endpoint)

    def test_image_validator_refuses_tags_and_unexpected_runtime_services(self):
        config = self.render("qa")
        validator = ROOT / "scripts/release/validate-hosted.py"
        self.assertTrue(validator.is_file(), "hosted validator missing")
        with tempfile.TemporaryDirectory(prefix="xynes-hosted-policy-") as tmp:
            file = Path(tmp) / "rendered.json"
            for mutation in ["valid", "tag", "host_port", "wrong_stage", "mount", "extra", "caps", "volume", "secret", "foreign_credential", "processor_uid"]:
                candidate = json.loads(json.dumps(config))
                if mutation == "tag":candidate["services"]["gateway"]["image"] = "xynes/gateway:latest"
                if mutation == "host_port":candidate["services"]["gateway"]["ports"] = [{"published": "4100", "target": 4100}]
                if mutation == "wrong_stage":candidate["services"]["gateway"]["labels"]["com.xynes.environment"] = "prod"
                if mutation == "mount":candidate["services"]["gateway"]["volumes"] = [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
                if mutation == "extra":candidate["services"]["unreviewed"] = candidate["services"]["gateway"]
                if mutation == "caps":candidate["services"]["gateway"]["cap_add"] = ["SYS_ADMIN"]
                if mutation == "volume":candidate["services"]["gateway"]["volumes"] = [{"type": "volume", "source": "prod-db", "target": "/data"}]
                if mutation == "secret":candidate["secrets"]["gateway-internal-request-private"]["file"] = "/srv/xynes/prod/identities/gateway-private.pem"
                if mutation == "foreign_credential":candidate["services"]["accounts-service"]["environment"]["MAIL_CREDENTIAL_XYNES__MAIL__RESEND_PROD_API_KEY"] = "inert-foreign-key"
                if mutation == "processor_uid":candidate["services"]["libreoffice-sidecar"]["user"] = "1000:1000"
                file.write_text(json.dumps(candidate))
                result = subprocess.run(["python3", str(validator), "qa", str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode, 0 if mutation == "valid" else 1, mutation)
                self.assertNotIn("inert-test-only-value",result.stdout+result.stderr)


if __name__ == "__main__":
    unittest.main()
