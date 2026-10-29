#!/usr/bin/env python3
"""Accepted-ledger/locking and fixed Discord transport; no network/providers."""
import fcntl,hashlib,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_backup as m
import host_release_state

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-schedule-test-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.config=m.Configuration(root=self.root)
        self.assertTrue(all(hasattr(m,name) for name in ('periodic','_create','prepare_wrappers','alert','main')),'scheduled accepted backup/alert implementation missing')
        self.directory=self.root/'var/lib/xynes/deploy/state';self.directory.mkdir(parents=True,mode=0o700)
        self.request=dict(schema_version=1,artifact_kind='apex-deployment-request',controller=dict(repository='Xynes-Studio/xynes-infra',commit='a'*40,workflow='apex-build.yml',run_id=100,run_attempt=1),publication=dict(id=456,digest='sha256:'+'b'*64),deployment=dict(run_id=101,run_attempt=1,job_id=201))
    def accepted(self,pending=False):
        with host_release_state.ReleaseState(self.directory,stage='qa',owner_uid=os.geteuid()) as state:
            token=state.begin(self.request,source_sha256='sha256:'+'c'*64,started_at=int(time.time())-1)
            if not pending:state.accept(token,smoke=dict(storage_verified=True,stage='qa',publication=self.request['publication'],source_sha256='sha256:'+'c'*64,result='passed',observed_at=int(time.time())),accepted_at=int(time.time()))
    def test_schedule_requires_real_accepted_ledger_without_inventing_attempt(self):
        self.accepted();before=(self.directory/'qa.json').read_bytes();seen=[]
        def capture(state,token,**kwargs):
            self.assertIsNone(token);self.assertTrue(kwargs['scheduled']);self.assertIsNone(state.read_pending())
            seen.append(state.read_accepted()['current']);return {'status':'backup-created'}
        with patch.object(m,'_create',capture):result=m.periodic('qa',configuration=self.config)
        self.assertEqual(result['status'],'backup-created');self.assertEqual(len(seen),1)
        self.assertEqual((self.directory/'qa.json').read_bytes(),before)
    def test_no_accepted_stage_skips_without_backup_or_secret_access(self):
        with patch.object(m,'_create',side_effect=AssertionError('must not back up')):
            self.assertEqual(m.periodic('qa',configuration=self.config)['status'],'backup-skipped')
    def test_pending_and_busy_locks_refuse_before_backup(self):
        self.accepted(pending=True)
        with patch.object(m,'_create',side_effect=AssertionError('must not back up')):
            with self.assertRaises(m.BackupError):m.periodic('qa',configuration=self.config)
        with host_release_state.ReleaseState(self.directory,stage='qa',owner_uid=os.geteuid()):
            self.assertEqual(m.periodic('qa',configuration=self.config)['reason'],'busy')
    def test_deployment_winning_lock_after_preflight_defers_without_failure(self):
        self.accepted();original=host_release_state.ReleaseState.__enter__;held=[]
        def enter(state):
            fd=os.open(self.directory/'host.lock',os.O_RDWR)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held.append(fd)
            return original(state)
        try:
            with patch.object(host_release_state.ReleaseState,'__enter__',enter),patch.object(m,'_create',side_effect=AssertionError('no backup while busy')):
                self.assertEqual(m.periodic('qa',configuration=self.config),{'status':'backup-skipped','stage':'qa','reason':'busy'})
        finally:
            for fd in held:os.close(fd)

    def test_local_retention_preserves_newest_pending_and_unknown_files(self):
        import importlib.util
        from datetime import datetime,timezone
        spec=importlib.util.spec_from_file_location('retention_contract',ROOT/'scripts/lib/backup_recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        output=self.root/'backups';output.mkdir(mode=0o700)
        def job(marker,days,uploaded):
            path=output/('qa-20000101T000000Z-'+marker*32);path.mkdir(mode=0o700)
            (path/'backup.tar.age').write_bytes(b'cipher')
            module.write_json(path/'record.json',{'version':1,'environment':'qa','uploaded':uploaded,'bytes':6,'sha256':hashlib.sha256(b'cipher').hexdigest(),'snapshot_at':datetime.fromtimestamp(time.time()-days*86400,timezone.utc).isoformat()})
            return path
        newest=job('a',0,True);old=job('b',9,True);pending=job('c',10,False)
        unrelated=output/'unrelated';unrelated.write_bytes(b'preserve')
        unknown=old/'unknown';unknown.write_bytes(b'preserve')
        with self.assertRaises(module.RecoveryError):m.prune_local(module,'qa',output)
        self.assertTrue(unknown.is_file());self.assertTrue(old.is_dir())
        unknown.unlink()
        self.assertEqual(m.prune_local(module,'qa',output),{'days':7,'removed':1})
        self.assertTrue(newest.is_dir());self.assertTrue(pending.is_dir());self.assertTrue(unrelated.is_file())
        (newest/'backup.tar.age').write_bytes(b'broken')
        with self.assertRaises(module.RecoveryError):m.prune_local(module,'qa',output)

    def test_wrapper_namespace_changes_with_immutable_root_and_preserves_old_bytes(self):
        output=self.root/'backups';output.mkdir(mode=0o700)
        first=m.prepare_wrappers('qa',self.config,output);before={p.name:p.read_bytes() for p in first.iterdir()}
        with patch.object(m,'ROOT',ROOT/'different-sealed-bundle'):
            second=m.prepare_wrappers('qa',self.config,output)
        self.assertNotEqual(first,second);self.assertEqual({p.name:p.read_bytes() for p in first.iterdir()},before)
        self.assertEqual(first.parent,output/'wrappers');self.assertRegex(first.name,r'^[a-f0-9]{64}$')
    def alert_config(self,url='https://discord.com/api/webhooks/123456789012345678/'+'a'*68):
        path=self.root/'etc/xynes/deploy/alerts.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'schema_version':1,'provider':'discord','webhook_url':url}));path.chmod(0o600);return path
    def test_alert_uses_configured_fixed_webhook_and_only_bounded_redacted_payload(self):
        self.alert_config();calls=[]
        def transport(path,body):calls.append((path,json.loads(body)))
        result=m.alert('qa',configuration=self.config,transport=transport)
        self.assertEqual(result,{'status':'backup-alert-delivered','stage':'qa'})
        self.assertEqual(calls[0][1],{'content':'Xynes qa: encrypted offsite backup failed. Check the protected backup service journal.','allowed_mentions':{'parse':[]}})
        self.assertNotIn('webhook',json.dumps(result));self.assertNotIn('123456789012345678',json.dumps(result))
    def test_bad_webhook_urls_permissions_and_redirect_response_refuse(self):
        for url in ('http://discord.com/api/webhooks/1/abc','https://discord.com.evil/api/webhooks/1/abc','https://user@discord.com/api/webhooks/1/abc','https://discord.com:444/api/webhooks/1/abc','https://discord.com/api/webhooks/1/abc?wait=true','https://discord.com/elsewhere'):
            self.alert_config(url)
            with self.assertRaises(m.BackupError):m.alert('qa',configuration=self.config,transport=lambda *_:self.fail('unsafe request'))
        path=self.alert_config();path.chmod(0o644)
        with self.assertRaises(m.BackupError):m.alert('qa',configuration=self.config,transport=lambda *_:self.fail('unsafe request'))
    def test_alert_test_message_cannot_claim_a_backup_failure(self):
        self.alert_config();sent=[]
        result=m.alert('staging',configuration=self.config,test=True,transport=lambda path,body:sent.append(json.loads(body)))
        self.assertEqual(result['status'],'backup-alert-test-delivered')
        self.assertIn('TEST',sent[0]['content']);self.assertIn('No backup failure is being reported',sent[0]['content'])
    def test_discord_transport_refuses_redirects_and_oversize_responses_without_logging(self):
        class Response:
            status=302
            def read(self,limit):return b''
        class Connection:
            def __init__(self,*args,**kwargs):self.closed=False
            sock=None
            def connect(self):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):return Response()
            def close(self):self.closed=True
        connection=Connection()
        with patch.object(m.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(m.BackupError):m.discord_send('/api/webhooks/inert',b'{}')
        self.assertTrue(connection.closed)
        Response.status=200;Response.read=lambda self,limit:b'x'*4097
        with patch.object(m.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(m.BackupError):m.discord_send('/api/webhooks/inert',b'{}')
    def test_discord_response_header_and_body_reader_share_absolute_deadline(self):
        class Socket:
            def settimeout(self,value):self.timeout=value
        class Connection:
            sock=Socket()
            def connect(self):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):
                return self.response_class(self.sock)
            def close(self):self.closed=True
        class Response:
            status=204
            fp=object()
            def read(self,limit):return b''
        connection=Connection();response=Response()
        with patch.object(m.http.client,'HTTPSConnection',return_value=connection),patch.object(m.http.client,'HTTPResponse',return_value=response):
            m.discord_send('/api/webhooks/inert',b'{}')
        self.assertTrue(hasattr(response.fp,'_deadline'))
        self.assertLessEqual(connection.sock.timeout,15)

    def test_failed_notification_never_replaces_original_backup_refusal(self):
        with patch.object(m,'_create',side_effect=ValueError('SECRET original')),patch.object(m,'alert',side_effect=OSError('SECRET notification')):
            with self.assertRaises(m.BackupError) as caught:m.create(object(),'inert')
        self.assertEqual(str(caught.exception),m.ERROR)

    def test_completed_native_child_is_not_signalled_after_wait_reaps_pid(self):
        class Stream:
            def close(self):pass
        class Child:
            pid=123456789
            returncode=None
            stdout=Stream();stderr=Stream()
            def wait(self,**kwargs):self.returncode=0;return 0
        class Selector:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def register(self,*args):pass
            def get_map(self):return {}
        with patch.object(m.subprocess,'Popen',return_value=Child()),patch.object(m.selectors,'DefaultSelector',return_value=Selector()),patch.object(m.os,'killpg') as kill:
            self.assertEqual(m.bounded(['inert'],{'PATH':'/usr/bin:/bin'}),b'')
        kill.assert_not_called()

    def test_cli_has_no_caller_selected_message_url_or_stage_alias(self):
        for argv in ([],['run','dev'],['run','qa','https://other.invalid'],['alert','qa','arbitrary text']):
            self.assertEqual(m.main(argv),1)
    def test_timer_templates_fixed_six_hour_and_failure_alert_without_ssh_authority(self):
        base=ROOT/'infra/release/systemd'
        timer=(base/'xynes-backup@.timer').read_text();service=(base/'xynes-backup@.service').read_text();alert=(base/'xynes-backup-alert@.service').read_text()
        self.assertIn('OnCalendar=*-*-* 00,06,12,18:00:00 UTC\n',timer)
        self.assertIn('Persistent=true',timer);self.assertIn('OnFailure=xynes-backup-alert@%i.service',service)
        self.assertIn('ExecStart=/usr/local/libexec/xynes-backup run %i',service)
        self.assertIn('ExecStart=/usr/local/libexec/xynes-backup alert %i',alert)

if __name__=='__main__':unittest.main()
