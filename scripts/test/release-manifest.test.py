#!/usr/bin/env python3
"""Structural image-record checks, using inert registry/run/artifact fixtures."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/'scripts/release/release_manifest.py'
PREFIX='registry.example.invalid/inert'
BACKENDS=['xynes-gateway','xynes-accounts-service','xynes-authz-service','xynes-cms-core','xynes-doc-service','xynes-telemetry-service','xynes-storage-service']
APPS=['xynes-auth-app','xynes-cms-console-web']
STAGES=['qa','staging','prod']
SHARED=BACKENDS+['archive-scanner','libreoffice','clamav-freshclam','supabase-db','supabase-auth']


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def fixtures():
    source=json.loads((ROOT/'infra/release/source-manifest.json').read_text())
    source['status']='planning';source['approval_reference']=None
    source['external_images']['clamav-freshclam']['image']='clamav/clamav:1.5.2-debian@sha256:'+'a'*64
    source['external_images'].update({
        'supabase-db': {'image':'postgres:17-alpine@sha256:'+'b'*64,'platform':'linux/amd64'},
        'supabase-auth': {'image':'supabase/gotrue:v2.196.0@sha256:'+'c'*64,'platform':'linux/amd64'}})
    public={}
    for stage in STAGES:
        common={'NEXT_PUBLIC_AUTH_APP_URL':f'https://auth.{stage}.example.invalid','NEXT_PUBLIC_API_URL':f'https://api.{stage}.example.invalid','NEXT_PUBLIC_SUPABASE_URL':f'https://supabase.{stage}.example.invalid','NEXT_PUBLIC_SUPABASE_ANON_KEY':'sb_publishable_'+'a'*24,'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS':f'auth.{stage}.example.invalid,cms.{stage}.example.invalid'}
        public[stage]={'xynes-auth-app':{**common,'NEXT_PUBLIC_APP_URL':common['NEXT_PUBLIC_AUTH_APP_URL'],'NEXT_PUBLIC_CONSOLE_URL':f'https://cms.{stage}.example.invalid','NEXT_PUBLIC_CMS_CONSOLE_URL':f'https://cms.{stage}.example.invalid'},'xynes-cms-console-web':{**common,'NEXT_PUBLIC_APP_URL':f'https://cms.{stage}.example.invalid'}}
    artifacts={'operational':{'bundle_sha256':'b'*64,'binaries':{'xynes-idle':{'platform':'linux/amd64','sha256':'c'*64}}},'migrations':{name:[] for name in ['xynes-infra']+BACKENDS}}
    artifacts['migrations']['xynes-infra']=[{'path':'supabase/migrations/20261010090000_fixture.sql','sha256':'d'*64}]
    controller={'repository':'Xynes-Studio/xynes-infra','commit':'1'*40,'workflow':'apex-build.yml','run_id':1,'run_attempt':1}
    record={'schema_version':1,'artifact_kind':'apex-image-record','controller':controller,'source_manifest_sha256':digest(source),'images':{'shared':{},'frontends':{}},**copy.deepcopy(artifacts)}
    def image(name,stage=None,index=1):
        if name in ('clamav-freshclam','supabase-db','supabase-auth'):
            fields={'image':source['external_images'][name]['image'],'platform':'linux/amd64'}
            inputs={'controller':controller,'source_manifest_sha256':digest(source),'external_image':source['external_images'][name]}
        else:
            owner=name if name in BACKENDS+APPS else 'xynes-storage-service'
            recipe=source['recipes'][name] if name in BACKENDS+APPS else source['processors'][name]
            fields={'image':PREFIX+'/'+name+'@sha256:'+format(index,'064x'),'platform':'linux/amd64','source_commit':source['sources'][owner]['commit'],'recipe_sha256':recipe['sha256']}
            inputs={'controller':controller,'source_manifest_sha256':digest(source),'source_commit':fields['source_commit'],'recipe_sha256':fields['recipe_sha256'],'base_images':source['base_images'],'tools':source['tools']}
            if stage:
                fields['public_inputs_sha256']=digest(public[stage][name]);inputs.update(environment=stage,public_inputs_sha256=fields['public_inputs_sha256'])
        fields['build_inputs_sha256']=digest(inputs);return fields
    for i,name in enumerate(SHARED):record['images']['shared'][name]=image(name,index=i+10)
    for i,stage in enumerate(STAGES):record['images']['frontends'][stage]={name:image(name,stage,i*2+j+100) for j,name in enumerate(APPS)}
    return record,source,public,artifacts


class ReleaseRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-image-record-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.record,self.source,self.public,self.artifacts=fixtures()

    def call(self,record=None,source=None,public=None,artifacts=None,prefix=PREFIX,raw=None):
        self.assertTrue(SCRIPT.exists(),'structural image-record parser is missing')
        files={}
        for name,value in [('record',record if record is not None else self.record),('source',source if source is not None else self.source),('public',public if public is not None else self.public),('artifacts',artifacts if artifacts is not None else self.artifacts)]:
            path=self.root/(name+'.json');path.write_text(raw if name=='record' and raw is not None else json.dumps(value));files[name]=path
        return subprocess.run(['python3',str(SCRIPT),'--record',str(files['record']),'--source-manifest',str(files['source']),'--public-inputs',str(files['public']),'--artifact-bindings',str(files['artifacts']),'--registry-prefix',prefix],capture_output=True,text=True,timeout=10,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})

    def refuse(self,record=None,**kwargs):
        p=self.call(record,**kwargs);self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertNotIn('DO-NOT-PRINT',p.stdout+p.stderr)

    def test_valid_record_returns_only_structural_summary_without_authorization(self):
        p=self.call();self.assertEqual(p.returncode,0,p.stderr);summary=json.loads(p.stdout)
        self.assertEqual(summary,{'artifact_kind':'apex-image-record','record_sha256':digest(self.record),'structurally_valid':True,'trust_verified':False,'deployable':False})
        self.assertNotIn(PREFIX,p.stdout)

    def test_rejects_missing_unknown_sources_workloads_environments_platforms(self):
        changes=[lambda x:x['images']['shared'].pop('xynes-gateway'),lambda x:x['images']['shared'].update(unknown=x['images']['shared']['xynes-gateway']),lambda x:x['images']['frontends'].update(dev=x['images']['frontends']['qa']),lambda x:x['images']['frontends']['qa'].pop('xynes-auth-app'),lambda x:x['images']['shared']['xynes-gateway'].update(platform='linux/arm64'),lambda x:x.update(deployable=True)]
        for mutate in changes:
            with self.subTest(mutate=mutate):x=copy.deepcopy(self.record);mutate(x);self.refuse(x)

    def test_rejects_moving_controller_or_bad_run_and_foreign_workflow(self):
        for change in [{'commit':'main'},{'repository':'attacker/infra'},{'workflow':'../evil.yml'},{'run_id':0},{'run_id':True},{'run_attempt':0}]:
            x=copy.deepcopy(self.record);x['controller'].update(change);self.refuse(x)

    def test_rejects_local_config_ids_mutable_tags_foreign_registries_and_wrong_workload(self):
        for value in ['sha256:'+'a'*64,PREFIX+'/xynes-gateway:latest',PREFIX+'/xynes-gateway:latest@sha256:'+'a'*64,'attacker.invalid/inert/xynes-gateway@sha256:'+'a'*64,PREFIX+'/xynes-accounts-service@sha256:'+'a'*64,'https://user:DO-NOT-PRINT@registry.example.invalid/image']:
            x=copy.deepcopy(self.record);x['images']['shared']['xynes-gateway']['image']=value;self.refuse(x)

    def test_registry_policy_prefix_is_explicit_and_canonical(self):
        for prefix in ['','registry.example.invalid/inert/','https://registry.example.invalid/inert','user:DO-NOT-PRINT@registry.example.invalid/inert','registry.example.invalid/inert/../other']:
            self.refuse(prefix=prefix)

    def test_image_source_recipe_base_tool_bindings_cannot_drift(self):
        for key in ['source_commit','recipe_sha256','build_inputs_sha256']:
            x=copy.deepcopy(self.record);x['images']['shared']['xynes-gateway'][key]='2'*(40 if key=='source_commit' else 64);self.refuse(x)
        for change in ['base','tool','source']:
            source=copy.deepcopy(self.source)
            if change=='base':source['base_images'].append('example.invalid/base:1@sha256:'+'2'*64)
            if change=='tool':source['tools']['pnpm']='10.34.0'
            if change=='source':source['sources']['xynes-gateway']['commit']='2'*40
            self.refuse(source=source)

    def test_recomputed_public_hashes_cannot_route_qa_api_to_other_prod_role(self):
        public=copy.deepcopy(self.public)
        for values in public['qa'].values():values['NEXT_PUBLIC_API_URL']=public['prod']['xynes-auth-app']['NEXT_PUBLIC_AUTH_APP_URL']
        record=copy.deepcopy(self.record)
        for name,image in record['images']['frontends']['qa'].items():
            image['public_inputs_sha256']=digest(public['qa'][name])
            image['build_inputs_sha256']=digest({'controller':record['controller'],'source_manifest_sha256':digest(self.source),'source_commit':image['source_commit'],'recipe_sha256':image['recipe_sha256'],'base_images':self.source['base_images'],'tools':self.source['tools'],'environment':'qa','public_inputs_sha256':image['public_inputs_sha256']})
        self.refuse(record,public=public)

    def test_recomputed_public_hashes_cannot_route_qa_to_prod_api(self):
        public=copy.deepcopy(self.public)
        for values in public['qa'].values():values['NEXT_PUBLIC_API_URL']=public['prod']['xynes-auth-app']['NEXT_PUBLIC_API_URL']
        record=copy.deepcopy(self.record)
        for name,image in record['images']['frontends']['qa'].items():
            image['public_inputs_sha256']=digest(public['qa'][name])
            image['build_inputs_sha256']=digest({'controller':record['controller'],'source_manifest_sha256':digest(self.source),'source_commit':image['source_commit'],'recipe_sha256':image['recipe_sha256'],'base_images':self.source['base_images'],'tools':self.source['tools'],'environment':'qa','public_inputs_sha256':image['public_inputs_sha256']})
        self.refuse(record,public=public)

    def test_changed_frontend_source_or_other_stage_inputs_are_refused(self):
        for key,value in [('source_commit','2'*40),('public_inputs_sha256',self.record['images']['frontends']['prod']['xynes-auth-app']['public_inputs_sha256'])]:
            x=copy.deepcopy(self.record);x['images']['frontends']['qa']['xynes-auth-app'][key]=value;self.refuse(x)
        public=copy.deepcopy(self.public);public['qa']['xynes-auth-app']['NEXT_PUBLIC_API_URL']='https://wrong.example.invalid';self.refuse(public=public)

    def test_same_frontend_digest_cannot_be_relabelled_for_different_stage_inputs(self):
        x=copy.deepcopy(self.record);x['images']['frontends']['prod']['xynes-auth-app']['image']=x['images']['frontends']['qa']['xynes-auth-app']['image'];self.refuse(x)

    def test_external_images_must_match_pinned_vendors_without_synthesized_source(self):
        for name in ('clamav-freshclam','supabase-db','supabase-auth'):
            x=copy.deepcopy(self.record);x['images']['shared'][name]['image']=PREFIX+'/'+name+'@sha256:'+'a'*64;self.refuse(x)
            x=copy.deepcopy(self.record);x['images']['shared'][name]['source_commit']='a'*40;self.refuse(x)

    def test_operational_binary_and_migration_hashes_must_match_supplied_inventory(self):
        for change in ['bundle','binary','migration','omit']:
            x=copy.deepcopy(self.record)
            if change=='bundle':x['operational']['bundle_sha256']='2'*64
            if change=='binary':x['operational']['binaries']['xynes-idle']['sha256']='2'*64
            if change=='migration':x['migrations']['xynes-infra'][0]['sha256']='2'*64
            if change=='omit':x['migrations']['xynes-infra']=[]
            self.refuse(x)

    def test_gateway_inventory_is_not_assumed_empty(self):
        entry={'path':'src/infra/bodyLimitSetup.ts','sha256':'3'*64}
        self.artifacts['migrations']['xynes-gateway']=[entry];self.record['migrations']['xynes-gateway']=[entry]
        p=self.call();self.assertEqual(p.returncode,0,p.stderr)
        self.record['migrations']['xynes-gateway']=[];self.refuse()

    def test_migration_paths_links_duplicates_and_foreign_owners_refused(self):
        for path in ['../secret.sql','supabase/migrations/../secret.sql','/etc/passwords','.env.prod','src/private.key']:
            x=copy.deepcopy(self.record);x['migrations']['xynes-infra'][0]['path']=path
            artifacts=copy.deepcopy(self.artifacts);artifacts['migrations']=x['migrations'];self.refuse(x,artifacts=artifacts)
        x=copy.deepcopy(self.record);x['migrations']['xynes-infra']*=2;self.refuse(x,artifacts={'operational':x['operational'],'migrations':x['migrations']})
        x=copy.deepcopy(self.record);x['migrations']['foreign']=[{'path':'drizzle/x.sql','sha256':'a'*64}];self.refuse(x)

    def test_duplicate_nan_deep_oversized_json_and_secret_material_refused_safely(self):
        for raw in ['{"schema_version":1,"schema_version":1}',json.dumps(self.record).replace('"run_id": 1','"run_id": NaN'),'['*1000+']'*1000,'{"secret":"'+'x'*300000+'"}']:
            self.refuse(raw=raw)
        x=copy.deepcopy(self.record);x['secret']='-----BEGIN PRIVATE KEY-----DO-NOT-PRINT';self.refuse(x)
        x=copy.deepcopy(self.record);x['images']['shared']['xynes-gateway']['rawKey']='xynes_live_'+'a'*64;self.refuse(x)

    def test_source_inventory_foreign_or_missing_dependency_refused(self):
        for change in ['foreign','missing']:
            source=copy.deepcopy(self.source)
            if change=='foreign':source['sources']['lumia-ds']['repository']='attacker/lumia-ds'
            else:source['sources'].pop('xynes-auth-sdk')
            self.refuse(source=source)

    def test_symlink_record_is_refused(self):
        self.assertTrue(SCRIPT.exists(),'parser missing');p=self.call();self.assertEqual(p.returncode,0,p.stderr)
        original=self.root/'record.json';alternate=self.root/'alternate.json';original.rename(alternate);original.symlink_to(alternate)
        result=subprocess.run(['python3',str(SCRIPT),'--record',str(original),'--source-manifest',str(self.root/'source.json'),'--public-inputs',str(self.root/'public.json'),'--artifact-bindings',str(self.root/'artifacts.json'),'--registry-prefix',PREFIX],text=True,capture_output=True)
        self.assertEqual(result.returncode,1)

    def test_hardlinked_input_and_nested_unknown_image_fields_are_refused(self):
        self.assertEqual(self.call().returncode,0)
        original=self.root/'record.json';os.link(original,self.root/'hardlink.json')
        p=subprocess.run(['python3',str(SCRIPT),'--record',str(original),'--source-manifest',str(self.root/'source.json'),'--public-inputs',str(self.root/'public.json'),'--artifact-bindings',str(self.root/'artifacts.json'),'--registry-prefix',PREFIX],text=True,capture_output=True)
        self.assertEqual(p.returncode,1)
        (self.root/'hardlink.json').unlink()
        x=copy.deepcopy(self.record);x['images']['shared']['archive-scanner']['provenance_verified']=True;self.refuse(x)

    def test_registry_port_is_bounded_and_published_images_must_use_policy_prefix(self):
        for prefix in ['registry.example.invalid:99999/inert','registry.example.invalid:0/inert']:
            self.refuse(prefix=prefix)
        prefix='registry.example.invalid:5443/inert'
        x=copy.deepcopy(self.record)
        for item in list(x['images']['shared'].values())+[v for stage in x['images']['frontends'].values() for v in stage.values()]:
            if item['image'].startswith(PREFIX+'/'):item['image']=item['image'].replace(PREFIX+'/',prefix+'/',1)
        self.assertEqual(self.call(x,prefix=prefix).returncode,0)

    def test_changed_controller_run_binding_without_recomputed_image_inputs_refused(self):
        for change in [{'commit':'2'*40},{'run_id':2},{'run_attempt':2}]:
            x=copy.deepcopy(self.record);x['controller'].update(change);self.refuse(x)

    def test_unknown_artifact_inventory_and_binary_platform_refused(self):
        for change in ['binary','platform','owner','unknown']:
            x=copy.deepcopy(self.record);a=copy.deepcopy(self.artifacts)
            if change=='binary':x['operational']['binaries']['extra']={'platform':'linux/amd64','sha256':'a'*64};a['operational']=x['operational']
            if change=='platform':x['operational']['binaries']['xynes-idle']['platform']='linux/arm64';a['operational']=x['operational']
            if change=='owner':x['migrations'].pop('xynes-storage-service');a['migrations']=x['migrations']
            if change=='unknown':a['approval_verified']=True
            self.refuse(x,artifacts=a)

    def test_duplicate_optional_public_flag_json_is_refused(self):
        public=copy.deepcopy(self.public)
        public['qa']['xynes-auth-app']['NEXT_PUBLIC_FEATURE_FLAGS_OVERRIDE']='{"fixture":false,"fixture":true}'
        x=copy.deepcopy(self.record);image=x['images']['frontends']['qa']['xynes-auth-app']
        image['public_inputs_sha256']=digest(public['qa']['xynes-auth-app'])
        image['build_inputs_sha256']=digest({'controller':x['controller'],'source_manifest_sha256':digest(self.source),'source_commit':image['source_commit'],'recipe_sha256':image['recipe_sha256'],'base_images':self.source['base_images'],'tools':self.source['tools'],'environment':'qa','public_inputs_sha256':image['public_inputs_sha256']})
        self.refuse(x,public=public)

    def test_schema_is_closed_and_never_accepts_deployable_authorization_field(self):
        schema=ROOT/'infra/release/release-manifest.schema.json';self.assertTrue(schema.exists(),'image record schema missing')
        x=json.loads(schema.read_text());self.assertFalse(x['additionalProperties'])
        self.assertNotIn('deployable',x['properties']);self.assertNotIn('approval_reference',x['properties'])
        self.assertEqual(set(x['properties']['images']['properties']['shared']['required']),set(SHARED))
        self.assertEqual(set(x['properties']['images']['properties']['frontends']['required']),set(STAGES))

if __name__=='__main__':unittest.main(verbosity=2)
