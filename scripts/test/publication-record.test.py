#!/usr/bin/env python3
"""Qualify real publisher output; fixtures have no provider or signing authority."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import release_manifest as records

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/publication_record.py'
  self.assertTrue(path.is_file(),'publication record qualification is missing')
  self.module=load('publication_record',path)
  fixture=load('publication_fixture',ROOT/'scripts/test/publish-batch.test.py').Tests()
  fixture.setUp();self.addCleanup(fixture.doCleanups)
  self.record=fixture.run_batch();self.manifest=fixture.manifest;self.public=fixture.public;self.controller=fixture.controller
 def qualify(self,record=None,**changes):
  arguments=dict(receipt=self.record if record is None else record,manifest=self.manifest,public=self.public,controller=self.controller)
  arguments.update(changes);return self.module.qualify(**arguments)
 def rehash(self,record):
  record['receipt_sha256']=records.canonical_sha256({k:v for k,v in record.items() if k!='receipt_sha256'})
  return record
 def refused(self,record=None,**changes):
  with self.assertRaises(self.module.PublicationError) as caught:self.qualify(record,**changes)
  self.assertEqual(str(caught.exception),'Publication record refused')
 def test_actual_complete_publisher_receipt_is_qualified_without_signing_authority(self):
  result=self.qualify();self.assertEqual(result,self.record)
  self.assertIsNot(result,self.record);self.assertIsNot(result['images'],self.record['images'])
  self.assertFalse(result['attested']);self.assertFalse(result['trust_verified']);self.assertFalse(result['deployable'])
 def test_tampered_hashes_and_fabricated_completion_claims_are_refused(self):
  for key,value in [('receipt_sha256','0'*64),('build_receipt_sha256','0'*64),('source_manifest_sha256','0'*64),('attested',True),('trust_verified',True),('deployable',True),('published',False),('visibility_verified',1)]:
   record=copy.deepcopy(self.record);record[key]=value
   if key!='receipt_sha256':self.rehash(record)
   self.refused(record)
 def test_complete_task_inventory_order_and_stage_bindings_are_required(self):
  for mode in ('missing','duplicate','reorder','environment','source','recipe','inputs','registry','digest'):
   record=copy.deepcopy(self.record)
   if mode=='missing':record['images'].pop()
   elif mode=='duplicate':record['images'][-1]=copy.deepcopy(record['images'][0])
   elif mode=='reorder':record['images'].reverse()
   else:
    row=record['images'][-1]
    key,value={'environment':('environment','qa'),'source':('source_commit','0'*40),'recipe':('recipe_sha256','0'*64),'inputs':('build_inputs_sha256','0'*64),'registry':('image','ghcr.io/other/app@'+row['manifest_digest']),'digest':('manifest_digest','sha256:'+'0'*64)}[mode]
    row[key]=value
   self.refused(self.rehash(record))
 def test_metadata_types_bounds_and_per_workload_package_ids_are_required(self):
  for key,value in [('archive_size',True),('archive_size',0),('archive_size',2*1024**3+1),('package_id',True),('package_id',0),('config_digest','invalid'),('archive_sha256','invalid'),('platform','linux/arm64'),('visibility_verified',False),('attested',True)]:
   record=copy.deepcopy(self.record);record['images'][-1][key]=value;self.refused(self.rehash(record))
  record=copy.deepcopy(self.record);record['images'][1]['package_id']=record['images'][0]['package_id'];self.refused(self.rehash(record))
  record=copy.deepcopy(self.record);record['images'][-1]['package_id']=999;self.refused(self.rehash(record))
 def test_controller_manifest_public_and_external_inputs_are_bound(self):
  controller=copy.deepcopy(self.controller);controller['run_id']+=1;self.refused(controller=controller)
  manifest=copy.deepcopy(self.manifest);manifest['status']='planning';manifest['approval_reference']=None;self.refused(manifest=manifest)
  public=copy.deepcopy(self.public);public['prod']['xynes-auth-app']['NEXT_PUBLIC_API_URL']='https://other.example.invalid';self.refused(public=public)
  record=copy.deepcopy(self.record);record['external_images']['supabase-auth']['image']='supabase/gotrue:v2.196.0@sha256:'+'0'*64;self.refused(self.rehash(record))
 def test_admission_evidence_and_exact_keys_cannot_be_omitted_or_extended(self):
  for mutate in (lambda r:r.update(approved=True),lambda r:r['images'][0].update(approved=True),lambda r:r['admission_evidence_sha256'].pop(),lambda r:r['admission_evidence_sha256'].__setitem__(0,'not-a-hash')):
   record=copy.deepcopy(self.record);mutate(record);self.refused(self.rehash(record))

if __name__=='__main__':unittest.main()
