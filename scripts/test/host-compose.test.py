#!/usr/bin/env python3
"""No daemon mutations: real inert Compose rendering and injected command boundary."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import shutil
import secrets
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

class Tests(unittest.TestCase):
    def test_bounded_runner_permits_only_fixed_readonly_idle_unit_queries(self):
        for stage in ('qa','staging','prod'):
            for group in ('auth','cms','broker'):
                command=['/usr/bin/systemctl','show','--property=ActiveState','--value',
                         'xynes-idle-'+stage+'-'+group+'.service']
                with patch.object(self.module.subprocess,'Popen',side_effect=OSError('inert')) as process:
                    with self.assertRaises(self.module.ComposeError):
                        self.module.run_command(command,env=self.module.CLEAN_ENV,timeout=15,limit=65536)
                    process.assert_called_once()
        for command in (['/usr/bin/systemctl','restart','xynes-idle-qa-auth.service'],
                        ['/usr/bin/systemctl','show','--property=ActiveState','--value','unknown.service'],
                        ['/usr/bin/systemctl','show','--property=ActiveState','--value','xynes-idle-qa-auth.service','extra']):
            with patch.object(self.module.subprocess,'Popen') as process:
                with self.assertRaises(self.module.ComposeError):
                    self.module.run_command(command,env=self.module.CLEAN_ENV,timeout=15,limit=65536)
                process.assert_not_called()

    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / 'scripts/release/host_compose.py'
        if cls.path.exists():
            cls.module = load('host_compose', cls.path)
            cls.rendered = load('host_render_fixture', ROOT / 'scripts/test/hosted-release.test.py').HostedReleaseTests().render('qa')

    def setUp(self):
        self.assertTrue(self.path.is_file(), 'privileged Compose driver is missing')
        fixture = load('host_publish_fixture', ROOT / 'scripts/test/publish-batch.test.py').Tests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.receipt = fixture.run_batch()
        self.inputs = dict(receipt=self.receipt, manifest=fixture.manifest,
                           public=fixture.public, controller=fixture.controller)
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-host-driver-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        os.chmod(self.work, 0o700)
        self.config = copy.deepcopy(self.rendered)
        pointers = self.module.image_pointers(stage='qa', **self.inputs)
        for service, key in self.module.SERVICE_IMAGES.items():
            self.config['services'][service]['image'] = pointers[key]
        self.commands, self.containers = [], []
        self.up_failure = False
        self.runner = self.run_command
        self.generations=[];self.guard_events=[]
        class Control:
            stage='qa'
            def provision_token(inner): pass
            def provision_generation(inner):
                gen=SimpleNamespace(stage='qa',name=secrets.token_hex(32));self.generations.append(gen);return gen
            def verify_generation(inner,generation): self.assertIn(generation,self.generations)
            def bind_generation(inner,generation,cid,*,image): self.guard_events.append(('bind',cid))
        self.control=Control()
        self.driver = self.module.Driver('qa', runner=self.runner, environment_loader=lambda stage: None, storage_control=self.control)
        class Guard:
            stage='qa'
            def before_replacement(inner,prepared,previous,inventory,replace_services,**kwargs):
                self.guard_events.append(('before',set(replace_services)))
                if {'clamav-clamd','clamav-freshclam','libreoffice-sidecar'} & set(inventory) & replace_services:
                    raise ValueError('inert remote processor activity')
            def verify_before_stop(inner,**kwargs):self.guard_events.append(('verify',))
            def after_replacement(inner,prepared,previous,inventory):self.guard_events.append(('after',))
        self.guard=Guard();self.driver.bind_guard(self.guard)

    def run_command(self, argv, *, env, timeout, limit):
        self.commands.append((argv, env, timeout, limit))
        self.assertEqual(argv[:5], ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', '--config', '/etc/xynes/deploy/docker'])
        self.assertNotIn('DOCKER_HOST', env)
        if '--hash' in argv:
            selected=Path(argv[argv.index('-f')+1]);config=self.module.transform(json.loads(selected.read_text()),lambda s:s.replace('$$','$'))
            service=argv[-1];return (service+' '+self.service_hash(config['services'][service])+'\n').encode()
        if 'config' in argv:
            if env.get('STORAGE_CONTROL_GENERATION'):
                self.config['services']['storage-service']['labels']['com.xynes.storage-generation']=env['STORAGE_CONTROL_GENERATION']
                self.config['services']['storage-service']['volumes'][0]['source']=env['STORAGE_CONTROL_DIRECTORY']
            return json.dumps(self.module.transform(self.config,lambda s:s.replace('$','$$'))).encode()
        if 'ps' in argv:
            return ('\n'.join(row['id'] for row in self.containers)).encode()
        if 'inspect' in argv:
            return json.dumps(next(row for row in self.containers if row['id'] == argv[-1])).encode()
        if 'up' in argv:
            if self.up_failure:
                raise RuntimeError('inert-secret-do-not-print')
            # Parse the exact protected document selected by the driver.
            selected=Path(argv[argv.index('-f')+1]);config=self.module.transform(json.loads(selected.read_text()),lambda s:s.replace('$$','$'))
            services=set(argv[argv.index('180')+1:]);rows={r['labels']['com.docker.compose.service']:r for r in self.containers}
            for i,service in enumerate(sorted(services)):
                if '--no-recreate' in argv and service in rows:continue
                row=self.container(service,len(self.commands)*20+i);row['image']=config['services'][service]['image']
                row['labels']['com.docker.compose.config-hash']=self.service_hash(config['services'][service])
                if service=='storage-service':row['labels']['com.xynes.storage-generation']=config['services'][service]['labels']['com.xynes.storage-generation']
                rows[service]=row
            self.containers=list(rows.values())
            self.guard_events.append(('up',services,'--no-recreate' in argv))
            return b''
        self.fail('unexpected driver operation')

    def service_hash(self, spec):
        return hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()

    def container(self, service, index=0):
        return dict(id=f'{index+1:064x}', image=self.config['services'][service]['image'],
                    labels={'com.docker.compose.project':'xynes-qa', 'com.docker.compose.service':service,
                            'com.docker.compose.oneoff':'False', 'com.docker.compose.config-hash':self.service_hash(self.config['services'][service]), 'com.xynes.managed':'true', 'com.xynes.environment':'qa'},
                    state='running', health='healthy')

    def prepare(self):
        return self.driver.prepare(work=self.work, **self.inputs)

    def refused(self, callback):
        with self.assertRaises(self.module.ComposeError) as caught:
            callback()
        self.assertEqual(str(caught.exception), 'Hosted Compose operation refused')
        self.assertNotIn('inert', str(caught.exception))

    def test_fresh_generation_binding_and_metadata_tampering_refused(self):
        first=self.prepare();second=self.prepare()
        self.assertNotEqual(first.storage_generation,second.storage_generation)
        storage=self.driver.config(first)['services']['storage-service']
        self.assertEqual(storage['labels']['com.xynes.storage-generation'],first.storage_generation)
        self.assertEqual(storage['volumes'][0]['source'],'/run/xynes/storage/qa/'+first.storage_generation[:32])
        from dataclasses import replace
        self.refused(lambda:self.driver.config(replace(first,storage_generation='a'*64)))
        self.refused(lambda:self.driver.config(replace(first,storage_generation=None)))

    def test_idle_drain_and_generation_rebind_wrap_fresh_start_and_rollback(self):
        class Idle:
            stage='qa'
            def before_replace(inner):self.guard_events.append(('idle-drain',))
            def rebind(inner,prepared,inventory):
                self.assertEqual(set(inventory),self.module.SERVICES)
                self.guard_events.append(('idle-rebind',prepared.storage_generation))
        self.driver.idle_control=Idle();previous=self.prepare();self.driver.start(previous)
        self.assertEqual(self.guard_events[0],('idle-drain',));self.assertEqual(self.guard_events[-1],('idle-rebind',previous.storage_generation))
        current=self.prepare();self.guard_events=[];self.driver.start(current,previous=previous)
        self.assertEqual(self.guard_events[0],('idle-drain',));self.assertEqual(self.guard_events[-1],('idle-rebind',current.storage_generation))
        self.guard_events=[];result=self.driver.rollback(previous,current=current)
        self.assertEqual(self.guard_events[0],('idle-drain',));self.assertEqual(self.guard_events[-1],('idle-rebind',result['prepared'].storage_generation))

    def test_bootstrap_holds_storage_drain_through_migrations(self):
        previous=self.prepare();self.driver.start(previous);prepared=self.prepare();self.guard_events=[]
        self.driver.bootstrap(prepared,previous=previous)
        self.assertEqual(self.guard_events[0],('before',set()))
        self.assertFalse(any(event[0]=='after' for event in self.guard_events))

    def test_existing_storage_requires_guard_and_no_implicit_first_bootstrap(self):
        prepared=self.prepare();self.driver.start(prepared);self.driver.guard=None
        before=len(self.commands);self.refused(lambda:self.driver.bootstrap(prepared))
        self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))

    def test_unchanged_processors_keep_exact_processes_and_guard_wraps_replacement(self):
        previous=self.prepare();self.driver.start(previous);original={r['labels']['com.docker.compose.service']:r['id'] for r in self.containers}
        prepared=self.prepare();self.guard_events=[];result=self.driver.start(prepared,previous=previous)
        current={r['labels']['com.docker.compose.service']:r['id'] for r in self.containers}
        processors={'clamav-clamd','clamav-freshclam','libreoffice-sidecar'}
        self.assertEqual({s:original[s] for s in processors},{s:current[s] for s in processors})
        self.assertNotEqual(original['storage-service'],current['storage-service'])
        ups=[e for e in self.guard_events if e[0]=='up']
        self.assertEqual(ups[0],('up',processors,True));self.assertTrue(all('--no-deps' in args for args,_,_,_ in self.commands if 'up' in args))
        self.assertEqual(self.guard_events[0],('before',self.module.SERVICES-processors))
        self.assertLess(next(i for i,e in enumerate(self.guard_events) if e[0]=='verify'),next(i for i,e in enumerate(self.guard_events) if e[0]=='up' and not e[2]))
        self.assertEqual(self.guard_events[-1],('after',));self.assertEqual(result['services_ready'],14)

    def test_changed_existing_processor_refuses_before_any_up(self):
        previous=self.prepare();self.driver.start(previous)
        self.config['services']['libreoffice-sidecar']['mem_limit']='2147483648'
        prepared=self.prepare();before=len(self.commands)
        self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))

    def test_rollback_returns_fresh_generation_recovery_binding(self):
        previous=self.prepare();current=self.prepare();self.driver.start(current);self.guard_events=[]
        result=self.driver.rollback(previous,current=current)
        rebound=result['prepared'];self.assertEqual(rebound.images,previous.images)
        self.assertNotIn(rebound.storage_generation,{previous.storage_generation,current.storage_generation})
        self.assertEqual(self.driver.config(rebound)['services']['storage-service']['labels']['com.xynes.storage-generation'],rebound.storage_generation)
        self.assertEqual(self.guard_events[0][0],'before');self.assertEqual(self.guard_events[-1],('after',))

    def test_guard_refusal_or_failed_readiness_cannot_resume_or_bind_generation(self):
        previous=self.prepare();self.driver.start(previous);prepared=self.prepare()
        def refuse(*args,**kwargs):raise RuntimeError('inert-private-proof')
        self.guard.before_replacement=refuse;self.guard_events=[];before=len(self.commands)
        self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))
        self.assertFalse(any(e[0] in {'bind','after'} for e in self.guard_events))
        # A failed child cannot claim a new process binding or product acceptance.
        self.guard.before_replacement=lambda *args,**kwargs:self.guard_events.append(('before',))
        self.up_failure=True;self.guard_events=[]
        self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any(e[0] in {'bind','after'} for e in self.guard_events))

    def test_inspection_returns_exact_owned_inventory_and_no_foreign_proof(self):
        prepared=self.prepare();self.containers=[self.container('gateway'),self.container('storage-service',1)]
        inventory=self.driver.inspect({service:{prepared.images[key]} for service,key in self.module.SERVICE_IMAGES.items()})
        self.assertEqual(set(inventory),{'gateway','storage-service'})
        self.assertEqual(set(inventory['gateway']),{'id','image','labels','state','health'})
        inventory['gateway']['labels']['com.xynes.managed']='false'
        self.assertEqual(self.containers[0]['labels']['com.xynes.managed'],'true')

    def test_rebind_changes_only_storage_generation_and_its_fixed_directory(self):
        previous=self.prepare();original=self.driver.config(previous)
        rebound=self.driver.rebind(previous,work=self.work);actual=self.driver.config(rebound)
        actual['services']['storage-service']['labels']['com.xynes.storage-generation']=previous.storage_generation
        actual['services']['storage-service']['volumes'][0]['source']=original['services']['storage-service']['volumes'][0]['source']
        self.assertEqual(actual,original);self.assertEqual(rebound.images,previous.images)
        self.assertEqual(rebound.file.stat().st_mode&0o777,0o600)

    def test_drifted_native_processor_config_refused_before_any_up(self):
        previous=self.prepare();self.driver.start(previous);prepared=self.prepare()
        for row in self.containers:
            if row['labels']['com.docker.compose.service']=='clamav-clamd':row['labels']['com.docker.compose.config-hash']='0'*64
        before=len(self.commands);self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))

    def test_native_processor_hash_output_is_bounded_exact_and_not_a_caller_proof(self):
        previous=self.prepare();self.driver.start(previous);prepared=self.prepare();original=self.driver.runner
        def foreign_hash(argv,**kwargs):
            if '--hash' in argv:return b'foreign '+b'a'*64+b'\n'
            return original(argv,**kwargs)
        self.driver.runner=foreign_hash;before=len(self.commands)
        self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))

    def test_real_compose_hash_contract_uses_exact_protected_document(self):
        prepared=self.prepare();empty=self.work/'empty.env';empty.write_text('')
        result=subprocess.run([shutil.which('docker'),'compose','--env-file',str(empty),'-f',str(prepared.file),'config','--hash','clamav-clamd'],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0);self.assertRegex(result.stdout,r'^clamav-clamd [a-f0-9]{64}\n$')
        self.assertNotIn('inert',result.stdout)

    def test_no_recreate_must_preserve_native_processor_ids(self):
        previous=self.prepare();self.driver.start(previous);prepared=self.prepare();original=self.driver.runner
        def drift(argv,**kwargs):
            value=original(argv,**kwargs)
            if 'up' in argv and '--no-recreate' in argv:
                row=next(r for r in self.containers if r['labels']['com.docker.compose.service']=='clamav-clamd')
                row['id']='f'*64
            return value
        self.driver.runner=drift;self.guard_events=[]
        self.refused(lambda:self.driver.start(prepared,previous=previous))
        self.assertFalse(any(e[0]=='after' for e in self.guard_events))

    def test_exact_stage_pointers_and_source_publication_binding(self):
        for stage in ('qa','staging','prod'):
            images=self.module.image_pointers(stage=stage, **self.inputs)
            self.assertEqual(len(images),14)
            for image in self.receipt['images']:
                if image['environment'] in (None, stage):
                    self.assertIn(image['image'],images.values())
            self.assertEqual(images['IMAGE_FRESHCLAM'],self.inputs['manifest']['external_images']['clamav-freshclam']['image'])
            self.assertEqual(images['IMAGE_SUPABASE_DB'],self.inputs['manifest']['external_images']['supabase-db']['image'])
        self.refused(lambda:self.module.image_pointers(stage='dev',**self.inputs))
        broken=copy.deepcopy(self.inputs);broken['receipt']['images'][-1]['image']='ghcr.io/foreign/app@sha256:'+'0'*64
        self.refused(lambda:self.module.image_pointers(stage='qa',**broken))

    def test_prepare_uses_fixed_paths_clean_env_and_private_validated_render(self):
        prepared=self.prepare()
        argv,env,timeout,limit=self.commands[0]
        self.assertEqual(argv[5:9], ['compose','--project-name','xynes-qa','--project-directory'])
        self.assertIn('/srv/xynes/qa/env/.env.qa',argv)
        for path in ('hosted.yml','qa.yml','internal-request-identities.yml','auth/compose.fragment.yml'):
            self.assertIn('/srv/xynes/qa/compose/'+path,argv)
        self.assertEqual(argv[-3:],['config','--format','json'])
        self.assertEqual(os.stat(prepared.file).st_mode & 0o777,0o600)
        self.assertTrue(prepared.file.is_relative_to(self.work))
        self.assertNotIn('inert-test-only-value',repr(prepared))
        self.assertEqual(env['XYNES_ENV'],'qa')
        self.assertNotIn('GH_TOKEN',env)
        self.assertFalse(prepared.host_approved)

    def test_rendered_configuration_and_pointer_substitution_refused(self):
        for mutate in (lambda c:c['services']['gateway'].update(image='ghcr.io/foreign/app@sha256:'+'a'*64),
                       lambda c:c['services']['gateway'].update(privileged=True),
                       lambda c:c.update(name='xynes-prod')):
            saved=copy.deepcopy(self.config);mutate(self.config)
            self.refused(self.prepare)
            self.config=saved
        self.assertFalse(list(self.work.iterdir()))

    def test_start_fixed_services_and_readiness_and_no_destructive_operation(self):
        prepared=self.prepare();result=self.driver.start(prepared)
        argv=next(args for args,_,_,_ in self.commands if 'up' in args)
        # --quiet-pull is now part of the fixed up shape (pull progress cannot reach stderr).
        self.assertEqual(argv[argv.index('up')+1:argv.index('up')+9], ['-d','--no-build','--no-deps','--pull','always','--quiet-pull','--wait','--wait-timeout'])
        self.assertEqual(set(argv[argv.index('180')+1:]),self.module.SERVICES)
        self.assertEqual(result['services_ready'],14)
        self.assertFalse(result['product_accepted'])
        self.assertFalse(any(word in argv for word in ('down','prune','--remove-orphans','--volumes','build')))

    def test_foreign_existing_service_labels_and_image_refused_before_start(self):
        for key,value in [('image','ghcr.io/foreign/app@sha256:'+'0'*64),('service','unowned'),('stage','prod'),('managed','false'),('project','xynes-prod'),('oneoff','True')]:
            self.containers=[self.container('gateway')]
            if key=='image':self.containers[0][key]=value
            else:
                label={'service':'com.docker.compose.service','stage':'com.xynes.environment','managed':'com.xynes.managed','project':'com.docker.compose.project','oneoff':'com.docker.compose.oneoff'}[key]
                self.containers[0]['labels'][label]=value
            prepared=self.prepare();before=len(self.commands)
            self.refused(lambda:self.driver.start(prepared))
            self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))
        self.containers=[self.container('gateway'),self.container('gateway',1)]
        self.refused(lambda:self.driver.start(self.prepare()))

    def test_failed_readiness_never_reports_success_and_logs_are_redacted(self):
        self.up_failure=True
        self.refused(lambda:self.driver.start(self.prepare()))
        self.up_failure=False
        original=self.runner
        def unhealthy(argv,**kwargs):
            value=original(argv,**kwargs)
            if 'inspect' in argv:
                row=json.loads(value);row['health']='unhealthy';return json.dumps(row).encode()
            return value
        self.driver.runner=unhealthy
        self.refused(lambda:self.driver.start(self.prepare()))

    def test_missing_healthcheck_cannot_count_as_ready(self):
        original=self.runner
        def no_health(argv,**kwargs):
            value=original(argv,**kwargs)
            if 'inspect' in argv:
                row=json.loads(value);row['health']=None;return json.dumps(row).encode()
            return value
        self.driver.runner=no_health
        self.refused(lambda:self.driver.start(self.prepare()))

    def test_rollback_excludes_database_and_requires_previous_validated_config(self):
        previous=self.prepare();current=self.prepare()
        self.containers=[self.container(service,i) for i,service in enumerate(sorted(self.module.SERVICES))]
        result=self.driver.rollback(previous,current=current)
        argv=next(args for args,_,_,_ in reversed(self.commands) if 'up' in args)
        self.assertIn('--no-deps',argv)
        self.assertNotIn('supabase-db',argv[argv.index('180')+1:])
        self.assertEqual(set(argv[argv.index('180')+1:]),self.module.SERVICES-{'supabase-db','clamav-clamd','clamav-freshclam','libreoffice-sidecar'})
        self.assertFalse(result['database_rolled_back']);self.assertFalse(result['product_accepted'])
        other=self.module.Driver('prod',runner=self.runner,environment_loader=lambda stage:None)
        self.refused(lambda:other.rollback(previous,current=current))

    def test_literal_dollars_survive_second_compose_interpolation(self):
        self.config['services']['gateway']['environment']['POSTHOG_API_KEY']='inert$VALUE$$VALUE'
        prepared=self.prepare()
        stored=json.loads(prepared.file.read_text())
        self.assertEqual(stored['services']['gateway']['environment']['POSTHOG_API_KEY'],'inert$$VALUE$$$$VALUE')
        self.assertEqual(self.driver.config(prepared)['services']['gateway']['environment']['POSTHOG_API_KEY'],'inert$VALUE$$VALUE')

    def test_concrete_runner_bounds_output_and_deadline_without_secret_diagnostics(self):
        real_popen=subprocess.Popen
        for code,timeout,limit in [("print('inert-secret-do-not-print'*1000)",2,32),
                                   ("import time; time.sleep(2)",1,32),
                                   ("import sys; sys.stderr.write('inert-secret');sys.exit(2)",2,32)]:
            def spawn(argv,**kwargs):
                self.assertEqual(argv,self.module.BASE+['version'])
                self.assertEqual(kwargs['env'],self.module.CLEAN_ENV)
                return real_popen([sys.executable,'-c',code],**kwargs)
            with patch.object(self.module.subprocess,'Popen',spawn):
                self.refused(lambda:self.module.run_command(self.module.BASE+['version'],env=self.module.CLEAN_ENV,timeout=timeout,limit=limit))
        def ok(argv,**kwargs):
            return real_popen([sys.executable,'-c',"print('bounded')"],**kwargs)
        with patch.object(self.module.subprocess,'Popen',ok):
            self.assertEqual(self.module.run_command(self.module.BASE+['version'],env=self.module.CLEAN_ENV,timeout=2,limit=32),b'bounded\n')

    def test_real_compose_second_pass_preserves_literal_secret_dollars(self):
        self.config['services']['gateway']['environment']['POSTHOG_API_KEY']='inert$VALUE$$VALUE'
        prepared=self.prepare()
        empty=self.work/'empty.env';empty.write_text('')
        command=[shutil.which('docker'),'compose','--env-file',str(empty),'-f',str(prepared.file),'config','--format','json']
        result=subprocess.run(command,env={key:value for key,value in os.environ.items() if not key.startswith(('IMAGE_','XYNES_'))},text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,'inert Compose second render refused')
        parsed=self.module.transform(json.loads(result.stdout),lambda s:s.replace('$$','$'))
        self.assertEqual(parsed['services']['gateway']['environment']['POSTHOG_API_KEY'],'inert$VALUE$$VALUE')

    def test_bootstrap_starts_only_database_and_auth_without_dependencies(self):
        self.assertTrue(callable(getattr(self.driver,'bootstrap',None)),'infrastructure bootstrap is missing')
        prepared=self.prepare()
        original=self.runner
        def bootstrap_runner(argv,**kwargs):
            value=original(argv,**kwargs)
            if 'up' in argv:
                self.containers=[self.container(service,i) for i,service in enumerate(('supabase-db','supabase-auth'))]
            return value
        self.driver.runner=bootstrap_runner
        result=self.driver.bootstrap(prepared)
        argv=next(args for args,_,_,_ in self.commands if 'up' in args)
        self.assertIn('--no-deps',argv);self.assertIn('--no-build',argv)
        self.assertEqual(set(argv[argv.index('180')+1:]),{'supabase-db','supabase-auth'})
        self.assertEqual(result,{'stage':'qa','services_ready':2,'product_accepted':False,'migrations_applied':False})

    def test_bootstrap_subset_readiness_preserves_owned_apps_but_refuses_foreign_adoption(self):
        self.assertTrue(callable(getattr(self.driver,'bootstrap',None)),'infrastructure bootstrap is missing')
        prepared=self.prepare();original=self.runner
        def bootstrap_runner(argv,**kwargs):
            if 'up' in argv:
                self.commands.append((argv,kwargs['env'],kwargs['timeout'],kwargs['limit']))
                self.containers=[self.container(service,i) for i,service in enumerate(('supabase-db','supabase-auth','gateway'))]
                self.containers[-1].update(state='exited',health='unhealthy')
                return b''
            return original(argv,**kwargs)
        self.driver.runner=bootstrap_runner
        self.assertEqual(self.driver.bootstrap(prepared)['services_ready'],2)
        for mode in ('foreign-image','foreign-service','duplicate'):
            self.containers=[self.container('supabase-db')]
            if mode=='foreign-image':self.containers[0]['image']='postgres:old-unowned'
            elif mode=='foreign-service':self.containers[0]['labels']['com.docker.compose.service']='unowned'
            else:self.containers.append(self.container('supabase-db',1))
            before=len(self.commands);self.refused(lambda:self.driver.bootstrap(prepared))
            self.assertFalse(any('up' in args for args,_,_,_ in self.commands[before:]))

    def test_bootstrap_requires_both_infrastructure_services_healthy(self):
        self.assertTrue(callable(getattr(self.driver,'bootstrap',None)),'infrastructure bootstrap is missing')
        prepared=self.prepare();original=self.runner
        for mode in ('missing-auth','unhealthy-auth'):
            self.containers=[]
            def bootstrap_runner(argv,**kwargs):
                value=original(argv,**kwargs)
                if 'up' in argv:
                    self.containers=[self.container('supabase-db')]
                    if mode=='unhealthy-auth':
                        self.containers.append(self.container('supabase-auth',1));self.containers[-1]['health']='unhealthy'
                return value
            self.driver.runner=bootstrap_runner
            self.refused(lambda:self.driver.bootstrap(prepared))

    def ups(self):
        return [args for args,_,_,_ in self.commands if 'up' in args]

    def test_every_driver_up_pulls_quietly_for_bootstrap_unchanged_replace_and_rollback(self):
        original=self.runner
        def bootstrap_runner(argv,**kwargs):
            value=original(argv,**kwargs)
            if 'up' in argv and set(argv[argv.index('180')+1:])=={'supabase-db','supabase-auth'} and not self.containers[2:]:
                self.containers=[r for r in self.containers if r['labels']['com.docker.compose.service'] in {'supabase-db','supabase-auth'}]
            return value
        self.driver.runner=bootstrap_runner
        first=self.prepare();self.driver.bootstrap(first)
        self.driver.runner=original;self.driver.start(first)
        second=self.prepare();self.driver.start(second,previous=first)
        self.driver.rollback(first,current=second)
        ups=self.ups()
        self.assertTrue(any('--no-recreate' in argv for argv in ups),'unchanged processor path not exercised')
        self.assertTrue(any(set(argv[argv.index('180')+1:])=={'supabase-db','supabase-auth'} for argv in ups))
        self.assertGreaterEqual(len(ups),5)
        for argv in ups:
            self.assertEqual(argv.count('--quiet-pull'),1)
            self.assertLess(argv.index('--quiet-pull'),argv.index('--wait-timeout'))
            self.assertEqual(argv[argv.index('--pull')+1],'always')
            self.assertTrue(set(argv[argv.index('180')+1:])<=self.module.SERVICES)

    def pull_child(self,stderr_without_quiet=200000):
        real_popen=subprocess.Popen
        # Emulates Compose pull progress on stderr unless --quiet-pull is present.
        code=("import sys\nquiet='--quiet-pull' in sys.argv\n"
              "sys.stderr.write(('Pulling layer inert-secret-ghp_'+'A'*36+'\\n')*(4 if quiet else "+str(stderr_without_quiet)+"//60))\n")
        def spawn(argv,**kwargs):
            self.assertEqual(kwargs['env']['PATH'],'/usr/bin:/bin')
            return real_popen([sys.executable,'-c',code,*argv],**kwargs)
        return spawn

    def test_real_bounded_runner_admits_quiet_up_and_refuses_progress_flood(self):
        concrete=self.module.run_command;emulated=self.run_command
        def runner(argv,**kwargs):
            if 'up' in argv:
                with patch.object(self.module.subprocess,'Popen',self.pull_child()):
                    concrete(argv,**kwargs)
            return emulated(argv,**kwargs)
        self.driver.runner=runner
        self.assertEqual(self.driver.start(self.prepare())['services_ready'],14)
        # RED: the previous argv shape (no --quiet-pull) floods stderr and is killed.
        previous=tuple(item for item in self.module.UP if item!='--quiet-pull')
        with patch.object(self.module,'UP',previous):
            self.refused(lambda:self.driver.start(self.prepare()))
        with patch.object(self.module.subprocess,'Popen',self.pull_child()):
            with self.assertRaises(self.module.ComposeError) as caught:
                concrete(self.module.BASE+['compose','--project-name','xynes-qa',*previous,'gateway'],
                         env=self.module.CLEAN_ENV,timeout=30,limit=65536)
        native=caught.exception.native
        self.assertEqual((native['command'],native['outcome'],native['exit_code']),('compose up','stderr-limit',None))
        self.assertGreater(native['stderr_bytes'],65536);self.assertEqual(native['stdout_bytes'],0)
        self.assertEqual(str(caught.exception),'Hosted Compose operation refused')
        self.assertNotIn('inert',json.dumps(native));self.assertEqual(self.module.native_failure(native),native)

    def test_native_failure_classification_is_fixed_labels_and_counts_only(self):
        real_popen=subprocess.Popen;secret='ghp_'+'B'*36
        cases=[("import sys;sys.stdout.write('x'*100)",2,32,'stdout-limit',None),
               ("import time;time.sleep(3)",1,32,'timeout',None),
               ("import sys;sys.stderr.write('inert-"+secret+"');sys.exit(3)",2,32,'exit',3)]
        for code,timeout,limit,outcome,exit_code in cases:
            def spawn(argv,**kwargs):return real_popen([sys.executable,'-c',code],**kwargs)
            with patch.object(self.module.subprocess,'Popen',spawn):
                with self.assertRaises(self.module.ComposeError) as caught:
                    self.module.run_command(self.module.BASE+['inspect','--format',secret,'a'*64],env=self.module.CLEAN_ENV,timeout=timeout,limit=limit)
            native=caught.exception.native
            self.assertEqual((native['command'],native['outcome'],native['exit_code']),('docker inspect',outcome,exit_code))
            self.assertNotIn(secret,json.dumps(native));self.assertNotIn('inert',json.dumps(native))
        with self.assertRaises(self.module.ComposeError) as caught:
            self.module.run_command(['/bin/sh','-c',secret],env=self.module.CLEAN_ENV,timeout=1,limit=1)
        self.assertEqual(caught.exception.native['command'],'refused');self.assertEqual(caught.exception.native['outcome'],'refused')
        label=self.module.command_label
        self.assertEqual(label(self.module.BASE+['compose','--project-name','xynes-qa','-f','/x','config','--hash','gateway']),'compose config')
        self.assertEqual(label(self.module.BASE+['compose','-f',secret]),'compose other')
        self.assertEqual(label(self.module.BASE+[secret]),'docker other')
        self.assertEqual(label(['/usr/bin/systemctl','stop','xynes-idle-qa-auth.service']),'systemctl stop')
        good={'command':'compose up','outcome':'exit','exit_code':1,'stdout_bytes':0,'stderr_bytes':5,'killed':False}
        self.assertEqual(self.module.native_failure(good),good)
        for change in ({'command':secret},{'outcome':secret},{'exit_code':'1'},{'stdout_bytes':-1},{'killed':1},{'extra':secret}):
            self.assertIsNone(self.module.native_failure({**good,**change}))

    def test_private_environment_reader_rejects_symlink_hardlink_permissions_and_change(self):
        file=self.work/'fixture.env';file.write_text('INERT=value\n');file.chmod(0o640)
        check=lambda path:self.module.private_environment(path,uid=os.getuid(),gid=os.getgid())
        self.assertEqual(check(file),b'INERT=value\n')
        file.chmod(0o644);self.refused(lambda:check(file));file.chmod(0o640)
        link=self.work/'link.env';link.symlink_to(file);self.refused(lambda:check(link));link.unlink()
        os.link(file,link);self.refused(lambda:check(file));link.unlink()
        self.refused(lambda:self.module.private_environment(file,uid=os.getuid()+1,gid=os.getgid()))

    def test_tampered_prepared_file_refused_before_daemon_operations(self):
        prepared=self.prepare();prepared.file.write_text('{}')
        before=len(self.commands);self.refused(lambda:self.driver.start(prepared))
        self.assertEqual(len(self.commands),before)

if __name__=='__main__':unittest.main()
