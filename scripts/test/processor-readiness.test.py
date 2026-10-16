#!/usr/bin/env python3
"""Executable readiness refusals using owned fixtures and fake native commands."""
import os
import signal
import time
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
SCRIPTS=ROOT/'infra/processors'
NOW=1781049600

class ProcessorReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='xynes-definitions-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.defs=self.root/'defs';self.defs.mkdir();self.bin=self.root/'bin';self.bin.mkdir();self.run=self.root/'run';self.run.mkdir()
        for name in ['main','daily','bytecode']:(self.defs/(name+'.cvd')).write_bytes(b'valid signed fixture')
        (self.defs/'.xynes-freshclam-success').write_text(f'{NOW} {NOW}\n')
        fake='''#!/usr/bin/env python3
import os,sys
from pathlib import Path
name=Path(sys.argv[0]).name;a=sys.argv[1:]
with open(os.environ['COMMAND_LOG'],'a') as f:f.write(name+' '+repr(a)+'\\n')
if name=='date':print(os.environ['FIXTURE_NOW'] if a==['+%s'] else os.environ.get('BUILD_TIME',os.environ['FIXTURE_NOW']))
elif name=='sigtool':
 print('Build time: fixture signed timestamp\\nVerification OK.' if not os.environ.get('BAD_SIGNATURE') else 'Verification FAILED')
 sys.exit(1 if os.environ.get('BAD_SIGNATURE') else 0)
elif name=='timeout':os.execvp(a[3],a[3:])
elif name=='freshclam':sys.exit(int(os.environ.get('UPDATE_EXIT','0')))
elif name=='id':print('0')
elif name=='chown':pass
elif name=='nc':
 data=sys.stdin.buffer.read();Path(os.environ['REQUEST']).write_bytes(data)
 sys.stdout.write(os.environ.get('SCANNER_REPLY','stream: OK')+'\\0')
else:sys.exit(9)
'''
        for name in ['date','sigtool','timeout','freshclam','id','chown','nc']:
            path=self.bin/name;path.write_text(fake);path.chmod(0o755)
        self.env=os.environ.copy();self.env.update(PATH=str(self.bin)+os.pathsep+self.env['PATH'],XYNES_DEFINITIONS_DIRECTORY=str(self.defs),XYNES_UPDATER_RUN_DIRECTORY=str(self.run),FIXTURE_NOW=str(NOW),COMMAND_LOG=str(self.root/'commands'),REQUEST=str(self.root/'request'))

    def call(self,name,*args,**changes):
        path=SCRIPTS/(name+'.sh');self.assertTrue(path.exists(),'readiness script missing: '+name)
        env={**self.env,**changes};return subprocess.run(['sh',str(path),*args],env=env,text=True,capture_output=True,timeout=5)

    def test_recent_verified_definitions_are_ready(self):
        self.assertEqual(self.call('definitions-ready').returncode,0)
        self.assertEqual(self.call('definitions-ready','--verify').returncode,0)

    def test_missing_symlink_or_empty_definition_refused(self):
        for kind in ['missing','symlink','empty']:
            path=self.defs/'daily.cvd';path.unlink(missing_ok=True)
            if kind=='symlink':path.symlink_to(self.defs/'main.cvd')
            if kind=='empty':path.touch()
            self.assertNotEqual(self.call('definitions-ready').returncode,0,kind)
            path.unlink(missing_ok=True);path.write_text('valid signed fixture')

    def test_stale_future_missing_malformed_or_symlink_receipt_refused(self):
        receipt=self.defs/'.xynes-freshclam-success'
        for text in [f'{NOW-14401} {NOW}\n',f'{NOW+1} {NOW}\n',f'{NOW} {NOW-172801}\n','PRIVATE-malformed\n',f'{NOW} {NOW} trailing\n']:
            receipt.write_text(text);result=self.call('definitions-ready');self.assertNotEqual(result.returncode,0)
            self.assertNotIn('PRIVATE',result.stdout+result.stderr)
        receipt.unlink();self.assertNotEqual(self.call('definitions-ready').returncode,0)
        receipt.symlink_to(self.defs/'daily.cvd');self.assertNotEqual(self.call('definitions-ready').returncode,0)

    def test_invalid_signature_or_old_daily_build_refused(self):
        self.assertNotEqual(self.call('definitions-ready','--verify',BAD_SIGNATURE='1').returncode,0)
        self.assertNotEqual(self.call('definitions-ready','--verify',BUILD_TIME=str(NOW-172801)).returncode,0)

    def test_failed_update_preserves_all_existing_definitions_and_receipt(self):
        before={p.name:p.read_bytes() for p in self.defs.iterdir()}
        result=self.call('freshclam-updater','--once',UPDATE_EXIT='8')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.defs.iterdir()})

    def test_successful_native_update_publishes_receipt_only_after_signature_gate(self):
        receipt=self.defs/'.xynes-freshclam-success';receipt.unlink()
        self.assertEqual(self.call('freshclam-updater','--once').returncode,0)
        self.assertEqual(receipt.read_text(),f'{NOW} {NOW}\n')
        before=receipt.read_bytes();result=self.call('freshclam-updater','--once',BAD_SIGNATURE='1')
        self.assertNotEqual(result.returncode,0);self.assertEqual(receipt.read_bytes(),before)
        commands=(self.root/'commands').read_text();self.assertIn('freshclam',commands)
        self.assertNotIn('clamd',commands)

    def test_updater_initializes_empty_definitions_through_native_update(self):
        for path in self.defs.iterdir():path.unlink()
        native=self.bin/'freshclam'
        native.write_text("#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\np=Path(os.environ['XYNES_DEFINITIONS_DIRECTORY'])\nfor n in ['main','daily','bytecode']:(p/(n+'.cvd')).write_text('valid signed fixture')\n")
        native.chmod(0o755)
        self.assertEqual(self.call('freshclam-updater','--once').returncode,0)
        self.assertEqual(self.call('definitions-ready').returncode,0)

    def test_dangling_unused_definition_symlink_is_refused(self):
        (self.defs/'daily.cld').symlink_to(self.defs/'missing')
        self.assertNotEqual(self.call('definitions-ready').returncode,0)

    def test_term_is_forwarded_to_owned_update_child_and_supervisor_exits(self):
        native=self.bin/'freshclam';marker=self.root/'started';stopped=self.root/'stopped'
        native.write_text("#!/usr/bin/env python3\nimport os,signal,time,sys\nfrom pathlib import Path\ndef done(*a):Path(os.environ['STOPPED']).write_text('term');sys.exit(0)\nsignal.signal(signal.SIGTERM,done)\nPath(os.environ['STARTED']).write_text('running')\nwhile True:time.sleep(.1)\n")
        native.chmod(0o755)
        env={**self.env,'STARTED':str(marker),'STOPPED':str(stopped)}
        proc=subprocess.Popen(['sh',str(SCRIPTS/'freshclam-updater.sh')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            deadline=time.monotonic()+3
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(marker.exists());proc.send_signal(signal.SIGTERM)
            try:code=proc.wait(timeout=3)
            except subprocess.TimeoutExpired:self.fail('supervisor did not forward TERM to bounded update child')
            self.assertEqual(code,0);self.assertEqual(stopped.read_text(),'term')
        finally:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.wait(timeout=3)

    def test_scanner_health_exercises_public_supervisor_instream_not_private_ping(self):
        self.assertEqual(self.call('scanner-health').returncode,0)
        self.assertEqual((self.root/'request').read_bytes(),b'zINSTREAM\x00\x00\x00\x00\x00')
        commands=(self.root/'commands').read_text();self.assertIn("'3310'",commands);self.assertNotIn('3311',commands)
        self.assertNotEqual(self.call('scanner-health',SCANNER_REPLY='stream: scanner busy ERROR').returncode,0)
        self.assertNotEqual(self.call('scanner-health',SCANNER_REPLY='stream: infected FOUND').returncode,0)

    def test_updater_health_requires_live_supervisor_and_fresh_definitions(self):
        self.assertNotEqual(self.call('freshclam-health').returncode,0)
        (self.run/'xynes-updater.pid').write_text(str(os.getpid())+'\n')
        self.assertEqual(self.call('freshclam-health').returncode,0)
        (self.defs/'.xynes-freshclam-success').write_text(f'{NOW-20000} {NOW}\n')
        self.assertNotEqual(self.call('freshclam-health').returncode,0)

    def test_updater_health_accepts_live_container_init_and_refuses_pid_zero(self):
        # A PID namespace presents its entrypoint as 1. Stub only peer liveness;
        # the native container reproduces this case with actual kill -0 1.
        wrapper='kill() { [ "$1" = -0 ] && [ "$2" = 1 ]; }; . "$1"'
        for pid, expected in [('1',0),('0',1),('999999',1)]:
            (self.run/'xynes-updater.pid').write_text(pid+'\n')
            result=subprocess.run(['sh','-c',wrapper,str(SCRIPTS/'freshclam-health.sh'),str(SCRIPTS/'freshclam-health.sh')],env=self.env,text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,expected,pid)

    def test_scanner_start_cannot_exec_before_definitions_ready(self):
        marker=self.root/'scanner-started';scanner=self.bin/'fixture-scanner';scanner.write_text('#!/bin/sh\nprintf started > "$SCANNER_MARKER"\n');scanner.chmod(0o755)
        (self.defs/'.xynes-freshclam-success').unlink()
        self.assertNotEqual(self.call('scanner-start',XYNES_SCANNER_EXECUTABLE=str(scanner),SCANNER_MARKER=str(marker)).returncode,0)
        self.assertFalse(marker.exists())
        (self.defs/'.xynes-freshclam-success').write_text(f'{NOW} {NOW}\n')
        self.assertEqual(self.call('scanner-start',XYNES_SCANNER_EXECUTABLE=str(scanner),SCANNER_MARKER=str(marker)).returncode,0)
        self.assertTrue(marker.exists())

if __name__=='__main__':unittest.main(verbosity=2)
