#!/usr/bin/env python3
"""Check the real release graph, data artifacts and isolated signing boundary."""
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]

class Tests(unittest.TestCase):
 def setUp(self):self.value=json.loads((ROOT/'.github/workflows/apex-build.yml').read_text());self.jobs=self.value['jobs']
 def test_only_manual_protected_main_can_build_publish_and_sign(self):
  self.assertEqual(self.value['on'],{'workflow_dispatch':{}});self.assertEqual(self.value['permissions'],{'contents':'read'})
  self.assertFalse(self.value['concurrency']['cancel-in-progress']);self.assertEqual(set(self.jobs),{'admission','build-images','publish-images','attest-images'})
  for job in self.jobs.values():
   self.assertIn("github.event_name == 'workflow_dispatch'",job['if']);self.assertIn("github.ref == 'refs/heads/main'",job['if']);self.assertIn('github.ref_protected',job['if'])
  self.assertEqual(self.jobs['build-images']['needs'],['admission'])
  self.assertEqual(self.jobs['publish-images']['needs'],['admission','build-images'])
  self.assertEqual(self.jobs['attest-images']['needs'],['publish-images'])
 def test_data_only_admission_and_complete_oci_build_artifacts(self):
  for id in ('admission','build-images'):
   job=self.jobs[id];self.assertNotIn('environment',job);self.assertTrue(all(level=='read' for level in job['permissions'].values()))
   self.assertNotIn('secrets.',json.dumps(job));self.assertIn('artifact-digest',job['outputs']['receipt'])
  build=self.jobs['build-images'];upload=next(step for step in build['steps'] if step.get('id')=='bundle')
  self.assertEqual(upload['with']['path'],'${{ runner.temp }}/apex-images/image-receipts.json\n${{ runner.temp }}/apex-images/archives/*.oci.tar')
  self.assertNotIn('logs',upload['with']['path']);self.assertNotIn('contexts',upload['with']['path'])
  for job,operation in [('admission','admission'),('build-images','build')]:
   calls=[step for step in self.jobs[job]['steps'] if 'run' in step]
   self.assertEqual([s['run'] for s in calls],['python3 scripts/release/build_pipeline.py '+operation])
  self.assertTrue((ROOT/'scripts/release/build_pipeline.py').is_file())
 def test_migrations_are_a_separate_exact_data_artifact(self):
  build=self.jobs['build-images'];uploads=[step for step in build['steps'] if step.get('uses','').startswith('actions/upload-artifact@')]
  self.assertEqual(len(uploads),2)
  step=next(item for item in uploads if item.get('id')=='migrations')
  self.assertEqual(step['uses'],'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02')
  self.assertEqual(step['with'],{'name':'apex-migration-${{ github.run_id }}-${{ github.run_attempt }}',
      'path':'${{ runner.temp }}/apex-migrations/migration-bundle.tar','if-no-files-found':'error','retention-days':14,'compression-level':0})
  self.assertNotIn('migrations',self.jobs['publish-images']['outputs'])
 def test_publisher_persists_exact_receipt_and_signer_uses_verified_subjects_only(self):
  publisher=self.jobs['publish-images'];self.assertEqual(publisher['environment'],'release')
  self.assertEqual(publisher['permissions'],{'contents':'read','actions':'read','packages':'write'})
  self.assertEqual(publisher['steps'][1]['run'],'python3 scripts/release/publish_images.py')
  upload=publisher['steps'][2];self.assertEqual(upload['with']['path'],'${{ runner.temp }}/apex-publications/published-receipt.json')
  self.assertEqual(upload['with']['if-no-files-found'],'error')
  self.assertEqual(publisher['outputs']['subjects'],'${{ steps.publish.outputs.subjects }}')
  self.assertIn('steps.publication.outputs.artifact-digest',publisher['outputs']['receipt'])
  signer=self.jobs['attest-images'];self.assertEqual(signer['environment'],'release')
  self.assertEqual(signer['permissions'],{'contents':'read','id-token':'write','attestations':'write'})
  self.assertEqual(signer['strategy']['matrix']['subject'],'${{ fromJSON(needs.publish-images.outputs.subjects) }}')
  self.assertEqual(len(signer['steps']),1);self.assertTrue(signer['steps'][0]['uses'].startswith('actions/attest@'))
  self.assertFalse(signer['steps'][0]['with']['push-to-registry']);self.assertNotIn('run',signer['steps'][0])

if __name__=='__main__':unittest.main()
