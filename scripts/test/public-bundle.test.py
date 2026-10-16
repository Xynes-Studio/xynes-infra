#!/usr/bin/env python3
import os
import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
TOOL=ROOT/'scripts/release/public_bundle.py'
HOST_ASSETS=['infra/compose/'+name for name in ('qa.yml','staging.yml','prod.yml','internal-request-identities.yml')]+[
 'infra/processors/'+name+'.sh' for name in ('definitions-ready','freshclam-updater','freshclam-health','scanner-start','scanner-health')]
BACKUP_UNITS=['infra/release/systemd/'+name for name in ('xynes-backup@.service','xynes-backup@.timer','xynes-backup-alert@.service')]
INGRESS_IDLE_ASSETS=['scripts/setup/configure-apex-ingress.py','infra/release/systemd/xynes-idle-tmpfiles.conf',
 *['infra/release/systemd/xynes-idle-'+stage+'-'+role+'.service' for stage in ('qa','staging','prod') for role in ('auth','cms','broker')]]
GUARD_SUPPORT=['scripts/setup/vps-reconcile.sh',
 'scripts/test/cms-content-delivery-routes.test.sh','scripts/test/cms-delivery-contract-parity.test.sh',
 *['scripts/test/fixtures/cms-delivery/'+name for name in ('routes-prerequisites.test.py','routes-contract.py','access-smoke.py')],
 *['scripts/test/fixtures/cms-content-integrations/'+name for name in ('pinned-inputs.test.py','pinned_inputs.py','smoke-safety.test.py','smoke.py','parity.py','schema-probe.ts','copied-request.ts','copied-request-hooks.ts','copied-request-unit.test.ts')]]
class Tests(unittest.TestCase):
 def test_only_exact_reviewed_auth_assets_are_included(self):
  import sys
  sys.path.insert(0,str(ROOT/'scripts/release'))
  spec=importlib.util.spec_from_file_location('public_bundle',TOOL);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  self.assertTrue(module.allowed('infra/release/verifier-toolchain.json'))
  self.assertTrue(module.allowed('infra/release/backup-toolchain.json'))
  for path in ['compose.fragment.yml','bootstrap.sh','bootstrap.sql','templates.Caddyfile',
               *['templates/'+kind+'.html' for kind in ('confirmation','recovery','magic_link','invite','email_change','reauthentication')]]:
   self.assertTrue(module.allowed('infra/release/auth/'+path),path)
  for path in ('credentials.json','.env.prod','templates/customer.html','private/report.md','bootstrap.sql.bak'):
   self.assertFalse(module.allowed('infra/release/auth/'+path),path)
  for path in HOST_ASSETS:self.assertTrue(module.allowed(path),path)
  for path in BACKUP_UNITS+INGRESS_IDLE_ASSETS:self.assertTrue(module.allowed(path),path)
  self.assertFalse(module.allowed('infra/release/systemd/private.service'))
  self.assertFalse(module.allowed('infra/release/systemd/xynes-idle-other-auth.service'))
  self.assertFalse(module.allowed('scripts/setup/configure-apex-ingress.py.bak'))
  for path in GUARD_SUPPORT:self.assertTrue(module.allowed(path),path)
  for path in ('scripts/test/fixtures/cms-content-integrations/private.py','scripts/test/fixtures/cms-delivery/internal-report.md','scripts/setup/vps-reconcile.sh.bak'):
   self.assertFalse(module.allowed(path),path)
  for path in ('infra/compose/customer.yml','infra/compose/qa.yml.bak','infra/processors/private.sh',
               'infra/processors/definitions-ready.sh.bak','infra/processors/.env.prod'):
   self.assertFalse(module.allowed(path),path)
 def test_operational_allowlist_excludes_tracked_internal_material(self):
  with tempfile.TemporaryDirectory(prefix='apex-public-bundle-') as tmp:
   repo=Path(tmp)/'repo';repo.mkdir();out=Path(tmp)/'source.tar'
   subprocess.run(['git','init','-q',str(repo)],check=True)
   for file in ['scripts/release/controller.py','scripts/lib/helper.py','infra/release/source-check-catalog.json','infra/images/app.Dockerfile','infra/compose/hosted.yml','supabase/migrations/20260101000000_schema.sql','docs/private-report.md','security-audits/internal.md','.env.prod','private-source/application.ts','scripts/release/reports/deploy-record.json','scripts/lib/generated/customer-data.json',*HOST_ASSETS,*GUARD_SUPPORT,*BACKUP_UNITS,*INGRESS_IDLE_ASSETS,'infra/release/systemd/private.service','infra/processors/private.sh','infra/compose/customer.yml']:
    path=repo/file;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# inert fixture\n')
   subprocess.run(['git','-C',str(repo),'add','.'],check=True)
   subprocess.run(['git','-c','user.name=fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','-C',str(repo),'commit','-qm','fixture'],check=True)
   commit=subprocess.run(['git','-C',str(repo),'rev-parse','HEAD'],text=True,capture_output=True,check=True).stdout.strip()
   p=subprocess.run(['python3',str(TOOL),'--repo',str(repo),'--commit',commit,'--output',str(out)],text=True,capture_output=True)
   self.assertEqual(p.returncode,0,'public operational bundler missing or refused inert fixture')
   with tarfile.open(out) as archive:names=set(archive.getnames())
   self.assertIn('scripts/release/controller.py',names)
   self.assertIn('infra/images/app.Dockerfile',names)
   self.assertIn('infra/release/source-check-catalog.json',names)
   for path in HOST_ASSETS:self.assertIn(path,names)
   for path in BACKUP_UNITS+INGRESS_IDLE_ASSETS:self.assertIn(path,names)
   self.assertNotIn('infra/release/systemd/private.service',names)
   for path in GUARD_SUPPORT:self.assertIn(path,names)
   self.assertNotIn('infra/processors/private.sh',names)
   self.assertNotIn('infra/compose/customer.yml',names)
   self.assertNotIn('scripts/release/reports/deploy-record.json',names)
   self.assertNotIn('scripts/lib/generated/customer-data.json',names)
   self.assertFalse(any(x.startswith(('docs/','security-audits/','private-source/')) or '.env' in x for x in names))
 def test_mutable_ref_and_existing_output_are_refused_without_diagnostics(self):
  with tempfile.TemporaryDirectory(prefix='apex-public-bundle-') as tmp:
   out=Path(tmp)/'source.tar';out.write_bytes(b'preserve')
   p=subprocess.run(['python3',str(TOOL),'--repo',str(ROOT),'--commit','HEAD','--output',str(out)],text=True,capture_output=True)
   self.assertNotEqual(p.returncode,0);self.assertEqual(out.read_bytes(),b'preserve')
if __name__=='__main__':unittest.main()
