#!/usr/bin/env python3
"""Deterministic Docker command fixtures; never claim real product acceptance."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))

class Engine:
    def __init__(self,work,selected,manifest,containerd=False):
        self.work=work;self.calls=[];self.resources={};self.images={};self.next=1;self.fail=None;self.containerd=containerd
        for task,path,receipt in selected:
            identity=receipt['manifest_digest'] if containerd else receipt['config_digest']
            self.images[identity]={'Id':identity,'Os':'linux','Architecture':'amd64','Config':{'Labels':task['labels']},'Descriptor':{'digest':receipt['manifest_digest']}}
        for key,row in manifest['external_images'].items():
            self.images[row['image']]={'Id':'sha256:'+('8' if key=='supabase-db' else '9')*64,'Os':'linux','Architecture':'amd64','Config':{'Labels':{}},'RepoDigests':[row['image']]}
    def command(self,argv,**kwargs):
        self.calls.append((list(argv),kwargs));args=argv[3:] if '--config' in argv else argv[1:]
        if 'genpkey' in argv:
            Path(argv[argv.index('-out')+1]).write_text('INERT PRIVATE KEY');return b''
        if 'pkey' in argv:return b'INERT PUBLIC KEY\n'
        if self.fail and self.fail(args):raise ValueError('DO_NOT_EXPOSE')
        op=args[0]
        if op=='load' or op=='pull':return b'Loaded image\n'
        if args[:2]==['image','inspect']:
            key=args[-1]
            if key not in self.images:raise ValueError('no such image')
            return json.dumps(self.images[key]).encode()
        if args[:2]==['image','tag']:
            self.images[args[-1]]=copy.deepcopy(self.images[args[-2]]);return b''
        if args[:2]==['image','rm']:self.images.pop(args[-1],None);return b''
        if op in ('network','volume') and args[1]=='create':
            labels={}
            for i,value in enumerate(args):
                if value=='--label':key,val=args[i+1].split('=',1);labels[key]=val
            name=args[-1];identifier=f'{self.next:064x}';self.next+=1
            self.resources[name]={'Id':identifier,'Name':name,'Labels':labels,'Internal':True,'Driver':'bridge'}
            return (identifier if op=='network' else name).encode()
        if op=='create':
            name=args[args.index('--name')+1];labels={}
            for i,value in enumerate(args):
                if value=='--label':key,val=args[i+1].split('=',1);labels[key]=val
            image=args[-1] if '--eval' not in args else args[args.index('--eval')-1]
            if image not in self.images:
                image=next((arg for arg in args if arg in self.images),image)
            identifier=f'{self.next:064x}';self.next+=1
            self.resources[name]={'Id':identifier,'Name':'/'+name,'Config':{'Labels':labels,'Image':image},'Image':self.images[image]['Id'],
                'State':{'Status':'running','Health':{'Status':'healthy'},'ExitCode':0},
                'HostConfig':{'NetworkMode':next(row['Name'] for row in self.resources.values() if row['Name'].endswith('-net'))},
                'NetworkSettings':{'Networks':{row['Name']:{'NetworkID':row['Id']} for row in self.resources.values() if row['Name'].endswith('-net')},'Ports':{'9999/tcp':[{'HostIp':'127.0.0.1','HostPort':'32001'}],'4100/tcp':[{'HostIp':'127.0.0.1','HostPort':'32002'}]}}}
            return identifier.encode()
        if op=='start':
            if '--attach' in args:
                self.resources_by_id(args[-1])['State']['Status']='exited'
                return json.dumps({'kind':'authz','permissions':2,'roles':2,'mappings':4} if 'authz' in self.resources_by_id(args[-1])['Name'] else {'kind':'cms','templates':3,'workspaces':0,'defaults':0}).encode()
            return b''
        if op=='exec':
            if '--command' in args:return json.dumps({'authz_permissions':2,'authz_roles':2,'authz_mappings':4,'cms_templates':3,'workspaces':0,'cms_defaults_satisfied':0}).encode()
            return b''
        if op=='inspect':return json.dumps(self.resources_by_id(args[-1])).encode()
        if op in ('network','volume') and args[1]=='inspect':return json.dumps(self.resources_by_id(args[-1])).encode()
        if op=='ps' or (op in ('network','volume') and args[1]=='ls'):
            filters=[args[i+1] for i,v in enumerate(args) if v=='--filter'];name=next(value[5:] for value in filters if value.startswith('name='))
            name=name.removeprefix('^/').removesuffix('$');row=self.resources.get(name)
            return ((row['Name'] if op=='volume' else row['Id'])+'\n').encode() if row else b''
        if op=='rm' or (op in ('network','volume') and args[1]=='rm'):
            row=self.resources_by_id(args[-1]);key=next(k for k,v in self.resources.items() if v is row);del self.resources[key];return b''
        raise AssertionError(args)
    def resources_by_id(self,value):
        if value in self.resources:return self.resources[value]
        return next(row for row in self.resources.values() if row['Id']==value)

class Tests(unittest.TestCase):
    def setUp(self):
        import ci_smoke_stack
        self.module=ci_smoke_stack;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name).resolve();self.work.chmod(0o700)
        self.selected=[]
        for i,name in enumerate(sorted(self.module.BACKENDS),1):
            path=self.work/(name+'.tar');path.write_bytes(b'OCI')
            task={'workload':name,'labels':{'org.opencontainers.image.revision':str(i)*40}}
            receipt={'config_digest':'sha256:'+str(i)*64,'manifest_digest':'sha256:'+format(i+9,'x')*64}
            self.selected.append((task,path,receipt))
        self.manifest={'external_images':{key:{'image':name+'@sha256:'+digit*64,'platform':'linux/amd64'} for key,name,digit in [('supabase-db','postgres:17-alpine','a'),('supabase-auth','supabase/gotrue:v2.196.0','b')]}}
        self.bundle=self.work/'migration.tar';self.bundle.write_bytes(b'MIGRATIONS')
        self.engine=Engine(self.work,self.selected,self.manifest)
        self.command_runner=self.module.command
        self.patches=[patch.object(self.module,'command',self.engine.command),patch.object(self.module.os,'chown'),
            patch.object(self.module.image_build,'inspect_oci',side_effect=lambda path,task:next(row[2] for row in self.selected if row[0]==task)),
            patch.object(self.module.migration_transport,'extract',return_value=object()),
            patch.object(self.module.migration_transport,'application_inputs',return_value=([],{})),
            patch.object(self.module.host_seed,'requirements',return_value=[]),
            patch.object(self.module.migration_sql,'render',return_value='SELECT 1;'),
            patch.object(self.module.ci_smoke_runtime,'execute_flow',return_value={'result':'passed','storage_verified':False})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def run_stack(self):return self.module.run(selected=tuple(self.selected),manifest=self.manifest,manifest_bytes=b'{}',migration_bundle=self.bundle,controller={},work=self.work)
    def test_order_eight_services_private_network_and_cleanup(self):
        result=self.run_stack();self.assertEqual(result,{'result':'passed','storage_verified':False})
        calls=[argv for argv,kwargs in self.engine.calls]
        self.assertEqual(sum('load' in argv for argv in calls),6)
        self.assertEqual(sum('pull' in argv for argv in calls),2)
        self.assertEqual(sum('create' in argv and '--name' in argv for argv in calls),10) # eight services + two oneoff seeds
        self.assertTrue(any('--internal' in argv for argv in calls))
        self.assertFalse(self.engine.resources)
        self.assertFalse(any('prune' in argv for argv in calls))
        self.assertTrue(all('DO_NOT_EXPOSE' not in str(argv) for argv in calls))
    def test_containerd_identity_supported_with_matching_descriptor_only(self):
        self.engine=Engine(self.work,self.selected,self.manifest,containerd=True)
        with patch.object(self.module,'command',self.engine.command):self.run_stack()
        self.assertFalse(self.engine.resources)
    def test_oci_mismatch_before_first_docker_command(self):
        with patch.object(self.module.image_build,'inspect_oci',return_value={}):
            with self.assertRaises(self.module.StackError):self.run_stack()
        self.assertEqual(self.engine.calls,[])
    def test_failure_redacts_and_cleans_owned_resources(self):
        self.engine.fail=lambda args:args[:1]==['start']
        with self.assertRaises(self.module.StackError) as caught:self.run_stack()
        self.assertNotIn('DO_NOT_EXPOSE',str(caught.exception));self.assertFalse(self.engine.resources)
    def test_failure_at_later_boundaries_still_cleans_every_owned_object(self):
        base=self.work
        for boundary in ('gotrue','seeds','services'):
            with self.subTest(boundary=boundary):
                self.work=base/boundary;self.work.mkdir(mode=0o700)
                engine=Engine(self.work,self.selected,self.manifest)
                suffix={'gotrue':'-supabase-auth','seeds':'-seed-cms','services':'-gateway'}[boundary]
                engine.fail=lambda args:args[0]=='create' and args[args.index('--name')+1].endswith(suffix)
                with patch.object(self.module,'command',engine.command):
                    with self.assertRaises(self.module.StackError) as caught:self.run_stack()
                self.assertEqual(caught.exception.safe_evidence['stack_phase'],boundary)
                self.assertFalse(engine.resources)
        self.work=base
    def test_cleanup_refuses_changed_ownership_and_never_removes_that_volume(self):
        stack=self.module.Stack(self.work);stack.substrate()
        self.engine.resources[stack.volume]['Labels'][self.module.LABEL]='foreign-run'
        with self.assertRaises(self.module.StackError):stack.cleanup()
        self.assertIn(stack.volume,self.engine.resources)
        self.assertFalse(any(argv[-2:]==['rm',stack.volume] for argv,_ in self.engine.calls))
    def test_generated_environment_and_private_identity_bindings(self):
        self.run_stack()
        gateway=dict(line.split('=',1) for line in (self.work/'gateway.env').read_text().splitlines())
        self.assertEqual(gateway['STORAGE_SERVICE_URL'],'')
        self.assertEqual(gateway['NODE_ENV'],'production')
        self.assertEqual(gateway['JWT_ISSUER'],'https://supabase.qa.xynes.com/auth/v1')
        self.assertEqual(gateway['POSTHOG_API_KEY'],'')
        accounts=dict(line.split('=',1) for line in (self.work/'accounts-service.env').read_text().splitlines())
        self.assertEqual(accounts['MAIL_PROVIDER'],'noop')
        trust=json.loads((self.work/'identities/authz-trust.json').read_text())
        self.assertEqual({row['issuer'] for row in trust},{'gateway','accounts','cms','docs'})
        self.assertTrue(all(path.stat().st_mode&0o777==0o600 for path in self.work.glob('*.env')))
        creates=[argv for argv,_ in self.engine.calls if 'create' in argv and '--name' in argv]
        self.assertEqual(sum('--publish' in argv for argv in creates),2)
        self.assertTrue(all(argv[argv.index('--publish')+1].startswith('127.0.0.1::') for argv in creates if '--publish' in argv))
    def test_invalid_containerd_descriptor_refused(self):
        engine=Engine(self.work,self.selected,self.manifest,containerd=True)
        for row in engine.images.values():row['Descriptor']={'digest':'sha256:'+'0'*64}
        with patch.object(self.module,'command',engine.command):
            with self.assertRaises(self.module.StackError) as caught:self.run_stack()
        self.assertEqual(caught.exception.safe_evidence['stack_phase'],'image-import')
        self.assertFalse(engine.resources)
    def test_runtime_failure_evidence_survives_cleanup(self):
        error=self.module.ci_smoke_runtime.RuntimeError('CI product smoke refused')
        error.safe_evidence={'phase':'flow','failed_flow_phase':'cms-entry-publish','steps':[], 'result':'refused','storage_verified':False}
        with patch.object(self.module.ci_smoke_runtime,'execute_flow',side_effect=error):
            with self.assertRaises(self.module.ci_smoke_runtime.RuntimeError) as caught:self.run_stack()
        self.assertEqual(caught.exception.safe_evidence,error.safe_evidence)
        self.assertFalse(self.engine.resources)
    def test_command_failures_are_redacted_and_use_clean_child_environment(self):
        with patch.object(self.module.subprocess,'Popen',side_effect=OSError('DO_NOT_EXPOSE')) as process:
            with self.assertRaises(self.module.StackError) as caught:
                # Call the real runner despite orchestration's mocked command boundary.
                self.command_runner(['/usr/bin/openssl','version'])
            self.assertNotIn('DO_NOT_EXPOSE',str(caught.exception))
            self.assertEqual(process.call_args.kwargs['env'],self.module.clean_environment())
    def test_environment_has_no_external_credentials_or_storage(self):
        with patch.dict(os.environ,{'GH_TOKEN':'DO_NOT_EXPOSE','DOCKER_HOST':'tcp://evil'}):self.run_stack()
        for path in self.work.rglob('*.env'):
            raw=path.read_text();self.assertNotIn('DO_NOT_EXPOSE',raw);self.assertNotIn('R2_',raw)
        commands=' '.join(' '.join(argv) for argv,kwargs in self.engine.calls)
        for path in self.work.glob('*.env'):
            for line in path.read_text().splitlines():
                value=line.split('=',1)[1]
                if len(value)>=64:self.assertNotIn(value,commands)
        environment=self.module.clean_environment()
        self.assertNotIn('GH_TOKEN',environment);self.assertNotIn('DOCKER_HOST',environment)

if __name__=='__main__':unittest.main()
